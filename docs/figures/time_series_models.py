"""Draws docs/images/time-series-models.svg: the five patterns the time series analysis tells apart.

Run it through `make docs`, or directly (numpy is the only requirement):
    backend/.venv/bin/python docs/figures/time_series_models.py

The look follows the project palette (see .claude/skills/brand-palette/SKILL.md).
"""
from pathlib import Path

import numpy as np
G, AMBER, TEXT, MUTED = "#3dff9a", "#ffb020", "#eef2f0", "#8a9490"
rng = np.random.default_rng(5)
N = 60
t = np.linspace(0, 4, N)
panels = [
    ("Stable", "No trend beyond the noise", np.zeros(N), None),
    ("Steady subsidence", "One constant velocity", -3 * t, None),
    ("Accelerating subsidence", "Velocity keeps growing", -0.6 * t - 1.1 * t ** 2, None),
    ("Trend reversal", "The slope changes at a date", np.where(t < 2, -5 * t, -10 + 4.5 * (t - 2)), 2.0),
    ("Abrupt shift", "A sudden offset at a date", np.where(t >= 2.4, -11.0, 0.0), 2.4),
]
W, H = 900, 712
pw, ph, cols = 400, 150, 2
out = [f'''<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="The five patterns the time series analysis distinguishes">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#07090a"/><stop offset="1" stop-color="#0b1311"/></linearGradient>
    <radialGradient id="glow" cx="90%" cy="-10%" r="80%"><stop offset="0" stop-color="{G}" stop-opacity="0.14"/><stop offset="1" stop-color="{G}" stop-opacity="0"/></radialGradient>
    <filter id="soft" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#bg)"/><rect width="{W}" height="{H}" fill="url(#glow)"/>
  <g font-family="Sora, 'Segoe UI', system-ui, -apple-system, sans-serif">''']
def x_of(v, gx, gw):
    return gx + (v - t[0]) / (t[-1] - t[0]) * gw


def y_of(v, gy, gh, lo, hi):
    return gy + (hi - v) / (hi - lo) * gh


def polyline(values, gx, gy, gw, gh, lo, hi):
    """SVG points for `values` plotted against the shared time axis `t`."""
    return " ".join(f"{x_of(a, gx, gw):.1f},{y_of(b, gy, gh, lo, hi):.1f}" for a, b in zip(t, values, strict=True))


def cell(i):
    r, c = divmod(i, cols)
    return 40 + c * (pw + 20), 36 + r * (ph + 70)
for i, (title, sub, base, brk) in enumerate(panels):
    x0, y0 = cell(i)
    out.append(f'<rect x="{x0}" y="{y0}" width="{pw}" height="{ph + 50}" rx="16" fill="#ffffff" fill-opacity="0.035" stroke="#ffffff" stroke-opacity="0.09"/>')
    out.append(f'<rect x="{x0 + 40}" y="{y0}" width="{pw - 80}" height="1" fill="{G}" fill-opacity="0.55"/>')
    out.append(f'<text x="{x0 + 18}" y="{y0 + 30}" font-size="18" font-weight="600" fill="{TEXT}">{title}</text>')
    out.append(f'<text x="{x0 + 18}" y="{y0 + 52}" font-size="14" fill="{MUTED}">{sub}</text>')
    gx, gy, gw, gh = x0 + 20, y0 + 72, pw - 40, ph - 46
    for k in range(3):
        out.append(f'<line x1="{gx}" y1="{gy + k * gh / 2:.1f}" x2="{gx + gw}" y2="{gy + k * gh / 2:.1f}" stroke="#ffffff" stroke-opacity="0.07"/>')
    y = base + 1.1 * rng.standard_normal(N)
    lo, hi = min(y.min(), base.min()) - 2, max(y.max(), base.max()) + 2
    box = (gx, gy, gw, gh, lo, hi)
    if brk is not None:
        bx = x_of(brk, gx, gw)
        out.append(f'<line x1="{bx:.1f}" y1="{gy - 4}" x2="{bx:.1f}" y2="{gy + gh}" stroke="{AMBER}" stroke-dasharray="4 4"/>')
        out.append(f'<text x="{bx + 5:.1f}" y="{gy + 10}" font-size="13" fill="{AMBER}" font-family="ui-monospace, monospace">detected date</text>')
    pts = polyline(y, *box)
    out.append(f'<polyline points="{pts}" fill="none" stroke="{G}" stroke-width="2.4" stroke-linejoin="round" filter="url(#soft)"/>')
    fit = polyline(base, *box)
    out.append(f'<polyline points="{fit}" fill="none" stroke="#ffffff" stroke-opacity="0.8" stroke-width="1.6" stroke-dasharray="6 5"/>')
# legend cell
x0, y0 = cell(5)
out.append(f'<rect x="{x0}" y="{y0}" width="{pw}" height="{ph + 50}" rx="16" fill="#ffffff" fill-opacity="0.035" stroke="#ffffff" stroke-opacity="0.09"/>')
items = [(G, "", "Measured displacement, in mm", False), ("#ffffff", "6 5", "Model chosen by the analysis", False), (AMBER, "4 4", "Date of a trend change or shift", True)]
for k, (col, dash, label, vertical) in enumerate(items):
    yy = y0 + 56 + k * 38
    if vertical:
        out.append(f'<line x1="{x0 + 32}" y1="{yy - 10}" x2="{x0 + 32}" y2="{yy + 10}" stroke="{col}" stroke-width="2" stroke-dasharray="{dash}"/>')
    else:
        out.append(f'<line x1="{x0 + 18}" y1="{yy}" x2="{x0 + 52}" y2="{yy}" stroke="{col}" stroke-width="2.2" {"stroke-dasharray=" + chr(34) + dash + chr(34) if dash else ""}/>')
    out.append(f'<text x="{x0 + 66}" y="{yy + 5}" font-size="15" fill="{MUTED}">{label}</text>')
out.append(f'<text x="{x0 + 18}" y="{y0 + ph + 30}" font-size="13" fill="{MUTED}">Illustrative data, not from a real site.</text>')
out.append('</g></svg>')
OUT = Path(__file__).resolve().parent.parent / "images" / "time-series-models.svg"
OUT.write_text("\n".join(out))
print(f"wrote {OUT}")
