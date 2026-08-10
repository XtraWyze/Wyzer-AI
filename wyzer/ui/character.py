"""Transparent desktop character and speech bubble for the optional Wyzer UI."""

from __future__ import annotations

import math
import random
import re
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QMenu, QWidget


class SpeechBubble(QFrame):
    """Small always-on-top speech bubble anchored near the character."""

    def __init__(self, owner: QWidget) -> None:
        super().__init__(None)
        self._owner = owner
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._label = QLabel(self)
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._label.setStyleSheet(
            "QLabel { background: rgba(30, 32, 40, 235); color: white; "
            "border: 1px solid rgba(255,255,255,55); border-radius: 12px; "
            "padding: 10px 12px; font-size: 13px; }"
        )
        # Use an explicit logical-pixel size instead of adjustSize()/layout
        # negotiation.  Mixed-DPI Windows desktops can otherwise turn a one-line
        # tool window into a tall bubble when Qt's DPI context changes at startup.
        self.setMinimumSize(1, 1)
        self.setMaximumSize(360, 1000)

    def set_text(self, text: str) -> None:
        self._label.setText(text)
        metrics = self._label.fontMetrics()
        text_rect = metrics.boundingRect(
            QRect(0, 0, 320, 10_000),
            Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            text,
        )
        width = max(92, min(360, text_rect.width() + 30))
        height = max(39, text_rect.height() + 24)
        self.setFixedSize(width, height)
        self._label.setGeometry(0, 0, width, height)
        self.update_position()

    def update_position(self) -> None:
        if not self._owner:
            return
        owner = self._owner.frameGeometry()
        x = owner.center().x() - self.width() // 2
        y = owner.top() - self.height() - 8
        screen = QApplication.screenAt(owner.center()) or QApplication.primaryScreen()
        if screen is not None:
            geom = screen.availableGeometry()
            x = max(geom.left() + 8, min(x, geom.right() - self.width() - 8))
            if y < geom.top() + 8:
                y = owner.bottom() + 8
        self.move(x, y)


class WyzerCharacter(QWidget):
    """Original lightweight desktop mascot with drag, reactions, bubbles and context menu."""

    open_chat_requested = Signal()
    stop_requested = Signal()
    listen_requested = Signal()
    quit_requested = Signal()
    muted_changed = Signal(bool)
    comments_changed = Signal(bool)

    def __init__(self, assistant_name: str = "Wyzer", avatar_dir: Path | None = None) -> None:
        super().__init__(None)
        self.assistant_name = assistant_name
        self._dragging = False
        self._drag_offset = QPoint()
        self._moved = False
        self._muted = False
        self._comments_enabled = True
        self._status = "Idle"
        self._phase = 0.0
        self._blink = 0
        self._frame_index = 0
        self._animations: dict[str, list[QPixmap]] = {}

        # Lightweight desktop-pet motion.  These states move the existing avatar
        # image/window; they do not require separate animation folders or assets.
        self._pet_state = "sitting"
        self._walk_direction = -1
        self._walk_speed = 1.5
        self._vertical_velocity = 0.0
        self._walk_ticks_remaining = 0
        # 1 = face right / original artwork, -1 = face left / mirrored artwork.
        # This lets one set of PNGs work in both horizontal directions.
        self._facing_direction = 1

        self._bubble: SpeechBubble | None = None
        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.timeout.connect(self.hide_bubble)

        self.setWindowTitle(assistant_name)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(220, 300)

        self._load_frames(avatar_dir)
        self._animation_timer = QTimer(self)
        self._animation_timer.timeout.connect(self._animate)
        self._animation_timer.start(90)

        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._start_blink)
        self._blink_timer.start(3400)

        # Physics/movement runs independently of the artwork.  A single PNG works.
        self._motion_timer = QTimer(self)
        self._motion_timer.timeout.connect(self._motion_step)
        self._motion_timer.start(30)

        self._wander_timer = QTimer(self)
        self._wander_timer.setSingleShot(True)
        self._wander_timer.timeout.connect(self._choose_idle_action)

        # After an automatic walk, stand using the idle animation briefly before
        # changing to the sit animation.  This is intentionally a separate timer
        # so the pet remains responsive to dragging while it is standing.
        self._stand_timer = QTimer(self)
        self._stand_timer.setSingleShot(True)
        self._stand_timer.timeout.connect(self._finish_standing)
        self._stand_duration_ms = 3000

        self.move_to_bottom_right()
        self._schedule_idle_action()

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def comments_enabled(self) -> bool:
        return self._comments_enabled

    def _load_frames(self, avatar_dir: Path | None) -> None:
        """Load behavior animations from filename prefixes in one avatar folder.

        Supported names are idle1.png, walk1.png, drag1.png, fall1.png and
        sit1.png (plus additional numbered frames and .webp equivalents).
        No behavior subfolders are used.  Older unprefixed images remain usable
        as the idle animation for backwards compatibility.
        """
        root = avatar_dir or (Path(".wyzer") / "avatar")
        if not root.exists():
            return

        grouped: dict[str, list[tuple[int, Path]]] = {
            "idle": [],
            "walk": [],
            "drag": [],
            "fall": [],
            "sit": [],
        }
        legacy: list[Path] = []
        frame_pattern = re.compile(
            r"^(idle|walk|drag|fall|sit)(\d*)\.(png|webp)$",
            re.IGNORECASE,
        )

        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file():
                continue
            match = frame_pattern.match(path.name)
            if match is None:
                if path.suffix.casefold() in {".png", ".webp"}:
                    legacy.append(path)
                continue
            animation = match.group(1).casefold()
            number_text = match.group(2)
            number = int(number_text) if number_text else 0
            grouped[animation].append((number, path))

        for animation, numbered_paths in grouped.items():
            frames: list[QPixmap] = []
            for _, path in sorted(
                numbered_paths, key=lambda item: (item[0], item[1].name.casefold())
            )[:48]:
                pixmap = QPixmap(str(path))
                if not pixmap.isNull():
                    frames.append(pixmap)
            if frames:
                self._animations[animation] = frames

        # Preserve the original avatar behavior for users who already have plain
        # PNG/WebP filenames that do not use the animation prefixes.
        if "idle" not in self._animations and legacy:
            frames = []
            for path in legacy[:48]:
                pixmap = QPixmap(str(path))
                if not pixmap.isNull():
                    frames.append(pixmap)
            if frames:
                self._animations["idle"] = frames

    def _animation_name(self) -> str:
        return {
            "walking": "walk",
            "dragging": "drag",
            "falling": "fall",
            "standing": "idle",
            "sitting": "sit",
        }.get(self._pet_state, "idle")

    def _current_frames(self) -> list[QPixmap]:
        animation = self._animation_name()
        return self._animations.get(animation) or self._animations.get("idle", [])

    def move_to_bottom_right(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        self.move(geom.right() - self.width() - 18, self._floor_y(geom))
        self._pet_state = "sitting"
        self._vertical_velocity = 0.0

    def _current_screen_geometry(self) -> QRect:
        center = self.frameGeometry().center()
        screen = QApplication.screenAt(center) or QApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1920, 1080)
        return screen.availableGeometry()

    def _floor_y(self, geom: QRect | None = None) -> int:
        geom = geom or self._current_screen_geometry()
        # availableGeometry excludes the taskbar, so the avatar stands directly
        # above it instead of falling behind it.
        return geom.bottom() - self.height() + 1

    def _schedule_idle_action(self) -> None:
        if not self._wander_timer.isActive():
            self._wander_timer.start(random.randint(2500, 7000))

    def _stand_before_sitting(self) -> None:
        """Use idle art for a few seconds after walking, then sit."""
        self._wander_timer.stop()
        self._pet_state = "standing"
        self._frame_index = 0
        self._stand_timer.start(self._stand_duration_ms)
        self.update()

    def _finish_standing(self) -> None:
        if self._dragging or self._pet_state != "standing":
            return
        self._pet_state = "sitting"
        self._frame_index = 0
        self._schedule_idle_action()
        self.update()

    def _set_facing_from_horizontal_delta(self, delta_x: int | float) -> None:
        if delta_x < 0:
            self._facing_direction = -1
        elif delta_x > 0:
            self._facing_direction = 1

    def _choose_idle_action(self) -> None:
        if self._dragging or self._pet_state == "falling":
            self._schedule_idle_action()
            return
        # Mostly sit, sometimes wander a short distance.
        if random.random() < 0.68:
            self._pet_state = "sitting"
            self._schedule_idle_action()
            self.update()
            return
        self._stand_timer.stop()
        self._walk_direction = random.choice((-1, 1))
        self._facing_direction = self._walk_direction
        self._walk_speed = random.uniform(1.1, 2.0)
        self._walk_ticks_remaining = random.randint(45, 140)
        self._pet_state = "walking"
        self.update()

    def _motion_step(self) -> None:
        if self._dragging:
            return

        geom = self._current_screen_geometry()
        floor_y = self._floor_y(geom)

        if self._pet_state == "falling":
            self._vertical_velocity = min(self._vertical_velocity + 1.35, 28.0)
            next_y = int(self.y() + self._vertical_velocity)
            if next_y >= floor_y:
                self.move(self.x(), floor_y)
                self._vertical_velocity = 0.0
                self._pet_state = "sitting"
                self._schedule_idle_action()
            else:
                self.move(self.x(), next_y)
            self._sync_bubble()
            self.update()
            return

        # If a screen layout change leaves the pet above the ground, let it fall.
        if self.y() < floor_y - 2:
            self._pet_state = "falling"
            self._vertical_velocity = max(0.0, self._vertical_velocity)
            self.update()
            return

        if self.y() != floor_y:
            self.move(self.x(), floor_y)

        if self._pet_state != "walking":
            return

        next_x = round(self.x() + self._walk_direction * self._walk_speed)
        left = geom.left()
        right = geom.right() - self.width() + 1
        if next_x <= left:
            next_x = left
            self._walk_direction = 1
        elif next_x >= right:
            next_x = right
            self._walk_direction = -1
        self._facing_direction = self._walk_direction
        self.move(next_x, floor_y)
        self._walk_ticks_remaining -= 1
        if self._walk_ticks_remaining <= 0:
            self._stand_before_sitting()
        self._sync_bubble()
        self.update()

    def _sync_bubble(self) -> None:
        if self._bubble is not None and self._bubble.isVisible():
            self._bubble.update_position()

    def set_status(self, status: str) -> None:
        self._status = status or "Idle"
        self.update()

    def say(self, text: str, duration_ms: int = 4500) -> None:
        cleaned = " ".join(str(text).split())
        if not cleaned:
            return
        if self._bubble is None:
            self._bubble = SpeechBubble(self)
        self._bubble.set_text(cleaned)
        self._bubble.show()
        self._bubble.raise_()
        if duration_ms > 0:
            self._bubble_timer.start(duration_ms)
        else:
            self._bubble_timer.stop()

    def hide_bubble(self) -> None:
        self._bubble_timer.stop()
        if self._bubble is not None:
            self._bubble.hide()

    def _animate(self) -> None:
        self._phase = (self._phase + 0.13) % (math.tau)
        if self._blink > 0:
            self._blink -= 1
        frames = self._current_frames()
        if frames:
            self._frame_index = (self._frame_index + 1) % len(frames)
        self.update()
        if self._bubble is not None and self._bubble.isVisible():
            self._bubble.update_position()

    def _start_blink(self) -> None:
        self._blink = 2
        self._blink_timer.start(random.randint(2600, 5200))

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        frames = self._current_frames()
        if frames:
            pixmap = frames[self._frame_index % len(frames)]
            target = self.size()
            y_offset = 0
            rotation = 0.0
            animation = self._animation_name()
            using_state_art = animation in self._animations
            if not using_state_art:
                # When a behavior-specific PNG is missing, animate the idle art a
                # little so the behavior is still readable instead of failing.
                if self._pet_state == "sitting":
                    target.setHeight(max(1, int(target.height() * 0.90)))
                    y_offset = 14
                elif self._pet_state == "walking":
                    y_offset = int(abs(math.sin(self._phase * 2.0)) * 4)
                elif self._pet_state == "falling":
                    rotation = max(-8.0, min(8.0, self._vertical_velocity * 0.22))

            scaled = pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if self._facing_direction < 0:
                scaled = scaled.transformed(
                    QTransform().scale(-1.0, 1.0),
                    Qt.TransformationMode.SmoothTransformation,
                )
            x = (self.width() - scaled.width()) // 2
            y = self.height() - scaled.height() + y_offset
            if rotation:
                painter.save()
                painter.translate(self.width() / 2, self.height() / 2)
                painter.rotate(rotation)
                painter.translate(-self.width() / 2, -self.height() / 2)
                painter.drawPixmap(x, y, scaled)
                painter.restore()
            else:
                painter.drawPixmap(x, y, scaled)
            return
        self._paint_default_mascot(painter)

    def _paint_default_mascot(self, painter: QPainter) -> None:
        """Draw an original chibi-style placeholder so the UI works without bundled art."""
        if self._pet_state == "walking":
            bob = int(abs(math.sin(self._phase * 2.0)) * -5)
        elif self._pet_state == "sitting":
            bob = 12
        else:
            bob = int(math.sin(self._phase) * 2)
        cx = self.width() // 2
        base_y = 278 + bob

        # Soft ground shadow.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 45))
        painter.drawEllipse(QRectF(cx - 54, base_y - 10, 108, 18))

        # Legs / shoes.
        painter.setBrush(QColor(48, 53, 68))
        painter.drawRoundedRect(QRectF(cx - 40, base_y - 62, 28, 56), 12, 12)
        painter.drawRoundedRect(QRectF(cx + 12, base_y - 62, 28, 56), 12, 12)
        painter.setBrush(QColor(32, 35, 45))
        painter.drawRoundedRect(QRectF(cx - 46, base_y - 18, 39, 16), 8, 8)
        painter.drawRoundedRect(QRectF(cx + 7, base_y - 18, 39, 16), 8, 8)

        # Hoodie/body.
        body = QPainterPath()
        body.moveTo(cx - 55, base_y - 132)
        body.quadTo(cx - 68, base_y - 88, cx - 45, base_y - 46)
        body.lineTo(cx + 45, base_y - 46)
        body.quadTo(cx + 68, base_y - 88, cx + 55, base_y - 132)
        body.closeSubpath()
        painter.setBrush(QColor(83, 92, 122))
        painter.drawPath(body)
        painter.setPen(QPen(QColor(205, 213, 235, 170), 2))
        painter.drawLine(cx, base_y - 126, cx, base_y - 68)
        painter.setPen(Qt.PenStyle.NoPen)

        # Head / hair silhouette.
        head_y = base_y - 184
        painter.setBrush(QColor(45, 50, 67))
        painter.drawEllipse(QRectF(cx - 67, head_y - 63, 134, 132))
        # Side hair pieces.
        painter.drawRoundedRect(QRectF(cx - 72, head_y - 8, 32, 88), 14, 14)
        painter.drawRoundedRect(QRectF(cx + 40, head_y - 8, 32, 88), 14, 14)

        # Face.
        painter.setBrush(QColor(245, 218, 202))
        painter.drawEllipse(QRectF(cx - 50, head_y - 45, 100, 102))

        # Bangs.
        painter.setBrush(QColor(45, 50, 67))
        bangs = QPainterPath()
        bangs.moveTo(cx - 51, head_y - 22)
        bangs.quadTo(cx - 30, head_y - 68, cx - 5, head_y - 35)
        bangs.quadTo(cx + 13, head_y - 70, cx + 51, head_y - 24)
        bangs.lineTo(cx + 48, head_y - 48)
        bangs.lineTo(cx - 48, head_y - 51)
        bangs.closeSubpath()
        painter.drawPath(bangs)

        # Eyes change slightly while thinking/listening.
        eye_y = head_y + 7
        painter.setPen(
            QPen(
                QColor(37, 41, 55),
                4,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        if self._blink:
            painter.drawLine(cx - 27, eye_y, cx - 15, eye_y)
            painter.drawLine(cx + 15, eye_y, cx + 27, eye_y)
        else:
            painter.setBrush(QColor(66, 75, 110))
            painter.setPen(QPen(QColor(37, 41, 55), 2))
            painter.drawEllipse(QRectF(cx - 29, eye_y - 7, 13, 18))
            painter.drawEllipse(QRectF(cx + 16, eye_y - 7, 13, 18))
            painter.setBrush(QColor(240, 245, 255))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(cx - 25, eye_y - 4, 4, 5))
            painter.drawEllipse(QRectF(cx + 20, eye_y - 4, 4, 5))

        # Mouth.
        painter.setPen(
            QPen(
                QColor(117, 73, 75),
                2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        if self._status.casefold() in {"listening", "thinking", "working"}:
            painter.drawEllipse(QRectF(cx - 4, head_y + 31, 8, 6))
        else:
            painter.drawArc(QRectF(cx - 10, head_y + 22, 20, 14), 200 * 16, 140 * 16)

        # Tiny status light on hoodie.
        status = self._status.casefold()
        if status == "listening":
            light = QColor(87, 190, 135)
        elif status in {"thinking", "working"}:
            light = QColor(234, 183, 76)
        elif status == "error":
            light = QColor(213, 89, 89)
        else:
            light = QColor(115, 145, 220)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(light)
        painter.drawEllipse(QRectF(cx - 6, base_y - 104, 12, 12))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._pet_state = "dragging"
            self._vertical_velocity = 0.0
            self._wander_timer.stop()
            self._stand_timer.stop()
            self._moved = False
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            target = event.globalPosition().toPoint() - self._drag_offset
            delta = target - self.pos()
            if delta.manhattanLength() > 2:
                self._moved = True
            self._set_facing_from_horizontal_delta(delta.x())
            self.move(target)
            if self._bubble is not None and self._bubble.isVisible():
                self._bubble.update_position()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            geom = self._current_screen_geometry()
            floor_y = self._floor_y(geom)
            if self.y() < floor_y - 2:
                # Releasing the avatar in mid-air lets gravity take over.
                self._pet_state = "falling"
                self._vertical_velocity = 0.0
            else:
                self.move(self.x(), floor_y)
                self._pet_state = "sitting"
                self._schedule_idle_action()
            if not self._moved:
                self.say(random.choice(("Hey!", "Need something?", "I'm here.")), 1800)
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_chat_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)

        chat = QAction(f"Chat with {self.assistant_name}", self)
        chat.triggered.connect(self.open_chat_requested.emit)
        menu.addAction(chat)

        listen = QAction("Listen now", self)
        listen.triggered.connect(self.listen_requested.emit)
        menu.addAction(listen)

        stop = QAction("Stop current task", self)
        stop.triggered.connect(self.stop_requested.emit)
        menu.addAction(stop)

        menu.addSeparator()

        muted = QAction("Mute voice", self)
        muted.setCheckable(True)
        muted.setChecked(self._muted)
        muted.toggled.connect(self._set_muted)
        menu.addAction(muted)

        comments = QAction("Ambient comments", self)
        comments.setCheckable(True)
        comments.setChecked(self._comments_enabled)
        comments.toggled.connect(self._set_comments_enabled)
        menu.addAction(comments)

        hide = QAction("Hide character", self)
        hide.triggered.connect(self.hide)
        menu.addAction(hide)

        menu.addSeparator()
        quit_action = QAction(f"Quit {self.assistant_name}", self)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)
        menu.exec(event.globalPos())

    def _set_muted(self, muted: bool) -> None:
        self._muted = muted
        self.muted_changed.emit(muted)
        self.say("Voice muted." if muted else "Voice unmuted.", 1800)

    def _set_comments_enabled(self, enabled: bool) -> None:
        self._comments_enabled = enabled
        self.comments_changed.emit(enabled)
        if enabled:
            self.say("I'll hang around.", 1800)
