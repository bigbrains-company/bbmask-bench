"""Dependency-free SVG charts for the README.

The charts carry their own background so they read the same on GitHub's light and dark themes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from html import escape

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
# The palette of bbmask.co: warm paper, near-black ink, one signal colour.
INK = "#11110f"
MUTED = "#6f6b61"
GRID = "#e3ddcf"
ACCENT = "#ee4318"
SURFACE = "#fbf9f4"


def _frame(width: int, height: int, title: str, body: list[str]) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" font-family="{FONT}" role="img" '
            f'aria-label="{escape(title)}">',
            f'<rect width="{width}" height="{height}" rx="12" fill="{SURFACE}" stroke="{GRID}"/>',
            f'<text x="24" y="34" font-size="16" font-weight="600" fill="{INK}">'
            f"{escape(title)}</text>",
            *body,
            "</svg>",
            "",
        ]
    )


def _thousands(value: float) -> str:
    return f"{value:,.0f}"


def horizontal_bars(title: str, items: Sequence[tuple[str, float]], width: int = 760) -> str:
    """One bar per (label, value), longest first as given."""
    row, top, left, right = 26, 56, 190, 90
    height = top + row * len(items) + 18
    peak = max((value for _, value in items), default=1) or 1
    span = width - left - right
    body = []
    for index, (label, value) in enumerate(items):
        y = top + index * row
        bar = max(2.0, span * value / peak)
        body += [
            f'<text x="{left - 12}" y="{y + 14}" font-size="12" text-anchor="end" '
            f'fill="{INK}">{escape(label)}</text>',
            f'<rect x="{left}" y="{y + 3}" width="{bar:.1f}" height="16" rx="3" fill="{ACCENT}"/>',
            f'<text x="{left + bar + 8:.1f}" y="{y + 15}" font-size="12" '
            f'fill="{MUTED}">{_thousands(value)}</text>',
        ]
    return _frame(width, height, title, body)


def _log_ticks(low: float, high: float) -> list[float]:
    first = math.floor(math.log10(low))
    last = max(math.ceil(math.log10(high)), first + 1)
    return [10.0**power for power in range(first, last + 1)]


def _tick_label(value: float, unit: str) -> str:
    if unit == "chars":
        return f"{value / 1000:g}k" if value >= 1000 else f"{value:g}"
    return f"{value:g} s"


def latency_scatter(
    points: Sequence[tuple[int, float]],
    chars_per_second: float | None,
    width: int = 760,
    height: int = 380,
) -> str:
    """Anonymize latency against document size, both on logarithmic axes."""
    title = "Anonymize latency by document size"
    points = [(chars, seconds) for chars, seconds in points if chars > 0 and seconds > 0]
    if not points:
        return _frame(width, height, title, [])

    left, right, top, bottom = 64, 28, 58, 48
    x_ticks = _log_ticks(min(c for c, _ in points), max(c for c, _ in points))
    y_ticks = _log_ticks(min(s for _, s in points), max(s for _, s in points))
    x_low, x_high = math.log10(x_ticks[0]), math.log10(x_ticks[-1])
    y_low, y_high = math.log10(y_ticks[0]), math.log10(y_ticks[-1])

    def px(chars: float) -> float:
        return left + (math.log10(chars) - x_low) / (x_high - x_low) * (width - left - right)

    def py(seconds: float) -> float:
        return (
            height
            - bottom
            - (math.log10(seconds) - y_low) / (y_high - y_low) * (height - top - bottom)
        )

    body = []
    for tick in x_ticks:
        x = px(tick)
        body += [
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height - bottom}" stroke="{GRID}"/>',
            f'<text x="{x:.1f}" y="{height - bottom + 18}" font-size="11" text-anchor="middle" '
            f'fill="{MUTED}">{_tick_label(tick, "chars")}</text>',
        ]
    for tick in y_ticks:
        y = py(tick)
        body += [
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="{GRID}"/>',
            f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end" '
            f'fill="{MUTED}">{_tick_label(tick, "s")}</text>',
        ]

    if chars_per_second:
        # Reference line: the latency a document would have at the average throughput.
        start = max(x_ticks[0], y_ticks[0] * chars_per_second)
        end = min(x_ticks[-1], y_ticks[-1] * chars_per_second)
        if start < end:
            body.append(
                f'<line x1="{px(start):.1f}" y1="{py(start / chars_per_second):.1f}" '
                f'x2="{px(end):.1f}" y2="{py(end / chars_per_second):.1f}" stroke="{MUTED}" '
                f'stroke-dasharray="5 4"/>'
            )
            body.append(
                # Bottom right: large documents are never fast, so that corner stays empty.
                f'<text x="{width - right - 8}" y="{height - bottom - 10}" font-size="11" '
                f'text-anchor="end" '
                f'fill="{MUTED}">dashed: {_thousands(chars_per_second)} characters/second</text>'
            )

    body += [
        f'<circle cx="{px(chars):.1f}" cy="{py(seconds):.1f}" r="2.4" fill="{ACCENT}" '
        f'fill-opacity="0.45"/>'
        for chars, seconds in points
    ]
    body += [
        f'<text x="{(left + width - right) / 2:.0f}" y="{height - 10}" font-size="12" '
        f'text-anchor="middle" fill="{MUTED}">document size (characters, log scale)</text>',
        f'<text x="18" y="{(top + height - bottom) / 2:.0f}" font-size="12" text-anchor="middle" '
        f'fill="{MUTED}" transform="rotate(-90 18 {(top + height - bottom) / 2:.0f})">'
        f"latency (log scale)</text>",
    ]
    return _frame(width, height, f"{title} ({_thousands(len(points))} documents)", body)
