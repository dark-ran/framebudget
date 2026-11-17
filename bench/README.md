# FrameBudget 전력 측정 도구

웹 콘텐츠의 FPS를 낮추면 전력이 실제로 줄어드는지 재는 도구입니다. 시험 장면을 FPS 상한 조건별로 띄우고, 같은 시간 동안 전력을 기록합니다.

## 조건

| 조건 | 방식 | 의미 |
|---|---|---|
| `none` | 제한 없음 | 기준값 (모니터 주사율 그대로) |
| `budget30`, `budget15` | FrameBudget 빌드의 `--framebudget-default-fps` | 브라우저 엔진이 프레임 생성 자체를 줄임 |
| `js30`, `js15` | 페이지의 `requestAnimationFrame` 콜백만 솎아냄 | 확장 프로그램으로 할 수 있는 수준 |
| `pref30`, `pref15` | Firefox `layout.frame_rate` | 사전 실험용. Firefox 전체에 값 하나 |

`js` 방식은 컴포지터를 그대로 두고 페이지 스크립트만 줄입니다. 그래서 CSS 장면에서는 효과가 없는 것이 정상입니다.

## 장면

| 장면 | 내용 |
|---|---|
| `static` | 움직임 없는 문서. 바닥 전력 |
| `css` | CSS 애니메이션 150개 (회전 + 그림자) |
| `canvas` | Canvas 2D 입자 6,000개 |
| `webgl` | 전체 화면 프랙탈 셰이더 |

부하 조절: `--page-args "iters=32&particles=8000&boxes=200"`

## 측정 값

- `pkg_w`: CPU 패키지 전력 (Linux `/sys/class/powercap` RAPL, Windows `Energy Meter`)
- `cores_w`: CPU 코어 전력
- `dgpu_w`: 외장 NVIDIA GPU 전력 (`nvidia-smi`). 외장 GPU 전력은 RAPL에 잡히지 않아서 따로 읽습니다
- `total_w`: `pkg_w + dgpu_w`
- `fps`: 페이지가 실제로 새로 그린 프레임 수
- `pkg_vs_none_pct`, `total_vs_none_pct`: 같은 장면의 `none` 대비 변화율
- `extra_w`: 정적 문서(바닥) 위에 그 장면이 추가로 쓰는 CPU 패키지 전력

## 실행

FrameBudget 빌드로 측정합니다. 장면 4개 × 조건 3개 × 3회 = 36회, 약 41분 걸립니다.

```bash
python3 bench/bench.py --browser chrome --exe out/Default/chrome --method budget --caps 0,30,15
```

같은 브라우저에서 확장 프로그램 수준과 비교합니다.

```bash
python3 bench/bench.py --browser chrome --exe out/Default/chrome --method js --caps 0,15
```

- Ubuntu 23.10 이후처럼 `No usable sandbox!`로 실행되지 않으면 `--browser-arg=--no-sandbox --browser-arg=--test-type`을 붙입니다. `--test-type`은 경고 줄이 화면 크기를 바꾸지 않게 합니다.
- Linux에서 RAPL 카운터는 root만 읽을 수 있습니다. 한 번 `sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj`를 실행합니다. 재부팅하면 다시 막힙니다.
- 반복마다 조건 순서를 돌려 순서 효과를 줄이고, 결과는 중앙값으로 냅니다.
- 측정 전 배경 부하를 재서 크면 경고합니다. 측정 중에는 PC를 쓰지 않습니다.

결과는 `bench/results/<시각>-<브라우저>-<방식>/`에 저장됩니다. `runs.csv`는 실행별 원본, `summary.csv`는 조건별 중앙값, `meta.json`은 버전·설정·배경 상태입니다.

`measure_url.py`는 외부 페이지 하나를 Firefox 조건별로 재는 보조 스크립트입니다.

## 저장된 결과

| 폴더 | 내용 |
|---|---|
| `20261117-143526-chrome-budget` | FrameBudget 빌드, `none`·`budget30`·`budget15` (데스크톱) |
| `20261117-151229-chrome-js` | FrameBudget 빌드, `none`·`js15` (데스크톱) |
| `20261028-*`, `20261030-*` | Firefox 사전 실험 |

데스크톱 측정 환경: AMD Ryzen (Granite Ridge), NVIDIA GeForce RTX 5060 Ti, 1920×1080 100Hz, Ubuntu (Wayland), 창 모드(kiosk). 측정 중 모니터 전원은 꺼져 있었지만, HDMI 연결과 화면 출력은 켜진 상태였습니다. 제한 없음 WebGL 장면이 99.99FPS로 그려졌습니다.
