# FrameBudget Chromium 빌드

FrameBudget 기능은 Chromium 원본 소스에 적용하는 패치로 제공합니다. Chromium 소스 전체(약 50GB)는 이 저장소에 넣지 않습니다.

- **기준 버전:** Chromium `156.0.8078.48` (commit `d0c02bfad0`)
- **확인한 환경:** Ubuntu, CPU 16코어, 메모리 30GB

## 패치

[`patches/`](patches)의 파일을 번호 순서대로 적용합니다. 로컬 Chromium 저장소의 커밋을 `git format-patch`로 내보낸 것입니다.

| 패치 | 내용 |
|---|---|
| 0001 | viz·content: 프레임 싱크마다 다른 BeginFrame 간격을 지정하는 API (`content::SetFrameBudgetForFrameSink`) |
| 0002 | chrome: 사이트·탭별 FPS 정책 (`FrameBudgetTabHelper`), 실행 옵션, Alt+Shift+F 단축키 |
| 0003 | chrome: `chrome://framebudget` 설정 화면, 상태 표시, 입력 시 제한 해제 |
| 0004 | 수정: 첫 빌드의 `framebudget_tab_helper.cc` 컴파일 오류 2건 |
| 0005 | 수정: `framebudget_ui.cc` 컴파일 오류 1건 |
| 0006 | 설정 화면 안내 문구 정리 |

0004와 0005는 첫 빌드에서 나온 오류를 고친 기록입니다. 기능 패치에 합치지 않고 따로 남겼습니다.

- `GURL::host()`가 `std::string_view`를 돌려주는데 `std::string`으로 암시 변환하고 있었습니다.
- `BrowserContext`가 전방 선언만 되어 있어서 `UserPrefs::Get()`에 넘길 수 없었습니다.

6개 패치를 `156.0.8078.48`에 순서대로 적용한 결과가 실제로 빌드한 소스 트리와 같은 것을 git 트리 해시로 확인했습니다.

## 빌드 방법

Chromium 공식 [Linux 빌드 문서](https://chromium.googlesource.com/chromium/src/+/main/docs/linux/build_instructions.md)를 기준으로 합니다. 디스크는 100GB 이상 비워 두는 것을 권합니다.

1. [depot_tools](https://chromium.googlesource.com/chromium/tools/depot_tools.git)를 받아 `PATH`에 추가합니다.
2. 소스를 받고 기준 버전으로 맞춥니다.

   ```bash
   mkdir chromium && cd chromium
   fetch --nohooks --no-history chromium
   cd src
   git fetch --depth 1 origin refs/tags/156.0.8078.48:refs/tags/156.0.8078.48
   git checkout 156.0.8078.48
   gclient sync --no-history --shallow -D
   ```

   확인한 PC에서는 의존성 설치 스크립트를 따로 실행하지 않아도 빌드되었습니다. 빌드 중 시스템 패키지가 없다는 오류가 나면 공식 문서의 `build/install-build-deps.sh`를 실행합니다.

3. 패치를 적용합니다. `FRAMEBUDGET`은 이 저장소를 받은 위치입니다.

   ```bash
   git am "$FRAMEBUDGET"/chromium/patches/*.patch
   ```

4. 빌드합니다.

   ```bash
   gn gen out/Default --args="is_debug=false symbol_level=0 is_component_build=true"
   autoninja -C out/Default -j 6 chrome
   ```

   메모리가 30GB인 PC에서 `-j 6`으로 빌드했습니다. 처음부터 끝까지 약 2시간 30분 걸렸습니다. 메모리가 부족하면 `-j` 값을 줄입니다. 빌드를 중간에 멈췄다가 같은 명령으로 다시 실행하면 남은 부분만 이어서 빌드합니다.

## 사용법

```bash
out/Default/chrome
```

- 주소창에 `chrome://framebudget`을 입력하면 기본값, 사이트 규칙, 열린 탭의 상태를 보고 바꿀 수 있습니다.
- Alt+Shift+F로 현재 탭의 FPS를 바로 바꿉니다.
- 실행 옵션으로도 규칙을 줄 수 있습니다. 실행 옵션은 저장된 규칙보다 우선합니다.

  ```bash
  out/Default/chrome --framebudget-default-fps=30 --framebudget-sites=youtube.com=60,example.com=15
  ```

  사이트 규칙은 하위 도메인에도 적용됩니다.

## 동작 확인

`requestAnimationFrame` 콜백 수를 초마다 세는 테스트 페이지를 헤드리스 모드로 열었습니다. 실행 옵션으로 규칙을 바꿔 가며 1초씩 5번 측정했습니다.

| 조건 | 초당 프레임 (5회) |
|---|---|
| 제한 없음 | 60, 60, 60, 60, 60 |
| `--framebudget-sites=127.0.0.1=30` | 30, 30, 30, 30, 30 |
| `--framebudget-sites=127.0.0.1=15` | 15, 15, 15, 15, 15 |

`chrome://framebudget` 페이지가 열리고, 실행 옵션으로 준 규칙이 화면에 표시되는 것도 확인했습니다.

아직 확인하지 않은 것:

- 창을 띄운 일반 모드에서의 동작
- CSS 애니메이션 제한
- 입력 시 제한 해제, Alt+Shift+F 단축키, 상태 표시
- 전력 절감량 (FrameBudget 빌드로 다시 측정할 예정)

## 출처와 라이선스

이 패치는 [Chromium](https://www.chromium.org/) 소스를 수정합니다. Chromium은 BSD-3-Clause 라이선스이며, 포함된 제3자 코드는 각자의 라이선스를 따릅니다. 패치 자체도 이 저장소의 [LICENSE](../LICENSE)(BSD-3-Clause)를 따릅니다.
