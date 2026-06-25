#!/usr/bin/env python3

import argparse
import os
import signal
import sys

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtWebEngineWidgets import QWebEngineProfile, QWebEngineView


class Web3DWindow(QtWidgets.QMainWindow):
    def __init__(self, url: str, x: int, y: int, width: int, height: int) -> None:
        super().__init__()
        self._drag_active = False
        self._drag_offset = QtCore.QPoint()
        self._default_geometry = QtCore.QRect(x, y, width, height)

        self.setWindowTitle("Insight Web 3D")
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint, True)
        self.setGeometry(self._default_geometry)

        view = QWebEngineView(self)
        profile = view.page().profile()
        profile.setHttpCacheType(QWebEngineProfile.NoCache)
        profile.clearHttpCache()
        view.setUrl(QtCore.QUrl(url))
        self.setCentralWidget(view)

    def keyPressEvent(self, event) -> None:
        if event.key() == QtCore.Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_active = True
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_active and (event.buttons() & QtCore.Qt.LeftButton):
            self.move(event.globalPos() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._drag_active:
            self._drag_active = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self.setGeometry(self._default_geometry)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/3d")
    parser.add_argument("--x", type=int, default=960)
    parser.add_argument("--y", type=int, default=0)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=1080)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

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
    window = Web3DWindow(args.url, args.x, args.y, args.width, args.height)

    signal.signal(signal.SIGINT, lambda *_args: QtCore.QTimer.singleShot(0, app.quit))
    signal.signal(signal.SIGTERM, lambda *_args: QtCore.QTimer.singleShot(0, app.quit))
    signal_timer = QtCore.QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(200)

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
