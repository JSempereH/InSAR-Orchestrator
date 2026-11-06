---
name: brand-palette
description: The InSAR Orchestrator visual identity (black and green glass UI). Use it whenever you create or change anything visual in this project, including UI components and CSS, documentation, README or docs images, SVG banners and diagrams, screenshots, animations, charts and artifacts. It lists the exact colors, fonts and rules so the palette is always kept.
---

# InSAR Orchestrator palette

Everything visual in this project is **black with green**, with a glossy glass feel. This holds for the web UI and for everything in the docs: images, diagrams, the banner, the animation and screenshots. Do not introduce another accent (no blue, indigo or slate tones, no white or light backgrounds).

The source of truth for the UI is the `:root` block in `frontend/src/index.css`. If a value there changes, this file must change with it.

## Colors

| Role | Value | Notes |
|---|---|---|
| Background | `#07090a` | Near black. Pages sit on `radial-gradient` glows of the accent at the top right and bottom left. |
| Surface | `#0e1211` | Panels, popups and the map's loading state. |
| Glass panel | `rgba(255,255,255,0.035)` over a vertical gradient `0.07` to `0.015` | Cards. A 1px top highlight fades the accent in from the sides. |
| Border | `rgba(255,255,255,0.09)` | Hairline. |
| Text | `#eef2f0` | Primary. |
| Muted text | `#8a9490` | Secondary text, axes, captions. |
| Faint text | `#5d6663` | Hints and disabled text. |
| **Accent (green)** | `#3dff9a` | The only brand color. Primary buttons, active states, data lines, glows. |
| Accent 2 (teal) | `#00c2a8` | Second stop of green gradients, never on its own as a different hue. |
| On accent | `#04110a` | Text on a green fill. |
| Warning / EGMS | `#ffb020` | Warnings, EGMS areas, change points and outliers. |
| Danger / subsidence | `#ff5d6c` | Errors, negative velocity (subsidence). |
| Info | `#7fd1ff` | Running or neutral status only. |

Meaning is part of the palette: green is the project, uplift and success; red is subsidence and errors; amber is EGMS, warnings and detected changes. Keep those meanings in every chart and diagram.

In CSS use the tokens (`var(--accent)`, `var(--text-muted)`, `var(--panel)` and so on) instead of hex values. MapLibre paint properties and canvas code cannot read CSS variables, so those take the hex values from the table.

## Logo

The mark is a rounded square with the accent gradient, faint concentric rings at the top right (the interferogram motif) and a stylised "IS" in near black (`#04110a`). The single source is `frontend/public/logo.svg` (also the favicon). Never redraw it as a plain square with the letters "IS" in a font. Reuse the same paths wherever the logo appears:

- UI: `<img src="/logo.svg">`, as the sidebar does.
- SVGs such as `docs/images/banner.svg`: inline the same `<rect>`, ring `<circle>`s and the two `<path>`s, scaled with a `transform`.
- Canvas (`docs/animation/pipeline.html`): the `drawLogo()` helper, built on `Path2D` with those paths.

When the logo changes, regenerate everything that shows it: the banner, the animation (`docs/animation/render.mjs`) and every screenshot that includes the sidebar (`asf-panel`, `downloads-panel`, `viewer-*` in `docs/images/`).

## Typography

- **Sora** (600, 700) for headings and big numbers.
- **Figtree** (400 to 700) for body text.
- **JetBrains Mono** for dates, ids, coordinates and anything tabular.

SVG files shown through `<img>` (the README banner, diagrams) cannot load web fonts, so they declare `font-family="Sora, 'Segoe UI', system-ui, -apple-system, sans-serif"` and accept the fallback. Canvas code must call `document.fonts.load(...)` for each face before drawing.

## Look and feel

- Cards: rounded (`16px`), glass gradient, hairline border, accent line along the top edge, a soft dark shadow. A card inside a card is flat and darker (no second blur).
- Glow, not flat color, for the things that matter: `0 0 22px rgba(61,255,154,0.35)` on primary buttons, `drop-shadow` on data lines, a halo on strong movers.
- Primary buttons: gradient from `#7dffbb` to the accent, dark text.
- Sliders: 2px track, filled part in the accent, small thumb with a glow.
- Maps use a dark basemap (OpenFreeMap `dark` style, no API key). Never a light basemap.
- Charts: data line in the accent with a glow, grid at `rgba(255,255,255,0.06)`, axes in muted text, trend as a white dashed line, change points in amber.

## Documentation assets

The generated ones (screenshots, `time-series-models.svg`, the animation) are rebuilt with **`make docs`**, which also opens a local preview of the user docs (see `docs/maintaining-docs.md`). Rebuild them whenever the UI or the logo changes. When you add or change docs, README images, diagrams or the animation:

1. Use the palette above. Backgrounds are black, never white, and nothing looks like a default chart.
2. Prefer **real screenshots** of the running app (they already follow the palette) over mockups. Take them at 1600 px wide.
3. Diagrams and figures are SVG, generated by a script when they contain data, with the same glass panels and accent line. See `docs/images/banner.svg` and `docs/images/time-series-models.svg`.
4. The animation is `docs/animation/pipeline.html` (canvas, rendered by `docs/animation/render.mjs`). New scenes use the same constants as its `C` object.
5. Keep GIFs light (a few MB) and use a static background so they compress.
6. Do not write em dashes or en dashes in code, text or docs of this project.

## Checklist before finishing

- [ ] No color outside the table (search for `#2563eb`, `indigo`, `slate`, `#fff` backgrounds).
- [ ] Green is the only accent, and red, amber and teal keep their meaning.
- [ ] CSS uses tokens, canvas and MapLibre use the hex values.
- [ ] Images and SVGs have a black background and the glass look.
- [ ] If a token changed, `frontend/src/index.css` and this file agree.
- [ ] If the UI or the logo changed, the screenshots, banner and animation in `docs/images/` were regenerated.
