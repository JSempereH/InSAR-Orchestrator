// Regenerates every generated asset of the docs.
//
//   node docs/build.mjs                         everything
//   node docs/build.mjs --only=screenshots      just some of them: screenshots, figures, animation
//
// screenshots  docs/screenshots.mjs  needs the app running with data (started here when it is not)
// figures      docs/figures/*.py     needs numpy
// animation    docs/animation/render.mjs  needs ffmpeg, takes a few minutes
//
// Needs Google Chrome for the browser steps (DOCS_BROWSER_CHANNEL=chromium to use Playwright's own).
import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const APP = process.env.APP_URL ?? 'http://localhost:5173';
const API = process.env.API_URL ?? 'http://localhost:8000';

const onlyArg = process.argv.find((a) => a.startsWith('--only='));
const steps = new Set((onlyArg ? onlyArg.split('=')[1] : 'screenshots,figures,animation').split(',').map((s) => s.trim()));
for (const s of steps) {
  if (!['screenshots', 'figures', 'animation'].includes(s)) {
    console.error(`Unknown step "${s}". Use screenshots, figures or animation.`);
    process.exit(2);
  }
}

const started = [];
const stopStarted = () => started.forEach((child) => { try { child.kill('SIGTERM'); } catch { /* already gone */ } });
process.on('exit', stopStarted);
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => process.exit(130));

const isUp = (url) => fetch(url).then((r) => r.ok || r.status < 500, () => false);

async function waitUntilUp(url, label, seconds = 90) {
  for (let i = 0; i < seconds; i++) {
    if (await isUp(url)) return;
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`${label} did not come up at ${url} within ${seconds}s`);
}

async function ensureApp() {
  if (!(await isUp(`${API}/api/health`))) {
    const uvicorn = join(root, 'backend/.venv/bin/uvicorn');
    if (!existsSync(uvicorn)) throw new Error('backend/.venv is missing. Run `make install` first.');
    console.log('==> Starting the backend for the screenshots');
    started.push(spawn(uvicorn, ['app.main:app', '--port', new URL(API).port || '8000'], { cwd: join(root, 'backend'), stdio: 'ignore' }));
    await waitUntilUp(`${API}/api/health`, 'The backend');
  }
  if (!(await isUp(APP))) {
    if (!existsSync(join(root, 'frontend/node_modules'))) throw new Error('frontend/node_modules is missing. Run `make install` first.');
    console.log('==> Starting the frontend for the screenshots');
    started.push(spawn('npm', ['run', 'dev', '--prefix', 'frontend', '--', '--port', new URL(APP).port || '5173'], { cwd: root, stdio: 'ignore' }));
    await waitUntilUp(APP, 'The frontend');
  }
}

function run(label, command, args) {
  console.log(`==> ${label}`);
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit' });
  if (result.status !== 0) throw new Error(`${label} failed`);
}

const failures = [];
async function step(name, fn) {
  if (!steps.has(name)) return;
  try { await fn(); } catch (err) { failures.push(`${name}: ${err.message}`); console.error(`!! ${name} failed: ${err.message}`); }
}

await step('screenshots', async () => {
  await ensureApp();
  console.log('==> Screenshots');
  const { captureScreenshots } = await import('./screenshots.mjs');
  const { done, skipped } = await captureScreenshots();
  done.forEach((f) => console.log('    wrote', f));
  skipped.forEach((f) => console.warn('    skipped', f));
});

await step('figures', async () => {
  const venvPython = join(root, 'backend/.venv/bin/python');
  const python = existsSync(venvPython) ? venvPython : 'python3';
  run('Figures', python, [join(here, 'figures/time_series_models.py')]);
});

await step('animation', async () => {
  if (spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' }).status !== 0) throw new Error('ffmpeg is not installed');
  run('Animation (this takes a few minutes)', 'node', [join(here, 'animation/render.mjs')]);
});

if (failures.length) {
  console.error(`\nDone with problems:\n  - ${failures.join('\n  - ')}`);
  process.exit(1);
}
console.log('\nDocs assets are up to date.');
