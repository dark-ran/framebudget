// FrameBudget 빌드를 헤드리스로 띄워 CDP로 동작을 확인합니다.
// 사용법: node headless_check.mjs <chrome 실행 파일> [--no-sandbox]
// Node 22 이상(내장 WebSocket·fetch)만 있으면 됩니다.
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {readFileSync, mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

const [chrome, ...extra] = process.argv.slice(2);
if (!chrome) { console.error('사용법: node headless_check.mjs <chrome> [--no-sandbox]'); process.exit(2); }
const here = dirname(fileURLToPath(import.meta.url));
const sleep = ms => new Promise(r => setTimeout(r, ms));

const server = createServer((req, res) => {
  const name = req.url.slice(1);
  if (!['raf.html', 'css.html'].includes(name)) { res.writeHead(404).end(); return; }
  res.writeHead(200, {'content-type': 'text/html'}).end(readFileSync(join(here, name)));
}).listen(0, '127.0.0.1');
await new Promise(r => server.once('listening', r));
const base = `http://127.0.0.1:${server.address().port}`;

async function launch(flags) {
  const profile = mkdtempSync(join(tmpdir(), 'fb-check-'));
  const proc = spawn(chrome, ['--headless=new', '--no-first-run', '--no-default-browser-check',
    `--user-data-dir=${profile}`, '--remote-debugging-port=0', ...extra, ...flags, 'about:blank'],
    {stdio: ['ignore', 'ignore', 'pipe']});
  const port = await new Promise((resolve, reject) => {
    let buf = '';
    proc.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on ws:\/\/[^:]+:(\d+)/); if (m) resolve(m[1]); });
    proc.on('exit', c => reject(new Error(`chrome 종료(${c}): ${buf.slice(-500)}`)));
  });
  const exited = new Promise(r => proc.once('exit', r));
  // 브라우저가 프로필을 다 쓰고 끝난 뒤에 지웁니다.
  return {port, close: async () => { proc.kill(); await exited; rmSync(profile, {recursive: true, force: true, maxRetries: 10, retryDelay: 300}); }};
}

async function open(port, url) {
  const t = await (await fetch(`http://127.0.0.1:${port}/json/new?${url}`, {method: 'PUT'})).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  let id = 0; const pend = new Map(), handlers = [];
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id) pend.get(m.id)?.(m); else handlers.forEach(h => h(m)); };
  const send = (method, params = {}) => new Promise(r => { pend.set(++id, r); ws.send(JSON.stringify({id, method, params})); });
  await send('Page.enable');
  await sleep(1500);
  return {send, on: h => handlers.push(h), close: () => ws.close()};
}

const evalJ = async (p, expr) => (await p.send('Runtime.evaluate', {expression: expr, returnByValue: true})).result.result.value;

// raf.html이 1초마다 쌓는 rAF 횟수를 sec초 동안 모읍니다.
async function rafPerSec(p, sec, during) {
  await evalJ(p, 'window.counts=[]');
  const task = during?.();
  await sleep(sec * 1000 + 100);
  await task;
  return evalJ(p, 'window.counts');
}

// 화면 캡처 중에는 설계상 제한이 풀리므로 screencast 대신 tracing으로 DrawFrame 수를 셉니다.
async function drawFramesPerSec(p, sec) {
  let count = 0, done;
  const fin = new Promise(r => done = r);
  p.on(m => {
    if (m.method === 'Tracing.dataCollected') count += m.params.value.filter(e => e.name === 'DrawFrame').length;
    if (m.method === 'Tracing.tracingComplete') done();
  });
  await p.send('Tracing.start', {traceConfig: {includedCategories: ['disabled-by-default-devtools.timeline.frame']}, transferMode: 'ReportEvents'});
  await sleep(sec * 1000);
  await p.send('Tracing.end');
  await fin;
  return +(count / sec).toFixed(1);
}

async function run(label, flags, withInput, withCss = true) {
  const b = await launch(flags);
  const out = {label};
  try {
    const p = await open(b.port, `${base}/raf.html`);
    out.raf = await rafPerSec(p, 3);
    if (withInput) {
      const wheel = async () => {
        const end = Date.now() + 2000;
        while (Date.now() < end) {
          await p.send('Input.dispatchMouseEvent', {type: 'mouseWheel', x: 50, y: 50, deltaX: 0, deltaY: 10});
          await sleep(50);
        }
      };
      out.raf_during_wheel = await rafPerSec(p, 2, wheel);
      await sleep(1500);
      out.raf_after_wheel = await rafPerSec(p, 2);
    }
    p.close();
    if (withCss) {
      const c = await open(b.port, `${base}/css.html`);
      out.css_draw_frames = await drawFramesPerSec(c, 3);
      c.close();
    }
  } finally {
    await b.close();
  }
  return out;
}

const results = [
  await run('제한 없음', [], false),
  await run('127.0.0.1=15', ['--framebudget-sites=127.0.0.1=15'], true),
  await run('127.0.0.1=15, 입력 시 해제 끔',
            ['--framebudget-sites=127.0.0.1=15', '--framebudget-no-input-boost=127.0.0.1'], true, false),
];
console.log(JSON.stringify(results, null, 1));

// 자체 점검: 기대 범위를 벗어나면 종료 코드 1
const [free, fb, noBoost] = results;
const median = a => [...a].sort((x, y) => x - y)[a.length >> 1];
const checks = {
  '제한 없음 rAF ≈ 60': median(free.raf) >= 55,
  '15FPS rAF ≈ 15': Math.abs(median(fb.raf) - 15) <= 2,
  '휠 입력 중 해제(rAF ≥ 55)': Math.max(...fb.raf_during_wheel) >= 55,
  '입력 멈춘 뒤 복귀(rAF ≈ 15)': Math.abs(median(fb.raf_after_wheel) - 15) <= 2,
  '15FPS CSS DrawFrame ≈ 15': Math.abs(fb.css_draw_frames - 15) <= 2,
  '제한 없음 CSS DrawFrame ≈ 60': free.css_draw_frames >= 55,
  '입력 시 해제 끈 사이트는 휠 입력 중에도 rAF ≈ 15': Math.max(...noBoost.raf_during_wheel) <= 17,
};
for (const [k, v] of Object.entries(checks)) console.log(`${v ? 'PASS' : 'FAIL'} ${k}`);
server.close();
process.exit(Object.values(checks).every(Boolean) ? 0 : 1);
