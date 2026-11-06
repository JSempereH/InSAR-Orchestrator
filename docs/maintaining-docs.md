# Maintaining the docs

This page is for people working on the project. Users read [the guide](guide.md).

The written docs are plain Markdown in `docs/` (`guide.md`, `time-series-analysis.md`). Everything in `docs/images/` that is not a hand-drawn logo is **generated**, so it can be refreshed in one step whenever the interface changes.

```bash
make docs          # regenerate every asset, then preview the docs at http://localhost:8001/docs/preview/
make docs-assets   # only regenerate
make docs-serve    # only preview
```

Pick what to regenerate with `ONLY`, for example `make docs-assets ONLY=screenshots` (the animation takes a few minutes, the rest about a minute):

| Step | Script | Output | Needs |
|---|---|---|---|
| `screenshots` | `docs/screenshots.mjs` | `asf-panel.png`, `downloads-panel.png`, `viewer-mintpy.png`, `viewer-egms.png` | The app with some data, and Google Chrome |
| `figures` | `docs/figures/time_series_models.py` | `time-series-models.svg` | numpy |
| `animation` | `docs/animation/render.mjs` | `pipeline.mp4`, `pipeline.gif` | ffmpeg and Google Chrome |

The first run installs the tooling (`npm install --prefix docs`, only `playwright-core`). If the backend or the frontend is not running, the screenshots step starts them and stops them again afterwards.

## Screenshots

The script drives the app like a user would and picks what to show by itself:

- the first project with a finished MintPy run, and the point of that run that subsides the most;
- the first EGMS L2A/L2B download, and the point that moves most relative to the median of the download.

A screenshot is skipped with a warning when its data is missing (for example, no finished MintPy run). Nothing is hardcoded about your projects.

To find a point on screen, the script uses a hook that the map exposes in development only (`window.__insarMaps`, set in `DownloadsMap.tsx` when `import.meta.env.DEV`). Run it against `make dev` or let `make docs` start the frontend; a production build does not have the hook.

Options, as environment variables: `APP_URL` and `API_URL` (default `http://localhost:5173` and `http://localhost:8000`), `DOCS_BROWSER_CHANNEL` (default `chrome`; use `chromium` for Playwright's own browser) and `DOCS_PORT` for the preview port (default 8001).

## The animation

`docs/animation/pipeline.html` holds the scenes (canvas, deterministic). Open it in a browser to preview it with no rendering. `render.mjs` exports the frames and encodes the MP4 and the GIF with ffmpeg. The background is static on purpose, so that the GIF stays small.

## The preview

`docs/preview/index.html` is a static page, served from the repository root by `python3 -m http.server`. It shows the user docs the way GitHub would (relative images and links work), with the project's palette. The page loads the Markdown renderer from a CDN, so it needs an internet connection. To add a page to it, add the file to the `DOCS` list at the top of the page's script.

## Keep in mind

- Always keep the black and green palette in new assets: see `.claude/skills/brand-palette/SKILL.md`.
- If the logo changes (`frontend/public/logo.svg`), the banner (`banner.svg`) is edited by hand, and everything else is regenerated with `make docs`.
- Keep the user docs short and in plain language. The guide is meant to be read in a few minutes.
