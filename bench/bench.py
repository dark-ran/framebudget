"""FrameBudget 1단계 실험 하네스.

같은 테스트 장면을 FPS 제한 조건만 바꿔 번갈아 실행하면서
CPU 패키지 전력(Windows Energy Meter = Intel RAPL)과 페이지가 실제로 그린 프레임 수를 기록한다.

예)
  python bench.py --browser chrome --method js --caps 0,30,15
  python bench.py --browser firefox --method pref --caps 0,30,15
  python bench.py --browser chrome --exe C:/src/chromium/src/out/FrameBudget/chrome.exe --method budget --caps 0,30,15
"""
import argparse
import csv
import ctypes
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"

BROWSER_PATHS = {
    "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
               r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
    "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
             r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
    "firefox": [r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
                r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"],
}

# Energy Meter의 Power 값 단위는 mW
POWER_COUNTERS = {
    "pkg_w": r"\Energy Meter(rapl_package0_pkg)\Power",   # SoC 패키지 전체
    "cores_w": r"\Energy Meter(rapl_package0_pp0)\Power",  # CPU 코어
    "gfx_w": r"\Energy Meter(rapl_package0_pp1)\Power",    # 내장 GPU
}
CPU_COUNTER = r"\Processor Information(_Total)\% Processor Utility"
GPU_COUNTER = r"\GPU Engine(*engtype_3D)\Utilization Percentage"

# Linux: RAPL은 /sys/class/powercap, CPU 사용률은 /proc/stat에서 읽는다
RAPL_DIR = Path("/sys/class/powercap")
RAPL_DOMAINS = {"pkg_w": "intel-rapl:0", "cores_w": "intel-rapl:0:0", "gfx_w": "intel-rapl:0:1"}
LINUX_BINARIES = {"chrome": ["google-chrome", "chromium"], "edge": ["microsoft-edge"], "firefox": ["firefox"]}
SNAP_FIREFOX = Path("/snap/firefox/current/usr/lib/firefox/firefox")

FIREFOX_PREFS = {
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "datareporting.policy.dataSubmissionEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.sessionstore.resume_from_crash": False,
    "widget.windows.window_occlusion_tracking.enabled": False,
}


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_ac_power():
    if not IS_WINDOWS:
        mains = [p for p in Path("/sys/class/power_supply").glob("*")
                 if (p / "type").read_text().strip() == "Mains"]
        return not mains or any((p / "online").read_text().strip() == "1" for p in mains)
    s = _PowerStatus()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    return s.ACLineStatus == 1


def find_browser(name):
    if not IS_WINDOWS:
        # /usr/bin/firefox 래퍼는 기존 인스턴스에 붙으려 해서 측정이 멈출 수 있으므로 snap 실행 파일을 먼저 쓴다
        if name == "firefox" and SNAP_FIREFOX.exists():
            return str(SNAP_FIREFOX)
        for binary in LINUX_BINARIES[name]:
            if shutil.which(binary):
                return shutil.which(binary)
        raise SystemExit(f"{name} 실행 파일을 찾지 못했습니다: {LINUX_BINARIES[name]}")
    for p in BROWSER_PATHS[name]:
        path = os.path.expandvars(p)
        if os.path.exists(path):
            return path
    raise SystemExit(f"{name} 실행 파일을 찾지 못했습니다: {BROWSER_PATHS[name]}")


def file_version(path):
    if not IS_WINDOWS:
        return subprocess.run([path, "--version"], capture_output=True, text=True).stdout.strip()
    cmd = ["powershell", "-NoProfile", "-Command", f"(Get-Item '{path}').VersionInfo.ProductVersion"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


# ---- 페이지 결과 수신 서버 ----

class Inbox:
    def __init__(self):
        self.lock = threading.Lock()
        self.run_id = None
        self.begin = threading.Event()
        self.result = threading.Event()
        self.payload = None

    def expect(self, run_id):
        with self.lock:
            self.run_id = run_id
            self.payload = None
            self.begin.clear()
            self.result.clear()

    def deliver(self, run_id, kind, payload):
        with self.lock:
            if run_id != self.run_id:
                return  # 이전 실행에서 늦게 도착한 요청
            if kind == "begin":
                self.begin.set()
            elif kind == "result":
                self.payload = payload
                self.result.set()


class Handler(SimpleHTTPRequestHandler):
    inbox = None

    def do_POST(self):
        url = urlparse(self.path)
        run_id = parse_qs(url.query).get("run", [None])[0]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.inbox.deliver(run_id, url.path.strip("/"), json.loads(body) if body else {})
        self.send_response(204)
        self.end_headers()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


# ---- 카운터 샘플링 ----

def _num(cell):
    try:
        return float(cell)
    except ValueError:
        return None


def process_tree(root_pid):
    """root_pid와 그 자손 프로세스의 PID 집합 (브라우저의 GPU·렌더러 프로세스 포함)."""
    children, skip = {}, set()
    if IS_WINDOWS:
        cmd = ["powershell", "-NoProfile", "-Command",
               "Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId | ConvertTo-Json -Compress"]
        query = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        for p in json.loads(query.communicate()[0]):
            children.setdefault(p["ParentProcessId"], []).append(p["ProcessId"])
        skip.add(query.pid)  # 조회에 쓴 PowerShell 자신은 제외
    else:
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                ppid = int(stat.read_text().rsplit(")", 1)[1].split()[1])  # 필드: state, ppid, ...
            except (OSError, IndexError, ValueError):
                continue  # 조회 중 끝난 프로세스
            children.setdefault(ppid, []).append(int(stat.parent.name))
    tree, stack = set(), [root_pid]
    while stack:
        pid = stack.pop()
        if pid not in tree:
            tree.add(pid)
            stack.extend(children.get(pid, []))
    return tree - skip


def _read_energy():
    """도메인별 (누적 에너지 µJ, 카운터 최댓값). 없는 도메인(예: 내장 GPU)은 빠진다."""
    out = {}
    for key, dom in RAPL_DOMAINS.items():
        p = RAPL_DIR / dom
        if (p / "energy_uj").exists():
            out[key] = (int((p / "energy_uj").read_text()), int((p / "max_energy_range_uj").read_text()))
    return out


def _cpu_times():
    f = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    return f[3] + f[4], sum(f)  # idle + iowait, 전체


def sample_counters_linux(seconds):
    """RAPL 에너지 카운터 차이를 1초 간격으로 읽어 평균 W를 낸다.
    GPU 3D 사용률은 Linux에서 공통으로 읽을 방법이 없어 None으로 둔다."""
    series = {k: [] for k in [*RAPL_DOMAINS, "cpu_pct"]}
    try:
        prev, (prev_idle, prev_total) = _read_energy(), _cpu_times()
    except PermissionError:
        raise RuntimeError("RAPL 에너지 카운터를 읽을 수 없습니다. root 권한이 필요합니다. "
                           "sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj 를 실행한 뒤 다시 시도하세요.")
    for _ in range(seconds):
        t0 = time.monotonic()
        time.sleep(1)
        dt = time.monotonic() - t0
        cur = _read_energy()
        for key, (e, rng) in cur.items():
            series[key].append((e - prev[key][0]) % rng / dt / 1e6)  # 카운터가 한 바퀴 돌아도 차이가 맞게 나온다
        idle, total = _cpu_times()
        if total > prev_total:
            series["cpu_pct"].append(100 * (1 - (idle - prev_idle) / (total - prev_total)))
        prev, prev_idle, prev_total = cur, idle, total
    result = {k: round(statistics.mean(v), 3) if v else None for k, v in series.items()}
    result.update(gpu3d_pct=None, gpu3d_other_pct=None, samples=len(series["pkg_w"]))
    return result


def sample_counters(seconds, pids=frozenset()):
    """typeperf로 1초 간격 샘플을 받아 항목별 평균을 낸다.
    GPU 3D 사용률은 pids(측정 대상 브라우저)와 그 밖의 프로세스(배경 소음)로 나눠 합산한다."""
    if not IS_WINDOWS:
        return sample_counters_linux(seconds)
    cmd = ["typeperf", *POWER_COUNTERS.values(), CPU_COUNTER, GPU_COUNTER, "-si", "1", "-sc", str(seconds)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 60).stdout
    rows = list(csv.reader(line for line in out.splitlines() if line.startswith('"')))
    if len(rows) < 2:
        raise RuntimeError(f"typeperf 출력이 비어 있습니다:\n{out}")
    header, data = rows[0], rows[1:]
    series = {k: [] for k in [*POWER_COUNTERS, "cpu_pct", "gpu3d_pct", "gpu3d_other_pct"]}
    for row in data:
        gpu = {"gpu3d_pct": 0.0, "gpu3d_other_pct": 0.0}
        for name, cell in zip(header[1:], row[1:]):
            v = _num(cell)
            if v is None:
                continue
            if "GPU Engine" in name:
                m = re.search(r"pid_(\d+)_", name)
                gpu["gpu3d_pct" if m and int(m.group(1)) in pids else "gpu3d_other_pct"] += v
            elif name.endswith(CPU_COUNTER):
                series["cpu_pct"].append(v)
            else:
                for key, path in POWER_COUNTERS.items():
                    if name.endswith(path):
                        series[key].append(v / 1000)
        for key, v in gpu.items():
            series[key].append(v)
    result = {k: round(statistics.mean(v), 3) if v else None for k, v in series.items()}
    result["samples"] = len(series["pkg_w"])
    return result


# ---- 브라우저 실행 ----

def launch(browser, exe, profile, url, browser_fps, headless=False):
    """browser_fps: 브라우저 수준 FPS 상한 (0은 제한 없음).
    Firefox는 layout.frame_rate, Chromium 계열은 FrameBudget 패치의 --framebudget-default-fps로 건다.
    headless: 창을 띄우지 않는다. 소프트웨어 렌더링이라 GPU 장면의 결과는 실제 화면과 다를 수 있다."""
    if browser == "firefox":
        prefs = {**FIREFOX_PREFS, "layout.frame_rate": browser_fps or -1}
        (profile / "user.js").write_text(
            "".join(f'user_pref("{k}", {json.dumps(v)});\n' for k, v in prefs.items()), encoding="utf-8")
        args = [exe, "-no-remote", "-wait-for-browser", "-profile", str(profile),
                "-headless" if headless else "-kiosk", url]
    else:
        args = [exe, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-sync", "--hide-crash-restore-bubble",
                "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                "--headless=new" if headless else "--kiosk", url]
        if browser_fps:
            args.insert(1, f"--framebudget-default-fps={browser_fps}")
    return subprocess.Popen(args)


def browser_pids():
    # Firefox 런처는 실제 브라우저 프로세스를 띄운 쪽(이 스크립트)의 자식으로 붙이므로,
    # proc 하나가 아니라 이 스크립트의 자손 전체를 측정 대상 브라우저로 본다
    return process_tree(os.getpid()) - {os.getpid()}


def _taskkill(pids, force):
    if not IS_WINDOWS:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass  # 이미 끝났거나 내 소유가 아닌 프로세스
    elif pids:
        subprocess.run(["taskkill", *[a for pid in pids for a in ("/PID", str(pid))], *(["/T", "/F"] if force else [])],
                       capture_output=True)


def close(proc):
    # 먼저 창을 정상 종료해서 다음 실행에 '복원' 안내가 뜨지 않게 한다
    _taskkill(browser_pids(), force=False)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass
    for _ in range(5):
        left = browser_pids()
        if not left:
            return
        time.sleep(1)
    _taskkill(left, force=True)
    time.sleep(2)
    if browser_pids():
        raise SystemExit("브라우저 프로세스가 종료되지 않았습니다. 남은 창을 닫고 다시 실행하세요.")


# ---- 요약 ----

def median(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(vals), 3) if vals else None


def summarize(runs, scenes, labels):
    static_floor = median([r for r in runs if r["scene"] == "static" and r["condition"] == "none"], "pkg_w")
    out = []
    for scene in scenes:
        base = median([r for r in runs if r["scene"] == scene and r["condition"] == "none"], "pkg_w")
        for label in labels:
            rs = [r for r in runs if r["scene"] == scene and r["condition"] == label]
            if not rs:
                continue
            pkg = median(rs, "pkg_w")
            out.append({
                "scene": scene, "condition": label, "n": len(rs),
                "fps": median(rs, "fps"),
                "pkg_w": pkg, "cores_w": median(rs, "cores_w"), "gfx_w": median(rs, "gfx_w"),
                "cpu_pct": median(rs, "cpu_pct"), "gpu3d_pct": median(rs, "gpu3d_pct"),
                "gpu3d_other_pct": median(rs, "gpu3d_other_pct"),
                # 같은 장면의 '제한 없음' 대비 패키지 전력 변화율
                "pkg_vs_none_pct": round((pkg - base) / base * 100, 1) if base and pkg else None,
                # 정적 문서(바닥 전력) 위에 이 장면이 추가로 쓰는 전력
                "extra_w": round(pkg - static_floor, 3) if static_floor is not None and pkg else None,
            })
    return out


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def print_table(rows):
    cols = ["scene", "condition", "n", "fps", "pkg_w", "gfx_w", "cores_w", "gpu3d_pct", "gpu3d_other_pct",
            "pkg_vs_none_pct", "extra_w"]
    table = [cols] + [[("-" if r[c] is None else str(r[c])) for c in cols] for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    for row in table:
        print("  ".join(cell.rjust(widths[i]) for i, cell in enumerate(row)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", choices=list(BROWSER_PATHS), default="chrome")
    ap.add_argument("--method", choices=["js", "pref", "budget"], default="js",
                    help="js: 페이지 rAF만 솎아냄(확장 수준) / pref: Firefox layout.frame_rate(브라우저 수준) / "
                         "budget: FrameBudget 패치 Chromium의 --framebudget-default-fps(브라우저 수준)")
    ap.add_argument("--exe", help="브라우저 실행 파일 경로. 직접 빌드한 chrome.exe를 쓸 때 지정")
    ap.add_argument("--caps", default="0,30,15", help="FPS 상한 목록. 0은 제한 없음")
    ap.add_argument("--scenes", default="static,css,canvas,webgl")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=10, help="페이지 로드 후 측정 전 대기(초)")
    ap.add_argument("--duration", type=int, default=40, help="측정 시간(초)")
    ap.add_argument("--cooldown", type=int, default=10, help="실행 사이 대기(초)")
    ap.add_argument("--settle", type=int, default=20, help="첫 실행 전 시스템 안정화 대기(초)")
    ap.add_argument("--page-args", default="", help="페이지에 그대로 넘길 쿼리. 예: iters=32&particles=8000")
    ap.add_argument("--headless", action="store_true",
                    help="창을 띄우지 않고 측정. 기본값(전체 화면 kiosk)은 실제 화면 조건에 가깝다")
    args = ap.parse_args()

    if args.method == "pref" and args.browser != "firefox":
        raise SystemExit("--method pref는 Firefox에서만 쓸 수 있습니다.")
    if args.method == "budget" and args.browser == "firefox":
        raise SystemExit("--method budget은 FrameBudget 패치를 적용한 Chromium에서만 쓸 수 있습니다. --exe로 지정하세요.")
    if args.exe and not os.path.exists(args.exe):
        raise SystemExit(f"실행 파일이 없습니다: {args.exe}")
    exe = args.exe or find_browser(args.browser)
    caps = [int(c) for c in args.caps.split(",")]
    scenes = args.scenes.split(",")
    conds = [("none" if c == 0 else f"{args.method}{c}", c) for c in caps]
    labels = [label for label, _ in conds]

    inbox = Inbox()
    Handler.inbox = inbox
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    out_dir = ROOT / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-{args.browser}-{args.method}"
    out_dir.mkdir(parents=True)
    # Linux의 Firefox snap은 /tmp가 격리되어 있어서 프로필을 홈 아래에 만든다
    profile_parent = None if IS_WINDOWS else os.path.expanduser("~/.cache")
    profile = Path(tempfile.mkdtemp(prefix=f"fbb-{args.browser}-", dir=profile_parent))

    total = args.reps * len(scenes) * len(conds)
    per_run = args.warmup + args.duration + args.cooldown + 8
    print(f"{args.browser} {file_version(exe)} | 방식 {args.method} | 조건 {labels} | 장면 {scenes}")
    print(f"총 {total}회, 예상 {round((total * per_run + args.settle) / 60)}분. 측정 중에는 PC를 만지지 마세요.")
    if not on_ac_power():
        print("경고: 배터리로 동작 중입니다. 전원 어댑터를 연결하는 것을 권장합니다.")
    time.sleep(args.settle)

    # 브라우저를 띄우기 전 배경 소음: 여기서 GPU나 CPU를 많이 쓰면 결과 차이가 소음에 묻힌다
    idle = sample_counters(10)
    print(f"배경 상태: pkg={idle['pkg_w']}W cores={idle['cores_w']}W gfx={idle['gfx_w']}W "
          f"cpu={idle['cpu_pct']}% 다른 프로세스 GPU3D={idle['gpu3d_other_pct']}%")
    if (idle["gpu3d_other_pct"] or 0) > 10 or (idle["cpu_pct"] or 0) > 15:
        print("경고: 배경 부하가 큽니다. 다른 브라우저·영상·채팅 앱을 닫고 다시 실행하는 것을 권장합니다.")

    runs = []
    n = 0
    try:
        for rep in range(args.reps):
            for scene in scenes:
                k = rep % len(conds)  # 반복마다 조건 순서를 회전시켜 순서 효과를 줄인다
                for label, cap in conds[k:] + conds[:k]:
                    n += 1
                    run_id = uuid.uuid4().hex[:8]
                    page_cap = cap if args.method == "js" else 0
                    url = (f"http://127.0.0.1:{port}/index.html?scene={scene}&cap={page_cap}"
                           f"&warmup={args.warmup}&duration={args.duration}&run={run_id}&hud=0")
                    if args.page_args:
                        url += "&" + args.page_args
                    inbox.expect(run_id)
                    ac = on_ac_power()
                    proc = launch(args.browser, exe, profile, url, cap if args.method in ("pref", "budget") else 0, args.headless)
                    try:
                        if not inbox.begin.wait(args.warmup + 60):
                            raise RuntimeError("페이지가 측정을 시작하지 않았습니다")
                        power = sample_counters(args.duration, browser_pids())
                        if not inbox.result.wait(30):
                            raise RuntimeError("페이지 결과가 도착하지 않았습니다")
                        page = inbox.payload
                    except Exception as e:
                        print(f"[{n}/{total}] {scene} {label} 실패: {e}")
                        continue
                    finally:
                        close(proc)
                    row = {"rep": rep + 1, "scene": scene, "condition": label, "cap": cap,
                           "fps": page["fps"], "interval_p50_ms": page["interval_p50_ms"],
                           "interval_p95_ms": page["interval_p95_ms"], **power,
                           "on_ac": ac and on_ac_power(), "viewport": page["viewport"], "dpr": page["dpr"]}
                    runs.append(row)
                    print(f"[{n}/{total}] {scene:6} {label:7} fps={row['fps']:6}  pkg={row['pkg_w']}W "
                          f"gfx={row['gfx_w']}W gpu3d={row['gpu3d_pct']}% (다른 프로세스 {row['gpu3d_other_pct']}%)")
                    time.sleep(args.cooldown)
    finally:
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
        write_csv(out_dir / "runs.csv", runs)

    summary = summarize(runs, scenes, labels)
    write_csv(out_dir / "summary.csv", summary)
    (out_dir / "meta.json").write_text(json.dumps({
        "browser": args.browser, "version": file_version(exe), "method": args.method,
        "args": vars(args), "idle": idle, "viewport": runs[0]["viewport"] if runs else None,
        "on_ac_all_runs": all(r["on_ac"] for r in runs),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 요약 (중앙값) ===")
    print_table(summary)
    if runs and not all(r["on_ac"] for r in runs):
        print("경고: 일부 실행이 배터리 상태에서 측정됐습니다. 결과를 신뢰하기 어렵습니다.")
    print(f"\n결과: {out_dir}")


if __name__ == "__main__":
    main()
