import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pdfPath = path.resolve(process.argv[2] || path.join(root, 'data/resumes/generic.pdf'));
const pdfjsPath = path.join(root, 'node_modules/pdfjs-dist/legacy/build/pdf.mjs');
if (!fs.existsSync(pdfjsPath)) throw new Error('JobPilot PDF.js dependency is unavailable');
const pdfjs = await import(pathToFileURL(pdfjsPath));
const document = await pdfjs.getDocument({data: new Uint8Array(fs.readFileSync(pdfPath)), disableWorker: true}).promise;
const lines = [];
for (let pageNumber = 1; pageNumber <= document.numPages; pageNumber++) {
  const page = await document.getPage(pageNumber);
  const content = await page.getTextContent();
  const rows = new Map();
  for (const item of content.items) {
    if (!item.str?.trim()) continue;
    const y = Math.round(item.transform[5]);
    const row = rows.get(y) || [];
    row.push([item.transform[4], item.str]);
    rows.set(y, row);
  }
  for (const [, items] of [...rows].sort((a, b) => b[0] - a[0])) {
    lines.push(items.sort((a, b) => a[0] - b[0]).map(item => item[1]).join(' ').replace(/\s+/g, ' ').trim());
  }
}
process.stdout.write(JSON.stringify({lines}));
