"""Small text chat window used by the desktop companion."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ChatWindow(QWidget):
    submitted = Signal(str)
    stop_requested = Signal()

    def __init__(self, assistant_name: str = "Wyzer") -> None:
        super().__init__()
        self.assistant_name = assistant_name
        self.setWindowTitle(f"{assistant_name} Chat")
        self.resize(460, 390)

        self.status = QLabel("Idle")
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.input = QLineEdit()
        self.input.setPlaceholderText(f"Ask {assistant_name} anything...")
        self.send = QPushButton("Send")
        self.stop = QPushButton("Stop")

        buttons = QHBoxLayout()
        buttons.addWidget(self.input, 1)
        buttons.addWidget(self.send)
        buttons.addWidget(self.stop)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.transcript, 1)
        layout.addLayout(buttons)

        self.send.clicked.connect(self._submit)
        self.input.returnPressed.connect(self._submit)
        self.stop.clicked.connect(self.stop_requested.emit)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.append_user(text)
        self.submitted.emit(text)

    def append_user(self, text: str) -> None:
        self.transcript.appendPlainText(f"You: {text}\n")

    def append_assistant(self, text: str) -> None:
        self.transcript.appendPlainText(f"{self.assistant_name}: {text}\n")

    def set_status(self, status: str) -> None:
        self.status.setText(status)

    def show_near_character(self, character: QWidget) -> None:
        pos = character.frameGeometry()
        self.move(max(10, pos.left() - self.width() + 90), max(10, pos.top() - 50))
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
