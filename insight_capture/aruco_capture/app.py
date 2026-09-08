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
from .dataset import align, connect, export, validate_segments


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

    async def status(request):
        return web.json_response(capture.status())

    async def start(request):
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
        with connect(path) as db:
            row = db.execute(
                "SELECT payload,t FROM samples WHERE kind='image' AND name=? ORDER BY ABS(t-?) LIMIT 1",
                (stream, seconds),
            ).fetchone()
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

    async def qc(request):
        path = session(request)
        result = await asyncio.to_thread(align, path)
        report = result[-1]
        write_json(path / "quality.json", report)
        # One XY trace per arm, sampled for a lightweight review plot.
        stride = max(1, len(result[1]) // 500)
        report["trace"] = {
            side: [
                [float(result[1][i]), *result[2][i, offset : offset + 3].tolist()]
                for i in range(0, len(result[1]), stride)
                if any(result[2][i, offset : offset + 9])
            ]
            for side, offset in [("left", 0), ("right", 10)]
        }
        return web.json_response(report)

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
            web.get("/api/status", status),
            web.post("/api/start", start),
            web.post("/api/stop", stop),
            web.get("/api/sessions", sessions),
            web.get("/api/preview/{stream}", preview),
            web.get("/api/sessions/{name}/frame/{stream}", frame),
            web.get("/api/sessions/{name}/segments", segments),
            web.post("/api/sessions/{name}/segments", segments),
            web.get("/api/sessions/{name}/qc", qc),
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
