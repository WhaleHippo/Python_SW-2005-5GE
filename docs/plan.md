# Line Scan Camera Module Implementation Plan

> **For Hermes:** Use `subagent-driven-development` skill to implement this plan task-by-task after the user reviews and approves it.

**Goal:** `linescan_module.py`를 SW-4005M-5GE 라인스캔 카메라를 쉽게 연결, 설정, 취득, 진단할 수 있는 Python wrapper 모듈로 구현한다.

**Architecture:** Pleora/JAI `eBUS.py` SWIG wrapper는 직접 수정하지 않고, `linescan_module.py` 안에 고수준 API를 얹는다. `high_throughput_pipeline.py`에서 검증한 고속 `PvPipeline` 수신 패턴을 기본 취득 경로로 삼고, `eBUS_examples/*.py`에서 확인한 device discovery, GenICam parameter access, stream/pipeline lifecycle, buffer release, recovery 패턴을 모듈화한다.

**Tech Stack:** Python 3.11, JAI/Pleora eBUS Python binding, dataclasses, typing, pytest-compatible unit tests with fake eBUS objects.

---

## 1. 참고한 소스와 개발 방향

### 1.1 프로젝트 내 직접 개발 대상

- `linescan_module.py`
  - 현재 `import eBUS as eb`와 `# TODO`만 있는 상태이다.
  - 이번 개발의 주 구현 파일이다.
- `high_throughput_pipeline.py`
  - 고속 라인스캔 수신을 위해 이미 작성된 진단/벤치마크 코드이다.
  - 아래 구현 패턴을 재사용한다.
    - `eBUS` lazy import
    - `PvDevice.CreateAndConnect()` / `PvStream.CreateAndOpen()`
    - `PvDeviceGEV.NegotiatePacketSize()`
    - `SetStreamDestination()`
    - `PvPipeline` 기반 buffering
    - `RetrieveNextBuffer()` 후 `ReleaseBuffer()` 보장
    - `GetBlockID()`, packet loss/resend counter 기반 stream stats
    - CLI가 아니라 library API로 재구성할 `configure_device()`, `create_pipeline()`, `Counters` 개념
- `docs/*.md`
  - 카메라 매뉴얼 요약, error rate/correction, 5GigE bandwidth 분석 문서가 이미 있다.
  - 구현 중 feature 이름과 주의사항은 기존 docs를 기준으로 유지한다.

### 1.2 Vendor / sample 자료

아래 파일들은 참고용이며 기본적으로 수정하지 않는다.

- `eBUS.py`
  - JAI/Pleora 제공 SWIG wrapper이다.
- `eBUS_examples/*.py`
  - eBUS SDK 예제이다.
  - 개발 표면이 아니라 API 사용 패턴 참고용이다.
- `SW-2005-5GE_usermanual.pdf`
  - 기능 이름, 범위, 주의사항 확인용이다.

### 1.3 eBUS 예제에서 가져올 패턴

- `DeviceFinder.py`
  - `PvSystem().Find()`로 interface/device 탐색
  - `PvDeviceInfoGEV`에서 display ID, serial, MAC, IP 추출
  - 모듈에는 `list_devices()` / `select_first_device()` 같은 discovery helper를 제공한다.
- `GenICamParameters.py`
  - `GetParameters()`, `Get(name)`, `GetValue()`, `GetValueString()`, `SetValue()` 패턴
  - 모듈에는 safe read/write wrapper와 snapshot 기능을 제공한다.
- `PvPipelineSample.py`
  - `PvDevice.CreateAndConnect()`, `PvStream.CreateAndOpen()`
  - GigE Vision에서 `NegotiatePacketSize()` 후 `SetStreamDestination()`
  - `PvPipeline.SetBufferCount()`, `SetBufferSize(device.GetPayloadSize())`
  - `pipeline.Start()` → `device.StreamEnable()` → `AcquisitionStart`
  - 수신 후 반드시 `pipeline.ReleaseBuffer(pvbuffer)`
- `ImageProcessing.py`
  - `PvStream` 직접 queue 방식과 `image.GetDataPointer()` 접근 예시
  - 초기 구현은 `PvPipeline`을 기본으로 하되, frame callback에서 numpy-like data pointer 접근을 지원한다.
- `ConnectionRecovery.py`
  - `PvDeviceEventSink.OnLinkDisconnected()`에서는 flag만 세우고 실제 teardown/reconnect는 main loop에서 수행
  - 1차 버전은 recovery를 옵션 기능으로 설계하고, 기본 구현은 안전한 teardown을 우선한다.
- `ReceiveMultiPart.py`, `MultiSource.py`, `SoftDeviceGEV*.py`
  - multi-part/multi-source/soft-device는 현재 SW-4005M-5GE 단일 카메라 wrapper의 핵심 범위가 아니므로 1차 구현에서는 제외한다.

---

## 2. 모듈이 제공해야 할 기능

### 2.1 Device discovery / connection

필수 API:

- `list_devices() -> list[DeviceInfo]`
  - eBUS `PvSystem.Find()` 결과를 Python dataclass 목록으로 반환한다.
  - display id, serial, MAC, IP, interface index, device index를 포함한다.
- `connect(connection_id: str | None = None) -> LineScanCamera`
  - `connection_id`가 없으면 발견된 첫 번째 device 또는 명시적 선택 전략을 사용한다.
  - 연결 실패 시 eBUS result code/description을 포함한 예외를 던진다.
- `LineScanCamera.close()`
  - acquisition/stream/pipeline 상태와 무관하게 idempotent하게 정리한다.
- `LineScanCamera` context manager
  - `with connect(...) as cam:` 형태로 사용 가능해야 한다.

### 2.2 Safe GenICam parameter API

필수 API:

- `get_param(name: str) -> Any | None`
- `read_param(name: str, default: Any = None) -> Any`
- `read_param_string(name: str) -> str`
- `set_param(name: str, value: Any, required: bool = False) -> bool`
- `execute_command(name: str, required: bool = False) -> bool`
- `snapshot(names: Iterable[str] | None = None) -> dict[str, str]`

설계 원칙:

- eBUS parameter가 없거나 읽기/쓰기 실패할 때 raw exception 대신 원인을 담은 wrapper exception 또는 `False`를 반환한다.
- `required=True`인 설정은 실패하면 즉시 예외를 던진다.
- `required=False`인 optional 설정은 warning/loggable result로 처리한다.

### 2.3 Camera acquisition configuration presets

필수 dataclass:

- `CameraConfig`
  - `acquisition_mode: str = "Continuous"`
  - `trigger_mode: str | None = None`
  - `trigger_selector: str | None = None`
  - `trigger_source: str | None = None`
  - `trigger_activation: str | None = None`
  - `exposure_mode: str | None = None`
  - `exposure_time: float | None = None`
  - `pixel_format: str | None = None`
  - `width: int | None = None`
  - `height: int | None = None`
  - `offset_x: int | None = None`
  - `offset_y: int | None = None`
  - `line_rate: float | None = None`
  - `gain: float | None = None`

필수 helper:

- `configure(config: CameraConfig) -> ConfigReport`
- `configure_internal_line_rate(...)`
  - `TriggerMode=Off`, `AcquisitionLineRate`, optional `ExposureMode/ExposureTime` 설정
- `configure_external_line_trigger(...)`
  - `TriggerMode=On`, `TriggerSelector=LineStart`, `TriggerSource`, `TriggerActivation`, `ExposureMode` 설정
- `set_roi(width, height, offset_x=0, offset_y=0)`
- `set_pixel_format(pixel_format)`
- `set_gain(gain, selector="DigitalAll")`

주의사항:

- SW-4005M-5GE는 monochrome 모델이므로 color-only feature는 기본 API에서 제외한다.
- `ExposureTime + 2.06 µs`가 line period/trigger period보다 길어질 수 있다는 점을 docs와 validation warning에 반영한다.
- Full width에서 `Mono10`/`Mono12`는 최대 line rate가 72 kHz로 제한될 수 있음을 warning으로 제공한다.

### 2.4 GigE transport / throughput tuning

필수 dataclass:

- `TransportConfig`
  - `buffer_count: int = 64`
  - `packet_size: int | None = None`
  - `safety_margin: int | None = 92`
  - `scp_delay: int | None = 0`
  - `negotiate_packet_size: bool = True`
  - `request_missing_packets: bool | None = None`
  - `enable_missing_packets_list: bool | None = None`

필수 helper:

- `configure_transport(config: TransportConfig) -> TransportReport`
- `estimate_payload_bandwidth(width, height, pixel_format, frame_rate=None, line_rate=None) -> float`
- `read_transport_snapshot() -> dict[str, str]`

`high_throughput_pipeline.py`에서 가져올 기본값/패턴:

- buffer count 기본값은 원본 예제 16보다 큰 64로 둔다.
- 최대 성능 테스트에서는 128도 쉽게 설정 가능해야 한다.
- `NegotiatePacketSize()` 후 필요 시 `GevSCPSPacketSize` 수동 override를 허용한다.
- `NetworkThroughputSafetyMargin=92`, `GevSCPD=0`을 기본 기준으로 둔다.
- packet size 7976은 NIC MTU/jumbo frame이 준비된 경우에만 사용하도록 문서화한다.

### 2.5 Streaming / acquisition API

필수 API:

- `start_streaming(config: StreamConfig | None = None) -> None`
- `stop_streaming() -> None`
- `grab(timeout_ms: int = 1000, copy: bool = False) -> FrameResult`
- `frames(timeout_ms: int = 1000, stop_after: int | None = None, duration_s: float | None = None) -> Iterator[FrameResult]`
- `run(callback: Callable[[FrameResult], None], duration_s: float | None = None, stop_after: int | None = None) -> StreamStats`

필수 dataclass:

- `FrameResult`
  - `ok: bool`
  - `block_id: int | None`
  - `width: int | None`
  - `height: int | None`
  - `pixel_format: str | None`
  - `acquired_size: int`
  - `image_data: Any | None`
  - `timestamp_ns: int`
  - `result_code: str | None`
  - `operational_code: str | None`
  - `stats_delta: StreamStats`

중요 구현 원칙:

- `PvPipeline`을 기본 수신 방식으로 사용한다.
- `RetrieveNextBuffer()` 성공 여부와 `operational_result`를 모두 확인한다.
- callback에서 예외가 발생해도 가능한 한 `ReleaseBuffer()`를 `finally`에서 수행한다.
- copy하지 않은 `image_data`는 buffer release 이후 유효하지 않을 수 있으므로 API 문서에 명확히 적는다.
- 장시간 처리/저장은 callback에서 바로 하지 말고 별도 queue/thread로 넘기는 패턴을 예제로 제공한다.

### 2.6 Stream error/statistics

필수 dataclass:

- `StreamStats`
  - `received_buffers`
  - `ok_buffers`
  - `error_buffers`
  - `retrieve_failures`
  - `operational_errors`
  - `bytes_acquired`
  - `lost_packets`
  - `recovered_packets`
  - `recovered_single_resend_packets`
  - `resend_groups_requested`
  - `resend_packets_requested`
  - `missing_packet_ids`
  - `ignored_packets`
  - `redundant_packets`
  - `out_of_order_packets`
  - `missing_blocks`
  - `last_block_id`

계산 property:

- `frame_error_rate`
- `retrieve_failure_rate`
- `packet_recovery_rate`
- `block_missing_rate`
- `average_bandwidth_mbps(elapsed_s)`

진단 helper:

- `classify_stream_health(stats: StreamStats) -> HealthStatus`
  - `OK`: 손실/누락 없음
  - `WARNING`: resend가 증가하지만 최종 loss는 없음
  - `DANGER`: lost packet, missing block, operational error 발생

### 2.7 Diagnostics / report

필수 API:

- `print_snapshot()` 또는 `snapshot_report() -> str`
- `run_diagnostic(duration_s=10, ...) -> DiagnosticReport`
- `DiagnosticReport.to_dict()` / `to_markdown()`

포함할 항목:

- device model, serial, IP/MAC
- Width, Height, PixelFormat, AcquisitionLineRate, TriggerMode, ExposureMode, ExposureTime
- GevSCPSPacketSize, GevSCPD, NetworkThroughputSafetyMargin
- eBUS stream `AcquisitionRate`, `Bandwidth`, payload size
- local acquired-byte 기반 bandwidth
- StreamStats 전체
- 설정 대비 이론 bandwidth 추정값

### 2.8 UserSet / calibration helper

1차 구현에서는 안전한 thin wrapper만 제공한다.

- `load_user_set(name: str)`
- `save_user_set(name: str)`
- `run_dsnu_calibration(slot: str)`
- `run_prnu_calibration(slot: str)`
- `run_shading_calibration(slot: str)`

주의:

- UserSet 저장/로드는 acquisition 정지 상태에서만 수행한다.
- DSNU/PRNU/Shading은 네트워크 packet error correction이 아니라 이미지 품질 보정이다.
- calibration 함수는 실제 조명/렌즈/캡 조건이 필요하므로 기본 자동 실행 대상이 아니다.

### 2.9 Connection recovery

1차 구현 범위:

- `close()`가 어떤 중간 상태에서도 안전하게 동작
- streaming 중 exception 발생 시 `AcquisitionStop`, `StreamDisable`, `pipeline.Stop`, `stream.Close`, `device.Disconnect` 순서로 최대한 정리

2차 구현 후보:

- `enable_recovery=True` 옵션
- `PvDeviceEventSink.OnLinkDisconnected()` 기반 flag 처리
- reconnect loop
- 재연결 후 기존 `CameraConfig`/`TransportConfig` 재적용

`ConnectionRecovery.py`의 중요한 규칙:

- link-disconnect callback 안에서 직접 disconnect/free를 수행하지 않는다.
- callback은 flag만 세우고 main loop가 teardown/reconnect를 담당한다.

---

## 3. 제안할 공개 API 예시

```python
from linescan_module import (
    connect,
    CameraConfig,
    TransportConfig,
)

camera_config = CameraConfig(
    acquisition_mode="Continuous",
    trigger_mode="Off",
    pixel_format="Mono8",
    width=4096,
    height=256,
    line_rate=84000,
)

transport_config = TransportConfig(
    buffer_count=128,
    packet_size=7976,
    safety_margin=92,
    scp_delay=0,
)

with connect() as cam:
    cam.configure(camera_config)
    cam.configure_transport(transport_config)
    print(cam.snapshot_report())

    def on_frame(frame):
        if not frame.ok:
            print(frame.operational_code)
        # 필요한 경우 frame.image_data를 즉시 copy하거나 별도 queue로 넘긴다.

    stats = cam.run(on_frame, duration_s=10)
    print(stats)
```

간단한 single grab 예시:

```python
with connect() as cam:
    cam.configure_internal_line_rate(
        pixel_format="Mono8",
        width=4096,
        height=256,
        line_rate=84000,
        exposure_time=5.0,
    )
    frame = cam.grab(timeout_ms=1000, copy=True)
    assert frame.ok
    print(frame.block_id, frame.width, frame.height)
```

---

## 4. 구현 작업 계획

### Task 1: 테스트 가능한 eBUS adapter 계층 만들기

**Objective:** 실제 eBUS가 없는 환경에서도 unit test가 가능한 내부 adapter/helper를 만든다.

**Files:**
- Modify: `linescan_module.py`
- Create: `tests/test_linescan_params.py`

**Steps:**
1. `LineScanError`, `EBusResultError`, `_result_ok()`, `_describe_result()`를 추가한다.
2. `get_param`, `read_param`, `set_param`, `execute_command` helper를 구현한다.
3. fake result/parameter object로 성공/실패 unit test를 작성한다.
4. Run: `python3 -m py_compile linescan_module.py`
5. Run: `pytest tests/test_linescan_params.py -v` 또는 pytest가 없으면 `python3 -m unittest` 기반으로 대체한다.
6. Commit: `feat: add safe eBUS parameter helpers`

### Task 2: Device discovery dataclass와 list_devices 구현

**Objective:** `DeviceFinder.py` 패턴을 library API로 제공한다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_discovery.py`

**Steps:**
1. `DeviceInfo` dataclass를 추가한다.
2. `list_devices(eb_module=None)`를 구현한다.
3. fake `PvSystem`, interface, device info로 GEV device 변환 test를 작성한다.
4. eBUS import는 lazy 처리하여 `--help`/unit test 환경이 proprietary module에 묶이지 않게 한다.
5. Commit: `feat: add device discovery helpers`

### Task 3: LineScanCamera lifecycle 구현

**Objective:** connect/open/close/context manager의 안전한 lifecycle을 만든다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_lifecycle.py`

**Steps:**
1. `LineScanCamera` class를 만든다.
2. `connect(connection_id=None, eb_module=None)` helper를 만든다.
3. `PvDevice.CreateAndConnect`, `PvStream.CreateAndOpen` 실패 처리를 구현한다.
4. `close()`를 idempotent하게 구현한다.
5. context manager `__enter__`, `__exit__`를 추가한다.
6. fake device/stream으로 close 순서와 중복 close test를 작성한다.
7. Commit: `feat: add camera lifecycle wrapper`

### Task 4: CameraConfig와 기본 설정 API 구현

**Objective:** 카메라 acquisition, ROI, pixel format, trigger, exposure, gain 설정을 dataclass 기반으로 적용한다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_config.py`

**Steps:**
1. `CameraConfig`, `ConfigReport` dataclass를 추가한다.
2. `LineScanCamera.configure(config)`를 구현한다.
3. `configure_internal_line_rate()`와 `configure_external_line_trigger()` convenience method를 구현한다.
4. `set_roi`, `set_pixel_format`, `set_gain`을 구현한다.
5. 설정 순서는 acquisition stop → trigger/acquisition/image/exposure/gain 순서로 적용한다.
6. unsupported optional parameter는 warning/report에 남기고 계속 진행한다.
7. Commit: `feat: add camera configuration presets`

### Task 5: TransportConfig와 PvPipeline 생성 구현

**Objective:** 5GigE 고속 수신에 필요한 transport/pipeline 설정을 모듈화한다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_transport.py`

**Steps:**
1. `TransportConfig`, `TransportReport` dataclass를 추가한다.
2. `configure_transport(config)`를 구현한다.
3. GigE device이면 `NegotiatePacketSize()`와 `SetStreamDestination()`을 수행한다.
4. `GevSCPSPacketSize`, `NetworkThroughputSafetyMargin`, `GevSCPD` optional setting을 구현한다.
5. `_create_pipeline(buffer_count)`를 구현한다.
6. payload size가 0 이하이면 예외 처리한다.
7. Commit: `feat: add transport and pipeline setup`

### Task 6: FrameResult와 StreamStats 구현

**Objective:** 취득 결과와 에러 통계를 고수준 dataclass로 제공한다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_stats.py`

**Steps:**
1. `StreamStats`, `FrameResult`, `HealthStatus` dataclass/enum을 추가한다.
2. `StreamStats.update_from_buffer(pvbuffer, operational_result)`를 구현한다.
3. block id gap 계산을 구현한다.
4. packet counter safe read를 구현한다.
5. error rate property를 구현한다.
6. fake pvbuffer로 lost/recovered/missing block test를 작성한다.
7. Commit: `feat: add stream statistics models`

### Task 7: Streaming API 구현

**Objective:** `grab`, `frames`, `run`으로 쉬운 이미지 취득 API를 제공한다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_streaming.py`

**Steps:**
1. `start_streaming()`을 구현한다.
   - pipeline start
   - device stream enable
   - `AcquisitionStart.Execute()`
2. `stop_streaming()`을 구현한다.
   - `AcquisitionStop.Execute()`
   - `StreamDisable()`
   - `pipeline.Stop()`
3. `grab(timeout_ms=1000, copy=False)`를 구현한다.
4. `frames(...)` iterator를 구현한다.
5. `run(callback, ...)`를 구현한다.
6. `RetrieveNextBuffer()` 후 `ReleaseBuffer()`가 항상 호출되는지 test한다.
7. callback exception 발생 시 cleanup이 유지되는지 test한다.
8. Commit: `feat: add streaming acquisition API`

### Task 8: Diagnostics report 구현

**Objective:** 사용자가 한 번에 설정/대역폭/에러 상태를 확인할 수 있는 리포트를 제공한다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_diagnostics.py`
- Modify: `README.md` 또는 새 문서 `docs/usage.md`

**Steps:**
1. `DiagnosticReport` dataclass를 추가한다.
2. `snapshot(names=None)`, `snapshot_report()`를 구현한다.
3. `run_diagnostic(duration_s=10, ...)`를 구현한다.
4. `estimate_payload_bandwidth()`를 구현한다.
5. `to_dict()` / `to_markdown()`을 구현한다.
6. README에 quick start 예시를 추가한다.
7. Commit: `feat: add diagnostics report helpers`

### Task 9: UserSet과 calibration thin wrapper 구현

**Objective:** 위험한 자동화 없이 필요한 명령을 안전하게 호출할 수 있는 helper를 제공한다.

**Files:**
- Modify: `linescan_module.py`
- Create/Modify: `tests/test_linescan_calibration.py`

**Steps:**
1. `load_user_set(name)`, `save_user_set(name)`를 구현한다.
2. acquisition 중이면 저장/로드를 막거나 먼저 명시적으로 중지하도록 요구한다.
3. DSNU/PRNU/Shading calibration command wrapper를 추가한다.
4. calibration 결과 parameter read helper를 추가한다.
5. docs에 보정은 네트워크 error correction이 아니라 이미지 품질 보정이라고 명시한다.
6. Commit: `feat: add userset and calibration helpers`

### Task 10: Hardware smoke test script 추가

**Objective:** 실제 카메라 연결 환경에서 최소 검증을 쉽게 수행한다.

**Files:**
- Create: `examples/simple_grab.py`
- Create: `examples/diagnostic_run.py`
- Modify: `README.md`

**Steps:**
1. `examples/simple_grab.py`를 작성한다.
2. `examples/diagnostic_run.py`를 작성한다.
3. high-throughput 조건 예시를 README에 추가한다.
4. 실제 hardware가 연결되어 있으면 다음을 실행한다.
   - `python3 examples/diagnostic_run.py --duration 10 --height 256 --line-rate 84000`
5. hardware가 없으면 import/argparse/py_compile까지만 검증하고 hardware 미연결을 명시한다.
6. Commit: `docs: add line scan module usage examples`

---

## 5. 초기 구현에서 제외할 범위

YAGNI 원칙상 1차 구현에서는 아래를 제외한다.

- Multi-source 동시 취득
- Multi-part payload 전용 고수준 처리
- SoftDeviceGEV transmitter 구현
- GUI/OpenCV display loop
- 자동 reconnect full implementation
- color camera 전용 기능
- calibration 자동 절차 전체 자동화
- multiprocessing 기반 저장 pipeline

필요하면 기본 wrapper가 안정화된 후 별도 plan으로 확장한다.

---

## 6. 검증 전략

### 6.1 Hardware 없이 가능한 검증

- `python3 -m py_compile linescan_module.py`
- fake eBUS object 기반 unit tests
- lifecycle cleanup 순서 test
- `ReleaseBuffer()` 보장 test
- stats 계산 test
- diagnostic markdown 생성 test

### 6.2 Hardware 연결 후 검증

- device discovery
  - `list_devices()`가 serial/IP/MAC을 반환하는지 확인
- 기본 snapshot
  - model, width, height, pixel format, line rate, trigger mode 출력 확인
- 내부 line rate 취득
  - `TriggerMode=Off`, `AcquisitionLineRate` 설정 후 10초 수신
- 고속 조건
  - `PixelFormat=Mono8`, `Width=4096`, `Height=256`, `LineRate=84000`, `buffer_count=128`
- error counter
  - lost packet, recovered packet, block gap, operational error가 기대 범위인지 확인
- cleanup
  - Ctrl-C 또는 exception 이후에도 재실행 가능한지 확인

---

## 7. 피드백 받고 싶은 결정 사항

개발 전에 아래 방향을 확인하면 좋다.

1. `linescan_module.py` 단일 파일로 시작할지, `linescan_module/` 패키지로 분리할지
   - 추천: 초기에는 단일 파일, 기능이 커지면 패키지 분리
2. frame data는 기본적으로 zero-copy로 줄지, 항상 copy해서 안전성을 우선할지
   - 추천: 기본 zero-copy + `copy=True` 옵션
3. callback 기반 API와 iterator 기반 API 중 어떤 사용 방식을 주력 예제로 둘지
   - 추천: 둘 다 제공, 예제는 callback 기반 diagnostic와 iterator 기반 simple grab 제공
4. recovery는 1차에 포함할지 2차로 미룰지
   - 추천: 1차는 안전한 cleanup, 2차에서 reconnect
5. hardware smoke test를 README에 어느 정도까지 포함할지
   - 추천: high-throughput 조건 예시는 포함하되 MTU/jumbo frame 주의사항을 강조

---

## 8. 완료 기준

사용자가 개발 착수를 승인한 뒤 최종 구현은 아래를 만족해야 한다.

- `linescan_module.py`에서 discovery, connection, configuration, streaming, stats, diagnostics를 제공한다.
- hardware가 없어도 fake eBUS unit test가 통과한다.
- hardware 연결 시 `examples/diagnostic_run.py`로 10초 수신 리포트를 만들 수 있다.
- `high_throughput_pipeline.py`에서 검증한 5GigE 고속 수신 설정을 module API로 재현할 수 있다.
- 모든 buffer 수신 경로에서 release/requeue가 보장된다.
- 실패 시 eBUS result code/description이 예외나 report에 보존된다.
- 구현 commit은 task 단위로 작게 나눈다.
