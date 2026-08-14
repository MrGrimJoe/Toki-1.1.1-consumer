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
from PyQt6.QtGui import QColor, QAction, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSystemTrayIcon, QMenu,
    QGraphicsDropShadowEffect, QSizePolicy, QLineEdit, QScrollArea,
)

from mark_renderer import MoodMarkWidget
from ui_theme import (
    ACCENT, RADIUS, RADIUS_SM, TEXT_PRIMARY, TEXT_MUTED, TEXT_FAINT,
    OUTER_MARGINS, font_css, make_shadow, card_qss, button_qss, line_edit_qss,
)

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
    dictation_active = pyqtSignal(bool)  # continuous dictation started/stopped
                                 # (see AppController.start_dictation() /
                                 # stop_dictation()) -- emitted from the same
                                 # background dispatch thread as reply/done
                                 # above, same cross-thread-safety reasoning.
    dictation_stop_clicked = pyqtSignal()  # the stop panel's button, Qt-thread
    result_ready = pyqtSignal(str, str)  # (strategy, text) -- see
                                 # display_strategy.classify_display().
                                 # Added alongside that module (BETA
                                 # 0.3.51): `reply` above is left exactly
                                 # as it was (still used by drag/drop
                                 # feedback and the permission-gate
                                 # confirm's old call site) -- this is a
                                 # separate, additive signal so nothing
                                 # that already depends on `reply`'s
                                 # single-str-argument shape breaks.
    global_click = pyqtSignal(int, int)  # (x, y) screen coords of a left
                                 # click ANYWHERE (BETA 0.3.52) -- emitted
                                 # from _run_mouse_listener()'s pynput
                                 # thread, same cross-thread-safety
                                 # reasoning as reply/done above. Used to
                                 # dismiss a sticky INFO/ERROR card or the
                                 # hover panel when the person clicks
                                 # somewhere that isn't either -- including
                                 # somewhere outside this app entirely,
                                 # which a plain Qt event filter could
                                 # never see (Qt only gets events for its
                                 # own windows).


_bridge = _Bridge()   # module-level singleton so voice_pipeline.py can import it


# ── shared popup entrance/exit animation (BETA 0.3.52) ───────────────────────
#
# Every frameless popup in this file (_ReplyBubble, _DoneNote,
# _CommandsPanel) used to just self.show()/self.hide() instantly -- a hard
# cut with no transition. These two helpers give all of them the same
# smooth fade + short slide, so "the widget turns into a display card"
# actually reads as one continuous motion instead of a jump-cut.
#
# Animation objects are stored as attributes ON THE WIDGET itself (not
# local variables in these functions) -- PyQt/Qt garbage-collects a
# QPropertyAnimation the moment its last Python reference goes out of
# scope, which for a local variable is the instant the function returns,
# well before a 150-200ms animation has actually finished running.
# Confirmed this is a real, not theoretical, footgun: an early version of
# this fix used local variables and the animations visibly, silently cut
# off partway through every time. Storing them on the widget keeps a
# live reference for the animation's full duration.
def _popup_fade_in(widget: QWidget, target_pos: QPoint, duration: int = 170, slide_px: int = 10) -> None:
    for attr in ("_anim_in_opacity", "_anim_in_pos", "_anim_out_opacity"):
        anim = getattr(widget, attr, None)
        if anim is not None:
            anim.stop()

    start_pos = QPoint(target_pos.x(), target_pos.y() + slide_px)
    widget.setWindowOpacity(0.0)
    widget.move(start_pos)
    widget.show()

    widget._anim_in_opacity = QPropertyAnimation(widget, b"windowOpacity")
    widget._anim_in_opacity.setDuration(duration)
    widget._anim_in_opacity.setStartValue(0.0)
    widget._anim_in_opacity.setEndValue(1.0)
    widget._anim_in_opacity.setEasingCurve(QEasingCurve.Type.OutCubic)

    widget._anim_in_pos = QPropertyAnimation(widget, b"pos")
    widget._anim_in_pos.setDuration(duration)
    widget._anim_in_pos.setStartValue(start_pos)
    widget._anim_in_pos.setEndValue(target_pos)
    widget._anim_in_pos.setEasingCurve(QEasingCurve.Type.OutCubic)

    widget._anim_in_opacity.start()
    widget._anim_in_pos.start()


def _popup_fade_out(widget: QWidget, duration: int = 130) -> None:
    """Quick fade-out, then hide. No-op if already hidden (avoids
    restarting a fade on something that isn't visible, e.g. a dismiss
    triggered twice in quick succession)."""
    if not widget.isVisible():
        return
    for attr in ("_anim_in_opacity", "_anim_in_pos", "_anim_out_opacity"):
        anim = getattr(widget, attr, None)
        if anim is not None:
            anim.stop()

    widget._anim_out_opacity = QPropertyAnimation(widget, b"windowOpacity")
    widget._anim_out_opacity.setDuration(duration)
    widget._anim_out_opacity.setStartValue(widget.windowOpacity())
    widget._anim_out_opacity.setEndValue(0.0)
    widget._anim_out_opacity.setEasingCurve(QEasingCurve.Type.InCubic)
    widget._anim_out_opacity.finished.connect(widget.hide)
    widget._anim_out_opacity.start()


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

        # ── panel frame (see ui_theme.py: OUTER_MARGINS is coupled to the
        # shadow below -- shrinking it makes the shadow paint outside the
        # window's buffer, which silently kills rendering on Windows).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*OUTER_MARGINS)

        self._card = QFrame(self)
        self._card.setObjectName("card")
        self._card.setStyleSheet(card_qss("card", "blue"))
        self._card.setGraphicsEffect(make_shadow())
        outer.addWidget(self._card)

        self._layout = QVBoxLayout(self._card)
        self._layout.setContentsMargins(16, 14, 16, 14)
        self._layout.setSpacing(10)

        self._header = QLabel("SCHEDULED COMMANDS")
        self._header.setStyleSheet(
            f"color: {TEXT_MUTED}; {font_css(10, 600, tracking='letter-spacing: 0.09em;')}"
        )
        self._layout.addWidget(self._header)

        self._body = QVBoxLayout()
        self._body.setSpacing(6)
        self._layout.addLayout(self._body)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh)

    # ── public API ──────────────────────────────────────────────────────────

    def set_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    def popup_below(self, anchor_rect: QRect) -> None:
        """Position below anchor_rect and show, with a smooth fade+slide
        entrance (see _popup_fade_in) rather than an instant show()."""
        self._refresh()
        self.adjustSize()
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.bottom() + 6
        _popup_fade_in(self, QPoint(x, max(0, y)))
        self._refresh_timer.start()

    def hide_panel(self) -> None:
        _popup_fade_out(self)
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
            lbl.setStyleSheet(f"color: {TEXT_FAINT}; {font_css(12)}")
            self._body.addWidget(lbl)
            self.adjustSize()
            return

        for it in items:
            row = QHBoxLayout()
            row.setSpacing(12)

            secs = it.seconds_remaining()
            if secs >= 3600:
                countdown = f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"
            elif secs >= 60:
                countdown = f"{int(secs // 60)}m {int(secs % 60)}s"
            else:
                countdown = f"{int(secs)}s"

            desc = QLabel(f"{it.id}  ·  {it.description}")
            desc.setStyleSheet(f"color: {TEXT_PRIMARY}; {font_css(13)}")
            desc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            row.addWidget(desc)

            clock = QLabel(countdown)
            clock.setStyleSheet(f"color: {ACCENT['blue']['solid']}; {font_css(12, 600)}")
            row.addWidget(clock)

            btn = QPushButton("✕")
            btn.setFixedSize(24, 24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(button_qss("red", size=24))
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


# ── dictation stop panel ("start listening" widget) ─────────────────────────
#
# BETA 0.3.44. Deliberately its own tiny panel, not folded into
# _CommandsPanel above -- a scheduled-command list and a single always-on
# stop button don't share layout or lifecycle (this has no per-item rows,
# no auto-refresh timer, no scheduler dependency), so forcing them into one
# class would only have made both harder to follow for no real code
# sharing gained. Same frameless/translucent/shadow styling as
# _CommandsPanel purely for visual consistency with the rest of the
# overlay, copied rather than shared for the same reason.
#
# Design-doc interpretation call (see STATUS.md for the full reasoning):
# the original wording was "the user types control clicks the stop button"
# -- read here as a plain click on this dedicated button, not a
# Ctrl+modifier click. A dedicated button that only exists while dictation
# is active has nothing else on it competing for an accidental plain
# click, so a modifier requirement wasn't worth the extra friction of
# "remember to hold Ctrl" for something meant to be a fast, obvious exit.

class _DictationStopPanel(QFrame):
    """Small floating panel with one button. Shown for the entire
    duration of a dictation session (start_dictation() → stop_dictation()),
    hidden the rest of the time -- see DesktopMark._on_dictation_active()."""

    stop_clicked = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # See _CommandsPanel's constructor above for why these margins can't
        # be zero: they have to contain the drop shadow's blur bleed below.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*OUTER_MARGINS)

        card = QFrame(self)
        card.setObjectName("dictationCard")
        card.setStyleSheet(card_qss("dictationCard", "orange"))
        card.setGraphicsEffect(make_shadow())
        outer.addWidget(card)

        row = QHBoxLayout(card)
        row.setContentsMargins(16, 12, 12, 12)
        row.setSpacing(12)

        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(
            f"background: {ACCENT['orange']['solid']}; border-radius: 4px;"
        )
        row.addWidget(dot)

        label = QLabel("Listening…")
        label.setStyleSheet(f"color: {TEXT_PRIMARY}; {font_css(12, 600)}")
        row.addWidget(label)

        stop_btn = QPushButton("Stop")
        stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        stop_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT['orange']['solid']};
                color: white;
                border: none;
                border-radius: {RADIUS_SM}px;
                padding: 6px 16px;
                {font_css(12, 600)}
            }}
            QPushButton:hover {{ background: #EF9468; }}
            QPushButton:pressed {{ background: #C96A3C; }}
        """)
        stop_btn.clicked.connect(self.stop_clicked.emit)
        row.addWidget(stop_btn)

    def popup_below(self, anchor_rect: QRect) -> None:
        self.adjustSize()
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.bottom() + 6
        self.move(x, max(0, y))
        self.show()

    def hide_panel(self) -> None:
        self.hide()


# ── floating reply bubble (appears next to the mark, fades after 3s) ────────

class _ReplyBubble(QFrame):
    """
    Frameless popup that shows TOKI's last text reply next to the mark.
    Same visual language/pattern as _CommandsPanel (rounded card, drop
    shadow) so it reads as part of the same UI rather than a different
    widget style.

    Two display modes, added alongside display_strategy.py (BETA
    0.3.51): the default call from show_reply() -- drag/drop feedback,
    permission-gate confirmations, and anything else that doesn't go
    through the new classify_display() path -- keeps the original
    behavior exactly: blue accent, auto-hides after REPLY_SHOW_MS.
    DesktopMark.show_result() instead calls show_below(..., persistent=True,
    accent=...) for INFO/ERROR turns: no auto-hide timer (a "did you mean
    X?" question or a real file listing must not vanish before it's
    read), a taller scrollable body for longer content (a directory
    listing can run to dozens of lines), and an accent that matches the
    strategy ("blue" for info, "red" for error). Click-anywhere-to-dismiss
    is the only way a persistent card goes away on its own.
    """

    # Non-persistent (show_reply's original behavior) content stays
    # short -- cap the scroll area low so it never grows the popup's
    # footprint. Persistent (INFO/ERROR) content can run much longer
    # (a real directory listing, a generated file's contents), so it
    # gets a taller viewport with internal scrolling instead of an
    # ever-growing window.
    _SCROLL_MAX_H_TRANSIENT = 90
    _SCROLL_MAX_H_PERSISTENT = 340

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # See _CommandsPanel's constructor above for why these margins can't
        # be zero: they have to contain the drop shadow's blur bleed below.
        # This is the reply bubble specifically -- the fix here is what
        # makes TOKI's actual answers (including "did you mean X?"
        # clarifying questions and yes/no confirmation prompts) visible at
        # all; they were rendering to an off-screen/invalid buffer before.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*OUTER_MARGINS)

        self._card = QFrame(self)
        self._card.setObjectName("replycard")
        self._card.setStyleSheet(card_qss("replycard", "blue"))
        self._card.setGraphicsEffect(make_shadow())
        outer.addWidget(self._card)

        inner = QVBoxLayout(self._card)
        inner.setContentsMargins(16, 12, 16, 14)
        inner.setSpacing(4)

        self._eyebrow = QLabel("TOKI")
        self._eyebrow.setStyleSheet(
            f"color: {ACCENT['blue']['solid']}; {font_css(9, 700, tracking='letter-spacing: 0.12em;')}"
        )
        inner.addWidget(self._eyebrow)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(360)
        self._label.setStyleSheet(f"color: {TEXT_PRIMARY}; {font_css(13)}")

        # Scroll area so long INFO/ERROR content (a directory listing, a
        # generated file's contents) doesn't grow the popup off-screen --
        # it scrolls internally instead. Transparent background + no
        # frame so it reads as part of the same card, not a nested
        # widget with its own visible edges.
        self._scroll = QScrollArea(self._card)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("background: transparent; border: none;")
        self._scroll.viewport().setStyleSheet("background: transparent;")
        _scroll_inner = QWidget()
        _scroll_inner.setStyleSheet("background: transparent;")
        _scroll_layout = QVBoxLayout(_scroll_inner)
        _scroll_layout.setContentsMargins(0, 0, 0, 0)
        _scroll_layout.addWidget(self._label)
        self._scroll.setWidget(_scroll_inner)
        self._scroll.setMaximumHeight(self._SCROLL_MAX_H_TRANSIENT)
        inner.addWidget(self._scroll)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(lambda: _popup_fade_out(self))

        self._persistent = False

    def show_below(
        self,
        anchor_rect: QRect,
        text: str,
        accent: str = "blue",
        persistent: bool = False,
    ) -> None:
        if not text:
            return
        self._card.setStyleSheet(card_qss("replycard", accent))
        self._eyebrow.setStyleSheet(
            f"color: {ACCENT[accent]['solid']}; {font_css(9, 700, tracking='letter-spacing: 0.12em;')}"
        )
        self._eyebrow.setText("TOKI" if accent != "red" else "TOKI · ERROR")
        self._label.setMaximumWidth(420 if persistent else 360)
        self._scroll.setMaximumHeight(
            self._SCROLL_MAX_H_PERSISTENT if persistent else self._SCROLL_MAX_H_TRANSIENT
        )
        self._label.setText(text)
        self.adjustSize()
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.bottom() + 6
        # keep on-screen horizontally
        screen = QApplication.primaryScreen().geometry()
        x = max(4, min(x, screen.width() - self.width() - 4))
        _popup_fade_in(self, QPoint(x, max(0, y)))
        self._persistent = persistent
        if persistent:
            self._hide_timer.stop()
        else:
            self._hide_timer.start(REPLY_SHOW_MS)

    def cancel_autohide(self) -> None:
        """Used by the hover panel: keep the last reply visible while
        the user is actively looking at the panel, instead of racing
        the 3s fade against their mouse movement."""
        self._hide_timer.stop()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Persistent INFO/ERROR cards have no auto-hide timer -- clicking
        # anywhere on the card is the dismiss gesture. Transient cards
        # (the original show_reply() behavior) just let the click pass
        # through to their normal fade-out; no need to special-case them.
        if self._persistent:
            _popup_fade_out(self)
        super().mousePressEvent(event)


class _DoneNote(QFrame):
    """
    Tiny, quick-fading confirmation pill for DONE-strategy turns (see
    display_strategy.py) -- an action happened, there's nothing to read,
    just a quick "✓ Done" so it's visible without lingering the way an
    INFO/ERROR _ReplyBubble deliberately does. Kept as its own small
    class rather than a third mode on _ReplyBubble because the DONE
    treatment is meant to look and feel different at a glance -- smaller,
    quieter, gone almost as soon as it's noticed.
    """

    DONE_SHOW_MS = 1400

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
        outer.setContentsMargins(*OUTER_MARGINS)

        pill = QFrame(self)
        pill.setObjectName("donepill")
        pill.setStyleSheet(card_qss("donepill", "green"))
        pill.setGraphicsEffect(make_shadow())
        outer.addWidget(pill)

        inner = QHBoxLayout(pill)
        inner.setContentsMargins(14, 8, 14, 8)
        inner.setSpacing(6)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(280)
        self._label.setStyleSheet(f"color: {ACCENT['green']['solid']}; {font_css(12, 700)}")
        inner.addWidget(self._label)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(lambda: _popup_fade_out(self, duration=110))

    def show_below(self, anchor_rect: QRect, detail: str = "") -> None:
        # Most DONE response text from orchestrator.py already starts
        # with a literal "Done." (e.g. "Done. Scheduled as sch_1: ...")
        # -- strip that prefix so the pill doesn't read "✓ Done — Done.
        # Scheduled...".
        detail = (detail or "").strip()
        if detail.startswith("Done."):
            detail = detail[len("Done."):].strip(" .")
        text = f"✓ Done — {detail}" if detail else "✓ Done"
        self._label.setText(text)
        self.adjustSize()
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.bottom() + 6
        screen = QApplication.primaryScreen().geometry()
        x = max(4, min(x, screen.width() - self.width() - 4))
        _popup_fade_in(self, QPoint(x, max(0, y)), duration=130, slide_px=6)
        self._hide_timer.start(self.DONE_SHOW_MS)


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

        # See _CommandsPanel's constructor above for why these margins can't
        # be zero: they have to contain the drop shadow's blur bleed below.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*OUTER_MARGINS)

        card = QFrame(self)
        card.setObjectName("promptcard")
        card.setStyleSheet(card_qss("promptcard", "blue"))
        card.setGraphicsEffect(make_shadow())
        outer.addWidget(card)

        inner = QVBoxLayout(card)
        inner.setContentsMargins(12, 10, 12, 10)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Type a command…")
        self._edit.setMinimumWidth(300)
        self._edit.setStyleSheet(line_edit_qss("blue"))
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

        self._state: str = "idle"    # "idle" | "listening" | "working" | "engaged"
        # "engaged" (BETA 0.3.52): mark stays at active size/position
        # showing a persistent INFO/ERROR card -- see show_result() and
        # _dismiss_sticky_ui() below.
        self._scheduler = None
        self._dispatch_fn: Optional[Callable[[str], None]] = None
        self._assistant = None          # WindowsAIAssistant or None, for permission gate
        self._last_reply: str = ""
        self._home_rect: Optional[QRect] = None   # set once the user drags the mark

        # ── sticky/persistent UI state (BETA 0.3.52) ───────────────────────
        # An INFO/ERROR reply card and/or the hover "scheduled commands"
        # panel, once genuinely opened, stay on screen until the person
        # explicitly dismisses them -- clicking anywhere else (tracked
        # globally, not just within this app -- see _start_global_click_
        # listener()) or pressing Ctrl+K again (_on_hotkey). Neither used
        # to behave this way: the reply card used to auto-fade after 3s
        # regardless of content, and the hover panel used to auto-collapse
        # the instant the mouse left its geometry -- both of which could
        # yank away content, or a whole scheduled-commands list mid-
        # interaction, before the person was done with it.
        self._sticky_reply_active = False
        self._sticky_panel_active = False

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
        # BETA 0.3.52: no more mouse-leave auto-hide timer here -- the
        # panel is sticky once opened (see _expand_for_hover /
        # _dismiss_sticky_ui below).

        # ── dictation stop panel (see AppController.start_dictation())
        self._dictation_panel = _DictationStopPanel(None)   # top-level window
        self._dictation_panel.stop_clicked.connect(self._on_dictation_stop_clicked)

        # dwell timer: enterEvent starts this instead of expanding
        # immediately; only actually expands if the mouse is still there
        # when it fires (see HOVER_INTENT_MS above).
        self._hover_intent_timer = QTimer(self)
        self._hover_intent_timer.setSingleShot(True)
        self._hover_intent_timer.setInterval(HOVER_INTENT_MS)
        self._hover_intent_timer.timeout.connect(self._expand_for_hover)

        # ── reply bubble (fades after REPLY_SHOW_MS) + typed-prompt box
        self._reply_bubble = _ReplyBubble()
        self._done_note = _DoneNote()   # BETA 0.3.51: see show_result() below
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
        # NOT connected directly to self.idle anymore (BETA 0.3.52): a
        # turn finishing doesn't always mean "go idle" now -- an INFO/
        # ERROR result puts the mark into "engaged" state instead (see
        # show_result()), and _on_turn_done() is what actually decides
        # whether idle() is appropriate for the current state.
        _bridge.done.connect(self._on_turn_done)
        _bridge.reply.connect(self.show_reply)
        _bridge.result_ready.connect(self.show_result)
        _bridge.dictation_active.connect(self._on_dictation_active)
        _bridge.global_click.connect(self._on_global_click)

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

        # start the hotkey listener + the global click-outside-to-dismiss
        # listener (BETA 0.3.52)
        _start_hotkey_listener()
        _start_global_click_listener()

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

    def show_dictation_panel(self) -> None:
        """Shown for the duration of a dictation session -- see
        AppController.start_dictation() / _bridge.dictation_active."""
        self._dictation_panel.popup_below(self.geometry())

    def hide_dictation_panel(self) -> None:
        self._dictation_panel.hide_panel()

    @pyqtSlot(bool)
    def _on_dictation_active(self, active: bool) -> None:
        if active:
            self.show_dictation_panel()
        else:
            self.hide_dictation_panel()

    def _on_dictation_stop_clicked(self) -> None:
        # Just relays the click onward -- main_widget.py is the one that
        # actually holds the AppController/orchestrator reference and
        # calls stop_dictation(), same separation as set_dispatch()'s
        # callback above: this widget module doesn't import orchestrator.py
        # or app_control.py directly anywhere else either.
        _bridge.dictation_stop_clicked.emit()

    # ── internal state transitions ───────────────────────────────────────────

    @pyqtSlot()
    def _on_hotkey(self) -> None:
        """Ctrl+K pressed. First dismisses any sticky/persistent display
        that's currently up (BETA 0.3.52) -- pressing Ctrl+K again is
        both "I'm done reading that" and "start a new command", so it
        does both rather than overloading the hotkey into a pure
        close-only action (which would silently eat the very next Ctrl+K
        press someone expects to start listening with). Then: if idle →
        go active. If already listening → extend."""
        if self._sticky_reply_active or self._sticky_panel_active:
            self._dismiss_sticky_ui()
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
    #
    # BETA 0.3.52: once genuinely opened (HOVER_INTENT_MS dwell confirmed,
    # not just a brush-past), the panel no longer auto-collapses the
    # instant the mouse leaves its geometry -- it's "sticky" now, same as
    # a persistent INFO/ERROR reply card, and only closes via the shared
    # _dismiss_sticky_ui() path (click anywhere else, or Ctrl+K). This
    # matters most while actually interacting with it: cancelling a
    # scheduled item involves moving the mouse toward a small ✕ button,
    # and the old mouse-leaves-geometry-for-200ms auto-hide could easily
    # fire mid-click on a fast or imprecise mouse move, closing the panel
    # out from under the very click meant to use it.

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
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
        self._sticky_panel_active = True
        # keep the last reply on screen while the panel is up, rather
        # than letting its own timer race the user's mouse -- both are
        # sticky now and dismiss together via _dismiss_sticky_ui().
        if self._last_reply:
            self._reply_bubble.cancel_autohide()
            self._reply_bubble.show_below(visible_rect, self._last_reply)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        # Only cancels a not-yet-fired expand (this was just a brush-past
        # that never actually opened the panel) -- once the panel IS open
        # (self._sticky_panel_active), leaving no longer closes anything;
        # see the class-level comment above this section for why.
        self._hover_intent_timer.stop()

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
        if self._sticky_reply_active or self._sticky_panel_active:
            self._dismiss_sticky_ui()
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

    def show_result(self, strategy: str, text: str) -> None:
        """BETA 0.3.51/0.3.52: routes a classified turn result (see
        display_strategy.classify_display()) to the display treatment
        that actually fits it, instead of every response -- 'Done.',
        a full directory listing, a clarifying question, a failure --
        going through the same 3-second auto-fading bubble:

          "done"  -> a small, quick-fading confirmation pill. Nothing to
                     read, just a quick acknowledgement -- the mark drops
                     straight back to idle right after (see
                     _on_turn_done()).
          "info"  -> the reply bubble in its persistent "island" mode
                     (blue accent, no auto-hide, scrolls internally for
                     longer content) -- for anything the person actually
                     needs to read: a listing, a reading, a question,
                     generated content. The mark stays at its expanded
                     size/position ("engaged" state) the whole time the
                     card is up, instead of shrinking back to the tiny
                     idle notch while a big card hangs below it -- that
                     mismatch (small notch, large disconnected card)
                     was the first thing that looked broken when this was
                     tried without it. Dismisses (both card and mark)
                     together -- see _dismiss_sticky_ui().
          "error" -> the same persistent island + engaged mark, red
                     accent instead.

        Keeps self._last_reply in sync either way, same field the hover
        panel reads as "previous reply"."""
        self._last_reply = text or ""
        if not text:
            return
        anchor = QRect(self.geometry())
        if strategy == "done":
            self._sticky_reply_active = False
            self._done_note.show_below(anchor, text)
        else:
            # "info" and any unrecognized strategy default to the safe,
            # persistent path -- see display_strategy.py's own comment on
            # why INFO is the safer unknown-case default (losing content
            # to an over-eager auto-fade is worse than an unnecessary
            # persistent card).
            accent = "red" if strategy == "error" else "blue"
            self._reply_bubble.show_below(anchor, text, accent=accent, persistent=True)
            self._sticky_reply_active = True
            self._state = "engaged"
            self._view.set_mood("calm")
            # deliberately no _go_to_idle / _go_to_active call here: the
            # mark is already sitting at the active rect from working()
            # (see _on_working) -- staying put IS the "turns into the
            # display box and holds" behavior. It only moves again when
            # the card is dismissed (_dismiss_sticky_ui) or a new turn
            # starts.

    @pyqtSlot()
    def _on_turn_done(self) -> None:
        """Connected to _bridge.done (BETA 0.3.52, replacing a direct
        connection to self.idle). A turn finishing doesn't always mean
        "go idle" anymore -- show_result() may have just put the mark
        into "engaged" state for a persistent INFO/ERROR card, and that
        state should hold until the person actually dismisses it
        (_dismiss_sticky_ui), not collapse the instant the card appears."""
        if self._state == "engaged":
            return
        self.idle()

    def _dismiss_sticky_ui(self) -> None:
        """Smoothly collapses whatever sticky/persistent UI is open --
        an INFO/ERROR reply card and/or the hover 'scheduled commands'
        panel -- back down. Triggered by a click anywhere else (tracked
        globally, see _on_global_click) or Ctrl+K pressed again
        (_on_hotkey). This is the 'display turns back into the widget'
        half of the transition; show_result()'s persistent path above is
        the other half."""
        if self._sticky_reply_active:
            _popup_fade_out(self._reply_bubble)
            self._sticky_reply_active = False
        if self._sticky_panel_active:
            _popup_fade_out(self._panel)
            self._panel._refresh_timer.stop()
            self._sticky_panel_active = False
        if self._state == "engaged":
            self._state = "idle"
            self._view.set_mood("calm")
            self._go_to_idle(animated=True)
        elif self._state == "idle":
            # the hover-panel-only case: state was never anything but
            # "idle" during a hover peek (_expand_for_hover explicitly
            # only runs when self._state == "idle" and never changes it)
            # -- but the notch itself is currently sitting at its fully-
            # visible "peeking" position rather than off-screen, so it
            # still needs to slide back regardless of which sticky thing
            # (if either) was actually open.
            self._go_to_idle(animated=True)

    @pyqtSlot(int, int)
    def _on_global_click(self, x: int, y: int) -> None:
        """A left click anywhere on screen (BETA 0.3.52) -- see
        _run_mouse_listener(). Dismisses sticky UI if the click landed
        outside every surface that's currently sticky; otherwise (click
        was on the mark itself, the reply card, or the panel) does
        nothing and lets that widget's own click handling take over."""
        if not (self._sticky_reply_active or self._sticky_panel_active):
            return
        pt = QPoint(x, y)
        surfaces = [self.geometry()]
        if self._sticky_reply_active:
            surfaces.append(self._reply_bubble.geometry())
        if self._sticky_panel_active:
            surfaces.append(self._panel.geometry())
        if any(rect.contains(pt) for rect in surfaces):
            return
        self._dismiss_sticky_ui()

    # ── tray icon ────────────────────────────────────────────────────────────

    def _build_tray(self) -> QSystemTrayIcon:
        pix = QPixmap(16, 16)
        pix.fill(QColor(ACCENT["blue"]["solid"]))
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


# ── global click-outside-to-dismiss listener (pynput, background thread) ─────
#
# BETA 0.3.52. Same pattern and thread-safety reasoning as
# _start_hotkey_listener() above -- a second lightweight pynput listener,
# this time for mouse clicks, so a sticky INFO/ERROR reply card or the
# hover panel can be dismissed by clicking anywhere, including outside
# this app's own windows entirely (a plain Qt event filter only ever sees
# events for Qt's own windows -- it has no way to know about a click
# landing on some other application, or bare desktop).
#
# Worth being upfront about the tradeoff this adds: this is a second
# system-wide input hook running continuously (the app already has one,
# for Ctrl+K), which costs a small amount of background CPU and is the
# kind of capability some antivirus/security software flags more
# cautiously than a single keyboard hook (global input hooks in general
# resemble what a keylogger does, even though this one only ever reads
# click coordinates -- never any keystroke content, window contents, or
# which application/control was clicked). If that tradeoff isn't wanted,
# the fix is to not call _start_global_click_listener() from
# DesktopMark.__init__ -- everything else in this file still works
# without it, just without click-anywhere dismissal (Ctrl+K still
# dismisses either way, see _on_hotkey).
_mouse_thread: Optional[threading.Thread] = None


def _start_global_click_listener() -> None:
    global _mouse_thread
    if _mouse_thread is not None and _mouse_thread.is_alive():
        return
    _mouse_thread = threading.Thread(target=_run_mouse_listener, daemon=True, name="toki-mouse-dismiss")
    _mouse_thread.start()


def _run_mouse_listener() -> None:
    """
    Listens for left-clicks globally (pynput). Emits _bridge.global_click
    with just (x, y) screen coordinates on the Qt thread via Qt's
    signal/slot mechanism -- never anything about the click target itself
    (no window title, no application, no clicked control), since all
    DesktopMark._on_global_click() needs is "was this point inside one of
    my own sticky widgets or not".
    """
    try:
        from pynput import mouse as ms
    except ImportError:
        print(
            "[TOKI] pynput not installed -- click-anywhere-to-dismiss disabled "
            "(Ctrl+K still dismisses).\n       Fix: pip install pynput"
        )
        return

    def on_click(x, y, button, pressed):
        if pressed and button == ms.Button.left:
            _bridge.global_click.emit(int(x), int(y))

    with ms.Listener(on_click=on_click) as listener:
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
