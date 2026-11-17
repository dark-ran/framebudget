"""외부 페이지 하나를 Firefox layout.frame_rate 조건별로 띄워 패키지 전력을 잰다.

bench.py의 실행·종료·카운터 함수를 그대로 쓴다. 외부 페이지는 결과를 보고하지 않으므로
FPS는 재지 않고, 측정 대상 Firefox의 GPU 3D 사용률로 실제로 그리고 있는지 확인한다.

python measure_url.py URL [--reps 3] [--warmup 20] [--duration 40] [--cooldown 10] [--out DIR]
"""
import argparse
import csv
import json
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
import bench  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--duration", type=int, default=40)
    ap.add_argument("--cooldown", type=int, default=10)
    ap.add_argument("--label", default="url")
    args = ap.parse_args()

    exe = bench.find_browser("firefox")
    # blank: 빈 페이지(바닥 전력), none/pref30/pref15: 대상 페이지
    conds = [("blank", "about:blank", 0), ("none", args.url, 0),
             ("pref30", args.url, 30), ("pref15", args.url, 15)]
    out_dir = BENCH_DIR / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-firefox-{args.label}"
    out_dir.mkdir(parents=True)
    profile = Path(tempfile.mkdtemp(prefix="fbb-url-"))

    total = args.reps * len(conds)
    print(f"대상 {args.url} | 총 {total}회, 예상 "
          f"{round(total * (args.warmup + args.duration + args.cooldown + 8) / 60)}분", flush=True)
    idle = bench.sample_counters(10)
    print(f"배경 상태: pkg={idle['pkg_w']}W cpu={idle['cpu_pct']}% 다른 프로세스 GPU3D={idle['gpu3d_other_pct']}%",
          flush=True)

    runs = []
    n = 0
    try:
        for rep in range(args.reps):
            k = rep % len(conds)  # 반복마다 조건 순서를 회전
            for label, url, fps in conds[k:] + conds[:k]:
                n += 1
                ac = bench.on_ac_power()
                proc = bench.launch("firefox", exe, profile, url, fps)
                try:
                    time.sleep(args.warmup)
                    power = bench.sample_counters(args.duration, bench.browser_pids())
                finally:
                    bench.close(proc)
                row = {"rep": rep + 1, "condition": label, **power, "on_ac": ac and bench.on_ac_power()}
                runs.append(row)
                print(f"[{n}/{total}] {label:6} pkg={row['pkg_w']}W gfx={row['gfx_w']}W cores={row['cores_w']}W "
                      f"gpu3d={row['gpu3d_pct']}% (다른 프로세스 {row['gpu3d_other_pct']}%)", flush=True)
                time.sleep(args.cooldown)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        bench.write_csv(out_dir / "runs.csv", runs)

    def med(label, key):
        vals = [r[key] for r in runs if r["condition"] == label and r[key] is not None]
        return round(statistics.median(vals), 3) if vals else None

    blank, base = med("blank", "pkg_w"), med("none", "pkg_w")
    summary = []
    for label, _, _ in conds:
        pkg = med(label, "pkg_w")
        summary.append({
            "condition": label, "n": sum(r["condition"] == label for r in runs),
            "pkg_w": pkg, "gfx_w": med(label, "gfx_w"), "cores_w": med(label, "cores_w"),
            "gpu3d_pct": med(label, "gpu3d_pct"),
            "pkg_min_max": (f"{min(r['pkg_w'] for r in runs if r['condition'] == label)}~"
                            f"{max(r['pkg_w'] for r in runs if r['condition'] == label)}"),
            "pkg_vs_none_pct": round((pkg - base) / base * 100, 1) if base and pkg else None,
            "extra_w": round(pkg - blank, 3) if blank is not None and pkg is not None else None,
        })
    bench.write_csv(out_dir / "summary.csv", summary)
    (out_dir / "meta.json").write_text(json.dumps({
        "url": args.url, "version": bench.file_version(exe), "args": vars(args), "idle": idle,
        "on_ac_all_runs": all(r["on_ac"] for r in runs)}, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 요약 (중앙값) ===")
    cols = list(summary[0])
    table = [cols] + [["-" if r[c] is None else str(r[c]) for c in cols] for r in summary]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    for row in table:
        print("  ".join(cell.rjust(widths[i]) for i, cell in enumerate(row)))
    print(f"\n결과: {out_dir}")


if __name__ == "__main__":
    main()
