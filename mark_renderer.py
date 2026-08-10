"""
mark_renderer.py -- native Qt replacement for mark_visual.py's HTML/SVG/JS mark.

WHY THIS EXISTS
---------------
toki_desktop_mark.py used to render the animated mood mark via a
QWebEngineView showing mark_visual.py's SVG+CSS+JS. Confirmed directly
(BETA 0.3.39 investigation): QWebEngineView's Chromium compositor does
not composite correctly inside a top-level widget that has
WA_TranslucentBackground set on Windows -- this is a known Qt/Chromium
limitation, not a TOKI-specific mistake. The practical symptom was total
silent failure to paint: no exception, no console warning, the window
just never showed any content, regardless of position/size. Swapping in
a plain QLabel (toki_desktop_mark.py's old fallback path) confirmed the
diagnosis -- a native Qt widget composites fine with translucency;
Chromium's separate GPU surface does not.

This module reimplements the SAME visual design (7 rotating/breathing
ring layers + pulsing core + mood-based colour/scale/timing, per
mark_visual.py's `moods` config) using plain QPainter drawing, so it
composites correctly wherever the window is positioned, with no
Chromium/WebEngine dependency for this widget at all.

WHAT'S AN EXACT PORT VS. AN APPROXIMATION
------------------------------------------
- Ring geometry (points/rects/circles/radii/stroke widths), mood colour
  palettes, mark_scale/breathe/pulse numbers, and the layer sets per mood
  are transcribed directly from mark_visual.py's `_SVG_BODY` and `moods`
  dict -- these are exact, not approximated.
- Continuous spin is linear (constant angular velocity) rather than
  replaying a CSS easing curve every single rotation cycle. The original
  JS used `animation-timing-function: <mood's ease>` on an infinite
  `spin` animation, which in a browser produces a per-cycle speed ripple
  (e.g. mysterious's cubic-bezier eases into and out of each full spin).
  Reproducing that exactly would need re-deriving the eased angle every
  frame from the bezier curve for an unbounded infinite duration, for
  no clear visual payoff over constant speed. Deliberately simplified to
  linear; call this out if the resting-state motion needs to visually
  match the old JS 1:1.
- The breathing pulse (rings) and core pulse are exact sine-wave
  reconstructions of the CSS keyframes (`0%,100% -> scale(1)`,
  `50% -> scale(min)`), which *is* a faithful reproduction since a
  sine wave is precisely what an ease-in-out 0/50/100 keyframe set
  converges to.
- The mood-switch transition (shrink-out -> swap colours -> grow back
  in, staggered per ring) replays the same three-stage timing as the JS
  version (0ms / 220ms / 400ms) using QVariantAnimation + QTimer.singleShot
  chains instead of CSS transitions -- timings and stagger deltas are
  copied directly from mark_visual.py's setMood().
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QTimer, QVariantAnimation, QEasingCurve, QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget


# ── ring geometry, transcribed from mark_visual.py's _SVG_BODY (viewBox 220x220, centre 110,110) ──

RING_ORDER: List[str] = ["ring1", "ring2", "ring3", "ring4", "ring5", "ring6", "ring7"]

_RING_GEOMETRY: Dict[str, dict] = {
    "ring1": {"shape": "polygon", "points": [(110, 30), (178, 150), (42, 150)], "width": 3.0},
    "ring2": {"shape": "polygon", "points": [(110, 190), (42, 70), (178, 70)], "width": 3.0},
    "ring3": {"shape": "polygon", "points": [(110, 50), (165, 110), (110, 170), (55, 110)], "width": 2.0},
    "ring4": {"shape": "rect", "rect": (65, 65, 90, 90), "width": 2.0},
    "ring5": {"shape": "rect", "rect": (80, 80, 60, 60), "width": 1.6, "static_rotate": 45.0},
    "ring6": {"shape": "circle", "center": (110, 110), "radius": 72.0, "width": 1.5, "dash": [6, 10]},
    "ring7": {"shape": "circle", "center": (110, 110), "radius": 42.0, "width": 1.5, "dash": [2, 6]},
}

_BG_CIRCLE = {"center": (110, 110), "radius": 100.0}
_CORE_CIRCLE = {"center": (110, 110), "radius": 12.0}
_VIEWBOX = 220.0  # square, matches mark_visual.py's viewBox="0 0 220 220"


# ── mood configs, transcribed from mark_visual.py's `moods` dict ────────────

@dataclass(frozen=True)
class MoodConfig:
    bg: str
    core: str
    mark_scale: float
    dur: float          # base spin duration, seconds
    ease: QEasingCurve.Type
    breathe: float       # base breathe duration, seconds
    layers: Tuple[str, ...]   # active ring ids, subset of RING_ORDER
    palette: Tuple[str, ...]
    pulse: float          # core pulse duration, seconds
    pulse_scale: float


MOODS: Dict[str, MoodConfig] = {
    "calm": MoodConfig(
        bg="#050B14", core="#B5D4F4", mark_scale=1.1, dur=9.0,
        ease=QEasingCurve.Type.Linear, breathe=6.0,
        layers=("ring2", "ring6"), palette=("#378ADD", "#185FA5"),
        pulse=3.5, pulse_scale=1.35,
    ),
    "energetic": MoodConfig(
        bg="#0A0A0A", core="#FCEBEB", mark_scale=0.85, dur=1.1,
        ease=QEasingCurve.Type.Linear, breathe=0.55,
        layers=("ring1", "ring2", "ring3", "ring4", "ring5", "ring6", "ring7"),
        palette=("#E24B4A", "#A32D2D", "#F09595"),
        pulse=0.35, pulse_scale=1.6,
    ),
    "mysterious": MoodConfig(
        bg="#08070C", core="#042C53", mark_scale=1.3, dur=12.0,
        ease=QEasingCurve.Type.InOutCubic, breathe=5.0,
        layers=("ring3", "ring5"), palette=("#0C447C", "#042C53"),
        pulse=5.0, pulse_scale=1.25,
    ),
    "playful": MoodConfig(
        bg="#10131A", core="#E6F1FB", mark_scale=0.95, dur=1.8,
        ease=QEasingCurve.Type.OutBack, breathe=0.9,
        layers=("ring1", "ring4", "ring6", "ring7"),
        palette=("#378ADD", "#85B7EB", "#B5D4F4"),
        pulse=0.9, pulse_scale=1.5,
    ),
    "lifeless": MoodConfig(
        bg="#0B0B0B", core="#5F5E5A", mark_scale=0.8, dur=16.0,
        ease=QEasingCurve.Type.InOutQuad, breathe=9.0,
        layers=(), palette=("#5F5E5A",),
        pulse=4.5, pulse_scale=1.08,
    ),
}

STARTUP_ORDER: Tuple[str, ...] = ("calm", "energetic", "mysterious", "playful", "lifeless", "calm")
STARTUP_STEP_MS = 850  # matches mark_visual.py's playStartup()


@dataclass
class _RingRuntime:
    """Per-ring live animation state, recomputed whenever a mood becomes active."""
    active: bool = False
    color: QColor = field(default_factory=lambda: QColor("#378ADD"))
    spin_dur: float = 9.0
    spin_dir: int = 1          # +1 or -1
    breathe_dur: float = 6.0
    breathe_phase: float = 0.0  # seconds, phase offset (from JS's negative animation-delay)
    # transient transition state (0..1), independent of the resting spin/breathe above
    opacity: float = 0.0
    transition_scale: float = 1.0
    transition_rotation: float = 0.0


class MoodMarkWidget(QWidget):
    """
    Drop-in replacement for the old `QWebEngineView(self); self._view.setHtml(...)`
    line in toki_desktop_mark.py. Same public surface the rest of that file
    already calls through `self._js(...)`:

        mark = MoodMarkWidget(parent)
        mark.set_mood("calm")     # was: self._js("setMood('calm')")
        mark.play_startup()      # was: self._js("playStartup()")

    Renders via paintEvent -- composites correctly with WA_TranslucentBackground
    on the parent top-level window, unlike QWebEngineView.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        self._mood_name = "calm"
        self._bg_color = QColor(MOODS["calm"].bg)
        self._core_color = QColor(MOODS["calm"].core)
        self._core_pulse_scale = 1.0
        self._mark_group_scale = MOODS["calm"].mark_scale
        self._mark_group_rotation = 0.0

        self._rings: Dict[str, _RingRuntime] = {rid: _RingRuntime() for rid in RING_ORDER}

        self._settled_since = time.monotonic()  # when the current mood's resting animation began

        # single continuous-animation heartbeat -- drives spin + breathe + core pulse
        # for whatever's currently "settled" (i.e. not mid-transition).
        self._is_transitioning = False
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(16)  # ~60fps
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

        # transition-stage timers (mirrors mark_visual.py setMood()'s 0/220/400ms stages)
        self._stage_timers: List[QTimer] = []
        self._transition_anims: List[QVariantAnimation] = []

        self._apply_mood_instant("calm")

    # ── public API (replaces the old self._js(...) calls) ──────────────────

    def set_mood(self, name: str) -> None:
        """Equivalent to the old self._js(f"setMood('{name}')")."""
        if name not in MOODS:
            return
        if name == self._mood_name and not self._is_transitioning:
            return
        self._start_transition(name)

    def play_startup(self) -> None:
        """Equivalent to the old self._js("playStartup()")."""
        for i, name in enumerate(STARTUP_ORDER):
            QTimer.singleShot(i * STARTUP_STEP_MS, lambda n=name: self.set_mood(n))

    # ── mood application ─────────────────────────────────────────────────────

    def _apply_mood_instant(self, name: str) -> None:
        """Set a mood with no transition animation (startup / initial state)."""
        cfg = MOODS[name]
        self._mood_name = name
        self._bg_color = QColor(cfg.bg)
        self._core_color = QColor(cfg.core)
        self._mark_group_scale = cfg.mark_scale
        self._mark_group_rotation = 0.0
        self._configure_ring_runtimes(cfg)
        self._settled_since = time.monotonic()
        self.update()

    def _configure_ring_runtimes(self, cfg: MoodConfig) -> None:
        """
        Recompute per-ring color/spin/breathe params for the rings active in
        `cfg`, in RING_ORDER (not layer-declaration order) -- matches the JS
        `rings.forEach` iterating in DOM order (ring1..ring7) and only
        incrementing its palette/duration index `i` for rings that are
        actually active in this mood.
        """
        i = 0
        for rid in RING_ORDER:
            rt = self._rings[rid]
            if rid in cfg.layers:
                rt.active = True
                rt.opacity = 0.85
                rt.transition_scale = 1.0
                rt.transition_rotation = 0.0
                rt.color = QColor(cfg.palette[i % len(cfg.palette)])
                dur_mult = 1 + i * 0.17
                rt.spin_dur = max(cfg.dur * dur_mult, 0.05)
                rt.spin_dir = -1 if i % 2 == 0 else 1  # JS: reverse when i even
                breathe_dur = cfg.breathe * (1 + i * 0.12)
                rt.breathe_dur = max(breathe_dur, 0.05)
                rt.breathe_phase = i * breathe_dur * 0.15
                i += 1
            else:
                rt.active = False
                rt.opacity = 0.0
                rt.transition_scale = 0.1

    # ── mood-switch transition (staged, mirrors the JS timings) ─────────────

    def _start_transition(self, name: str) -> None:
        cfg = MOODS[name]
        self._is_transitioning = True

        for t in self._stage_timers:
            t.stop()
        for a in self._transition_anims:
            a.stop()
        self._stage_timers.clear()
        self._transition_anims.clear()

        # Stage A (t=0ms): shrink core, shrink+rotate mark group, fade out
        # currently-active rings -- mirrors setMood()'s immediate block.
        self._animate(self, "_core_pulse_scale", self._core_pulse_scale, 1.7, 220, QEasingCurve.Type.InOutCubic)
        self._animate(self, "_mark_group_scale", self._mark_group_scale, 0.3, 320, QEasingCurve.Type.InCubic)
        self._animate(self, "_mark_group_rotation", self._mark_group_rotation, 130.0, 320, QEasingCurve.Type.InCubic)
        for idx, rid in enumerate(RING_ORDER):
            rt = self._rings[rid]
            if rt.active:
                delay = idx * 25
                self._delayed_animate(rt, "opacity", rt.opacity, 0.0, 280, delay)
                self._delayed_animate(rt, "transition_scale", rt.transition_scale, 0.05, 280, delay)

        # Stage B (t=220ms): core collapses further, background swaps colour.
        t_b = QTimer(self)
        t_b.setSingleShot(True)
        t_b.timeout.connect(lambda: self._transition_stage_b(cfg))
        t_b.start(220)
        self._stage_timers.append(t_b)

        # Stage C (t=400ms): swap core colour, grow mark group back, bring in
        # the new mood's rings staggered.
        t_c = QTimer(self)
        t_c.setSingleShot(True)
        t_c.timeout.connect(lambda: self._transition_stage_c(name, cfg))
        t_c.start(400)
        self._stage_timers.append(t_c)

    def _transition_stage_b(self, cfg: MoodConfig) -> None:
        self._animate(self, "_core_pulse_scale", self._core_pulse_scale, 0.12, 160, QEasingCurve.Type.InQuad)
        self._bg_color = QColor(cfg.bg)
        self.update()

    def _transition_stage_c(self, name: str, cfg: MoodConfig) -> None:
        self._mood_name = name
        self._core_color = QColor(cfg.core)
        self._configure_ring_runtimes(cfg)

        self._animate(self, "_core_pulse_scale", self._core_pulse_scale, 1.0, 550, QEasingCurve.Type.OutBack)
        self._mark_group_rotation = 0.0
        self._animate(self, "_mark_group_scale", self._mark_group_scale, cfg.mark_scale, 600, QEasingCurve.Type.OutBack)

        i = 0
        for rid in RING_ORDER:
            rt = self._rings[rid]
            if rt.active:
                delay = i * 45
                self._delayed_animate(rt, "opacity", 0.0, 0.85, 500, delay, QEasingCurve.Type.OutBack)
                self._delayed_animate(rt, "transition_scale", 0.1, 1.0, 500, delay, QEasingCurve.Type.OutBack)
                i += 1

        self._settled_since = time.monotonic()
        self._is_transitioning = False

    # ── small animation helpers (QVariantAnimation, writes back onto a target object) ──

    def _animate(self, target, attr: str, start, end, duration_ms: int,
                 easing: QEasingCurve.Type = QEasingCurve.Type.OutCubic) -> None:
        anim = QVariantAnimation(self)
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setDuration(duration_ms)
        anim.setEasingCurve(easing)

        def _on_value(v, target=target, attr=attr):
            setattr(target, attr, v)
            self.update()

        anim.valueChanged.connect(_on_value)
        anim.start()
        self._transition_anims.append(anim)

    def _delayed_animate(self, target, attr: str, start, end, duration_ms: int,
                          delay_ms: int, easing: QEasingCurve.Type = QEasingCurve.Type.OutCubic) -> None:
        if delay_ms <= 0:
            self._animate(target, attr, start, end, duration_ms, easing)
            return
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(lambda: self._animate(target, attr, start, end, duration_ms, easing))
        t.start(delay_ms)
        self._stage_timers.append(t)

    # ── continuous resting animation (spin + breathe + core pulse) ──────────

    def _on_tick(self) -> None:
        if self._is_transitioning:
            self.update()
            return
        elapsed = time.monotonic() - self._settled_since

        cfg = MOODS[self._mood_name]
        if cfg.pulse > 0:
            phase = 2 * math.pi * elapsed / cfg.pulse
            self._core_pulse_scale = 1.0 + (cfg.pulse_scale - 1.0) / 2.0 * (1.0 - math.cos(phase))

        for rt in self._rings.values():
            if not rt.active:
                continue
            spin_phase = (elapsed / rt.spin_dur) if rt.spin_dur > 0 else 0.0
            rt.transition_rotation = (spin_phase * 360.0 * rt.spin_dir) % 360.0
            b_phase = 2 * math.pi * (elapsed + rt.breathe_phase) / rt.breathe_dur if rt.breathe_dur > 0 else 0.0
            rt.transition_scale = 0.91 + 0.09 * math.cos(b_phase)

        self.update()

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            painter.end()
            return

        scale = min(w, h) / _VIEWBOX
        painter.translate(w / 2.0, h / 2.0)
        painter.scale(scale, scale)
        painter.translate(-_VIEWBOX / 2.0, -_VIEWBOX / 2.0)

        # background circle
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._bg_color)
        cx, cy = _BG_CIRCLE["center"]
        r = _BG_CIRCLE["radius"]
        painter.drawEllipse(QPointF(cx, cy), r, r)

        # mark group (rings), under a group scale+rotation transform
        painter.save()
        painter.translate(110, 110)
        painter.rotate(self._mark_group_rotation)
        painter.scale(self._mark_group_scale, self._mark_group_scale)
        painter.translate(-110, -110)

        for rid in RING_ORDER:
            rt = self._rings[rid]
            if rt.opacity <= 0.0:
                continue
            self._draw_ring(painter, rid, rt)

        painter.restore()

        # core, pulsing, drawn in the same group space
        painter.save()
        painter.translate(110, 110)
        painter.scale(self._core_pulse_scale, self._core_pulse_scale)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._core_color)
        cr = _CORE_CIRCLE["radius"]
        painter.drawEllipse(QPointF(0, 0), cr, cr)
        painter.restore()

        painter.end()

    def _draw_ring(self, painter: QPainter, rid: str, rt: _RingRuntime) -> None:
        geo = _RING_GEOMETRY[rid]
        color = QColor(rt.color)
        color.setAlphaF(max(0.0, min(1.0, rt.opacity)))
        pen = QPen(color)
        pen.setWidthF(geo["width"])
        if "dash" in geo:
            pen.setDashPattern([float(x) for x in geo["dash"]])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        painter.save()
        cx, cy = geo.get("center") or self._shape_center(geo)
        painter.translate(cx, cy)
        painter.rotate(rt.transition_rotation + geo.get("static_rotate", 0.0))
        painter.scale(rt.transition_scale, rt.transition_scale)
        painter.translate(-cx, -cy)

        if geo["shape"] == "polygon":
            poly = QPolygonF([QPointF(x, y) for x, y in geo["points"]])
            painter.drawPolygon(poly)
        elif geo["shape"] == "rect":
            x, y, rw, rh = geo["rect"]
            painter.drawRect(int(x), int(y), int(rw), int(rh))
        elif geo["shape"] == "circle":
            ccx, ccy = geo["center"]
            painter.drawEllipse(QPointF(ccx, ccy), geo["radius"], geo["radius"])

        painter.restore()

    @staticmethod
    def _shape_center(geo: dict) -> Tuple[float, float]:
        if geo["shape"] == "rect":
            x, y, w, h = geo["rect"]
            return (x + w / 2.0, y + h / 2.0)
        if geo["shape"] == "polygon":
            xs = [p[0] for p in geo["points"]]
            ys = [p[1] for p in geo["points"]]
            return (sum(xs) / len(xs), sum(ys) / len(ys))
        return (110.0, 110.0)
