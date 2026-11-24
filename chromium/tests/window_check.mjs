// FrameBudget 빌드를 일반 창으로 띄워, 실제 키 입력(xdotool)과 CDP로 여러 상황을 확인합니다.
// 사용법 (가상 화면 예):
//   Xvfb :99 -screen 0 1400x800x24 & DISPLAY=:99 node window_check.mjs <chrome> [--no-sandbox]
// 필요: Node 22 이상, X 화면(DISPLAY), xdotool
// FB_NO_KEYS=1이면 클릭·키 입력이 필요한 시험을 건너뜁니다. 실제 화면에서는 창 위치를
// 데스크톱 환경이 정하므로, 좌표로 클릭하지 않도록 이 옵션을 씁니다.
import {spawn, execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {readFileSync, mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

const [chrome, ...extra] = process.argv.slice(2);
if (!chrome || !process.env.DISPLAY) {
  console.error('사용법: DISPLAY=:99 node window_check.mjs <chrome> [--no-sandbox]');
  process.exit(2);
}
const here = dirname(fileURLToPath(import.meta.url));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const keys = !process.env.FB_NO_KEYS;
// FB_LOG=1이면 FrameBudget 로그를 시험 시작부터의 시각과 함께 출력합니다.
const t0 = Date.now();
const ts = () => ((Date.now() - t0) / 1000).toFixed(1);

// 127.0.0.1과 localhost를 서로 다른 사이트로 쓰기 위해 IPv4·IPv6 모두에서 받습니다.
const server = createServer((req, res) => {
  const name = req.url.slice(1).split('?')[0];
  if (!['raf.html', 'css.html', 'frame.html', 'video.html', 'test.webm'].includes(name)) { res.writeHead(404).end(); return; }
  res.writeHead(200, {'content-type': name.endsWith('.webm') ? 'video/webm' : 'text/html'}).end(readFileSync(join(here, name)));
}).listen(0, '::');
await new Promise(r => server.once('listening', r));
const port = server.address().port;
const A = `http://127.0.0.1:${port}`, B = `http://localhost:${port}`;

const WIDTH = 680;  // 창 두 개를 겹치지 않게 나란히 둡니다.

async function launch(flags) {
  const profile = mkdtempSync(join(tmpdir(), 'fb-window-'));
  const proc = spawn(chrome, ['--ozone-platform=x11', '--no-first-run', '--no-default-browser-check',
    `--user-data-dir=${profile}`, '--remote-debugging-port=0', `--window-size=${WIDTH},760`,
    '--window-position=0,0', ...extra, ...flags,
    ...(process.env.FB_LOG ? ['--enable-logging=stderr', '--vmodule=framebudget_tab_helper=1'] : []),
    'about:blank'], {stdio: ['ignore', 'ignore', 'pipe']});
  if (process.env.FB_LOG) {
    proc.stderr.on('data', d => d.toString().split('\n').filter(l => l.includes('FrameBudget:'))
        .forEach(l => console.log(`${ts()} LOG ${l.replace(/^.*\] /, '')}`)));
  }
  const devtools = await new Promise((resolve, reject) => {
    let buf = '';
    proc.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on (ws:\/\/\S+)/); if (m) resolve(m[1]); });
    proc.on('exit', c => reject(new Error(`chrome 종료(${c}): ${buf.slice(-500)}`)));
  });
  const ws = new WebSocket(devtools);
  await new Promise(r => ws.onopen = r);
  let id = 0; const pend = new Map(), handlers = [];
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id) pend.get(m.id)?.(m); else handlers.forEach(h => h(m)); };
  // 응답이 없으면 멈추지 않고 실패로 넘어갑니다.
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const myId = ++id;
    const timer = setTimeout(() => { pend.delete(myId); reject(new Error(`${method}: 15초 동안 응답 없음`)); }, 15000);
    pend.set(myId, m => { clearTimeout(timer); m.error ? reject(new Error(`${method}: ${m.error.message}`)) : resolve(m.result); });
    ws.send(JSON.stringify({id: myId, method, params, ...(sessionId && {sessionId})}));
  });
  const exited = new Promise(r => proc.once('exit', r));
  return {
    send, on: h => handlers.push(h),
    close: async () => {
      ws.close(); proc.kill(); await exited;
      // 하위 프로세스가 늦게 끝나 지우지 못해도 시험 결과에는 영향이 없습니다.
      try { rmSync(profile, {recursive: true, force: true, maxRetries: 10, retryDelay: 500}); }
      catch (e) { console.warn(`임시 프로필을 지우지 못함: ${profile}`); }
    },
  };
}

// 페이지 하나를 다루는 도우미. slot은 창 위치(0 왼쪽, 1 오른쪽)입니다.
async function attach(b, targetId, slot) {
  const {sessionId} = await b.send('Target.attachToTarget', {targetId, flatten: true});
  const s = (method, params) => b.send(method, params, sessionId);
  await s('Page.enable');
  const {windowId} = await b.send('Browser.getWindowForTarget', {targetId});
  await b.send('Browser.setWindowBounds', {windowId, bounds: {left: slot * (WIDTH + 20), top: 0, width: WIDTH, height: 760}});
  const page = {
    targetId, slot,
    eval: async (expr, userGesture = false) =>
      (await s('Runtime.evaluate', {expression: expr, returnByValue: true, userGesture, awaitPromise: true})).result.value,
    goto: async url => { await s('Page.navigate', {url}); await sleep(2000); },
    reload: async () => { await s('Page.reload'); await sleep(2000); },
    // Page.crash는 렌더러가 죽어서 응답이 오지 않으므로 기다리지 않습니다.
    crash: () => { s('Page.crash').catch(() => {}); },
    // CDP로 마우스 이동을 보냅니다(실제 마우스는 쓰지 않습니다).
    mouseMove: () => s('Input.dispatchMouseEvent', {type: 'mouseMoved', x: 100 + Math.random() * 50, y: 100}),
    // 창 가운데를 눌러 키 입력을 받게 합니다. 클릭도 입력이라 잠시 기다립니다.
    focus: async () => { xdo('mousemove', String(slot * (WIDTH + 20) + WIDTH / 2), '450', 'click', '1'); await sleep(1500); },
    // sec초 동안 초당 rAF 수. key를 주면 그동안 그 키를 계속 누릅니다.
    raf: async (sec = 2, {key, counts = 'counts'} = {}) => {
      // 페이지의 카운터(n)도 0으로 맞춰, 첫 구간에 측정 전 프레임이 섞이지 않게 합니다.
      await page.eval(`window.${counts}=[];${counts === 'counts' ? 'n=0;' : ''}`);
      if (key) xdo('key', '--delay', '50', '--repeat', String(sec * 20), key);
      else await sleep(sec * 1000 + 150);
      if (key) await sleep(300);
      return page.eval(`window.${counts}`);
    },
  };
  return page;
}

async function newWindow(b, url, slot) {
  const {targetId} = await b.send('Target.createTarget', {url, newWindow: true});
  await sleep(1500);
  return attach(b, targetId, slot);
}

function xdo(...args) { execFileSync('xdotool', args, {stdio: 'ignore'}); }
const median = a => [...a].sort((x, y) => x - y)[a.length >> 1];
// FPS 상한 판정: 상한을 넘지 않고(측정 오차 1 허용), 화면 주기 때문에 낮아지더라도 상한의 75% 이상.
// 예: 100Hz 화면에서 30FPS 상한은 10ms 단위로 맞춰져 25FPS가 됩니다.
const near = (a, fps) => a.length > 0 && Math.max(...a) <= fps + 1 && median(a) >= fps * 0.75;

const results = [];
function check(name, ok, data) {
  results.push(ok);
  console.log(`${process.env.FB_LOG ? ts() + ' ' : ''}${ok ? 'PASS' : 'FAIL'} ${name}  ${JSON.stringify(data)}`);
}

// ---- 1회차: 실행 옵션 규칙 ----
{
  // 키 입력 시험을 건너뛸 때는(실제 화면) 바깥 입력이 결과를 흔들지 않게 두 사이트 모두 입력 시 해제를 끕니다.
  const b = await launch(['--framebudget-sites=127.0.0.1=15,localhost=30',
                          `--framebudget-no-input-boost=${keys ? 'localhost' : 'localhost,127.0.0.1'}`]);
  try {
    const first = (await b.send('Target.getTargets')).targetInfos.find(t => t.type === 'page');
    const pa = await attach(b, first.targetId, 0);
    await pa.goto(`${A}/raf.html`);
    const pb = await newWindow(b, `${B}/raf.html`, 1);
    await sleep(1500);

    // 1. 두 창을 동시에 잽니다.
    const [ra, rb] = await Promise.all([pa.raf(3), pb.raf(3)]);
    check('1. 탭별 동시 제한: 127.0.0.1=15', near(ra, 15), ra);
    check('1. 탭별 동시 제한: localhost=30', near(rb, 30), rb);

    // 2. 실제 키 입력
    if (keys) {
    await pa.focus();
    const ka = await pa.raf(2, {key: 'a'});
    check('2. 키 입력 중 해제(127.0.0.1): 최대 ≥ 55', Math.max(...ka) >= 55, ka);
    await pb.focus();
    const kb = await pb.raf(2, {key: 'a'});
    check('2. 입력 시 해제 끈 사이트(localhost)는 키 입력 중에도 30', near(kb, 30) && Math.max(...kb) <= 31, kb);
    }

    // 3. 다른 사이트로 이동했다가 돌아오기
    await pa.goto(`${B}/raf.html`);
    const nav1 = await pa.raf(2);
    check('3. 127.0.0.1 → localhost 이동 후 30', near(nav1, 30), nav1);
    await pa.goto(`${A}/raf.html`);
    const nav2 = await pa.raf(2);
    check('3. 다시 127.0.0.1로 돌아와 15', near(nav2, 15), nav2);

    // 4. 다른 출처 iframe은 탭 규칙을 따릅니다.
    await pa.goto(`${A}/frame.html?src=${encodeURIComponent(`${B}/raf.html`)}`);
    const fr1 = await pa.raf(3, {counts: 'childCounts'});
    check('4. 127.0.0.1(15) 탭 안의 localhost iframe도 15', near(fr1, 15), fr1);
    await pa.goto(`${B}/frame.html?src=${encodeURIComponent(`${A}/raf.html`)}`);
    const fr2 = await pa.raf(3, {counts: 'childCounts'});
    check('4. localhost(30) 탭 안의 127.0.0.1 iframe은 30', near(fr2, 30), fr2);

    // 6. 새로고침
    await pa.goto(`${A}/raf.html`);
    await pa.reload();
    const rl = await pa.raf(2);
    check('6. 새로고침 뒤에도 15', near(rl, 15), rl);

    // 7. 렌더러가 죽은 뒤 다시 불러오기
    await pa.crash();
    await sleep(1000);
    await pa.goto(`${A}/raf.html`);
    const cr = await pa.raf(2);
    check('7. 렌더러 크래시 후 다시 불러와도 15', near(cr, 15), cr);

    // 5. Alt+Shift+F 탭 고정 뒤 다른 사이트로 이동
    if (keys) {
    await pa.focus();
    xdo('key', 'alt+shift+f');
    await sleep(2000);
    const ov = await pa.raf(2);
    check('5. Alt+Shift+F 한 번 → 60 고정', near(ov, 60), ov);
    await pa.goto(`${B}/raf.html`);
    const ovNav = await pa.raf(2);
    check('5. 고정한 탭은 다른 사이트로 가도 60', near(ovNav, 60), ovNav);
    // 단축키를 누르는 것은 입력 시 해제를 일으키지 않아야 합니다.
    xdo('key', 'alt+shift+f');
    await sleep(300);
    const ovKey = await pa.raf(2);
    check('5. 단축키 직후에도 제한이 풀리지 않음 (60 → 30)', near(ovKey, 30), ovKey);
    }

    // 6. window.open 팝업은 자기 사이트 규칙을 따릅니다.
    await pb.goto(`${A}/raf.html`);
    const before = new Set((await b.send('Target.getTargets')).targetInfos.map(t => t.targetId));
    // 사용자 동작 없이 연 팝업은 차단되므로 userGesture로 엽니다.
    await pb.eval(`void window.open(location.href, '_blank', 'popup,left=0,top=0,width=${WIDTH},height=400')`, true);
    await sleep(2500);
    const popup = (await b.send('Target.getTargets')).targetInfos.find(t => t.type === 'page' && !before.has(t.targetId));
    if (popup) {
      await pa.goto('about:blank');  // 팝업과 겹치는 왼쪽 창은 비워 둡니다.
      const pp = await attach(b, popup.targetId, 0);
      const rp = await pp.raf(2);
      check('6. window.open 팝업(127.0.0.1)도 15', near(rp, 15), rp);
    } else {
      check('6. window.open 팝업(127.0.0.1)도 15', false, '팝업을 찾지 못함');
    }
  } finally {
    await b.close();
  }
}

// ---- 2회차: 설정 화면으로 바꾸는 규칙 ----
{
  const b = await launch([]);
  try {
    const first = (await b.send('Target.getTargets')).targetInfos.find(t => t.type === 'page');
    const pa = await attach(b, first.targetId, 0);
    await pa.goto(`${A}/raf.html`);
    const ui = await newWindow(b, 'chrome://framebudget', 1);
    const sendUi = async (msg, args) => { await ui.eval(`chrome.send(${JSON.stringify(msg)}, ${JSON.stringify(args)})`); await sleep(1500); };

    const r0 = await pa.raf(2);
    check('8. 규칙 없음: 제한 없음(≥ 55)', median(r0) >= 55, r0);
    // 규칙 없이 계속 그리는 탭에는 추천이 뜨고, 추천 버튼으로 규칙을 만들 수 있습니다.
    await sleep(3000);
    const rec = await ui.eval(`document.getElementById('recommend').innerText`);
    check('15. 자동 추천: 규칙 없이 계속 그리는 127.0.0.1을 추천', rec.includes('127.0.0.1'), rec);
    await ui.eval(`[...document.querySelectorAll('#recommend button')].find(b => b.textContent.startsWith('15FPS')).click()`);
    await sleep(1500);
    const r1 = await pa.raf(2);
    check('8. 추천 버튼으로 127.0.0.1=15 추가 → 열린 탭에 바로 15', near(r1, 15), r1);
    await sendUi('setDefaultFps', [30]);
    await pa.goto(`${B}/raf.html`);
    const r2 = await pa.raf(2);
    check('8. 기본값 30 → 규칙 없는 localhost는 30', near(r2, 30), r2);
    await pa.goto(`${A}/raf.html`);
    await sendUi('removeSiteRule', ['127.0.0.1']);
    const r3 = await pa.raf(2);
    check('8. 127.0.0.1 규칙 삭제 → 열린 탭이 기본값 30으로', near(r3, 30), r3);
    if (keys) {
      await sendUi('setInputBoost', [false]);
      await pa.focus();
      const r4 = await pa.raf(2, {key: 'a'});
      check('8. 전체 입력 시 해제를 끄면 키 입력 중에도 30', near(r4, 30) && Math.max(...r4) <= 31, r4);
    }
    const state = await ui.eval(`(() => [...document.querySelectorAll('#tabs tr')].map(tr => tr.innerText.replace(/\\s+/g, ' ')))()`);
    const savedRow = state.find(t => t.includes('raf')) || '';
    const [, drawnText = '0', skippedText = '0'] = savedRow.match(/([\d,]+) \/ ([\d,]+)/) || [];
    const drawn = Number(drawnText.replace(/,/g, '')), skipped = Number(skippedText.replace(/,/g, ''));
    check('13. 측정: 제한된 탭은 그린 프레임과 제한으로 건너뛴 프레임이 모두 0보다 큼', drawn > 0 && skipped > 0, {drawn, skipped});
    const exportJson = () => ui.eval(`new Promise(r => { framebudget.onExport = r; chrome.send('exportSettings'); })`);
    const exported = JSON.parse(await exportJson());
    check('14. 내보내기: 기본값 30이 들어 있음', exported.default_fps === 30, exported.default_fps);
    await ui.eval(`chrome.send('importSettings', [${JSON.stringify(JSON.stringify({default_fps: 5, sites: {'Example.COM/x': 20}, low_res_sites: ['https://A.com/p', 'a.com']}))}])`);
    await sleep(1000);
    const imported = JSON.parse(await exportJson());
    check('14. 가져오기: 기본값 5, 주소는 정리되어 example.com=20, 저해상도 a.com 하나',
          imported.default_fps === 5 && imported.sites['example.com'] === 20 &&
          JSON.stringify(imported.low_res_sites) === '["a.com"]', imported);
    check('8. 설정 화면의 열린 탭 표에 raf 탭이 30 FPS 기본값으로 표시', state.some(t => t.includes('raf') && t.includes('30 FPS') && t.includes('기본값')), state);
  } finally {
    await b.close();
  }
}

// ---- 3회차: 배터리 사용 중 기본값 ----
{
  const b = await launch(['--framebudget-assume-battery', '--framebudget-battery-default-fps=20',
                          '--framebudget-sites=localhost=30', '--framebudget-low-res=127.0.0.1']);
  try {
    const first = (await b.send('Target.getTargets')).targetInfos.find(t => t.type === 'page');
    const pa = await attach(b, first.targetId, 0);
    await pa.goto(`${A}/raf.html`);
    const r1 = await pa.raf(2);
    check('9. 배터리: 규칙 없는 127.0.0.1은 배터리 기본값 20', near(r1, 20), r1);
    await pa.goto(`${B}/raf.html`);
    const r2 = await pa.raf(2);
    check('9. 배터리: 규칙 있는 localhost는 규칙값 30', near(r2, 30), r2);

    // 저해상도 모드는 페이지가 보는 devicePixelRatio를 절반으로 낮춥니다.
    const dprB = await pa.eval('devicePixelRatio');
    await pa.goto(`${A}/raf.html`);
    const dprA = await pa.eval('devicePixelRatio');
    check('11. 저해상도: 127.0.0.1은 devicePixelRatio 0.5', dprA === 0.5, dprA);
    check('11. 저해상도: 지정하지 않은 localhost는 1', dprB === 1, dprB);
  } finally {
    await b.close();
  }
}

// ---- 4회차: 비활성 창 ----
// 클릭 대신 Target.activateTarget으로 창을 활성화하므로 실제 화면에서도 돌립니다.
{
  const b = await launch(['--framebudget-inactive-window-fps=10']);
  try {
    const first = (await b.send('Target.getTargets')).targetInfos.find(t => t.type === 'page');
    const pa = await attach(b, first.targetId, 0);
    await pa.goto(`${A}/raf.html`);
    const pb = await newWindow(b, `${B}/raf.html`, 1);
    await b.send('Target.activateTarget', {targetId: pa.targetId});
    await sleep(1500);
    const [a1, b1] = await Promise.all([pa.raf(2), pb.raf(2)]);
    check('10. 왼쪽 창 활성: 왼쪽은 제한 없음(≥ 55)', median(a1) >= 55, a1);
    check('10. 왼쪽 창 활성: 오른쪽 비활성 창은 10', near(b1, 10), b1);
    await b.send('Target.activateTarget', {targetId: pb.targetId});
    await sleep(1500);
    const [a2, b2] = await Promise.all([pa.raf(2), pb.raf(2)]);
    check('10. 오른쪽 창 활성: 왼쪽 비활성 창은 10', near(a2, 10), a2);
    check('10. 오른쪽 창 활성: 오른쪽은 제한 없음(≥ 55)', median(b2) >= 55, b2);
  } finally {
    await b.close();
  }
}

// ---- 5회차: 영상 재생 중 제한 해제 ----
{
  const b = await launch(['--framebudget-sites=127.0.0.1=15', '--framebudget-lift-during-video']);
  try {
    const first = (await b.send('Target.getTargets')).targetInfos.find(t => t.type === 'page');
    const pa = await attach(b, first.targetId, 0);
    await pa.goto(`${A}/video.html`);
    const playing = await pa.raf(2);
    // 가상 화면에서는 영상 재생만으로도 30FPS 안팎이 한계라, 15 상한보다 확실히 높은지만 봅니다.
    check('12. 영상 재생 중에는 제한 해제(15 상한보다 높음, ≥ 20)', median(playing) >= 20, playing);
    await pa.eval('document.getElementById("v").pause()');
    await sleep(1500);
    const paused = await pa.raf(2);
    check('12. 영상을 멈추면 다시 15', near(paused, 15), paused);
  } finally {
    await b.close();
  }
}

// ---- 6회차: 입력이 없는 탭 ----
{
  const b = await launch(['--framebudget-idle-fps=10', '--framebudget-idle-seconds=3']);
  try {
    const first = (await b.send('Target.getTargets')).targetInfos.find(t => t.type === 'page');
    const pa = await attach(b, first.targetId, 0);
    await pa.goto(`${A}/raf.html`);
    await pa.mouseMove();
    const active = await pa.raf(1);
    check('16. 입력 직후: 제한 없음(≥ 55)', median(active) >= 55, active);
    await sleep(3500);
    const idle = await pa.raf(2);
    check('16. 3초 넘게 입력 없음: 10', near(idle, 10), idle);
    await pa.mouseMove();
    await sleep(300);
    const back = await pa.raf(2);
    check('16. 마우스를 움직이면 다시 제한 없음(≥ 55)', median(back) >= 55, back);
  } finally {
    await b.close();
  }
}

server.close();
const failed = results.filter(ok => !ok).length;
console.log(`\n${results.length - failed}/${results.length} 통과`);
process.exit(failed ? 1 : 0);
