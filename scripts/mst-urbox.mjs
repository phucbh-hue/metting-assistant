#!/usr/bin/env node
// Cài và chạy UrBox Meeting Copilot trên máy bất kỳ (Windows, macOS, Linux) - chỉ cần Node.js 20+, pnpm và Python 3.10+:
//   pnpm mst-urbox install   tạo .venv, cài thư viện Python, cài Claude Code / Codex / Gemini CLI riêng cho dự án
//   pnpm mst-urbox build     tạo .env, tải model nhận diện giọng, giọng đọc tiếng Việt, trình duyệt tra cứu; tự kiểm tra
//   pnpm mst-urbox web       chạy ứng dụng ở http://127.0.0.1:8080 và mở trình duyệt
//   pnpm mst-urbox demo | test | doctor | help
// Khóa (Soniox, Claude, Gemini...) điền trong Cài đặt của ứng dụng; gói đăng ký bấm Kết nối rồi đăng nhập.
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createWriteStream, existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import { dirname, join, relative, resolve } from 'node:path';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const WIN = process.platform === 'win32';
const [cmd = 'help', ...rest] = process.argv.slice(2);
const opt = parseOpts(rest);
const VENV = resolve(ROOT, opt.venv || process.env.MST_VENV || '.venv');
const VPY = WIN ? join(VENV, 'Scripts', 'python.exe') : join(VENV, 'bin', 'python');
// máy phát triển cũ dùng chung venv với dự án bên cạnh; máy mới luôn dùng .venv do "install" tạo
const LEGACY_PY = resolve(ROOT, '..', 'interviewer-assistant-AI-circle', '.venv', WIN ? 'Scripts/python.exe' : 'bin/python');
export const SPEAKER = {
  url: 'https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx',
  size: 28281138, sha256: 'f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11',
  path: join(ROOT, 'models', 'speaker.onnx'),
};
// Model mặc định từ bản 3.14 (ERes2NetV2, 3D-Speaker): phân biệt người rõ hơn CAM++; CAM++ vẫn giữ cho cuộc họp cũ
export const SPEAKER_V2 = {
  url: 'https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx',
  size: 71441526, sha256: 'bf1a75b9930474cf3389ef415e6e5d38ca96fea4a3a00f7e301d080a58ee2239',
  path: join(ROOT, 'models', '3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx'),
};
export const SECRET_KEYS = ['ANTHROPIC_API_KEY', 'GEMINI_API_KEY', 'SONIOX_API_KEY', 'MONGODB_URL', 'GOOGLE_OAUTH_CLIENT_ID', 'GOOGLE_OAUTH_CLIENT_SECRET'];
const PY_ENV = { PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' };

function parseOpts(list) {
  const out = {};
  for (let i = 0; i < list.length; i++) {
    const m = /^--([\w-]+)(?:=(.*))?$/.exec(list[i]);
    if (!m) continue;
    out[m[1]] = m[2] ?? (list[i + 1] && !list[i + 1].startsWith('--') ? list[++i] : true);
  }
  return out;
}
const say = (msg) => console.log(`\x1b[35m[mst-urbox]\x1b[0m ${msg}`);
const fail = (msg) => { console.error(`\x1b[31m[mst-urbox] ${msg}\x1b[0m`); process.exit(1); };

function run(file, args, extraEnv = {}, { allowFail = false } = {}) {
  console.log(`\x1b[2m$ ${[file, ...args].map(a => (/\s/.test(a) ? `"${a}"` : a)).join(' ')}\x1b[0m`);
  const r = spawnSync(file, args, { cwd: ROOT, stdio: 'inherit', env: { ...process.env, ...extraEnv } });
  if (r.error) { if (allowFail) return 1; fail(`Không chạy được ${file}: ${r.error.message}`); }
  if (r.status !== 0 && !allowFail) fail(`Lệnh thất bại (mã ${r.status}): ${file} ${args.join(' ')}`);
  return r.status ?? 1;
}
function capture(file, args) {
  const r = spawnSync(file, args, { cwd: ROOT, encoding: 'utf-8', env: { ...process.env, ...PY_ENV } });
  return r.status === 0 ? (r.stdout || '').trim() : null;
}

// ---------------------------------------------------------------- Python
function systemPython() {
  const cands = [];
  if (opt.python) cands.push([opt.python]);
  if (process.env.MST_PYTHON) cands.push([process.env.MST_PYTHON]);
  if (WIN) cands.push(['py', '-3.12'], ['py', '-3.11'], ['py', '-3.13'], ['py', '-3'], ['python']);
  else cands.push(['python3.12'], ['python3.11'], ['python3.13'], ['python3'], ['python']);
  for (const [file, ...args] of cands) {
    const v = capture(file, [...args, '-c', 'import sys; print("%d.%d" % sys.version_info[:2])']);
    if (!v) continue;
    const [maj, min] = v.split('.').map(Number);
    if (maj === 3 && min >= 10) return { file, args, version: v };
  }
  fail('Không tìm thấy Python 3.10 trở lên. Cài Python 3.12 (python.org) rồi chạy lại, hoặc chỉ đường: --python <đường dẫn>.');
}
export function appPython(required = true) {
  if (opt.python && existsSync(opt.python)) return opt.python;
  if (existsSync(VPY)) return VPY;
  if (process.env.MST_PYTHON && existsSync(process.env.MST_PYTHON)) return process.env.MST_PYTHON;
  if (!opt.venv && existsSync(LEGACY_PY)) return LEGACY_PY;
  if (required) fail(`Chưa có môi trường Python (${relative(ROOT, VENV) || VENV}). Chạy: pnpm mst-urbox install`);
  return null;
}

// ---------------------------------------------------------------- .env
export function envTemplate(example) {
  // .env mới từ .env.example: giữ cấu hình và chú thích, để trống mọi khóa bí mật (điền trong Cài đặt của ứng dụng)
  return example.split(/\r?\n/).map((line) => {
    const m = /^(\s*)([A-Z][A-Z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m || !SECRET_KEYS.includes(m[2])) return line;
    const comment = /\s#\s.*$/.exec(m[3]);
    return `${m[1]}${m[2]}=${comment ? ' '.repeat(16) + comment[0].trim() : ''}`;
  }).join('\n');
}
function ensureEnvFile() {
  const env = join(ROOT, '.env');
  if (existsSync(env)) { say('.env đã có, giữ nguyên.'); return; }
  const example = join(ROOT, '.env.example');
  if (!existsSync(example)) { writeFileSync(env, '', 'utf-8'); return; }
  writeFileSync(env, envTemplate(readFileSync(example, 'utf-8')), 'utf-8');
  say('Đã tạo .env (khóa để trống, điền trong Cài đặt > Kết nối dịch vụ / Nguồn AI).');
}

// ---------------------------------------------------------------- tải model
export async function download(url, dest, expectSize, expectSha256) {
  mkdirSync(dirname(dest), { recursive: true });
  const part = `${dest}.part`;
  const res = await fetch(url, { redirect: 'follow' });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const total = Number(res.headers.get('content-length')) || expectSize || 0;
  let got = 0, shown = -10;
  const body = Readable.fromWeb(res.body);
  body.on('data', (c) => {
    got += c.length;
    const pct = total ? Math.floor((got / total) * 100) : 0;
    if (pct >= shown + 10) { shown = pct - (pct % 10); process.stdout.write(`  ${shown}% `); }
  });
  await pipeline(body, createWriteStream(part));
  process.stdout.write('\n');
  if (expectSize && statSync(part).size !== expectSize) { rmSync(part, { force: true }); throw new Error('tệp tải về không đúng kích thước'); }
  if (expectSha256 && createHash('sha256').update(readFileSync(part)).digest('hex') !== expectSha256) {
    rmSync(part, { force: true }); throw new Error('tệp tải về không đúng mã kiểm tra SHA-256');
  }
  renameSync(part, dest);
}
async function ensureSpeakerModel() {
  for (const [m, label] of [[SPEAKER_V2, 'ERes2NetV2 (71 MB, model mặc định)'], [SPEAKER, 'CAM++ (28 MB, cho cuộc họp cũ)']]) {
    if (existsSync(m.path) && statSync(m.path).size === m.size) { say(`Model nhận diện giọng ${label.split(' (')[0]} đã có.`); continue; }
    say(`Tải model nhận diện giọng ${label}, sherpa-onnx...`);
    try { await download(m.url, m.path, m.size, m.sha256); say(`Đã tải ${relative(ROOT, m.path)}.`); }
    catch (e) { fail(`Không tải được model nhận diện giọng: ${e.message}. Kiểm tra mạng rồi chạy lại pnpm mst-urbox build.`); }
  }
}

// ---------------------------------------------------------------- pnpm
function pnpm(args) {
  const exec = process.env.npm_execpath;
  if (exec && /\.(c|m)?js$/.test(exec)) return run(process.execPath, [exec, ...args], {}, { allowFail: true });
  if (exec && existsSync(exec)) return run(exec, args, {}, { allowFail: true });
  return run(WIN ? 'pnpm.cmd' : 'pnpm', args, {}, { allowFail: true });
}

// ---------------------------------------------------------------- mạng
function portBusy(port, host = '127.0.0.1') {
  return new Promise((done) => {
    const srv = net.createServer().once('error', () => done(true)).once('listening', () => srv.close(() => done(false)));
    srv.listen(port, host);
  });
}
async function health(url) {
  try { const r = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(1500) }); return r.ok ? await r.json() : null; }
  catch { return null; }
}
function openBrowser(url) {
  if (opt['no-open']) return;
  const [file, args] = WIN ? ['cmd', ['/c', 'start', '', url]] : process.platform === 'darwin' ? ['open', [url]] : ['xdg-open', [url]];
  try { spawn(file, args, { detached: true, stdio: 'ignore' }).unref(); } catch { /* không mở được trình duyệt: người dùng tự mở */ }
}
async function serve(file, args, port) {
  const url = `http://127.0.0.1:${port}`;
  const child = spawn(file, args, { cwd: ROOT, stdio: 'inherit', env: { ...process.env, ...PY_ENV } });
  const stop = () => { if (!child.killed) child.kill('SIGINT'); };
  process.on('SIGINT', stop); process.on('SIGTERM', stop);
  (async () => {
    for (let i = 0; i < 120 && child.exitCode === null; i++) {
      if (await health(url)) { say(`Ứng dụng đã sẵn sàng: ${url}  (Ctrl+C để dừng)`); openBrowser(url); return; }
      await new Promise((r) => setTimeout(r, 1000));
    }
  })();
  const code = await new Promise((r) => child.on('exit', (c) => r(c ?? 0)));
  process.exit(code);
}

// ---------------------------------------------------------------- lệnh
async function install() {
  const [maj] = process.versions.node.split('.').map(Number);
  if (maj < 20) fail(`Cần Node.js 20 trở lên (đang có ${process.versions.node}).`);
  if (WIN && VENV.length > 90) {
    say(`Cảnh báo: đường dẫn ${VENV} dài, Windows có thể báo lỗi "tên tệp quá dài" khi cài thư viện. `
      + 'Nên đặt dự án ở thư mục ngắn (ví dụ C:\\work\\meeting-assistant) hoặc bật Long Paths của Windows.');
  }
  if (!existsSync(VPY)) {
    const py = systemPython();
    say(`Tạo môi trường Python ${relative(ROOT, VENV) || VENV} bằng Python ${py.version}...`);
    run(py.file, [...py.args, '-m', 'venv', VENV]);
  } else say(`Môi trường Python ${relative(ROOT, VENV) || VENV} đã có.`);
  run(VPY, ['-m', 'pip', 'install', '--upgrade', 'pip'], PY_ENV);
  run(VPY, ['-m', 'pip', 'install', '-r', join(ROOT, 'requirements.txt')], PY_ENV);
  if (opt['skip-cli']) say('Bỏ qua cài Claude Code / Codex / Gemini CLI (--skip-cli).');
  else {
    say('Cài Claude Code, Codex CLI, Gemini CLI riêng cho dự án (dùng gói đăng ký, có thể mất vài phút)...');
    if (pnpm(['install']) !== 0) say('Cài CLI chưa xong (thiếu mạng?). Ứng dụng vẫn chạy với API key; chạy lại pnpm mst-urbox install sau.');
  }
  say('Xong bước cài. Tiếp theo: pnpm mst-urbox build');
}

async function build() {
  ensureEnvFile();
  await ensureSpeakerModel();
  const py = appPython();
  if (!existsSync(join(ROOT, 'models', 'tts'))) { say('Tải giọng đọc tiếng Việt (khoảng 67 MB)...'); run(py, [join('scripts', 'download_tts.py')], PY_ENV); }
  else say('Giọng đọc tiếng Việt đã có.');
  if (opt['skip-browser']) say('Bỏ qua trình duyệt tra cứu (--skip-browser).');
  else { say('Cài trình duyệt cho tra cứu web (Playwright Chromium)...'); run(py, ['-m', 'playwright', 'install', 'chromium'], PY_ENV, { allowFail: true }); }
  say('Tự kiểm tra ứng dụng...');
  run(py, ['-c', 'import meeting.app; print("Ứng dụng nạp được: OK")'], { ...PY_ENV, MEETING_DB: 'mock' });
  say('Xong. Chạy ứng dụng: pnpm mst-urbox web');
}

async function web() {
  const py = appPython();
  if (!existsSync(join(ROOT, '.env'))) ensureEnvFile();
  if (!existsSync(SPEAKER.path)) say('Chưa có model nhận diện giọng: chạy pnpm mst-urbox build để tự tách người nói bằng giọng.');
  const host = opt.host || '127.0.0.1';
  let port = Number(opt.port || process.env.PORT || 8080);
  if (await portBusy(port, host)) {
    const url = `http://127.0.0.1:${port}`;
    if (await health(url)) { say(`Ứng dụng đang chạy sẵn ở ${url}, mở trình duyệt.`); openBrowser(url); return; }
    while (await portBusy(++port, host));
    say(`Cổng ${opt.port || 8080} đang bận, dùng cổng ${port}.`);
  }
  say(`Khởi động ở http://127.0.0.1:${port} ...`);
  await serve(py, ['-m', 'uvicorn', 'meeting.app:app', '--host', host, '--port', String(port)], port);
}

async function demo() {
  const py = appPython();
  const port = Number(opt.port || 8090);
  await serve(py, [join('scripts', 'demo_replay.py'), '--port', String(port), '--loop'], port);
}

function test() {
  const py = appPython();
  process.exit(run(py, ['-m', 'unittest', 'discover', '-s', 'tests', '-t', '.'], { ...PY_ENV, MEETING_DB: 'mock' }, { allowFail: true }));
}

function doctor() {
  const rows = [];
  const add = (ok, name, detail) => rows.push(`${ok ? '\x1b[32m✓\x1b[0m' : '\x1b[33m!\x1b[0m'} ${name.padEnd(26)} ${detail}`);
  add(true, 'Node.js', process.versions.node);
  const py = appPython(false);
  const pv = py ? capture(py, ['-c', 'import sys; print(sys.version.split()[0])']) : null;
  add(!!pv, 'Python', py ? `${pv || 'không chạy được'} (${relative(ROOT, py)})` : 'chưa có: pnpm mst-urbox install');
  if (py && pv) {
    const miss = capture(py, ['-c', 'import importlib.util as u; m=[x for x in ["fastapi","uvicorn","sherpa_onnx","numpy","pymongo","mongomock","anthropic","websockets","playwright","pypdfium2","docx","pptx"] if not u.find_spec(x)]; print(",".join(m))']);
    add(miss === '', 'Thư viện Python', miss === '' ? 'đủ' : `thiếu: ${miss || '?'} (pnpm mst-urbox install)`);
  }
  const sm = existsSync(SPEAKER_V2.path) && statSync(SPEAKER_V2.path).size === SPEAKER_V2.size;
  add(sm, 'Model nhận diện giọng', sm ? 'ERes2NetV2 (models/)' : 'chưa có ERes2NetV2: pnpm mst-urbox build');
  add(existsSync(join(ROOT, 'models', 'tts')), 'Giọng đọc tiếng Việt', existsSync(join(ROOT, 'models', 'tts')) ? 'models/tts' : 'chưa có: pnpm mst-urbox build');
  const envPath = join(ROOT, '.env');
  if (existsSync(envPath)) {
    const text = readFileSync(envPath, 'utf-8');
    const has = (k) => new RegExp(`^\\s*${k}\\s*=\\s*["']?[^\\s"'#]`, 'm').test(text) && !new RegExp(`^\\s*${k}\\s*=.*(xxxx|your-|<user>)`, 'm').test(text);
    add(true, '.env', SECRET_KEYS.slice(0, 4).map((k) => `${k}: ${has(k) ? 'có' : 'trống'}`).join(', '));
  } else add(false, '.env', 'chưa có: pnpm mst-urbox build (hoặc điền trong Cài đặt sau khi chạy web)');
  for (const [name, pkg] of [['Claude Code', '@anthropic-ai/claude-code'], ['Codex CLI', '@openai/codex'], ['Gemini CLI', '@google/gemini-cli']]) {
    const local = existsSync(join(ROOT, 'node_modules', ...pkg.split('/')));
    add(local, name, local ? 'cài riêng cho dự án' : 'chưa cài trong dự án (pnpm mst-urbox install), có thể đang dùng bản cài toàn cục');
  }
  console.log(rows.join('\n'));
}

function help() {
  console.log(`UrBox Meeting Copilot - bộ lệnh cài và chạy

  pnpm mst-urbox install   Tạo .venv, cài thư viện Python và Claude Code / Codex / Gemini CLI cho dự án
                           (--skip-cli: bỏ CLI, --python <đường dẫn>: chọn Python)
  pnpm mst-urbox build     Tạo .env, tải model nhận diện giọng, giọng đọc, trình duyệt tra cứu, tự kiểm tra
                           (--skip-browser: bỏ Playwright Chromium)
  pnpm mst-urbox web       Chạy ứng dụng ở http://127.0.0.1:8080 và mở trình duyệt (--port, --no-open)
  pnpm mst-urbox demo      Chạy bản demo dữ liệu giả lập ở cổng 8090
  pnpm mst-urbox test      Chạy bộ test offline
  pnpm mst-urbox doctor    Kiểm tra môi trường

Sau khi chạy web: Cài đặt > Kết nối dịch vụ (Soniox, MongoDB) và Nguồn AI (dán API key, hoặc bấm Kết nối
để đăng nhập Claude.ai / ChatGPT / Gemini).`);
}

const COMMANDS = { install, build, web, demo, test, doctor, help };
const self = (x) => (WIN ? resolve(x).toLowerCase() : resolve(x));
if (process.argv[1] && self(process.argv[1]) === self(fileURLToPath(import.meta.url))) {
  const fn = COMMANDS[cmd.toLowerCase()];
  if (!fn) { help(); fail(`Không có lệnh "${cmd}".`); }
  Promise.resolve(fn()).catch((e) => fail(e.stack || String(e)));
}
