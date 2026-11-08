# Maintaining the docs

This page is for people working on the project. Users read [the guide](guide.md), which links to the other pages.

The written docs are plain Markdown in `docs/` (`guide.md` is the index; `quick-start.md`, `credentials.md`, `get-data.md`, `backup.md`, `time-series-analysis.md`, `deploy.md`). Everything in `docs/images/` that is not a hand-drawn logo is **generated**, so it can be refreshed in one step whenever the interface changes.

```bash
make docs          # regenerate every asset, then preview the docs site at http://localhost:8001/
make docs-assets   # only regenerate
make docs-serve    # only build and preview the site
```

Pick what to regenerate with `ONLY`, for example `make docs-assets ONLY=screenshots` (the animation takes a few minutes, the rest about a minute):

| Step | Script | Output | Needs |
|---|---|---|---|
| `screenshots` | `docs/screenshots.mjs` | `asf-panel.png`, `downloads-panel.png`, `viewer-mintpy.png`, `viewer-egms.png`, `settings-credentials.png`, `settings-backup.png`, `processing-details.png` | The app with some data, and Google Chrome |
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

## The site

The docs are published as a website on GitHub Pages: <https://jsempereh.github.io/InSAR-Orchestrator/>. `docs/site.mjs` builds it into `docs/_site` from the Markdown in `docs/` and the images in `docs/images/`, with the project's palette (`docs/site/style.css`). `make docs-serve` builds it and serves it locally, which is also how you check a change before pushing.

- The build **fails on a broken link, image or anchor**, so a bad link never gets deployed.
- Links between docs pages become links between site pages. Links to any other file of the repository become links to it on GitHub. Write them as normal relative links, so they also work in the repository view.
- To add a page, create the Markdown file and add it to the `PAGES` list at the top of `docs/site.mjs` (the order is the order of the navigation bar).
- `.github/workflows/docs.yml` deploys it on every push to `main` that touches `docs/`. One-time setup in the repository: **Settings, Pages, Build and deployment, Source: GitHub Actions**.
- The site loads its fonts from Google Fonts. Everything else is local.

## Keep in mind

- Always keep the black and green palette in new assets: see `.claude/skills/brand-palette/SKILL.md`.
- If the logo changes (`frontend/public/logo.svg`), the banner (`banner.svg`) is edited by hand, and everything else is regenerated with `make docs`.
- Keep the user docs short and in plain language. The guide is meant to be read in a few minutes.
