"""
toki_desktop_mark.py  --  TOKI desktop widget, hotkey + voice edition.

WHAT THIS REPLACES
------------------
The old app.py chat window is gone.  This file IS the entire UI.

BEHAVIOR
--------
IDLE (hidden-ish notch):
    A small 56 x 56 circle sits at the very top-centre of the primary
    screen, peeking 6 px below the screen edge so it's barely visible but
    still hoverable.  No chat window, no text field, nothing else.

HOVER while idle:
    The notch slides fully into view and a "Scheduled commands" panel
    opens below it, listing every pending timed command with a live
    countdown and a one-click cancel button.  Panel auto-closes when the
    mouse leaves both the notch and the panel.  If there are no scheduled
    commands the panel shows a quiet "Nothing scheduled" label.

Ctrl+K (hotkey):
    The mark slides fully into view AND expands to 128 × 128 px.  Mood
    switches to "mysterious" (same as the old "TOKI is listening" state).
    Audio capture starts immediately via HotkeyVoicePipeline.

    Pressing Ctrl+K again while already listening resets the silence
    hangover timer -- the recording window stays open even if you paused
    between sentences.

TRANSCRIBING:
    As soon as faster-whisper starts (VAD has seen silence), mood switches
    to "energetic" so there's visible feedback the command is being
    processed.

DONE / ERROR:
    The mark shrinks back to the idle notch size and slides partly off the
    top edge.  Mood returns to "calm".

STANDALONE TEST
---------------
    python toki_desktop_mark.py

Opens the notch.  Right-click the tray icon for test cycles; press
Ctrl+K to simulate a real voice turn (needs the rest of the pipeline
installed -- see voice_pipeline.py).

INTEGRATION
-----------
    from toki_desktop_mark import DesktopMark

    mark = DesktopMark()
    mark.set_scheduler(orchestrator.scheduler)   # for hover panel
    mark.set_dispatch(orchestrator.process_request)  # voice → command
    mark.show()
    mark.play_startup()
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import (
    Qt, QPoint, QRect, QTimer, QPropertyAnimation, QEasingCurve,
    pyqtSignal, QObject, pyqtSlot,
)
from PyQt6.QtGui import QColor, QAction, QIcon, QPixmap, QCursor
from PyQt6.QtWidgets import (
    QApplication, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSystemTrayIcon, QMenu,
    QGraphicsDropShadowEffect, QSizePolicy, QLineEdit,
)

from mark_renderer import MoodMarkWidget

# BETA 0.3.39: the mark used to render via QWebEngineView + mark_visual.py's
# HTML/SVG/JS. Confirmed directly that QWebEngineView's Chromium compositor
# does not paint correctly inside a WA_TranslucentBackground top-level window
# on Windows -- total silent failure (no exception, nothing ever visible,
# regardless of position/size). Replaced with mark_renderer.MoodMarkWidget,
# a plain QPainter-based widget with no Chromium dependency, which composites
# correctly with translucency. See mark_renderer.py's module docstring for
# what's an exact port of mark_visual.py's design vs. a deliberate
# approximation (continuous spin easing, specifically).


# ── geometry constants ───────────────────────────────────────────────────────

NOTCH_PX   = 56    # idle size (pixels, square)
ACTIVE_PX  = 128   # listening / working size
PEEK_PX    = 6     # how many px of the notch are visible above the screen edge
                   # (so y = -(NOTCH_PX - PEEK_PX) when "peeked")
ANIM_MS    = 500   # slide animation duration

# y offset from top when fully in view
ACTIVE_TOP = 8     # active mark sits 8 px below screen top

LONG_PRESS_MS  = 350   # hold-to-drag threshold (below this = a click, not a drag)
REPLY_SHOW_MS  = 3000  # how long the reply bubble stays visible before fading
HOVER_INTENT_MS = 180  # dwell time before an idle-hover actually expands the
                        # notch -- without this, brushing past the 6px-tall
                        # peek strip (e.g. reaching for a browser tab at the
                        # top of the screen) instantly triggers a full
                        # expand+panel every time, which reads as "too quick"


# ── tiny signal bridge (pynput → Qt main thread) ─────────────────────────────

class _Bridge(QObject):
    hotkey    = pyqtSignal()   # Ctrl+K pressed
    listening = pyqtSignal()   # pipeline started capturing
    working   = pyqtSignal(str)  # pipeline is running a command (mood name)
    done      = pyqtSignal()   # pipeline finished, back to idle
    error     = pyqtSignal(str)  # something went wrong
    reply     = pyqtSignal(str)  # a turn's response text, emitted from any
                                 # thread -- Qt signals are the only safe
                                 # way to hop off a background dispatch
                                 # thread back onto the Qt main thread; a
                                 # bare QTimer.singleShot(0, ...) called
                                 # from a non-Qt thread is NOT guaranteed
                                 # to fire (confirmed directly: it silently
                                 # never ran in testing).
    permission_confirm = pyqtSignal()  # avatar-click permission gate confirm


_bridge = _Bridge()   # module-level singleton so voice_pipeline.py can import it


# ── scheduled-commands hover panel ──────────────────────────────────────────

class _CommandsPanel(QFrame):
    """
    Frameless popup that appears below the idle notch.
    Shows every active ScheduledItem with a countdown and cancel button.
    Auto-refreshes every second.
    """

    cancel_requested = pyqtSignal(str)   # emits item ID

    def __init__(self, parent: QWidget):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self._items: List[Tuple[str, str, float]] = []  # (id, desc, fire_at)
        self._scheduler = None   # ScheduledCommandManager or None

        # ── panel frame
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = QFrame(self)
        self._card.setObjectName("card")
        self._card.setStyleSheet("""
            QFrame#card {
                background: rgba(8, 10, 18, 220);
                border: 1px solid rgba(55, 138, 221, 60);
                border-radius: 12px;
            }
        """)
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(28)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 6)
        self._card.setGraphicsEffect(shadow)
        outer.addWidget(self._card)

        self._layout = QVBoxLayout(self._card)
        self._layout.setContentsMargins(14, 12, 14, 12)
        self._layout.setSpacing(8)

        self._header = QLabel("Scheduled commands")
        self._header.setStyleSheet(
            "color: rgba(181, 212, 244, 160); font: 600 10px 'Inter', sans-serif;"
            "letter-spacing: 0.08em; text-transform: uppercase;"
        )
        self._layout.addWidget(self._header)

        self._body = QVBoxLayout()
        self._body.setSpacing(4)
        self._layout.addLayout(self._body)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh)

    # ── public API ──────────────────────────────────────────────────────────

    def set_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    def popup_below(self, anchor_rect: QRect) -> None:
        """Position below anchor_rect and show."""
        self._refresh()
        self.adjustSize()
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.bottom() + 6
        self.move(x, max(0, y))
        self.show()
        self._refresh_timer.start()

    def hide_panel(self) -> None:
        self.hide()
        self._refresh_timer.stop()

    # ── internals ───────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        # clear body
        while self._body.count():
            item = self._body.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        items = []
        if self._scheduler is not None:
            try:
                items = self._scheduler.list_active()
            except Exception:
                pass

        if not items:
            lbl = QLabel("Nothing scheduled")
            lbl.setStyleSheet(
                "color: rgba(181,212,244,60); font: 12px 'Inter', sans-serif;"
            )
            self._body.addWidget(lbl)
            self.adjustSize()
            return

        for it in items:
            row = QHBoxLayout()
            row.setSpacing(10)

            secs = it.seconds_remaining()
            if secs >= 3600:
                countdown = f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"
            elif secs >= 60:
                countdown = f"{int(secs // 60)}m {int(secs % 60)}s"
            else:
                countdown = f"{int(secs)}s"

            desc = QLabel(f"{it.id}  ·  {it.description}")
            desc.setStyleSheet(
                "color: rgba(181,212,244,200); font: 13px 'Inter', sans-serif;"
            )
            desc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            row.addWidget(desc)

            clock = QLabel(countdown)
            clock.setStyleSheet(
                "color: rgba(55,138,221,200); font: 600 12px 'Inter', sans-serif;"
            )
            row.addWidget(clock)

            btn = QPushButton("✕")
            btn.setFixedSize(22, 22)
            btn.setStyleSheet("""
                QPushButton {
                    background: rgba(226,75,74,30);
                    border: 1px solid rgba(226,75,74,60);
                    border-radius: 11px;
                    color: rgba(226,75,74,200);
                    font: 11px;
                }
                QPushButton:hover {
                    background: rgba(226,75,74,90);
                    color: #fff;
                }
            """)
            item_id = it.id
            btn.clicked.connect(lambda _checked, iid=item_id: self._on_cancel(iid))
            row.addWidget(btn)

            wrapper = QWidget()
            wrapper.setLayout(row)
            self._body.addWidget(wrapper)

        self.adjustSize()

    def _on_cancel(self, item_id: str) -> None:
        if self._scheduler is not None:
            self._scheduler.cancel(item_id)
        self.cancel_requested.emit(item_id)
        self._refresh()


# ── floating reply bubble (appears next to the mark, fades after 3s) ────────

class _ReplyBubble(QFrame):
    """
    Frameless popup that shows TOKI's last text reply next to the mark,
    then auto-hides after REPLY_SHOW_MS. Same visual language/pattern as
    _CommandsPanel (rounded card, drop shadow) so it reads as part of the
    same UI rather than a different widget style.
    """

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("replycard")
        card.setStyleSheet("""
            QFrame#replycard {
                background: rgba(8, 10, 18, 220);
                border: 1px solid rgba(55, 138, 221, 60);
                border-radius: 12px;
            }
        """)
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(28)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 6)
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        inner = QVBoxLayout(card)
        inner.setContentsMargins(14, 10, 14, 10)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(320)
        self._label.setStyleSheet(
            "color: rgba(181,212,244,230); font: 13px 'Inter', sans-serif;"
        )
        inner.addWidget(self._label)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_below(self, anchor_rect: QRect, text: str) -> None:
        if not text:
            return
        self._label.setText(text)
        self.adjustSize()
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.bottom() + 6
        # keep on-screen horizontally
        screen = QApplication.primaryScreen().geometry()
        x = max(4, min(x, screen.width() - self.width() - 4))
        self.move(x, max(0, y))
        self.show()
        self._hide_timer.start(REPLY_SHOW_MS)

    def cancel_autohide(self) -> None:
        """Used by the hover panel: keep the last reply visible while
        the user is actively looking at the panel, instead of racing
        the 3s fade against their mouse movement."""
        self._hide_timer.stop()


# ── double-click prompt box (typed input, no chat window) ───────────────────

class _PromptBox(QFrame):
    """
    Small inline text box, popped up below the mark on a double-click.
    Enter submits and dispatches through the same orchestrator path a
    voice command uses; Escape cancels without sending anything.
    """

    submitted = pyqtSignal(str)

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("promptcard")
        card.setStyleSheet("""
            QFrame#promptcard {
                background: rgba(8, 10, 18, 235);
                border: 1px solid rgba(55, 138, 221, 90);
                border-radius: 12px;
            }
        """)
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(28)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 6)
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        inner = QVBoxLayout(card)
        inner.setContentsMargins(10, 8, 10, 8)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Type a command…")
        self._edit.setMinimumWidth(280)
        self._edit.setStyleSheet("""
            QLineEdit {
                background: rgba(20, 24, 38, 200);
                border: 1px solid rgba(55, 138, 221, 60);
                border-radius: 8px;
                padding: 6px 10px;
                color: rgba(230, 240, 250, 230);
                font: 13px 'Inter', sans-serif;
            }
        """)
        self._edit.returnPressed.connect(self._on_submit)
        inner.addWidget(self._edit)

    def popup_below(self, anchor_rect: QRect) -> None:
        self.adjustSize()
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.bottom() + 6
        screen = QApplication.primaryScreen().geometry()
        x = max(4, min(x, screen.width() - self.width() - 4))
        self.move(x, max(0, y))
        self._edit.clear()
        self.show()
        self._edit.setFocus()

    def _on_submit(self) -> None:
        text = self._edit.text().strip()
        self.hide()
        if text:
            self.submitted.emit(text)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)


# ── main desktop mark widget ─────────────────────────────────────────────────

class DesktopMark(QWidget):
    """
    The entire TOKI desktop UI.  No chat window, no text field.
    Voice in → command out, all through this widget + voice_pipeline.py.
    """

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        self._state: str = "idle"    # "idle" | "listening" | "working"
        self._scheduler = None
        self._dispatch_fn: Optional[Callable[[str], None]] = None
        self._assistant = None          # WindowsAIAssistant or None, for permission gate
        self._last_reply: str = ""
        self._home_rect: Optional[QRect] = None   # set once the user drags the mark

        # ── drag / click-disambiguation state
        # Long-press-to-drag and single/double click all arrive through the
        # same mousePressEvent, so they need to be told apart deliberately:
        #   · press, release quickly, no second press  -> single click
        #     (handled via mouseReleaseEvent + a short timer, so a second
        #     click within Qt's own double-click window can still cancel it)
        #   · press, release quickly, second press follows -> double click
        #     (Qt's native mouseDoubleClickEvent handles this directly)
        #   · press, HOLD past LONG_PRESS_MS -> drag mode, not a click at all
        self._press_pos: Optional[QPoint] = None       # widget-local press pos
        self._press_global: Optional[QPoint] = None     # global press pos (for drag delta)
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(LONG_PRESS_MS)
        self._long_press_timer.timeout.connect(self._on_long_press)
        self._dragging = False
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self._on_single_click_confirmed)
        self._suppress_next_release_click = False

        # ── mark visual
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._view = MoodMarkWidget(self)
        layout.addWidget(self._view)

        # ── scheduled-commands panel
        self._panel = _CommandsPanel(None)   # top-level window
        self._panel_hover_timer = QTimer(self)
        self._panel_hover_timer.setSingleShot(True)
        self._panel_hover_timer.setInterval(200)
        self._panel_hover_timer.timeout.connect(self._maybe_hide_panel)

        # dwell timer: enterEvent starts this instead of expanding
        # immediately; only actually expands if the mouse is still there
        # when it fires (see HOVER_INTENT_MS above).
        self._hover_intent_timer = QTimer(self)
        self._hover_intent_timer.setSingleShot(True)
        self._hover_intent_timer.setInterval(HOVER_INTENT_MS)
        self._hover_intent_timer.timeout.connect(self._expand_for_hover)

        # ── reply bubble (fades after REPLY_SHOW_MS) + typed-prompt box
        self._reply_bubble = _ReplyBubble()
        self._prompt_box = _PromptBox()
        self._prompt_box.submitted.connect(self._on_prompt_submitted)

        # ── animation
        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # ── tray icon
        self._tray = self._build_tray()

        # ── bridge connections (hotkey + pipeline → UI)
        _bridge.hotkey.connect(self._on_hotkey)
        _bridge.listening.connect(self._on_listening)
        _bridge.working.connect(self._on_working)
        _bridge.done.connect(self.idle)
        _bridge.reply.connect(self.show_reply)

        # ── position: start peeking at top-centre
        self._go_to_idle(animated=False)

        # ── mouse tracking for hover panel
        self.setMouseTracking(True)

        # ── drag-and-drop file selection (BETA 0.3.41) ────────────────────
        # "click it once, I'll act on it": dropping a file onto the overlay
        # feeds selection_context.py's SelectionContext, which
        # apis.py's FileConvertAPI reads for CONVERT/RESIZE/COMPRESS/
        # EXTRACT_SELECTED_FILE. Chosen over reading Explorer's live
        # selection via UI Automation because it's explicit -- the user
        # dragged THIS file on purpose, no ambiguity about which of
        # several selected Explorer items was meant, and no dependency on
        # walking Explorer's UIA tree across Windows versions/themes.
        self.setAcceptDrops(True)

        # start the hotkey listener
        _start_hotkey_listener()

    # ── public API ──────────────────────────────────────────────────────────

    def set_scheduler(self, scheduler) -> None:
        """Pass in orchestrator.scheduler so the hover panel can list items."""
        self._scheduler = scheduler
        self._panel.set_scheduler(scheduler)

    def set_dispatch(self, fn: Callable[[str], None]) -> None:
        """Pass in orchestrator.process_request (or equivalent) so
        transcribed speech can be dispatched as a command."""
        self._dispatch_fn = fn

    def set_assistant(self, assistant) -> None:
        """Pass in the WindowsAIAssistant instance so the avatar single-click
        can confirm a pending caution/destructive command (BETA 0.3.38's
        self._pending_confirmation) by submitting an empty message, same as
        pressing Enter with nothing typed."""
        self._assistant = assistant

    def play_startup(self) -> None:
        """Run the startup mood sequence once."""
        self._view.play_startup()

    def listening(self) -> None:
        """Mark is now listening (called by voice pipeline or externally)."""
        _bridge.listening.emit()

    def working(self, mood: str = "energetic") -> None:
        """Mark is processing a command."""
        _bridge.working.emit(mood)

    def idle(self) -> None:
        """Return to idle notch state."""
        self._state = "idle"
        self._view.set_mood("calm")
        self._go_to_idle(animated=True)

    def stopped(self) -> None:
        """Error / stop -- brief lifeless flash, then idle."""
        self._view.set_mood("lifeless")
        QTimer.singleShot(800, self.idle)

    # ── internal state transitions ───────────────────────────────────────────

    @pyqtSlot()
    def _on_hotkey(self) -> None:
        """Ctrl+K pressed.  If idle → go active.  If already listening → extend."""
        if self._state == "listening":
            # signal pipeline to extend its silence window
            from voice_pipeline import extend_listening
            try:
                extend_listening()
            except Exception:
                pass
            return
        self._state = "listening"
        self._view.set_mood("mysterious")
        self._go_to_active(animated=True)

    @pyqtSlot()
    def _on_listening(self) -> None:
        self._state = "listening"
        self._view.set_mood("mysterious")
        self._go_to_active(animated=True)

    @pyqtSlot(str)
    def _on_working(self, mood: str) -> None:
        self._state = "working"
        self._view.set_mood(mood)
        # stay at active size while working

    # ── geometry helpers ─────────────────────────────────────────────────────

    def _screen_centre_x(self) -> int:
        screen = QApplication.primaryScreen().geometry()
        return screen.center().x()

    def _idle_rect(self) -> QRect:
        """
        Notch rect. Defaults to peeking below top-centre, but if the user
        has long-press-dragged the mark somewhere else while idle, that
        dragged spot (self._home_rect) is remembered and used instead --
        it only ever resets on restart, matching "you can move it."
        """
        if self._home_rect is not None:
            return QRect(self._home_rect)
        cx = self._screen_centre_x()
        x = cx - NOTCH_PX // 2
        y = -(NOTCH_PX - PEEK_PX)
        return QRect(x, y, NOTCH_PX, NOTCH_PX)

    def _active_rect(self) -> QRect:
        """Full active rect: fully below screen top."""
        cx = self._screen_centre_x()
        x = cx - ACTIVE_PX // 2
        return QRect(x, ACTIVE_TOP, ACTIVE_PX, ACTIVE_PX)

    def _go_to_idle(self, animated: bool = True) -> None:
        target = self._idle_rect()
        if animated:
            self._animate_to(target)
        else:
            self.setGeometry(target)

    def _go_to_active(self, animated: bool = True) -> None:
        target = self._active_rect()
        if animated:
            self._animate_to(target)
        else:
            self.setGeometry(target)

    def _animate_to(self, target: QRect) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(target)
        self._anim.start()

    # ── drag-and-drop file selection ─────────────────────────────────────────

    def dragEnterEvent(self, event) -> None:
        # Only accept a single local file -- multi-file drops are a future
        # "batch" feature, not silently truncated to "just the first one"
        # here (see selection_context.py's module docstring on why this
        # store is deliberately single-slot).
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if not urls or not urls[0].isLocalFile():
            event.ignore()
            return
        path = urls[0].toLocalFile()

        from selection_context import get_selection_context
        info = get_selection_context().set_selected(path)

        if info:
            self.show_reply(f"Got it — \"{info['name']}\" is selected. What should I do with it?")
        else:
            # Only a real, existing FILE is accepted (folders are rejected
            # by SelectionContext.set_selected too) -- a dropped folder or
            # a path that vanished between the drag and the drop gets a
            # plain, honest "didn't work" instead of silently doing nothing.
            self.show_reply("I couldn't select that — try dragging a single file.")
        event.acceptProposedAction()

    # ── hover panel ──────────────────────────────────────────────────────────

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        # cancel any pending hide -- mouse is back before it fired
        self._panel_hover_timer.stop()
        if self._state == "idle":
            # don't expand instantly: wait HOVER_INTENT_MS to confirm this
            # is an actual hover and not a brush-past on the way to
            # something else at the top of the screen.
            self._hover_intent_timer.start()

    def _expand_for_hover(self) -> None:
        if self._state != "idle":
            return
        # expand in place -- if the user dragged the mark elsewhere,
        # hovering should reveal it right there, not yank it back to
        # top-centre (that would fight the drag-to-reposition feature).
        current = self._idle_rect()
        visible_rect = QRect(
            current.center().x() - NOTCH_PX // 2,
            current.y() if current.y() >= 0 else ACTIVE_TOP,
            NOTCH_PX, NOTCH_PX,
        )
        self._animate_to(visible_rect)
        self._panel.popup_below(visible_rect)
        # keep the last reply on screen while the panel is up rather
        # than letting its own 3s timer race the user's mouse -- the
        # panel's own hide (_maybe_hide_panel) already handles hiding
        # everything again once the mouse actually leaves both.
        if self._last_reply:
            self._reply_bubble.cancel_autohide()
            self._reply_bubble.show_below(visible_rect, self._last_reply)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        # cancel a not-yet-fired expand -- this was just a brush-past
        self._hover_intent_timer.stop()
        # brief grace period so mouse can move to the panel
        self._panel_hover_timer.start()

    def _maybe_hide_panel(self) -> None:
        cursor = QCursor.pos()
        if self.geometry().contains(cursor) or self._panel.geometry().contains(cursor):
            return
        self._panel.hide_panel()
        self._reply_bubble.hide()
        if self._state == "idle":
            self._go_to_idle(animated=True)

    # ── mouse: click (stop-listening-now) / double-click (type) /
    #           long-press (drag) ───────────────────────────────────────────
    #
    # All three share mousePressEvent as their starting point, so they're
    # disambiguated in order of increasing hold/press-count:
    #   1. press starts _long_press_timer (LONG_PRESS_MS) -- if it fires
    #      before release, this press becomes a drag, not a click at all.
    #   2. mouseMoveEvent while pressed, past the long-press threshold,
    #      actually moves the widget (drag).
    #   3. mouseReleaseEvent, if no drag happened and no long-press fired:
    #      starts _single_click_timer for Qt's own double-click interval --
    #      if a second press arrives in that window, mouseDoubleClickEvent
    #      fires instead and cancels the pending single-click action, so a
    #      double-click never *also* triggers the single-click behavior.
    #   4. mouseDoubleClickEvent -- open the typed-prompt box.

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._press_pos = event.position().toPoint()
        self._press_global = event.globalPosition().toPoint()
        self._dragging = False
        self._long_press_timer.start()

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is None:
            return
        if not self._dragging and not self._long_press_timer.isActive():
            # long-press threshold already passed (timer fired) -> this is
            # a drag from here on, regardless of how far the mouse has
            # actually moved yet.
            self._dragging = True
        if self._dragging:
            delta = event.globalPosition().toPoint() - self._press_global
            self.move(self.pos() + delta)
            self._press_global = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._long_press_timer.stop()
        was_dragging = self._dragging
        self._dragging = False
        self._press_pos = None
        self._press_global = None
        if self._suppress_next_release_click:
            # this release is the one immediately following
            # mouseDoubleClickEvent (Qt's real sequence is press, release,
            # press, DOUBLE-CLICK, release) -- that double-click has
            # already been handled, so this release must NOT queue its
            # own single-click timer, or a double-click always fires an
            # extra spurious single-click ~250ms later.
            self._suppress_next_release_click = False
            return
        if was_dragging:
            # drag just ended -- remember this as the new idle "home" spot
            # instead of snapping back to top-centre next time it goes idle.
            self._home_rect = QRect(self.geometry())
            return
        # not a drag -- queue a single-click action, but give Qt's own
        # double-click detection a chance to cancel it first.
        self._single_click_timer.start(QApplication.doubleClickInterval())

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # cancel the pending single-click so both don't fire
        self._single_click_timer.stop()
        self._long_press_timer.stop()
        self._suppress_next_release_click = True
        self._show_prompt_box()

    def _on_long_press(self) -> None:
        # timer fired while still pressed (mouseReleaseEvent hasn't run) ->
        # mouseMoveEvent will pick this up as a drag on the next move.
        pass

    def _on_single_click_confirmed(self) -> None:
        """
        A confirmed single click, not part of a double-click.

        If a dangerous command is currently pending permission, the avatar
        click confirms it (same as pressing Enter with empty input).
        Otherwise, behaves as before: reuses the Ctrl+K entry point so a
        click behaves exactly like a hotkey press in every state.
        """
        if self._assistant is not None and getattr(self._assistant, "_pending_confirmation", None) is not None:
            # Confirm the pending dangerous command without any text input.
            _bridge.permission_confirm.emit()
            return
        _bridge.hotkey.emit()

    def _show_prompt_box(self) -> None:
        anchor = QRect(self.geometry())
        self._prompt_box.popup_below(anchor)

    def _on_prompt_submitted(self, text: str) -> None:
        if self._dispatch_fn is not None:
            self._dispatch_fn(text)
        else:
            print(f"[TOKI] (no dispatch configured) -> {text}")

    def show_reply(self, text: str) -> None:
        """Call with the response text once a turn completes -- shows the
        floating bubble next to the mark, auto-hides after REPLY_SHOW_MS,
        and is what the hover panel surfaces as 'previous reply'."""
        self._last_reply = text or ""
        if not text:
            return
        anchor = QRect(self.geometry())
        self._reply_bubble.show_below(anchor, text)

    # ── tray icon ────────────────────────────────────────────────────────────

    def _build_tray(self) -> QSystemTrayIcon:
        pix = QPixmap(16, 16)
        pix.fill(QColor("#378ADD"))
        tray = QSystemTrayIcon(QIcon(pix), self)
        tray.setToolTip("TOKI  ·  Ctrl+K to speak")

        menu = QMenu()

        act_listen = QAction("Simulate: listen → work → done", self)
        act_listen.triggered.connect(self._simulate_cycle)
        menu.addAction(act_listen)

        act_startup = QAction("Play startup animation", self)
        act_startup.triggered.connect(self.play_startup)
        menu.addAction(act_startup)

        menu.addSeparator()

        act_quit = QAction("Quit TOKI", self)
        act_quit.triggered.connect(QApplication.instance().quit)
        menu.addAction(act_quit)

        tray.setContextMenu(menu)
        tray.show()
        return tray

    def _simulate_cycle(self) -> None:
        self.listening()
        QTimer.singleShot(1400, lambda: self.working("energetic"))
        QTimer.singleShot(3000, self.idle)


# ── global hotkey listener (pynput, background thread) ──────────────────────

_hotkey_thread: Optional[threading.Thread] = None


def _start_hotkey_listener() -> None:
    global _hotkey_thread
    if _hotkey_thread is not None and _hotkey_thread.is_alive():
        return
    _hotkey_thread = threading.Thread(target=_run_listener, daemon=True, name="toki-hotkey")
    _hotkey_thread.start()


def _run_listener() -> None:
    """
    Listens for Ctrl+K globally (pynput).
    Emits _bridge.hotkey on the Qt thread via Qt's signal/slot mechanism.
    pynput must be installed: pip install pynput
    """
    try:
        from pynput import keyboard as kb
    except ImportError:
        print(
            "[TOKI] pynput not installed -- Ctrl+K hotkey disabled.\n"
            "       Fix: pip install pynput"
        )
        return

    ctrl_held = False

    # 'K' virtual-key code (Windows). Deliberately NOT matching on
    # key.char: pynput reports the *control character* Windows generates
    # for Ctrl+<letter> combos (Ctrl+K -> '\x0b', not 'k'), so
    # `key.char == 'k'` is false every time and the hotkey can never fire
    # while Ctrl is held -- confirmed this is why Ctrl+K did nothing.
    # vk is unaffected by modifier state, so it's the reliable match here.
    VK_K = 0x4B

    def on_press(key):
        nonlocal ctrl_held
        if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
            ctrl_held = True
            return
        if ctrl_held and getattr(key, "vk", None) == VK_K:
            _bridge.hotkey.emit()

    def on_release(key):
        nonlocal ctrl_held
        if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
            ctrl_held = False

    with kb.Listener(on_press=on_press, on_release=on_release, suppress=False) as listener:
        listener.join()


# ── standalone entry point ───────────────────────────────────────────────────

def standalone_main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    mark = DesktopMark()
    mark.show()
    QTimer.singleShot(300, mark.play_startup)

    sys.exit(app.exec())


if __name__ == "__main__":
    standalone_main()
