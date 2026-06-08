# Simple Line Scan Capture Module Plan

> **For Hermes:** 사용자가 이 문서를 다시 확인하고 승인하기 전까지 코드는 구현하지 않는다. 승인 후에는 이 계획을 기준으로 작은 task 단위로 구현/검증/commit/push 한다.

**Goal:** 초기 설정만 끝내면 `TriggerMode=On/Off` 양쪽 모두에서 “일정 시간 동안 촬상하고 이미지들을 얻어오는 것”을 최대한 간단한 API로 제공한다.

**Architecture:** `eBUS.py`와 `eBUS_examples/`는 수정하지 않고, `linescan_module.py`에 얇고 안전한 wrapper를 만든다. GenICam 전체를 노출하는 범용 설정 API는 만들지 않고, 현재 필요한 주요 설정만 Python property/getter/setter로 제한적으로 제공한다. 고속 수신은 `high_throughput_pipeline.py`처럼 `PvPipeline` 기반으로 구현하고, bandwidth/FPS/error counter는 debug 정보로 확인할 수 있게 한다.

**Tech Stack:** Python 3.11, JAI/Pleora eBUS Python binding, dataclasses, typing, fake eBUS object 기반 unit tests.

---

## 1. 수정된 개발 방향 요약

기존 계획은 device discovery, calibration, UserSet, recovery, generic GenICam API까지 포함해서 너무 넓었다. 이번 계획은 아래 목표에 집중한다.

1. **간단한 time-based capture**
   - `camera.capture(duration_s=5)`처럼 일정 시간 촬상하고 이미지 목록/iterator/result를 얻는다.
   - trigger off 내부 라인레이트 모드와 trigger on 외부/소프트웨어 트리거 모드를 모두 지원한다.
2. **안전한 제한적 설정 노출**
   - 임의 GenICam parameter 이름을 받아서 `set_param("AnyDangerousFeature", value)`처럼 쓰는 공개 API는 만들지 않는다.
   - 필요한 설정만 property 또는 명시적 getter/setter로 제공한다.
   - 내부 구현에서는 eBUS parameter helper를 쓰되, public surface에는 노출하지 않는다.
3. **초기 설정 후 쉬운 사용**
   - IP, pipeline buffer count, width/height, trigger/gain/exposure/line-rate 정도만 설정하면 바로 capture 가능해야 한다.
4. **디버깅 정보 제공**
   - `high_throughput_pipeline.py`에서 확인하던 bandwidth, FPS, packet loss/resend, block gap, operational/retrieve error를 API로 제공한다.

---

## 2. 참고 파일별 재사용할 패턴

### 2.1 `high_throughput_pipeline.py`

가장 중요한 기준 파일이다.

재사용할 것:

- `PvDevice.CreateAndConnect()` / `PvStream.CreateAndOpen()`
- `PvDeviceGEV.NegotiatePacketSize()`
- `SetStreamDestination(stream.GetLocalIPAddress(), stream.GetLocalPort())`
- `PvPipeline(stream)` 사용
- `pipeline.SetBufferCount(PIPELINE_BUFFER_COUNT)`
- `pipeline.SetBufferSize(device.GetPayloadSize())`
- `pipeline.Start()` → `device.StreamEnable()` → `AcquisitionStart`
- `RetrieveNextBuffer(timeout_ms)` 후 `finally: pipeline.ReleaseBuffer(pvbuffer)` 보장
- `GetAcquiredSize()` 기반 local bandwidth 계산
- eBUS stream parameter `AcquisitionRate`, `Bandwidth` 읽기
- packet loss/resend counter 수집
- `GetBlockID()` gap 기반 missing block 계산
- 1초 단위 debug stats 출력 또는 반환

### 2.2 `eBUS_examples/PvPipelineSample.py`

재사용할 것:

- eBUS의 표준 pipeline lifecycle 순서
- GigE Vision device에서 packet negotiation + stream destination 설정
- buffer를 처리한 뒤 반드시 release하는 패턴

제외할 것:

- OpenCV display
- frame마다 print하는 loop
- compressed/multipart payload 상세 처리

### 2.3 `eBUS_examples/GenICamParameters.py`

재사용할 것:

- 내부 helper에서 `GetParameters().Get(name)`, `GetValue()`, `GetValueString()`, `SetValue()`를 안전하게 호출하는 방식

주의:

- public API로 generic GenICam access를 열지 않는다.
- `read_param`, `set_param` 같은 함수는 `_read_param`, `_set_param` 형태의 private helper로 둔다.

### 2.4 `eBUS_examples/DeviceFinder.py`

재사용할 것:

- 필요 시 device 발견/연결에 참고한다.

이번 1차 범위:

- 기본 연결은 `DEVICE_IP_ADDR`를 우선 사용한다.
- 복잡한 device selection UI/API는 만들지 않는다.

### 2.5 `eBUS_examples/ImageProcessing.py`

재사용할 것:

- `pvbuffer.GetImage()`와 `image.GetDataPointer()`로 image data를 얻는 방식

주의:

- buffer release 이후 zero-copy pointer가 유효하지 않을 수 있다.
- `capture(..., copy=True)`를 기본으로 둘지 여부를 명확히 결정한다.

### 2.6 `eBUS_examples/ConnectionRecovery.py`

이번 1차 구현에서는 제외한다.

- 자동 reconnect는 나중에 필요해지면 별도 계획으로 구현한다.
- 단, `close()`와 exception cleanup은 안전하게 만든다.

---

## 3. 공개 API 설계

### 3.1 가장 간단한 사용 예시

```python
from linescan_module import LineScanCamera

with LineScanCamera() as cam:
    cam.width = 4096
    cam.height = 256
    cam.trigger_mode = False
    cam.acquisition_line_rate = 84000
    cam.exposure_time = 5.0
    cam.gain = 1.0

    result = cam.capture(duration_s=3.0)

print(len(result.frames))
print(result.stats.avg_bandwidth_mbps)
print(result.debug_summary())
```

### 3.2 IP와 buffer count만 바꾸는 예시

```python
from linescan_module import LineScanCamera

with LineScanCamera(
    device_ip_addr="192.168.1.200",
    pipeline_buffer_count=64,
) as cam:
    result = cam.capture(duration_s=5.0)
```

### 3.3 TriggerMode=On 예시

```python
from linescan_module import LineScanCamera

with LineScanCamera() as cam:
    cam.width = 4096
    cam.height = 256
    cam.trigger_mode = True
    cam.exposure_time = 5.0
    cam.gain = 1.0

    # 외부 trigger가 들어오는 동안 10초간 frame/block을 받는다.
    result = cam.capture(duration_s=10.0)
```

### 3.4 callback으로 저장/처리하는 예시

```python
with LineScanCamera() as cam:
    def on_frame(frame):
        # frame.image는 copy된 numpy-like data 또는 bytes로 제공할지 구현 시 결정
        process(frame.image)

    stats = cam.capture(duration_s=10.0, on_frame=on_frame, store_frames=False)
```

---

## 4. 공개 설정값

### 4.1 GenICam 기반 property

아래 항목만 1차 public property로 제공한다.

| Python property | GenICam feature | 용도 |
|---|---|---|
| `trigger_mode: bool` | `TriggerMode` | trigger on/off |
| `gain: float` | `Gain` with `GainSelector=DigitalAll` | 밝기 조절 |
| `exposure_time: float` | `ExposureTime` | 노출 시간 |
| `acquisition_line_rate: float` | `AcquisitionLineRate` | trigger off 내부 라인레이트 |
| `gev_scps_packet_size: int` | `GevSCPSPacketSize` | GigE packet size |
| `network_throughput_safety_margin: int` | `NetworkThroughputSafetyMargin` | throughput safety margin |
| `device_link_speed: int | str` | `DeviceLinkSpeed` | read-only link speed 확인 |
| `height: int` | `Height` | frame당 line 수 |
| `width: int` | `Width` | line width |

구현 원칙:

- `device_link_speed`는 read-only property로 둔다.
- setter가 있는 property는 내부에서 `_set_feature()`를 호출한다.
- getter는 내부에서 `_read_feature()`를 호출한다.
- 실패 시 어떤 feature가 실패했는지 포함한 `LineScanError`를 던진다.
- 필요 기능이 추가되면 새 property를 명시적으로 추가한다.

예시:

```python
cam.trigger_mode = False
cam.width = 4096
cam.height = 256
cam.gain = 1.0
cam.exposure_time = 5.0
cam.acquisition_line_rate = 84000
cam.gev_scps_packet_size = 7976
cam.network_throughput_safety_margin = 92
print(cam.device_link_speed)
```

### 4.2 GenICam이 아닌 module 설정값

아래 값은 GenICam parameter가 아니라 wrapper 설정값이다.

| Python property / init arg | 기본값 | 용도 |
|---|---:|---|
| `device_ip_addr` / `DEVICE_IP_ADDR` | `"192.168.1.200"` | 라인스캔 카메라에 강제 할당/연결할 IP 주소 |
| `pipeline_buffer_count` / `PIPELINE_BUFFER_COUNT` | `64` | `PvPipeline`에 사용할 buffer count |
| `timeout_ms` | `1000` | buffer retrieve timeout |
| `copy_frames` | `True` 후보 | capture 결과 frame을 안전하게 copy할지 여부 |
| `debug` | `False` | capture 중 interval debug 출력 여부 |
| `debug_interval_s` | `1.0` | bandwidth/FPS/error 출력 주기 |

IP 관련 구현 방향:

- 기본 connection ID는 `DEVICE_IP_ADDR = "192.168.1.200"`로 둔다.
- 실제 eBUS `CreateAndConnect()`에 이 IP string을 넘기는 방식부터 구현한다.
- “강제 할당”이 Pleora SDK의 force IP command를 의미하는 경우, 1차 구현에서는 명시적으로 `force_ip()` helper 후보로 분리하고 자동 실행하지 않는다.
  - 이유: force IP는 네트워크 장치 설정에 영향을 주는 side effect가 커서 안전하게 검증해야 한다.

---

## 5. 핵심 데이터 구조

### 5.1 `LineScanFrame`

```python
@dataclass
class LineScanFrame:
    image: Any
    block_id: int | None
    width: int | None
    height: int | None
    acquired_size: int
    timestamp_ns: int
    ok: bool
    result_code: str | None = None
    operational_code: str | None = None
```

### 5.2 `LineScanStats`

`high_throughput_pipeline.py`의 `Counters`를 module용으로 정리한다.

```python
@dataclass
class LineScanStats:
    frames: int = 0
    bytes_acquired: int = 0
    lost_packets: int = 0
    recovered_packets: int = 0
    recovered_single_resend_packets: int = 0
    resend_groups_requested: int = 0
    resend_packets_requested: int = 0
    missing_packet_ids: int = 0
    operational_errors: int = 0
    retrieve_errors: int = 0
    block_gaps: int = 0
    last_block_id: int | None = None
    elapsed_s: float = 0.0
    ebus_fps: float | None = None
    ebus_bandwidth_mbps: float | None = None
```

계산 property:

```python
@property
def avg_bandwidth_mbps(self) -> float: ...

@property
def error_count(self) -> int: ...

@property
def error_rate(self) -> float: ...
```

### 5.3 `CaptureResult`

```python
@dataclass
class CaptureResult:
    frames: list[LineScanFrame]
    stats: LineScanStats
    settings: dict[str, Any]

    def debug_summary(self) -> str: ...
```

`store_frames=False`인 경우:

- `frames`는 빈 list로 둔다.
- callback으로 사용자가 직접 저장/처리한다.
- stats는 그대로 반환한다.

---

## 6. `LineScanCamera` 클래스 책임

### 6.1 생성자

```python
class LineScanCamera:
    def __init__(
        self,
        device_ip_addr: str = DEVICE_IP_ADDR,
        pipeline_buffer_count: int = PIPELINE_BUFFER_COUNT,
        timeout_ms: int = 1000,
        copy_frames: bool = True,
        debug: bool = False,
        debug_interval_s: float = 1.0,
    ):
        ...
```

책임:

- eBUS lazy import
- device 연결
- stream open
- GigE stream destination 설정
- pipeline 생성 준비

### 6.2 Lifecycle

필수 method:

- `open()`
- `close()`
- `__enter__()`
- `__exit__()`

정리 순서:

1. acquisition 중이면 `AcquisitionStop`
2. `device.StreamDisable()`
3. `pipeline.Stop()`
4. `stream.Close()`
5. `PvStream.Free(stream)`
6. `device.Disconnect()`
7. `PvDevice.Free(device)`

`close()`는 여러 번 호출해도 안전해야 한다.

### 6.3 Capture API

```python
def capture(
    self,
    duration_s: float,
    *,
    trigger_mode: bool | None = None,
    store_frames: bool = True,
    copy_frames: bool | None = None,
    on_frame: Callable[[LineScanFrame], None] | None = None,
    debug: bool | None = None,
) -> CaptureResult:
    ...
```

동작:

1. `trigger_mode` argument가 있으면 `self.trigger_mode`에 적용한다.
2. pipeline buffer count와 payload size로 pipeline을 준비한다.
3. `pipeline.Start()`
4. `device.StreamEnable()`
5. `AcquisitionStart.Execute()`
6. `duration_s` 동안 `RetrieveNextBuffer()` 반복
7. 각 buffer에서 image, block id, size, error counter를 수집
8. `store_frames=True`이면 frame list에 추가
9. `on_frame`이 있으면 callback 호출
10. debug가 켜져 있으면 interval마다 FPS/bandwidth/error 출력
11. finally에서 acquisition/stream/pipeline 정지
12. `CaptureResult` 반환

주의:

- trigger on 모드에서는 외부 trigger가 들어오지 않으면 frame이 안 들어올 수 있다. 이 경우 retrieve timeout은 stats의 `retrieve_errors`로 집계한다.
- trigger off 모드에서는 `acquisition_line_rate`가 내부 라인레이트를 결정한다.
- `height`는 line scan에서 frame/block 하나에 묶는 line 수이므로 FPS와 bandwidth 계산에 중요하다.

---

## 7. Debug 정보

### 7.1 capture 중 interval debug 출력

`debug=True`이면 `high_throughput_pipeline.py`와 유사한 형식으로 출력한다.

예시:

```text
local= 2750.1 Mb/s, frames=   328, eBUS_FPS=   328.1, eBUS_BW= 2755.0 Mb/s, lost=0, recovered=0, resend_pkts=0, gaps=0, op_err=0, ret_err=0
```

포함 항목:

- local acquired-byte bandwidth Mbps
- interval frame count
- eBUS stream `AcquisitionRate` / FPS
- eBUS stream `Bandwidth` Mbps
- lost packets
- recovered packets
- resend packet requests
- block gaps
- operational errors
- retrieve errors

### 7.2 capture 후 summary

`CaptureResult.debug_summary()` 예시:

```text
elapsed_s: 10.002
frames: 3281
avg_local_bandwidth_mbps: 2751.4
ebus_fps: 328.1
ebus_bandwidth_mbps: 2755.0
bytes_acquired: 3437232128
lost_packets: 0
recovered_packets: 0
resend_packets_requested: 0
missing_packet_ids: 0
block_gaps: 0
operational_errors: 0
retrieve_errors: 0
```

---

## 8. 구현 작업 계획

### Task 1: 기존 plan을 단순 API 중심으로 정리

**Objective:** 현재 문서처럼 실제 목표인 “초기 설정 후 일정 시간 촬상” 중심으로 계획을 확정한다.

**Files:**
- Modify: `docs/plan.md`

**Verification:**
- 사용자가 다시 리뷰하고 범위가 맞는지 확인한다.

### Task 2: 최소 private eBUS helper와 error type 구현

**Objective:** public generic GenICam API 없이 내부 property 구현에 필요한 안전 helper만 만든다.

**Files:**
- Modify: `linescan_module.py`
- Create: `tests/test_linescan_private_features.py`

**Steps:**
1. `LineScanError`, `EBusResultError` 추가
2. `_result_ok()`, `_describe_result()` 추가
3. `_get_feature(name)`, `_read_feature(name)`, `_write_feature(name, value)` private helper 추가
4. fake parameter로 read/write 실패/성공 test 작성
5. `python3 -m py_compile linescan_module.py`
6. Commit: `feat: add private eBUS feature helpers`

### Task 3: `LineScanCamera` lifecycle 구현

**Objective:** IP 기반으로 연결하고 안전하게 닫는 최소 카메라 객체를 만든다.

**Files:**
- Modify: `linescan_module.py`
- Create: `tests/test_linescan_lifecycle.py`

**Steps:**
1. module constants 추가
   - `DEVICE_IP_ADDR = "192.168.1.200"`
   - `PIPELINE_BUFFER_COUNT = 64`
2. 생성자 인자로 `device_ip_addr`, `pipeline_buffer_count`, `timeout_ms`, `copy_frames`, `debug`, `debug_interval_s` 추가
3. `open()`, `close()`, context manager 구현
4. fake device/stream으로 close idempotency test 작성
5. Commit: `feat: add simple line scan camera lifecycle`

### Task 4: 제한된 property 구현

**Objective:** 필요한 GenICam 설정만 안전한 property로 제공한다.

**Files:**
- Modify: `linescan_module.py`
- Create: `tests/test_linescan_properties.py`

**Properties:**
- `trigger_mode`
- `gain`
- `exposure_time`
- `acquisition_line_rate`
- `gev_scps_packet_size`
- `network_throughput_safety_margin`
- `device_link_speed` read-only
- `height`
- `width`

**Steps:**
1. bool `trigger_mode`와 GenICam 문자열 `On`/`Off` mapping 구현
2. `gain` setter에서 필요하면 `GainSelector=DigitalAll` 먼저 설정
3. read-only `device_link_speed`에 setter가 없음을 test
4. 각 property가 올바른 GenICam feature 이름을 쓰는지 fake parameter로 test
5. Commit: `feat: expose safe camera properties`

### Task 5: Pipeline 준비와 transport 설정 구현

**Objective:** capture 전에 고속 수신 가능한 `PvPipeline`을 준비한다.

**Files:**
- Modify: `linescan_module.py`
- Create: `tests/test_linescan_pipeline.py`

**Steps:**
1. `_configure_gige_stream()` 구현
   - `NegotiatePacketSize()`
   - `SetStreamDestination()`
2. `_create_pipeline()` 구현
   - `PvPipeline(stream)`
   - `SetBufferCount(self.pipeline_buffer_count)`
   - `SetBufferSize(device.GetPayloadSize())`
3. payload size가 0 이하이면 예외 처리
4. Commit: `feat: add pipeline setup for capture`

### Task 6: Stats와 frame/result dataclass 구현

**Objective:** capture 결과와 debug 정보를 담을 구조를 만든다.

**Files:**
- Modify: `linescan_module.py`
- Create: `tests/test_linescan_stats.py`

**Steps:**
1. `LineScanFrame`, `LineScanStats`, `CaptureResult` 추가
2. `LineScanStats.update_from_buffer()` 구현
3. block gap 계산 구현
4. bandwidth/error rate property 구현
5. `debug_summary()` 구현
6. fake pvbuffer로 counter 누적 test 작성
7. Commit: `feat: add capture result and debug stats`

### Task 7: `capture(duration_s=...)` 구현

**Objective:** 이 모듈의 핵심 기능인 “일정 시간 촬상 후 이미지 얻기”를 구현한다.

**Files:**
- Modify: `linescan_module.py`
- Create: `tests/test_linescan_capture.py`

**Steps:**
1. `capture()` signature 구현
2. trigger_mode argument optional 적용
3. pipeline start / stream enable / acquisition start 순서 구현
4. duration 동안 retrieve loop 구현
5. `LineScanFrame` 생성
6. `store_frames`, `copy_frames`, `on_frame` 처리
7. finally에서 release/stop/disable 보장
8. fake pipeline으로 duration/stop_after 대체 가능한 test 작성
9. Commit: `feat: implement timed image capture`

### Task 8: Debug interval 출력 구현

**Objective:** capture 중 bandwidth/FPS/error를 확인할 수 있게 한다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_debug.py`

**Steps:**
1. stream parameters에서 `AcquisitionRate`, `Bandwidth` 읽기
2. interval-local bandwidth/frame count 계산
3. `debug=True`일 때 1초 단위 출력
4. 출력 문자열에 local Mbps, eBUS FPS/BW, lost/recovered/resend/gap/error 포함
5. Commit: `feat: add capture debug statistics output`

### Task 9: 최소 예제 작성

**Objective:** 사용자가 module을 바로 이해하고 실행할 수 있는 예제를 제공한다.

**Files:**
- Create: `examples/simple_capture.py`
- Create: `examples/debug_capture.py`
- Modify: `README.md`

**Examples:**
- trigger off 3초 capture
- trigger on 10초 capture
- debug 출력 켠 capture

**Verification:**
- hardware가 없으면 `python3 -m py_compile linescan_module.py examples/simple_capture.py examples/debug_capture.py`
- hardware가 있으면 debug example 10초 실행

**Commit:**
- `docs: add simple capture examples`

---

## 9. 1차 구현에서 명시적으로 제외할 범위

- generic public `get_param()` / `set_param()` API
- 전체 GenICam parameter dump/edit 기능
- UserSet save/load
- DSNU/PRNU/Shading calibration 자동화
- automatic reconnect
- multi-source / multi-part payload
- OpenCV display GUI
- device selection UI
- complex producer/consumer multiprocessing 저장 pipeline

필요해지면 사용자가 원하는 feature만 property/method로 추가한다.

---

## 10. 검증 기준

### 10.1 Hardware 없이

- `python3 -m py_compile linescan_module.py`
- fake eBUS tests 통과
- property mapping test 통과
- pipeline release/cleanup test 통과
- stats 계산 test 통과

### 10.2 Hardware 연결 후

1. 기본 연결
   - `LineScanCamera(device_ip_addr="192.168.1.200")`로 연결
2. 기본 property 읽기
   - `device_link_speed`, `width`, `height`, `trigger_mode` 확인
3. TriggerMode Off capture
   - `trigger_mode=False`
   - `acquisition_line_rate` 설정
   - `capture(duration_s=3, debug=True)` 실행
4. TriggerMode On capture
   - `trigger_mode=True`
   - 외부 trigger 입력 중 `capture(duration_s=10, debug=True)` 실행
5. Debug counter 확인
   - bandwidth/FPS 출력
   - `lost_packets`, `recovered_packets`, `block_gaps`, `operational_errors`, `retrieve_errors`
6. cleanup 확인
   - capture 종료 후 재실행 가능해야 한다.

---

## 11. 남은 확인 사항

개발 전에 아래만 확인하면 된다.

1. `capture()`의 기본 동작을 `copy_frames=True`로 안전하게 둘지, 성능 우선으로 `False`로 둘지
   - 추천: 초보 사용성과 안전성을 위해 기본 `True`, 고속 장시간 처리 시 `store_frames=False + on_frame` 권장
2. `DEVICE_IP_ADDR`가 “연결할 IP”인지, Pleora force-IP 명령으로 “강제 할당할 IP”까지 의미하는지
   - 추천: 1차는 연결 IP로만 사용. force-IP는 별도 explicit method로 나중에 추가
3. trigger on일 때 추가로 `TriggerSelector=LineStart`, `TriggerSource`, `TriggerActivation` property도 바로 필요할지
   - 현재 요청 필수 목록에는 없으므로 1차에서는 `trigger_mode`만 넣고, 필요 시 추가

---

## 12. 완료 기준

- `LineScanCamera`로 초기 설정 후 `capture(duration_s=...)` 한 줄에 가깝게 일정 시간 촬상 가능하다.
- `TriggerMode=On/Off` 모두 지원한다.
- 필요한 설정은 property로만 노출한다.
- 위험한 generic GenICam public setter는 없다.
- `DEVICE_IP_ADDR`, `PIPELINE_BUFFER_COUNT`를 제어할 수 있다.
- capture 결과로 image frames와 stats를 받을 수 있다.
- debug mode에서 bandwidth, FPS, packet/error 정보를 확인할 수 있다.
- buffer release와 acquisition stop/stream disable/pipeline stop cleanup이 항상 보장된다.
