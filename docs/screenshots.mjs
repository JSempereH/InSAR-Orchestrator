// Takes the screenshots used in the README and the docs from the running app.
//
// It talks to the app like a user would, so the app has to be running with some
// data in it (`make docs` starts it when it is not). Nothing is hardcoded about
// your data: it picks the datasets and the points to show from the API.
//
//   ASF project monitor   -> docs/images/asf-panel.png
//   Downloads             -> docs/images/downloads-panel.png
//   Viewer, MintPy        -> docs/images/viewer-mintpy.png   (needs a finished MintPy run)
//   Viewer, EGMS          -> docs/images/viewer-egms.png     (needs an L2A/L2B download)
//
// A screenshot whose data is missing is skipped with a warning.
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

const APP = process.env.APP_URL ?? 'http://localhost:5173';
const API = process.env.API_URL ?? 'http://localhost:8000';
const CHANNEL = process.env.DOCS_BROWSER_CHANNEL ?? 'chrome';
const OUT = resolve(process.env.DOCS_IMAGES_DIR ?? join(here, 'images'));

const getJson = async (path) => {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${path} answered ${res.status}`);
  return res.json();
};
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const median = (a) => { const s = [...a].sort((x, y) => x - y); return s[Math.floor((s.length - 1) / 2)]; };

async function discover() {
  const projects = await getJson('/api/projects');
  const statuses = await Promise.all(projects.map((p) => getJson(`/api/projects/${p.id}/mintpy/status`).catch(() => null)));
  const ready = projects.filter((_, i) => statuses[i]?.status === 'SUCCEEDED');
  const egms = (await getJson('/api/egms/downloads')).filter((d) => ['L2A', 'L2B'].includes(d.level));
  return { projects, mintpy: ready[0] ?? null, egms: egms[0] ?? null };
}

/** Most negative velocity point of a MintPy run, a good illustration of subsidence. */
async function mintpyPoint(projectId) {
  const points = (await getJson(`/api/projects/${projectId}/mintpy/points`)).features;
  const worst = points.reduce((a, b) => (b.properties.velocity < a.properties.velocity ? b : a));
  return { lon: worst.geometry.coordinates[0], lat: worst.geometry.coordinates[1] };
}

/** The EGMS point that moves most relative to the median, a good illustration of a change. */
async function egmsPoint(downloadId) {
  const points = (await getJson(`/api/egms/downloads/${downloadId}/points`)).features;
  const velocities = points.map((f) => f.properties.velocity).filter((v) => typeof v === 'number');
  const med = median(velocities);
  const best = points.reduce((a, b) => (Math.abs(b.properties.velocity - med) > Math.abs(a.properties.velocity - med) ? b : a));
  return { lon: best.geometry.coordinates[0], lat: best.geometry.coordinates[1] };
}

async function fresh(browser, height) {
  const page = await browser.newPage({ viewport: { width: 1600, height } });
  page.on('pageerror', (e) => console.warn('  page error:', e.message));
  await page.goto(APP);
  await page.evaluate(() => localStorage.clear()); // a clean viewer: default window size, sliders and so on
  await page.reload();
  await page.waitForLoadState('networkidle');
  return page;
}

async function openViewer(page) {
  // The basemap style is fetched from a public server and now and then stalls: retry the page a few times.
  for (let attempt = 0; attempt < 4; attempt++) {
    await page.getByRole('button', { name: 'Viewer' }).click();
    const loaded = await page.getByText('Loading map').waitFor({ state: 'hidden', timeout: 25_000 }).then(() => true, () => false);
    if (loaded) return;
    await page.reload();
    await page.waitForLoadState('networkidle');
  }
  throw new Error('The map never finished loading (is the basemap server reachable?)');
}

async function mapIdle(page) {
  await page.evaluate(() => new Promise((done) => {
    const map = window.__insarMaps?.at(-1);
    if (!map) return done();
    map.once('idle', () => done()); // not done(event): the event holds the map, which cannot be serialized
    map.triggerRepaint();
  }));
}

/** Medium-sized time series window, moved to the right so the map stays visible. */
async function arrangeWindow(page) {
  await page.getByLabel('Size M').click();
  await page.waitForTimeout(600);
  const header = await page.getByRole('dialog').locator('div').first().boundingBox();
  await page.mouse.move(header.x + 80, header.y + 15);
  await page.mouse.down();
  await page.mouse.move(header.x + 80 + 430, header.y + 25, { steps: 10 });
  await page.mouse.up();
  await page.waitForTimeout(1200);
}

export async function captureScreenshots() {
  mkdirSync(OUT, { recursive: true });
  const found = await discover();
  if (!found.projects.length && !found.egms) throw new Error('The app has no projects or downloads: there is nothing to photograph.');
  const browser = await chromium.launch({ channel: CHANNEL, headless: true });
  const done = [];
  const skipped = [];
  try {
    // 1. ASF project monitor
    if (found.projects.length) {
      const project = found.mintpy ?? found.projects[0];
      const page = await fresh(browser, 1000);
      await page.getByText(project.name).first().click();
      await page.waitForTimeout(3500);
      await page.screenshot({ path: join(OUT, 'asf-panel.png') });
      await page.close();
      done.push('asf-panel.png');
    } else skipped.push('asf-panel.png (no projects)');

    // 2. Downloads
    {
      const page = await fresh(browser, 520);
      await page.getByRole('button', { name: 'Downloads' }).click();
      await page.waitForTimeout(4500);
      await page.screenshot({ path: join(OUT, 'downloads-panel.png') });
      await page.close();
      done.push('downloads-panel.png');
    }

    // 3. Viewer with a MintPy project
    if (found.mintpy) {
      const point = await mintpyPoint(found.mintpy.id);
      const page = await fresh(browser, 820);
      await openViewer(page);
      await page.getByRole('button', { name: new RegExp(escapeRe(found.mintpy.name)) }).click();
      await page.getByPlaceholder('Latitude').waitFor({ timeout: 60_000 }); // shown once the points are loaded
      await page.getByPlaceholder('Latitude').fill(String(point.lat));
      await page.getByPlaceholder('Longitude').fill(String(point.lon));
      await page.getByRole('button', { name: 'Get time series' }).click();
      await page.getByRole('dialog').waitFor({ timeout: 30_000 });
      await page.getByText('Maximum').waitFor({ timeout: 30_000 }); // the statistics cards
      await arrangeWindow(page);
      await page.screenshot({ path: join(OUT, 'viewer-mintpy.png') });
      await page.close();
      done.push('viewer-mintpy.png');
    } else skipped.push('viewer-mintpy.png (no project with a finished MintPy run)');

    // 4. Viewer with an EGMS download, zoomed to street level on one point
    if (found.egms) {
      const point = await egmsPoint(found.egms.id);
      const page = await fresh(browser, 820);
      await openViewer(page);
      await page.getByRole('button', { name: new RegExp(escapeRe(found.egms.name)) }).click();
      await page.getByText('Reading points').waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {});
      await page.getByText('Reading points').waitFor({ state: 'hidden', timeout: 180_000 });
      // No value may be returned from here: jumpTo() returns the map itself, which Playwright would try to serialize.
      await page.evaluate(({ lon, lat }) => { window.__insarMaps.at(-1).jumpTo({ center: [lon, lat], zoom: 17, padding: { right: 560 } }); }, point); // keeps the point clear of the window
      await mapIdle(page);
      const where = await page.evaluate(({ lon, lat }) => window.__insarMaps.at(-1).project([lon, lat]), point);
      const canvas = await page.locator('canvas.maplibregl-canvas').first().boundingBox();
      await page.mouse.click(canvas.x + where.x, canvas.y + where.y);
      await page.getByRole('dialog').waitFor({ timeout: 30_000 });
      await page.getByText('Maximum').waitFor({ timeout: 30_000 });
      await arrangeWindow(page);
      await page.screenshot({ path: join(OUT, 'viewer-egms.png') });
      await page.close();
      done.push('viewer-egms.png');
    } else skipped.push('viewer-egms.png (no L2A/L2B EGMS download)');
  } finally {
    await browser.close();
  }
  return { done, skipped };
}

// Run directly: `node docs/screenshots.mjs`
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  captureScreenshots().then(({ done, skipped }) => {
    done.forEach((f) => console.log('  wrote', f));
    skipped.forEach((f) => console.warn('  skipped', f));
  }, (err) => { console.error(err.message); process.exit(1); });
}
