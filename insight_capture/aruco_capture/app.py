"""Single-page local capture service."""

import argparse
import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path

from aiohttp import web

from .capture import Capture, write_json
from .dataset import align, connect, export, export_runs, validate_segments


def create_app(capture):
    busy = set()

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app = web.Application(middlewares=[errors])

    def session(request):
        name = request.match_info["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise web.HTTPNotFound()
        path = capture.root / name
        if not (path / "session.json").is_file():
            raise web.HTTPNotFound()
        return path

    async def index(request):
        return web.FileResponse(Path(__file__).with_name("index.html"))

    async def asset(request):
        name = request.match_info["asset"]
        if name == "babylon.js":
            path = (
                Path(__file__).resolve().parents[2]
                / "web_dashboard/dist/static/babylon.js"
            )
        elif name in {"workbench.css", "workbench.js", "trajectory.js"}:
            path = Path(__file__).with_name(name)
        else:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

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

    async def start(request):
        if busy:
            raise ValueError("请等待当前导出完成，再开始录制")
        return web.json_response({"recording": await asyncio.to_thread(capture.start)})

    async def stop(request):
        return web.json_response({"stopped": await asyncio.to_thread(capture.stop)})

    async def sessions(request):
        rows = []
        for path in sorted(capture.root.glob("*/session.json"), reverse=True):
            value = json.loads(path.read_text())
            rows.append(
                {
                    "name": path.parent.name,
                    "duration_s": value.get("duration_s", 0),
                    "status": value["status"],
                    "started_unix_ns": value.get("started_unix_ns"),
                    "streams": ["head"]
                    + [x["name"] for x in value["config"].get("rgb", [])],
                    "exported": (path.parent / "lerobot/meta/info.json").is_file(),
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

    def review_data(path, include_samples):
        meta, grid, state, video, good, report = align(path)
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
                [float(t), *[int(f[i]) for f in flags], *state[i].tolist()]
                for i, t in enumerate(grid)
            ]
        else:
            # Keep the original QC response usable by command-line callers.
            return report
        info = path / "lerobot/meta/info.json"
        result["exported"] = json.loads(info.read_text()) if info.is_file() else None
        return result

    async def qc(request):
        return web.json_response(
            await asyncio.to_thread(review_data, session(request), False)
        )

    async def review(request):
        return web.json_response(
            await asyncio.to_thread(review_data, session(request), True)
        )

    async def dataset(request):
        path = session(request)
        if path.name in busy or capture.active:
            raise ValueError("请先停止录制，且勿重复导出")
        busy.add(path.name)
        try:
            return web.json_response(await asyncio.to_thread(export, path))
        finally:
            busy.remove(path.name)

    async def cleanup(app):
        capture.close()

    app.on_cleanup.append(cleanup)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/assets/{asset}", asset),
            web.get("/api/live/trajectory", trajectory),
            web.get("/api/status", status),
            web.post("/api/start", start),
            web.post("/api/stop", stop),
            web.get("/api/sessions", sessions),
            web.get("/api/preview/{stream}", preview),
            web.get("/api/sessions/{name}/frame/{stream}", frame),
            web.get("/api/sessions/{name}/segments", segments),
            web.post("/api/sessions/{name}/segments", segments),
            web.get("/api/sessions/{name}/qc", qc),
            web.get("/api/sessions/{name}/review", review),
            web.post("/api/sessions/{name}/export", dataset),
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
    from .sources import start_sources

    node, listener, workers = start_sources(capture)
    try:
        web.run_app(create_app(capture), host=args.host, port=args.port)
    finally:
        import rclpy

        capture.close()
        rclpy.try_shutdown()
        for worker in workers:
            worker.join(2)
        node.destroy_node()


if __name__ == "__main__":
    main()
