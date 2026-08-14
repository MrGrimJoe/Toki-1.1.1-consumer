"""
ui_theme.py -- shared design tokens for every floating popup in
toki_desktop_mark.py (_CommandsPanel, _DictationStopPanel, _ReplyBubble,
_PromptBox).

WHY THIS EXISTS
---------------
Before this module, each of the 4 popup classes hand-rolled its own copy
of the same card/shadow/font styling directly in its __init__ (see BETA
0.3.46 UI report). Four independent copies drifted: slightly different
radii, paddings, and margins, all styled against a font family ('Inter')
that was never actually bundled with the app or verified installed on the
target machine -- so on a stock Windows box every one of those hardcoded
`font: ... 'Inter', sans-serif` rules silently fell back to whatever
generic sans Qt picked, with different character metrics than the
padding/sizes were tuned against. Net effect: cramped, inconsistent,
occasionally clipped popups that didn't read as one coherent product.

This module is the fix: one palette, one font stack (that actually
resolves to a real, good-looking font on Windows/macOS/Linux without
bundling anything), one card/shadow recipe, used by all four. Change
something here and every popup updates together.

USAGE
-----
    from ui_theme import ACCENT, RADIUS, FONT, card_qss, make_shadow, OUTER_MARGINS

    outer = QVBoxLayout(self)
    outer.setContentsMargins(*OUTER_MARGINS)

    card.setStyleSheet(card_qss("blue"))
    card.setGraphicsEffect(make_shadow())
"""

from __future__ import annotations

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsDropShadowEffect

# ── font stack ────────────────────────────────────────────────────────────
#
# Windows 10/11 always ships "Segoe UI" (and 11 ships the "Segoe UI
# Variable" family too); macOS always has "Helvetica Neue"/San Francisco
# system fonts; Linux desktops vary but "Noto Sans"/"DejaVu Sans" are
# near-universal. 'Inter' is kept as a trailing opportunistic match (it'll
# be used if the person happens to have it installed) but nothing here
# DEPENDS on it being present, unlike before.
FONT_DISPLAY = "'Segoe UI Variable Display', 'Segoe UI Semibold', 'Segoe UI', 'Inter', sans-serif"
FONT_BODY = "'Segoe UI Variable Text', 'Segoe UI', 'Inter', 'Helvetica Neue', 'Noto Sans', sans-serif"


def font_css(size: int, weight: int = 400, display: bool = False, tracking: str = "") -> str:
    """One-liner for a QLabel/QPushButton/QLineEdit font rule. `tracking`
    is an optional extra CSS fragment (e.g. 'letter-spacing: 0.06em;')."""
    family = FONT_DISPLAY if display else FONT_BODY
    return f"font: {weight} {size}px {family};{(' ' + tracking) if tracking else ''}"


# ── palette ───────────────────────────────────────────────────────────────
#
# Matches mark_renderer.py's MOODS palette on purpose (calm=#378ADD,
# energetic=#E24B4A) so the popups read as part of the same object as the
# mark itself, not a bolted-on separate UI. "blue" is the default/neutral
# accent (replies, commands, typed prompts); "orange" is reserved for the
# one thing that's actively recording (dictation); "red" is reserved for
# destructive/danger confirmations.
ACCENT = {
    "blue":   {"solid": "#4A9AE8", "border": "rgba(74, 154, 232, 110)", "glow": "rgba(74, 154, 232, 40)"},
    "orange": {"solid": "#E38652", "border": "rgba(227, 134, 82, 110)", "glow": "rgba(227, 134, 82, 40)"},
    "red":    {"solid": "#E8615F", "border": "rgba(232, 97, 95, 130)",  "glow": "rgba(232, 97, 95, 45)"},
    # Added alongside display_strategy.py's DONE/INFO/ERROR split: DONE's
    # quick note wants its own accent distinct from "blue" (which INFO
    # already owns) and "red" (which ERROR/danger already own) -- a
    # muted sage-green in the same desaturated family as the three above,
    # not a bright/saturated "success" green that would clash.
    "green":  {"solid": "#52B788", "border": "rgba(82, 183, 136, 110)", "glow": "rgba(82, 183, 136, 40)"},
}

TEXT_PRIMARY = "rgba(224, 234, 248, 235)"
TEXT_MUTED = "rgba(154, 174, 202, 165)"
TEXT_FAINT = "rgba(154, 174, 202, 85)"

RADIUS = 16          # card corner radius, used everywhere for one consistent silhouette
RADIUS_SM = 10        # inner controls (buttons, input fields)

# ── shadow + the margins it requires ────────────────────────────────────────
#
# blurRadius/offset here and OUTER_MARGINS below are coupled: the outer
# layout's margins MUST be big enough to contain the shadow's blur bleed,
# or Windows' UpdateLayeredWindowIndirect silently fails on every repaint
# and the whole popup never renders (see BETA 0.3.46 rendering bug report
# -- this is not optional padding, it's load-bearing). If you change
# SHADOW_BLUR or SHADOW_OFFSET_Y, update OUTER_MARGINS to match: roughly
# (blur * 0.85) on each side, and (blur * 0.85 + offset_y) on the bottom.
SHADOW_BLUR = 32
SHADOW_OFFSET_Y = 8
SHADOW_ALPHA = 175

OUTER_MARGINS = (24, 24, 24, 32)   # (left, top, right, bottom) for outer QVBoxLayout


def make_shadow() -> QGraphicsDropShadowEffect:
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(SHADOW_BLUR)
    shadow.setColor(QColor(0, 0, 0, SHADOW_ALPHA))
    shadow.setOffset(0, SHADOW_OFFSET_Y)
    return shadow


def card_qss(object_name: str, accent: str = "blue") -> str:
    """Full stylesheet rule for a card QFrame with the given objectName.
    Two-stop vertical gradient (instead of a flat fill) plus a soft
    accent-tinted border reads as "glass panel" rather than a flat
    rectangle -- the main visible difference between "premium" and
    "generic" here, and it costs nothing extra to render."""
    a = ACCENT[accent]
    return f"""
        QFrame#{object_name} {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(22, 26, 38, 242),
                stop:1 rgba(11, 13, 22, 242));
            border: 1px solid {a['border']};
            border-radius: {RADIUS}px;
        }}
    """


def button_qss(accent: str = "blue", size: int = 26) -> str:
    """Consistent small round icon-button style (close/stop/cancel
    buttons). Bumped from the old 22px touch target to 26px -- 22px reads
    as fussy/hard-to-hit at typical trackpad precision."""
    a = ACCENT[accent]
    r = size // 2
    return f"""
        QPushButton {{
            background: rgba(255, 255, 255, 10);
            border: 1px solid {a['border']};
            border-radius: {r}px;
            color: {a['solid']};
            font: 600 12px {FONT_BODY};
        }}
        QPushButton:hover {{
            background: {a['glow']};
            color: #ffffff;
            border: 1px solid {a['solid']};
        }}
        QPushButton:pressed {{
            background: {a['border']};
        }}
    """


def line_edit_qss(accent: str = "blue") -> str:
    a = ACCENT[accent]
    return f"""
        QLineEdit {{
            background: rgba(255, 255, 255, 12);
            border: 1px solid {a['border']};
            border-radius: {RADIUS_SM}px;
            padding: 8px 12px;
            color: {TEXT_PRIMARY};
            {font_css(13)}
        }}
        QLineEdit:focus {{
            border: 1px solid {a['solid']};
            background: rgba(255, 255, 255, 16);
        }}
    """
