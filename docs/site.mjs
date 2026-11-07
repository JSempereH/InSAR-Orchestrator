// Builds the docs site (docs/_site) from the Markdown in docs/. Same output for the local
// preview (`make docs-serve`) and for GitHub Pages (.github/workflows/docs.yml).
//
//   node docs/site.mjs
//
// The build fails on a broken link, image or anchor, so a bad link never gets deployed.
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, posix, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Marked } from 'marked';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const out = join(here, '_site');
const REPO = 'https://github.com/JSempereH/InSAR-Orchestrator';
const BRANCH = 'main';

// Order is the order of the navigation bar. `guide` is the home page.
const PAGES = [
  { src: 'guide.md', out: 'index.html', title: 'Guide' },
  { src: 'quick-start.md', out: 'quick-start.html', title: 'Quick start' },
  { src: 'credentials.md', out: 'credentials.html', title: 'Credentials' },
  { src: 'get-data.md', out: 'get-data.html', title: 'Get data' },
  { src: 'backup.md', out: 'backup.html', title: 'Your files' },
  { src: 'time-series-analysis.md', out: 'time-series-analysis.html', title: 'Reading a time series' },
  { src: 'deploy.md', out: 'deploy.html', title: 'Deploy' },
];

const errors = [];
const escapeHtml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// Same rule as GitHub, so anchors written for the repository view keep working.
const slugify = (text) => text.toLowerCase().trim().replace(/[^\p{L}\p{N}\s-]/gu, '').replace(/\s/g, '-');

function render(page) {
  const seen = new Map();
  const ids = new Set();
  const marked = new Marked({ gfm: true });
  marked.use({
    renderer: {
      heading(text, level, raw) {
        const base = slugify(raw);
        const n = seen.get(base) ?? 0;
        seen.set(base, n + 1);
        const id = n ? `${base}-${n}` : base;
        ids.add(id);
        return `<h${level} id="${id}">${text}</h${level}>\n`;
      },
    },
  });
  const html = marked.parse(readFileSync(join(here, page.src), 'utf8'));
  return { html, ids };
}

const rendered = new Map(PAGES.map((p) => [p.src, render(p)]));

// Relative links were written for the repository view. Between docs pages they point at the
// site; to any other file of the repository they point at GitHub; images stay relative.
function fixLinks(page, html) {
  return html.replace(/\b(href|src)="([^"]*)"/g, (whole, attr, value) => {
    if (!value || /^(https?:|mailto:|data:)/.test(value)) return whole;
    const [rawPath, fragment] = value.split('#');
    if (!rawPath) {
      if (fragment && !rendered.get(page.src).ids.has(fragment)) errors.push(`${page.src}: anchor #${fragment} does not exist`);
      return whole;
    }
    const target = posix.normalize(posix.join('docs', rawPath));
    const doc = PAGES.find((p) => `docs/${p.src}` === target);
    if (doc) {
      if (fragment && !rendered.get(doc.src).ids.has(fragment)) errors.push(`${page.src}: ${rawPath}#${fragment} does not exist`);
      return `${attr}="${doc.out}${fragment ? `#${fragment}` : ''}"`;
    }
    if (target.startsWith('docs/images/')) {
      if (!existsSync(join(root, target))) errors.push(`${page.src}: ${rawPath} does not exist`);
      return `${attr}="${target.slice('docs/'.length)}"`;
    }
    const abs = join(root, target);
    if (!existsSync(abs)) {
      errors.push(`${page.src}: ${rawPath} does not exist`);
      return whole;
    }
    return `${attr}="${REPO}/${statSync(abs).isDirectory() ? 'tree' : 'blob'}/${BRANCH}/${target}${fragment ? `#${fragment}` : ''}"`;
  });
}

const template = (page, body) => `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#07090a">
<title>${escapeHtml(page.title)} · InSAR Orchestrator docs</title>
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700&family=Figtree:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="top">
  <a class="brand" href="index.html"><img src="logo.svg" alt="">InSAR Orchestrator <small>docs</small></a>
  <nav aria-label="Documents">
${PAGES.map((p) => `    <a href="${p.out}"${p === page ? ' aria-current="page"' : ''}>${escapeHtml(p.title)}</a>`).join('\n')}
    <span class="spacer"></span>
    <a href="${REPO}">GitHub</a>
  </nav>
</header>
<main>
<article class="md">
${body}
<p class="footer">Found a mistake? <a href="${REPO}/edit/${BRANCH}/docs/${page.src}">Edit this page on GitHub</a>.</p>
</article>
</main>
</body>
</html>
`;

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
for (const page of PAGES) {
  writeFileSync(join(out, page.out), template(page, fixLinks(page, rendered.get(page.src).html)));
}
cpSync(join(here, 'site/style.css'), join(out, 'style.css'));
cpSync(join(root, 'frontend/public/logo.svg'), join(out, 'logo.svg'));
cpSync(join(root, 'frontend/public/favicon.svg'), join(out, 'favicon.svg'));
cpSync(join(here, 'images'), join(out, 'images'), { recursive: true });
writeFileSync(join(out, '.nojekyll'), '');

if (errors.length) {
  console.error(`Docs site has ${errors.length} problem(s):\n  - ${errors.join('\n  - ')}`);
  process.exit(1);
}
console.log(`Docs site written to ${out} (${PAGES.length} pages)`);
