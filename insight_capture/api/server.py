"""Thread-owned aiohttp dashboard server facade."""

import asyncio
import threading
from typing import Optional

from aiohttp import web

from .app import create_app
from .context import DashboardContext


class WebDashboardServer:
    def __init__(
        self,
        context: DashboardContext,
        host: str,
        port: int,
    ) -> None:
        self.context = context
        self.host = host
        self.port = int(port)
        self._loop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="web_dashboard_server")
        self._thread.start()
        self._started.wait(timeout=5.0)

    def stop(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        app = create_app(self.context)
        runner = web.AppRunner(app)
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, self.host, self.port)
        self._loop.run_until_complete(site.start())
        self._started.set()
        self.context.node.get_logger().info(
            f"Web dashboard backend listening on http://{self.host}:{self.port}"
        )
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(runner.cleanup())
            self._loop.close()
