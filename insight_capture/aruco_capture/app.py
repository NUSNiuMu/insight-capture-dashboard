"""Single-page local capture service."""

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

from aiohttp import web

from .capture import Capture, write_json
from .dataset import (
    align,
    connect,
    dataset_detail,
    dataset_export_index,
    delete_dataset,
    delete_session_from_dataset,
    export_runs,
    export_to_dataset,
    list_datasets,
    suggest_pause_segments,
    validate_segments,
)


def create_app(capture):
    busy = set()

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app = web.Application(middlewares=[errors])

    def session_path(name):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise web.HTTPNotFound()
        path = capture.root / name
        if not (path / "session.json").is_file():
            raise web.HTTPNotFound()
        return path

    def session(request):
        return session_path(request.match_info["name"])

    def dataset_root(request):
        name = request.match_info["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise web.HTTPNotFound()
        return capture.root.parent / "lerobot_datasets" / name

    # Browsers heuristically cache files that have Last-Modified but no
    # Cache-Control, so after a redeploy they could keep running the old
    # frontend; force a (cheap, ETag-based) revalidation every load.
    no_cache = {"Cache-Control": "no-cache"}

    async def index(request):
        return web.FileResponse(Path(__file__).with_name("index.html"), headers=no_cache)

    async def asset(request):
        name = request.match_info["asset"]
        if name == "babylon.js":
            path = (
                Path(__file__).resolve().parents[2]
                / "web_dashboard/dist/static/babylon.js"
            )
        elif name in {"workbench.css", "workbench.js", "trajectory.js", "devices.js"}:
            path = Path(__file__).with_name(name)
        else:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers=no_cache)

    async def trajectory(request):
        after = int(request.query.get("after", 0))
        with capture.lock:
            return web.json_response(
                {
                    "epoch": capture.history_epoch,
                    "rows": [x for x in capture.history if x["seq"] > after],
                }
            )

    async def status(request):
        return web.json_response(capture.status())

    async def devices(request):
        from .devices import scan_devices

        if request.method == "POST":
            if busy or capture.sources is None:
                raise ValueError("当前无法应用设备配置，请等待导出完成")
            settings = await request.json()
            await asyncio.to_thread(capture.sources.apply, settings)
        found = await asyncio.to_thread(scan_devices)
        return web.json_response(
            {
                "available": found,
                "default_rgb": capture.default_rgb,
                "bindings": {
                    key: capture.config.get(key, []) for key in ("rgb", "serial")
                },
            }
        )

    async def serial_command(request):
        index = int(request.match_info["index"])
        if not 0 <= index < len(capture.config.get("serial", [])):
            raise web.HTTPNotFound()
        body = await request.json()
        command = body.get("command")
        if command == "start_cal":
            line = "start_cal"
        elif command == "end_cal":
            line = "end_cal"
        elif command == "opening_mm":
            line = json.dumps({"opening_mm": float(body["value"])})
        else:
            raise ValueError("未知标定命令")
        await asyncio.to_thread(capture.send_serial, index, line)
        return web.json_response({"sent": command})

    async def serial_log(request):
        index = int(request.match_info["index"])
        after = int(request.query.get("after", 0))
        with capture.lock:
            entries = [e for e in capture.serial_log.get(index, []) if e["seq"] > after]
            angle = capture.serial_angle.get(index)
        return web.json_response({"entries": entries, "angle": angle})

    async def settings(request):
        if capture.active:
            raise ValueError("录制中不能修改采样率或 VIO 开关")
        body = await request.json()
        if "fps" in body:
            fps = int(body["fps"])
            if not 1 <= fps <= 120:
                raise ValueError("fps 须在 1-120 之间")
            capture.config["fps"] = fps
        if "vio_enabled" in body:
            capture.config["vio_enabled"] = bool(body["vio_enabled"])
        write_json(
            capture.root / "_devices.json",
            {
                **{key: capture.config.get(key, []) for key in ("rgb", "serial")},
                "fps": capture.config.get("fps", 20),
                "vio_enabled": capture.config.get("vio_enabled", True),
            },
        )
        return web.json_response(
            {
                "fps": capture.config.get("fps", 20),
                "vio_enabled": capture.config.get("vio_enabled", True),
            }
        )

    async def start(request):
        if busy:
            raise ValueError("请等待当前导出完成，再开始录制")
        text = await request.text()
        label = json.loads(text).get("name") if text else None
        return web.json_response(
            {"recording": await asyncio.to_thread(capture.start, label)}
        )

    async def stop(request):
        return web.json_response({"stopped": await asyncio.to_thread(capture.stop)})

    async def sessions(request):
        exported_index = await asyncio.to_thread(
            dataset_export_index, capture.root.parent / "lerobot_datasets"
        )
        rows = []
        for path in sorted(capture.root.glob("*/session.json"), reverse=True):
            value = json.loads(path.read_text())
            segments = json.loads((path.parent / "segments.json").read_text())
            rows.append(
                {
                    "name": path.parent.name,
                    "duration_s": value.get("duration_s", 0),
                    "status": value["status"],
                    "started_unix_ns": value.get("started_unix_ns"),
                    "streams": ["head"]
                    + [x["name"] for x in value["config"].get("rgb", [])],
                    "annotated": bool(segments),
                    "exported_to": exported_index.get(path.parent.name, []),
                }
            )
        return web.json_response(rows)

    async def preview(request):
        name = request.match_info["stream"]
        payload = capture.preview.get(name)
        if payload is None:
            raise web.HTTPNotFound(text="尚无图像")
        return web.Response(
            body=payload,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    async def frame(request):
        path = session(request)
        seconds = float(request.query.get("t", 0))
        stream = request.match_info["stream"]

        def lookup():
            with connect(path) as db:
                before = db.execute(
                    "SELECT payload,t FROM samples WHERE kind='image' AND name=? AND t<=? ORDER BY t DESC LIMIT 1",
                    (stream, seconds),
                ).fetchone()
                after = db.execute(
                    "SELECT payload,t FROM samples WHERE kind='image' AND name=? AND t>? ORDER BY t LIMIT 1",
                    (stream, seconds),
                ).fetchone()
                return min(
                    (r for r in (before, after) if r is not None),
                    key=lambda r: abs(r[1] - seconds),
                    default=None,
                )

        row = await asyncio.to_thread(lookup)
        if row is None or abs(row[1] - seconds) > 0.1:
            raise web.HTTPNotFound(text="此时刻图像缺失")
        return web.Response(body=row[0], content_type="image/jpeg")

    async def segments(request):
        path = session(request)
        if request.method == "POST":
            if path.name == capture.active or path.name in busy:
                raise ValueError("请等待录制或导出完成")
            values = await request.json()
            validate_segments(
                values, json.loads((path / "session.json").read_text())["duration_s"]
            )
            write_json(path / "segments.json", values)
        return web.json_response(json.loads((path / "segments.json").read_text()))

    async def auto_segments(request):
        path = session(request)

        def compute():
            meta, grid, state, video, good, report, _reasons = align(path)
            return suggest_pause_segments(
                grid, state, good, report["fps"], duration_s=meta["duration_s"]
            )

        return web.json_response(await asyncio.to_thread(compute))

    def review_data(path, include_samples):
        meta, grid, state, video, good, report, reasons = align(path)
        parts = json.loads((path / "segments.json").read_text())
        validate_segments(parts, meta["duration_s"])
        runs = export_runs(grid, good, parts)
        report["export_plan"] = {
            "episodes": len(runs),
            "frames": sum(len(r[0]) - 1 for r in runs),
            "annotated_segments": len(parts),
        }
        write_json(path / "quality.json", report)
        result = {
            "quality": report,
            "segments": parts,
            "streams": list(video),
            "duration_s": meta["duration_s"],
            "fps": report["fps"],
        }
        if include_samples:
            flags = []
            for key in (
                "left.position",
                "right.position",
                "left.gripper",
                "right.gripper",
            ):
                mask = [True] * len(grid)
                for start, end in report["missing_intervals"][key]:
                    for i in range(
                        round(start * report["fps"]),
                        min(len(grid), round(end * report["fps"])),
                    ):
                        mask[i] = False
                flags.append(mask)
            result["samples"] = [
                [
                    float(t),
                    *[int(f[i]) for f in flags],
                    *state[i].tolist(),
                    reasons["left"][i],
                    reasons["right"][i],
                ]
                for i, t in enumerate(grid)
            ]
        else:
            # Keep the original QC response usable by command-line callers.
            return report
        return result

    async def qc(request):
        return web.json_response(
            await asyncio.to_thread(review_data, session(request), False)
        )

    async def review(request):
        return web.json_response(
            await asyncio.to_thread(review_data, session(request), True)
        )

    async def delete_session(request):
        path = session(request)
        if path.name == capture.active:
            raise ValueError("正在录制这条记录，请先停止录制")
        if path.name in busy:
            raise ValueError("这条录制正在导出，请等待导出完成")
        await asyncio.to_thread(shutil.rmtree, path)
        return web.json_response({"deleted": path.name})

    async def datasets_list(request):
        return web.json_response(
            await asyncio.to_thread(
                list_datasets, capture.root.parent / "lerobot_datasets"
            )
        )

    async def datasets_detail(request):
        return web.json_response(
            await asyncio.to_thread(dataset_detail, dataset_root(request))
        )

    async def datasets_export(request):
        if capture.active or capture.reconfiguring:
            raise ValueError("请先停止录制，再导出")
        output_root = dataset_root(request)
        body = await request.json()
        names = body.get("sessions")
        if not isinstance(names, list) or not names:
            raise ValueError("请至少选择一条录制记录")
        done = []
        try:
            for name in names:
                path = session_path(name)
                if path.name in busy:
                    raise ValueError(f"{name} 正在使用中，请稍后重试")
                busy.add(path.name)
                try:
                    done.append(
                        await asyncio.to_thread(export_to_dataset, path, output_root)
                    )
                finally:
                    busy.remove(path.name)
        except ValueError as exc:
            if done:
                raise ValueError(
                    f"{exc}（已成功导出 {len(done)} 条：{', '.join(names[: len(done)])}）"
                ) from exc
            raise
        return web.json_response(
            {
                "results": done,
                "detail": await asyncio.to_thread(dataset_detail, output_root),
            }
        )

    async def datasets_delete_session(request):
        output_root = dataset_root(request)
        session_name = request.match_info["session"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_name):
            raise web.HTTPNotFound()
        return web.json_response(
            await asyncio.to_thread(
                delete_session_from_dataset, output_root, session_name
            )
        )

    async def datasets_delete(request):
        output_root = dataset_root(request)
        await asyncio.to_thread(delete_dataset, output_root)
        return web.json_response({"deleted": output_root.name})

    async def cleanup(app):
        capture.close()

    app.on_cleanup.append(cleanup)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/assets/{asset}", asset),
            web.get("/api/live/trajectory", trajectory),
            web.get("/api/status", status),
            web.get("/api/devices", devices),
            web.post("/api/devices", devices),
            web.post("/api/settings", settings),
            web.post("/api/serial/{index}/command", serial_command),
            web.get("/api/serial/{index}/log", serial_log),
            web.post("/api/start", start),
            web.post("/api/stop", stop),
            web.get("/api/sessions", sessions),
            web.get("/api/preview/{stream}", preview),
            web.get("/api/sessions/{name}/frame/{stream}", frame),
            web.get("/api/sessions/{name}/segments", segments),
            web.post("/api/sessions/{name}/segments", segments),
            web.get("/api/sessions/{name}/segments/auto", auto_segments),
            web.get("/api/sessions/{name}/qc", qc),
            web.get("/api/sessions/{name}/review", review),
            web.delete("/api/sessions/{name}", delete_session),
            web.get("/api/datasets", datasets_list),
            web.get("/api/datasets/{name}", datasets_detail),
            web.post("/api/datasets/{name}/export", datasets_export),
            web.delete("/api/datasets/{name}/sessions/{session}", datasets_delete_session),
            web.delete("/api/datasets/{name}", datasets_delete),
        ]
    )
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/devices/x86-aruco/capture.json")
    )
    parser.add_argument("--output", default="outputs/aruco-capture")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    os.environ["ROS_DOMAIN_ID"] = str(config["ros_domain_id"])
    capture = Capture(args.config, args.output)
    from .sources import start_ros_layer

    ros_layer, watchdog_stop, watchdog = start_ros_layer(capture)
    try:
        web.run_app(create_app(capture), host=args.host, port=args.port)
    finally:
        import rclpy

        watchdog_stop.set()
        capture.close()
        capture.sources.close()
        ros_layer.node.destroy_node()
        rclpy.try_shutdown()
        for worker in ros_layer.workers:
            worker.join(2)
        watchdog.join(2)


if __name__ == "__main__":
    main()
