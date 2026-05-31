# SW-2005-5GE Python Wrapper 개발 방향

## 1. 문서 목적

이 문서는 본 저장소의 `README.md`, JAI/Pleora eBUS Python SWIG wrapper인 `eBUS.py`, `eBUS_examples` 예제 코드, 그리고 `SW-2005-5GE_usermanual.pdf`를 바탕으로 `linescan_module.py`를 어떤 방향으로 개발할지 정리한 개발 기준 문서이다.

핵심 목표는 JAI SW-2005-5GE 라인스캔 카메라를 Python에서 안정적으로 제어하기 위해, 저수준 eBUS SDK API를 직접 노출하지 않고 실제 사용 흐름 중심의 고수준 wrapper를 제공하는 것이다.

## 2. 현재 저장소 상태 요약

### 2.1 README 기준 자료 구성

`README.md`에 따르면 이 저장소는 다음 자료를 기반으로 한다.

- `eBUS.py`: JAI/Pleora eBUS Python SDK의 SWIG wrapper. 자동 생성 파일이며 직접 수정 대상이 아니다.
- `eBUS_examples/`: eBUS API 사용 예제. 연결, 스트리밍, GenICam 파라미터, 이벤트, 복구, 소프트 디바이스 예제가 포함된다.
- `SW-2005-5GE_usermanual.pdf`: SW-2005/4005 5GE 라인스캔 카메라 사용자 매뉴얼.
- `linescan_module.py`: 앞으로 개발할 사용자 친화 wrapper 라이브러리. 현재는 `import eBUS as eb`와 TODO만 존재한다.
- `docs/`: 개발 문서 저장 위치.

### 2.2 eBUS.py 성격

`eBUS.py`는 SWIG 4.3.1로 자동 생성된 파일이며, 내부적으로 `_ebus_python` C/C++ 확장 모듈을 import한다.

주요 API 묶음은 다음과 같다.

- 결과/문자열/로그
  - `PvResult`, `PvString`, `PvLogger`, `PvLogSink`
- 이미지/버퍼/페이로드
  - `PvBuffer`, `PvImage`, `PvRawData`, `PvChunkData`, `IPvMultiPartContainer*`
  - 페이로드 타입: Image, RawData, ChunkData, MultiPart, 압축 페이로드 등
  - 픽셀 타입: Mono8/10/12/16, RGB8, Bayer, BiColor 계열 등
- GenICam 파라미터
  - `PvGenParameterArray`, `PvGenInteger`, `PvGenFloat`, `PvGenEnum`, `PvGenBoolean`, `PvGenCommand`, `PvGenString`, `PvGenRegister`
  - `PvGenStateStack`으로 임시 파라미터 상태 변경 가능
- 디바이스 탐색/연결
  - `PvSystem`, `PvDeviceInfo*`, `PvInterface`, `PvNetworkAdapter`
  - `PvDevice`, `PvDeviceGEV`, `PvDeviceU3V`
- 스트림/파이프라인
  - `PvStream`, `PvStreamGEV`, `PvPipeline`, `PvPipelineEventSink`
- 이벤트/복구
  - `PvDeviceEventSink`, `PvDevice.RegisterEventSink`, `OnLinkDisconnected`, `OnEvent`, `OnEventGenICam`
- GigE Vision 전송 설정
  - `PvDeviceGEV.NegotiatePacketSize()`, `SetStreamDestination()`, `ResetStreamDestination()`
- 시리얼 포트
  - `PvDeviceSerialPort`
- SoftDevice GEV
  - `PvSoftDeviceGEV`, `IPvSoftDeviceGEVEventSink`, `PvStreamingChannelSourceDefault` 등

개발 원칙: `eBUS.py`는 vendor SDK wrapper이므로 수정하지 않고, `linescan_module.py`에서 안전한 추상화 계층을 만든다.

## 3. SW-2005-5GE 매뉴얼 핵심 사항

### 3.1 카메라 특성

매뉴얼 대상 모델은 다음을 포함한다.

| 모델 | 센서 | 유효 픽셀 | 픽셀 크기 | 최대 라인레이트 |
| --- | --- | --- | --- | --- |
| SW-2005TL-5GE | Trilinear RGB | 2048 x 3 | 7.0um x 7.0um | 44 kHz |
| SW-2005M-5GE | Mono | 2048 x 1 | 7.0um x 7.0um | 172 kHz |
| SW-4005BL-5GE | Bilinear RB-G | 4096 x 2 | 3.5um x 3.5um | 42 kHz |
| SW-4005M-5GE | Mono | 4096 x 1 | 3.5um x 3.5um | 84 kHz |

공통 특징:

- 5GBASE-T GigE Vision 인터페이스
- PoE 지원
- C-mount
- 직접 encoder 연결 지원
- 다양한 trigger 옵션
- 라인스캔에 필요한 PRNU, DSNU, shading correction, binning, LUT, color space conversion 등 지원

### 3.2 물리 연결과 I/O

- RJ-45: GigE Vision 인터페이스. Cat5e 이상, Cat6 권장.
- DC IN/TRIG 12핀 커넥터:
  - DC 10.8~26.4V 입력
  - TTL In/Out, Opto In 제공
  - DigitalIOControl과 관련됨
- LINK LED:
  - 느린 녹색 점멸: 1000BASE-T
  - 빠른 녹색 점멸: 2.5GBASE-T 또는 5GBASE-T

개발 시 wrapper에는 카메라 연결 자체뿐 아니라 링크 속도, payload size, packet size, stream bandwidth 상태를 확인하는 진단 기능이 필요하다.

### 3.3 ROI와 이미지 포맷

ROI는 `ImageFormatControl`의 `Width`, `OffsetX`, `Height`, `OffsetY`로 설정한다.

SW-2005 기준:

- WidthMax: 2048
- Binning Off:
  - Width: 128 ~ 2048 - OffsetX, step 8
  - OffsetX: 0 ~ 2048 - Width, step 8
- Binning On:
  - WidthMax: 1024
  - Width: 64 ~ 1024 - OffsetX, step 8
  - OffsetX: 0 ~ 1024 - Width, step 8
- Height: 1 ~ 4096 - OffsetY
- OffsetY: 0 ~ 4096 - Height
- vertical binning은 지원하지 않는다.
- 카메라는 `Width x Height`를 1 block으로 전송한다.

Wrapper는 ROI 값 검증을 SDK 에러에만 맡기지 말고 모델별 width/step 제약을 사전에 검사해야 한다.

### 3.4 Binning

- 지원 항목: `BinningHorizontal`
- 방식: Digital(FPGA)
- 모드: Sum, Average
- SW-2005에서 horizontal binning On 시 virtual pixel은 14um x 7um, WidthMax는 1024가 된다.

Wrapper API는 `set_binning(enabled=True, mode="Sum" | "Average")`처럼 명시적이어야 하며, binning 변경 후 ROI/PayloadSize 재계산이 필요하다.

### 3.5 Trigger, Exposure, Line Rate

매뉴얼은 trigger/exposure/line rate 제어를 5가지 시나리오로 구분한다.

1. 외부 trigger + 지정 exposure time
   - `TriggerMode=On`
   - `TriggerSelector=LineStart`
   - `TriggerSource=Any` 또는 실제 입력 라인
   - `TriggerActivation=RisingEdge/FallingEdge`
   - `ExposureMode=Timed`
   - 외부 trigger 주기가 line rate를 결정하며, `ExposureTime`은 trigger period보다 길 수 없다.
2. 외부 trigger + exposure time 미지정
   - `TriggerMode=On`
   - `ExposureMode=Off`
   - 노출 시간은 line rate 기반으로 계산된다.
3. 외부 trigger width로 exposure 제어
   - `TriggerMode=On`
   - `TriggerActivation=LevelHigh/LevelLow`
   - `ExposureMode=TriggerWidth`
4. 외부 trigger 없음 + 지정 exposure time
   - `TriggerMode=Off`
   - `ExposureMode=Timed`
   - `AcquisitionLineRate` 사용
5. 외부 trigger 없음 + exposure time 미지정
   - `TriggerMode=Off`
   - `ExposureMode=Off`
   - `AcquisitionLineRate` 사용

Wrapper는 사용자가 저수준 GenICam 이름을 모두 외우지 않아도 되도록 다음과 같은 고수준 preset을 제공하는 것이 좋다.

- `configure_free_run(line_rate_hz, exposure_us=None)`
- `configure_external_trigger(source, activation="RisingEdge", exposure_us=None)`
- `configure_trigger_width(source, active_level="High")`

### 3.6 영상 품질 보정 순서

매뉴얼의 권장 순서:

1. DSNU Correction / Pixel Black Correct
2. PRNU Correction / Pixel Gain Correct
3. Gain 조정
4. White Balance 조정, color model only
5. Black Level 조정

개발 방향:

- 보정 기능은 초기 MVP에서는 직접 자동화하지 않아도 되지만, GenICam command wrapper와 사용자 절차 문서를 준비해야 한다.
- 추후 `calibrate_dsnu()`, `calibrate_prnu()`, `set_gain()`, `set_black_level()`, `set_white_balance()` API로 확장한다.

### 3.7 Chunk Data, Event, PTP, Action

- Chunk Data:
  - `ChunkModeActive=True`
  - `ChunkSelector` 선택
  - `ChunkEnable=True`
  - image output 중에는 설정 변경 불가. 변경 전 acquisition stop 필요.
  - chunk 예: OffsetX/Y, Width/Height, PixelFormat, Timestamp, LineStatus, CounterValue, ExposureTime, Gain, BlackLevel, DeviceSerialNumber, DeviceUserID, DeviceTemperature 등
- Event:
  - `EventSelector` 선택 후 `EventNotification=On`
  - AcquisitionStart/Stop 외 이벤트를 켜면 line rate 제한 발생
    - 1개 이벤트: 최대 약 6 kHz
    - 2개 이벤트: 최대 약 4 kHz
    - 3개 이상은 권장하지 않음
- PTP:
  - `GevIEEE1588=True`
  - timestamp tick frequency는 1 GHz 고정
  - PTP sync 중 timestamp reset은 비활성화
- Action Control:
  - PTP와 함께 scheduled action command로 다중 카메라 동기화 가능

MVP에서는 chunk timestamp와 frame/block id를 안정적으로 얻는 기능이 중요하다. Event/PTP/Action은 고급 기능으로 단계적으로 추가한다.

## 4. eBUS 예제 코드에서 가져올 구현 패턴

### 4.1 기본 획득 흐름: PvStreamSample.py

핵심 순서:

1. `PvDevice.CreateAndConnect(connection_ID)`
2. `PvStream.CreateAndOpen(connection_ID)`
3. GigE Vision인 경우:
   - `device.NegotiatePacketSize()`
   - `device.SetStreamDestination(stream.GetLocalIPAddress(), stream.GetLocalPort())`
4. `device.GetPayloadSize()`로 buffer size 확인
5. `PvBuffer().Alloc(size)`로 buffer 여러 개 생성
6. `stream.QueueBuffer(pvbuffer)`
7. `device.GetParameters().Get("AcquisitionStart")`, `AcquisitionStop` command 획득
8. `device.StreamEnable()` 후 `AcquisitionStart.Execute()`
9. loop에서 `stream.RetrieveBuffer(timeout_ms)`
10. `operational_result.IsOK()`면 payload 처리
11. 처리 후 반드시 `stream.QueueBuffer(pvbuffer)` 재등록
12. 종료 시 `AcquisitionStop.Execute()`, `device.StreamDisable()`, `stream.AbortQueuedBuffers()`, `stream.Close()`, `device.Disconnect()`

### 4.2 안정적 획득 흐름: PvPipelineSample.py

`PvPipeline`은 buffer queue 관리를 단순화한다.

핵심 순서:

1. `pipeline = eb.PvPipeline(stream)`
2. `pipeline.SetBufferCount(BUFFER_COUNT)`
3. `pipeline.SetBufferSize(device.GetPayloadSize())`
4. acquisition 시작 전에 `pipeline.Start()`
5. loop에서 `pipeline.RetrieveNextBuffer(timeout_ms)`
6. 처리 후 `pipeline.ReleaseBuffer(pvbuffer)`
7. 종료 시 `pipeline.Stop()`

개발 방향: 일반 사용자는 `PvStream` 직접 buffer 관리보다 `PvPipeline` 기반이 안전하다. `linescan_module.py`의 기본 acquisition backend는 `PvPipeline`으로 잡는다.

### 4.3 GenICam 파라미터 접근: GenICamParameters.py

예제는 다음 패턴을 보여준다.

- `device.GetParameters()`로 device node map 접근
- `stream.GetParameters()`로 stream 통계/설정 접근
- `parameter.GetType()`으로 Integer/Enum/Boolean/String/Command/Float 구분
- `IsVisible(PvGenVisibilityGuru)`, `IsAvailable()`, `IsReadable()`, `IsWritable()` 확인
- 예: `Width`를 읽고 max로 변경한 뒤 원래 값 복구

개발 방향:

- 공통 helper 필요:
  - `get_param(name)`
  - `get_value(name)`
  - `set_value(name, value)`
  - `execute_command(name)`
  - `dump_parameters(visibility="Guru")`
- `PvResult` 실패 시 code string/description을 포함한 Python exception으로 변환한다.

### 4.4 이미지 처리: ImageProcessing.py

예제는 `pvbuffer.GetImage().GetDataPointer()`가 numpy 배열처럼 사용 가능함을 보여준다.

주의점:

- 기본 표시/처리는 Mono8, RGB8만 직접 다룬다.
- 다른 pixel format은 변환 또는 명시적 미지원 처리가 필요하다.
- OpenCV 표시 시 RGB8은 BGR 변환 필요.

개발 방향:

- MVP에서 `Mono8` 우선 지원
- `RGB8` 지원은 OpenCV BGR 변환 옵션 제공
- `Mono10/12/16`, packed format, BiColor 포맷은 별도 변환 정책 필요

### 4.5 연결 복구: ConnectionRecovery.py

예제는 `PvDeviceEventSink`를 상속하고 `OnLinkDisconnected` 콜백을 이용해 cable pull/network loss 상황을 처리한다.

주요 패턴:

- device event sink 등록: `device.RegisterEventSink(self)`
- stream/pipeline 정리와 재생성 분리
- GigE Vision 재시작 시 `SetStreamDestination()` 재설정
- 같은 IP로 돌아온다는 가정이 있으며, IP가 바뀔 수 있으면 MAC 기반 connection ID가 더 적합하다고 주석 설명

개발 방향:

- 초기에는 명시적 `reconnect()` API 제공
- 이후 자동 복구 옵션 `auto_reconnect=True` 추가
- reconnect 정책은 IP 기반과 MAC 기반을 구분해야 한다.

### 4.6 DeviceFinder.py

예제는 `PvSystem.Find()`로 interface와 device를 열거하고, GEV device의 MAC/IP/serial/display ID를 출력한다.

개발 방향:

- `list_cameras()` API 제공
- 반환값은 display_id, serial, model, vendor, connection_id, mac, ip, interface 정보를 가진 dataclass로 정리

### 4.7 EventSample.py

`PvDevice.RegisterEventSink()`를 이용해 이벤트 콜백을 수신한다.

개발 방향:

- MVP에서는 이벤트 기능을 직접 노출하지 않고, chunk timestamp/metadata 확보를 우선한다.
- 추후 `register_event_callback(event_name, callback)` 형태로 확장한다.

### 4.8 ReceiveMultiPart.py, MultiSource.py

multi-part stream을 source/channel 단위로 선택하고 `PvPipeline`으로 수신한다.

개발 방향:

- SW-2005 단일 카메라 MVP에서는 multi-part를 필수 기능으로 보지 않는다.
- 단, color model/특수 payload나 chunk 결합 가능성을 고려해 payload type 분기는 열어둔다.

### 4.9 SoftDeviceGEV 계열 예제

`SoftDeviceGEV*.py` 및 하위 모듈은 PC가 GigE Vision device처럼 동작하는 transmitter 구현 예제다.

본 저장소의 목적이 실제 SW-2005-5GE host application wrapper라면 직접 개발 우선순위는 낮다. 다만 다음 구현 아이디어는 참고할 수 있다.

- `PvStreamingChannelSourceDefault` 상속 구조
- custom register와 GenApi feature 생성
- user set notify
- chunk/event/register sink 구현 방식
- file access register 구현

## 5. 권장 아키텍처

### 5.1 모듈 구조

초기에는 `linescan_module.py` 단일 파일로 시작하되, 기능이 커지면 아래처럼 분리한다.

```text
linescan_module.py              # 공개 API re-export 또는 단일 MVP 구현
linescan/
  __init__.py
  camera.py                     # LineScanCamera main class
  discovery.py                  # list_cameras, CameraInfo
  params.py                     # GenICam helper, typed set/get
  acquisition.py                # pipeline/stream acquisition loop
  image.py                      # PvBuffer -> numpy 변환, pixel format 처리
  config.py                     # CameraConfig dataclass
  errors.py                     # eBUS result -> Python exception
  constants.py                  # GenICam node names, model constraints
examples/
  grab_once.py
  stream_preview.py
  configure_free_run.py
  configure_external_trigger.py
```

현재 저장소에는 이미 `eBUS_examples`가 있으므로, 새 예제는 vendor 예제와 구분하기 위해 `examples/` 또는 `docs/examples/`에 두는 것이 좋다.

### 5.2 핵심 공개 API 초안

```python
from dataclasses import dataclass
from typing import Optional, Iterator, Callable

@dataclass
class CameraInfo:
    display_id: str
    connection_id: str
    serial_number: str | None = None
    model_name: str | None = None
    mac_address: str | None = None
    ip_address: str | None = None

@dataclass
class ROI:
    width: int
    height: int
    offset_x: int = 0
    offset_y: int = 0

@dataclass
class Frame:
    image: object          # numpy.ndarray로 확정 예정
    block_id: int
    timestamp_ns: int | None
    width: int
    height: int
    pixel_type: int
    metadata: dict

class LineScanCamera:
    @staticmethod
    def list_cameras(timeout_ms: int = 1000) -> list[CameraInfo]: ...

    def __init__(self, connection_id: str | None = None, *, auto_reconnect: bool = False): ...
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...

    def get_value(self, name: str): ...
    def set_value(self, name: str, value) -> None: ...
    def execute(self, command_name: str) -> None: ...
    def dump_parameters(self) -> dict: ...

    def configure_roi(self, roi: ROI) -> None: ...
    def configure_pixel_format(self, pixel_format: str = "Mono8") -> None: ...
    def configure_binning(self, enabled: bool, mode: str | None = None) -> None: ...

    def configure_free_run(self, line_rate_hz: float, exposure_us: float | None = None) -> None: ...
    def configure_external_trigger(self, source: str, activation: str = "RisingEdge", exposure_us: float | None = None) -> None: ...
    def configure_trigger_width(self, source: str, active_level: str = "High") -> None: ...

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def grab(self, timeout_ms: int = 1000) -> Frame: ...
    def frames(self, timeout_ms: int = 1000) -> Iterator[Frame]: ...
```

### 5.3 에러 처리 원칙

저수준 eBUS 호출은 대부분 `PvResult`를 반환한다. wrapper는 다음 규칙을 따른다.

- `result.IsOK()` 또는 `result.IsSuccess()` 확인을 누락하지 않는다.
- 실패 시 `EBusError`를 발생시킨다.
- error message에는 다음을 포함한다.
  - 호출한 operation 이름
  - `result.GetCodeString()`
  - `result.GetDescription()`
- acquisition loop에서는 timeout과 operational error를 구분한다.

예외 계층 초안:

```python
class LineScanError(Exception): ...
class EBusError(LineScanError): ...
class CameraNotFoundError(LineScanError): ...
class CameraConnectionError(LineScanError): ...
class AcquisitionError(LineScanError): ...
class UnsupportedPixelFormatError(LineScanError): ...
class InvalidCameraConfigurationError(LineScanError): ...
```

### 5.4 상태 관리 원칙

카메라 제어는 상태 순서가 중요하다.

권장 상태:

```text
DISCONNECTED -> CONNECTED -> STREAM_OPEN -> ACQUIRING -> STREAM_OPEN -> CONNECTED -> DISCONNECTED
```

중요 규칙:

- `AcquisitionStart` 전에 `pipeline.Start()` 또는 buffer queue 준비가 끝나야 한다.
- GigE Vision에서는 acquisition 전 `SetStreamDestination()`이 필요하다.
- `ChunkModeActive`, ROI, PixelFormat 같은 payload 관련 설정은 acquisition 중 변경하지 않는다.
- 종료는 항상 다음 순서로 한다.
  1. `AcquisitionStop.Execute()`
  2. `device.StreamDisable()`
  3. `pipeline.Stop()` 또는 `stream.AbortQueuedBuffers()`
  4. `stream.Close()`
  5. `device.Disconnect()`

Python context manager를 제공해 예외 발생 시에도 정리되게 해야 한다.

```python
with LineScanCamera() as cam:
    cam.configure_free_run(line_rate_hz=10000, exposure_us=50)
    frame = cam.grab()
```

## 6. 개발 단계별 로드맵

### Phase 0. 개발 환경/문서 정리

목표: vendor 파일과 개발 파일을 분리하고, 최소 실행 기준을 정한다.

작업:

- `eBUS.py`, `_ebus_python` 관련 파일은 vendor dependency로 취급하고 직접 수정 금지 명시
- README에 설치/실행 전제 추가
- docs에 본 개발 방향 문서 유지
- 최소 lint/import check 기준 마련

검증:

- `python3 -m py_compile linescan_module.py`
- 실제 카메라가 있는 환경에서는 `DeviceFinder.py` 또는 신규 `list_cameras()`로 탐색 확인

### Phase 1. Discovery와 연결 API

목표: 카메라 탐색과 연결/해제를 wrapper로 감싼다.

작업:

- `CameraInfo` dataclass 작성
- `list_cameras()` 구현
- `LineScanCamera.connect()` / `disconnect()` 구현
- `PvDeviceGEV` 여부 확인 및 IP/MAC/serial 정보 보관

검증:

- 카메라 미연결 시 빈 목록 반환 또는 명확한 예외
- 카메라 연결 시 display_id, serial, IP, MAC 출력
- 연결 후 `DeviceModelName`, `DeviceSerialNumber`, `DeviceLinkSpeed`, `PayloadSize` 조회

### Phase 2. GenICam 파라미터 helper

목표: 문자열 기반의 안전한 get/set/execute API를 만든다.

작업:

- `get_parameter(name)`
- `get_value(name)`
- `set_value(name, value)`
- enum/integer/float/boolean/string/command 타입별 처리
- readable/writable/available 검사
- `dump_parameters()` 구현

검증:

- `Width`, `Height`, `PixelFormat`, `AcquisitionLineRate`, `ExposureMode` 읽기
- writable인 값만 변경
- 변경 실패 시 eBUS code/description 포함 예외 발생

### Phase 3. 기본 acquisition MVP

목표: `PvPipeline` 기반으로 한 장 또는 연속 frame을 가져온다.

작업:

- stream open/close 구현
- `PvPipeline` 생성, buffer count/size 설정
- `start()` / `stop()` 구현
- `grab()` 구현
- `frames()` generator 구현
- `PvBuffer`에서 image, block_id, timestamp, width, height, pixel_type 추출

검증:

- 1 frame grab 성공
- 100 frame 연속 grab 중 누락/timeout 통계 출력
- 종료 후 재시작 가능

### Phase 4. SW-2005 설정 preset

목표: 라인스캔 사용자가 자주 쓰는 설정을 고수준 API로 제공한다.

작업:

- `configure_roi(ROI)`
- `configure_pixel_format("Mono8")`
- `configure_binning(enabled, mode)`
- `configure_free_run(line_rate_hz, exposure_us=None)`
- `configure_external_trigger(source, activation, exposure_us=None)`
- `configure_trigger_width(source, active_level)`
- 설정 전 acquisition stop 상태인지 검사

검증:

- SW-2005 model에서 ROI width step 8 검증
- invalid ROI 입력 시 SDK 호출 전 `InvalidCameraConfigurationError`
- free-run에서 `TriggerMode=Off`, `ExposureMode`, `AcquisitionLineRate` 확인
- external trigger에서 `TriggerMode=On`, `TriggerSelector=LineStart` 확인

### Phase 5. 이미지 변환과 저장

목표: OpenCV/numpy 처리에 바로 쓰기 쉬운 frame 형태를 제공한다.

작업:

- Mono8 numpy array 반환
- RGB8이면 RGB/BGR 선택 옵션
- unsupported pixel format이면 명확한 예외 또는 raw 반환 옵션
- frame 저장 helper: `.save_png()`, `.save_npy()` 또는 별도 utility

검증:

- Mono8 frame shape = `(height, width)`
- RGB8 frame shape = `(height, width, 3)`
- 저장 파일을 OpenCV/Pillow로 다시 읽기

### Phase 6. 진단/성능/복구

목표: 현장 운용에서 필요한 정보를 제공한다.

작업:

- stream 통계: `AcquisitionRate`, `Bandwidth`
- link 정보: `DeviceLinkSpeed`, packet size negotiation 결과
- payload size 변화 감지
- timeout/missing packet 카운트 기록
- 명시적 `reconnect()` 구현
- 선택적으로 `auto_reconnect` 구현

검증:

- cable unplug/replug 시 명확한 에러와 reconnect 시도
- acquisition 중 timeout 발생 시 복구 가능
- bandwidth/line rate 로그 출력

### Phase 7. 고급 기능

목표: 실제 라인스캔 운용 고급 요구를 단계적으로 지원한다.

후보:

- Chunk Data 활성화 및 metadata parsing
- DSNU/PRNU 보정 command wrapper
- gain/black level/white balance helper
- PTP 상태 확인 및 timestamp 동기화
- Event callback wrapper
- Action command / scheduled action support
- multi-camera 동기 획득

## 7. 우선순위 결정

MVP에서 반드시 필요한 기능:

1. 카메라 탐색
2. 연결/해제
3. GenICam get/set/execute
4. ROI, pixel format, line rate, exposure, trigger 설정
5. `PvPipeline` 기반 grab/frames
6. Mono8 numpy frame 반환
7. 안전한 종료와 예외 처리

나중으로 미룰 기능:

- SoftDeviceGEV transmitter
- FileAccess register 구현
- MultiPart 송수신 일반화
- 복잡한 color model 전용 보정
- PTP scheduled action
- GUI preview

## 8. 구현 시 주의사항

- `eBUS.py`는 자동 생성 vendor wrapper라 직접 수정하지 않는다.
- `linescan_module.py`의 API는 eBUS 객체를 그대로 반환하기보다 Python dataclass와 예외로 감싼다.
- 모든 eBUS 호출 후 `PvResult`를 확인한다.
- acquisition 중 payload 관련 설정 변경을 막는다.
- stream/pipeline/device 정리 순서를 엄격히 지킨다.
- GigE Vision에서는 `NegotiatePacketSize()`와 `SetStreamDestination()`을 빼먹지 않는다.
- `PvBuffer`는 처리 후 반드시 pipeline에 release하거나 stream에 requeue한다.
- OpenCV 표시용 변환과 데이터 분석용 원본 배열 반환을 분리한다.
- 카메라가 없는 환경에서도 unit test 가능한 영역과 실제 장비가 필요한 integration test를 분리한다.

## 9. 첫 구현 제안

가장 먼저 `linescan_module.py`에 다음 범위만 구현하는 것을 권장한다.

1. `CameraInfo`, `ROI`, `Frame` dataclass
2. `LineScanCamera.list_cameras()`
3. `LineScanCamera.connect()` / `disconnect()` context manager
4. `get_value()` / `set_value()` / `execute()`
5. `configure_free_run()`
6. `grab()` 1장 획득

초기 사용 예시는 다음 형태를 목표로 한다.

```python
from linescan_module import LineScanCamera, ROI

cameras = LineScanCamera.list_cameras()
print(cameras)

with LineScanCamera(cameras[0].connection_id) as cam:
    cam.configure_roi(ROI(width=2048, height=100, offset_x=0, offset_y=0))
    cam.configure_pixel_format("Mono8")
    cam.configure_free_run(line_rate_hz=10000, exposure_us=50)
    frame = cam.grab(timeout_ms=1000)
    print(frame.image.shape, frame.block_id, frame.timestamp_ns)
```

이 API가 안정화되면 trigger preset, chunk metadata, 저장 기능을 순차적으로 추가한다.
