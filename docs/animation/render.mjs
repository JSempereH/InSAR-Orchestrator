// Renders docs/animation/pipeline.html to MP4 and GIF.
//
//   npm i playwright-core     (uses your installed Google Chrome; ffmpeg must be on PATH)
//   node docs/animation/render.mjs
//
// Output: docs/images/pipeline.mp4 and docs/images/pipeline.gif
import { chromium } from 'playwright-core';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(here, '../images');
const FPS = 30;
const frames = mkdtempSync(join(tmpdir(), 'insar-frames-'));

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
await page.goto(pathToFileURL(join(here, 'pipeline.html')).href + '?render');
await page.evaluate(() => window.fontsReady);

const duration = await page.evaluate(() => window.DURATION);
const total = Math.round(duration * FPS);
for (let i = 0; i < total; i++) {
  await page.evaluate((t) => window.draw(t), i / FPS);
  await page.screenshot({ path: join(frames, `f_${String(i).padStart(4, '0')}.png`) });
}
await browser.close();

mkdirSync(outDir, { recursive: true });
const input = ['-framerate', String(FPS), '-i', join(frames, 'f_%04d.png')];
execFileSync('ffmpeg', ['-loglevel', 'error', '-y', ...input, '-c:v', 'libx264', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', join(outDir, 'pipeline.mp4')]);
// GIF for the README (GitHub does not play MP4 files from the repo inline).
const palette = join(frames, 'palette.png');
const vf = 'fps=15,scale=800:-1:flags=lanczos';
execFileSync('ffmpeg', ['-loglevel', 'error', '-y', ...input, '-vf', `${vf},palettegen=max_colors=64:stats_mode=diff`, palette]);
execFileSync('ffmpeg', ['-loglevel', 'error', '-y', ...input, '-i', palette, '-lavfi', `${vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`, join(outDir, 'pipeline.gif')]);
rmSync(frames, { recursive: true, force: true });
console.log('done:', outDir);
