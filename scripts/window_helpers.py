from typing import Dict, Tuple

from PyQt5 import QtCore


LEFT_EDGE = 1
TOP_EDGE = 2
RIGHT_EDGE = 4
BOTTOM_EDGE = 8


def resolve_window_bounds(
    settings: Dict,
    screen_geometry: QtCore.QRect,
    fallback: Tuple[int, int, int, int],
) -> Tuple[int, int, int, int]:
    mode = str(settings.get("mode", "manual")).strip().lower()
    if screen_geometry is None:
        return fallback

    screen_x = screen_geometry.x()
    screen_y = screen_geometry.y()
    screen_width = max(screen_geometry.width(), 1)
    screen_height = max(screen_geometry.height(), 1)

    if mode == "left_half":
        return screen_x, screen_y, max(screen_width // 2, 1), screen_height
    if mode == "right_half":
        half_width = max(screen_width // 2, 1)
        return screen_x + screen_width - half_width, screen_y, half_width, screen_height
    if mode == "fullscreen":
        return screen_x, screen_y, screen_width, screen_height

    return (
        int(settings.get("x", fallback[0])),
        int(settings.get("y", fallback[1])),
        max(int(settings.get("width", fallback[2])), 1),
        max(int(settings.get("height", fallback[3])), 1),
    )


class AdjustableFramelessWindowMixin:
    RESIZE_MARGIN = 8

    def _init_adjustable_window(self, *, frameless: bool) -> None:
        self._window_frameless = bool(frameless)
        self._drag_active = False
        self._drag_offset = QtCore.QPoint()
        self._resize_active = False
        self._resize_edges = 0
        self._resize_start_pos = QtCore.QPoint()
        self._resize_start_geometry = QtCore.QRect()
        self.setMouseTracking(True)
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint, self._window_frameless)

    def _start_window_adjustment(self, event, *, allow_drag: bool) -> bool:
        if not self._window_frameless or event.button() != QtCore.Qt.LeftButton:
            return False
        resize_edges = self._hit_test_resize_edges(event.pos())
        if resize_edges:
            self._resize_active = True
            self._resize_edges = resize_edges
            self._resize_start_pos = event.globalPos()
            self._resize_start_geometry = self.geometry()
            event.accept()
            return True
        if allow_drag:
            self._drag_active = True
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
            return True
        return False

    def _handle_window_adjustment_move(self, event) -> bool:
        if self._resize_active:
            self._resize_window(event.globalPos())
            event.accept()
            return True
        if self._drag_active and (event.buttons() & QtCore.Qt.LeftButton):
            self.move(event.globalPos() - self._drag_offset)
            event.accept()
            return True
        self._update_resize_cursor(event.pos())
        return False

    def _finish_window_adjustment(self, event) -> bool:
        if event.button() != QtCore.Qt.LeftButton:
            return False
        if self._drag_active or self._resize_active:
            self._drag_active = False
            self._resize_active = False
            self._resize_edges = 0
            self._update_resize_cursor(event.pos())
            event.accept()
            return True
        return False

    def _clear_resize_cursor(self) -> None:
        if not self._drag_active and not self._resize_active:
            self.unsetCursor()

    def _hit_test_resize_edges(self, pos: QtCore.QPoint) -> int:
        if not self._window_frameless:
            return 0
        rect = self.rect()
        margin = max(4, self.RESIZE_MARGIN)
        edges = 0
        if pos.x() <= margin:
            edges |= LEFT_EDGE
        elif pos.x() >= rect.width() - margin:
            edges |= RIGHT_EDGE
        if pos.y() <= margin:
            edges |= TOP_EDGE
        elif pos.y() >= rect.height() - margin:
            edges |= BOTTOM_EDGE
        return edges

    def _update_resize_cursor(self, pos: QtCore.QPoint) -> None:
        if not self._window_frameless or self._drag_active or self._resize_active:
            return
        edges = self._hit_test_resize_edges(pos)
        cursor_shape = self._cursor_shape_for_edges(edges)
        if cursor_shape is None:
            self.unsetCursor()
        else:
            self.setCursor(cursor_shape)

    @staticmethod
    def _cursor_shape_for_edges(edges: int):
        if edges in {LEFT_EDGE | TOP_EDGE, RIGHT_EDGE | BOTTOM_EDGE}:
            return QtCore.Qt.SizeFDiagCursor
        if edges in {RIGHT_EDGE | TOP_EDGE, LEFT_EDGE | BOTTOM_EDGE}:
            return QtCore.Qt.SizeBDiagCursor
        if edges in {LEFT_EDGE, RIGHT_EDGE}:
            return QtCore.Qt.SizeHorCursor
        if edges in {TOP_EDGE, BOTTOM_EDGE}:
            return QtCore.Qt.SizeVerCursor
        return None

    def _resize_window(self, global_pos: QtCore.QPoint) -> None:
        delta = global_pos - self._resize_start_pos
        start = self._resize_start_geometry

        new_x = start.x()
        new_y = start.y()
        new_width = start.width()
        new_height = start.height()

        min_width = max(self.minimumWidth(), 160)
        min_height = max(self.minimumHeight(), 120)

        if self._resize_edges & LEFT_EDGE:
            max_left = start.x() + start.width() - min_width
            new_x = min(start.x() + delta.x(), max_left)
            new_width = start.width() - (new_x - start.x())
        if self._resize_edges & RIGHT_EDGE:
            new_width = max(min_width, start.width() + delta.x())
        if self._resize_edges & TOP_EDGE:
            max_top = start.y() + start.height() - min_height
            new_y = min(start.y() + delta.y(), max_top)
            new_height = start.height() - (new_y - start.y())
        if self._resize_edges & BOTTOM_EDGE:
            new_height = max(min_height, start.height() + delta.y())

        self.setGeometry(new_x, new_y, new_width, new_height)
