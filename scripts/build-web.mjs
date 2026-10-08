#!/usr/bin/env node
// Dựng bản giao diện tĩnh cho Vercel (web-dist/): trang + static/ + config.js trỏ tới server riêng.
//   MA_API_BASE=https://meeting-api.example.com node scripts/build-web.mjs
// Server (FastAPI) chạy riêng ở máy luôn bật, xem README mục "Chạy trên web". Chỉ dùng thư viện có sẵn của Node.
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const OUT = join(ROOT, 'web-dist');

const base = String(process.env.MA_API_BASE || '').trim().replace(/\/+$/, '');
if (!/^https:\/\/[^\s/]+/.test(base) && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(base)) {
  console.error('[build-web] Cần biến MA_API_BASE là địa chỉ https của server, ví dụ https://meeting-api.example.com '
    + '(Vercel: Project Settings > Environment Variables).');
  process.exit(1);
}

rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });
const html = readFileSync(join(ROOT, 'meeting', 'index.html'), 'utf-8');
if (!html.includes('<script src="/config.js"></script>')) {
  console.error('[build-web] meeting/index.html không nạp /config.js: trang sẽ gọi nhầm API về Vercel.');
  process.exit(1);
}
writeFileSync(join(OUT, 'index.html'), html);
if (existsSync(join(ROOT, 'static'))) cpSync(join(ROOT, 'static'), join(OUT, 'static'), { recursive: true });
writeFileSync(join(OUT, 'config.js'), `window.MA_CONFIG = ${JSON.stringify({ apiBase: base })};\n`);
console.log(`[build-web] Đã dựng web-dist/ (API: ${base})`);
