from PyQt5 import QtCore, QtWidgets


class ImagePanel(QtWidgets.QFrame):
    class AspectRatioLabel(QtWidgets.QLabel):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.aspect_ratio = 16.0 / 9.0
            self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

        def set_aspect_ratio(self, width: int, height: int) -> None:
            if width > 0 and height > 0:
                ratio = float(width) / float(height)
                if abs(ratio - self.aspect_ratio) > 1e-3:
                    self.aspect_ratio = ratio
                    self.updateGeometry()

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            if self.aspect_ratio <= 1e-6:
                return max(width, 1)
            return max(int(round(width / self.aspect_ratio)), 1)

        def sizeHint(self) -> QtCore.QSize:
            width = 480
            return QtCore.QSize(width, self.heightForWidth(width))

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: #16202a; border: 1px solid #233142; border-radius: 6px; }"
            "QLabel { color: #e9eef4; }"
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #e9eef4; border: none;")
        layout.addWidget(self.title_label)

        self.fps_label = QtWidgets.QLabel("waiting")
        self.fps_label.setStyleSheet("font-size: 12px; color: #8fa3b8; border: none;")
        layout.addWidget(self.fps_label)

        self.image_label = self.AspectRatioLabel()
        self.image_label.setAlignment(QtCore.Qt.AlignCenter)
        self.image_label.setMinimumSize(180, 120)
        self.image_label.setStyleSheet("background: #0c1116; border: none;")
        self.image_label.setText("Waiting for image...")
        layout.addWidget(self.image_label, 1)

    def set_image_shape(self, width: int, height: int) -> None:
        self.image_label.set_aspect_ratio(width, height)
