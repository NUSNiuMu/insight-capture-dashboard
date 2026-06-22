#!/usr/bin/env python3

import argparse
import os
import signal
import sys
from pathlib import Path

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtWebEngineWidgets import QWebEngineProfile, QWebEngineView

from camera_setup import build_window_layout, load_setup
from window_helpers import AdjustableFramelessWindowMixin, resolve_window_bounds


class Web3DWindow(AdjustableFramelessWindowMixin, QtWidgets.QMainWindow):
    def __init__(self, url: str, window_settings: dict) -> None:
        super().__init__()
        self.window_settings = dict(window_settings)
        self.title_bar_widget = QtWidgets.QWidget()

        self.setWindowTitle("Insight Web 3D")
        self.setMinimumSize(320, 240)
        self._init_adjustable_window(frameless=bool(self.window_settings.get("frameless", True)))
        self.resize(
            max(int(self.window_settings.get("width", 960)), self.minimumWidth()),
            max(int(self.window_settings.get("height", 1080)), self.minimumHeight()),
        )

        container = QtWidgets.QWidget(self)
        container.setStyleSheet("background: #0f1720;")
        root = QtWidgets.QVBoxLayout(container)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        self.title_bar_widget.setFixedHeight(30)
        self.title_bar_widget.setStyleSheet("background: #1a2632; border-radius: 4px;")
        title_layout = QtWidgets.QHBoxLayout(self.title_bar_widget)
        title_layout.setContentsMargins(10, 0, 10, 0)
        title_layout.setSpacing(0)
        title_label = QtWidgets.QLabel("Insight Web 3D")
        title_label.setStyleSheet("color: #e9eef4; font-size: 13px; font-weight: 600;")
        title_layout.addWidget(title_label)
        title_layout.addStretch(1)
        root.addWidget(self.title_bar_widget)

        view = QWebEngineView(self)
        profile = view.page().profile()
        profile.setHttpCacheType(QWebEngineProfile.NoCache)
        profile.clearHttpCache()
        view.setUrl(QtCore.QUrl(url))
        root.addWidget(view, 1)
        self.setCentralWidget(container)
        self._restore_window_geometry()

    def keyPressEvent(self, event) -> None:
        if event.key() == QtCore.Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if self._start_window_adjustment(event, allow_drag=self._is_drag_region(event.pos())):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._handle_window_adjustment_move(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._finish_window_adjustment(event):
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._is_drag_region(event.pos()):
            self._restore_window_geometry()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._clear_resize_cursor()
        super().leaveEvent(event)

    def _restore_window_geometry(self) -> None:
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        screen_geometry = screen.geometry() if screen is not None else None
        fallback = (
            int(self.window_settings.get("x", 960)),
            int(self.window_settings.get("y", 0)),
            max(int(self.window_settings.get("width", 960)), self.minimumWidth()),
            max(int(self.window_settings.get("height", 1080)), self.minimumHeight()),
        )
        x, y, width, height = resolve_window_bounds(self.window_settings, screen_geometry, fallback)
        self.setGeometry(x, y, width, height)

    def _is_drag_region(self, pos: QtCore.QPoint) -> bool:
        if not self.title_bar_widget.geometry().contains(pos):
            return False
        child = self.childAt(pos)
        if isinstance(child, (QtWidgets.QPushButton, QtWidgets.QToolButton, QtWidgets.QAbstractButton)):
            return False
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "config" / "cameras.json"),
    )
    parser.add_argument("--window-key", default="web_3d")
    parser.add_argument("--url", default="http://127.0.0.1:8765/3d")
    parser.add_argument("--mode", choices=["manual", "left_half", "right_half", "fullscreen"], default=None)
    parser.add_argument("--x", type=int, default=None)
    parser.add_argument("--y", type=int, default=None)
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--frameless", dest="frameless", action="store_true")
    parser.add_argument("--decorated", dest="frameless", action="store_false")
    parser.set_defaults(frameless=None)
    return parser.parse_args()


def load_window_settings(config_path: str, window_key: str) -> dict:
    settings = dict(build_window_layout({}).get(window_key, {}))
    try:
        config = load_setup(Path(config_path))
    except FileNotFoundError:
        return settings
    settings.update(build_window_layout(config).get(window_key, {}))
    return settings


def main() -> None:
    args = parse_args()
    window_settings = load_window_settings(args.config, args.window_key)
    if args.mode is not None:
        window_settings["mode"] = args.mode
    for field in ("x", "y", "width", "height"):
        value = getattr(args, field)
        if value is not None:
            window_settings[field] = value
    if args.frameless is not None:
        window_settings["frameless"] = args.frameless

    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("XAUTHORITY", os.path.expanduser("~/.Xauthority"))
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    os.environ.setdefault("XDG_SESSION_TYPE", "x11")
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--enable-gpu-rasterization --ignore-gpu-blocklist --use-gl=desktop",
    )

    app = QtWidgets.QApplication(sys.argv)
    window = Web3DWindow(args.url, window_settings)

    signal.signal(signal.SIGINT, lambda *_args: QtCore.QTimer.singleShot(0, app.quit))
    signal.signal(signal.SIGTERM, lambda *_args: QtCore.QTimer.singleShot(0, app.quit))
    signal_timer = QtCore.QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(200)

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
