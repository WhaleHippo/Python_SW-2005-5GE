# Line-scan GUI capture native crash investigation

이 문서는 `linescan_gui.py` 기반 SW-2005/4005 5GigE 라인스캔 카메라 GUI에서 관찰된 간헐적 native crash 현상과 현재까지의 완화/해결 방향을 정리한다.

## 환경 / 증상

- 환경: ODROID-H4, Ubuntu 22.04 계열 운용 환경에서 GUI capture 수행
- 대상 코드: `linescan_gui.py`, `linescan_module.py`, Pleora/JAI eBUS Python binding (`eBUS.py`, `_ebus_python`)
- 주요 증상:
  - capture 도중 또는 capture 직후 간헐적으로 `Segmentation fault (core dumped)` 발생
  - 일부 GUI 쪽 수정 후 segmentation fault 빈도는 크게 줄었으나, 간헐적으로 `Bus error (core dumped)` 발생

`Segmentation fault`와 `Bus error` 모두 Python exception이 아니라 native C/C++ extension 또는 Qt/eBUS SDK 내부에서 프로세스가 signal로 종료되는 문제이다. 따라서 `try/except`로 잡히지 않는다.

## 원인 후보 1: released eBUS buffer pointer 접근

### 의심 경로

기존 GUI capture는 다음 구조였다.

```text
eBUS buffer retrieve
→ LineScanFrame에 image payload 저장
→ pipeline.ReleaseBuffer(pvbuffer)
→ capture 완료 후 GUI에서 frame.image를 NumPy/QImage로 변환
```

문제는 `copy_frames=True`여도 `linescan_module._extract_image_payload()`가 안전한 Python-owned `bytes`를 만들지 못하면 native/SWIG pointer-like object를 그대로 반환할 수 있었다는 점이다.

```python
def _extract_image_payload(image, *, copy_image):
    data = image.GetDataPointer()
    ...
    try:
        return bytes(data)
    except Exception:
        ...
    return data
```

이 경우 `ReleaseBuffer()` 이후 GUI가 `frame.image`에 접근하면 use-after-free 또는 invalid native memory 접근이 되어 segfault/bus error가 날 수 있다.

### 적용한 완화

- GUI capture에서 더 이상 전체 `frames` list를 저장하지 않고 `store_frames=False` 사용
- `on_frame` callback 안에서 eBUS buffer가 아직 checkout된 상태일 때 preview용 NumPy 데이터를 즉시 복사
- GUI의 `_coerce_payload_to_uint8()`에서 unknown native/SWIG pointer-like payload에 대해 임의 `bytes(payload)` fallback을 하지 않도록 변경

관련 commit:

- `0677bea fix: bound GUI capture preview memory`

## 원인 후보 2: GUI에서 전체 frame 저장/표시로 인한 native memory pressure

### 의심 경로

라인스캔에서 `Height=1` 또는 작은 frame height를 사용하면 line rate가 거의 frame/block rate가 된다. 예를 들어 84 kHz line rate에서 `Height=1`이면 초당 수만 개의 Python frame object가 만들어질 수 있다.

기존 GUI는 다음 메모리 중복을 만들었다.

```text
frame별 bytes copy
→ CaptureResult.frames list
→ np.vstack() 결과 배열
→ QImage.copy()
→ QPixmap 내부 copy
```

장시간/고속 capture에서는 Python heap과 Qt/native memory가 크게 증가하고, native allocation 실패나 SDK 내부 불안정으로 crash가 발생할 수 있다.

### 적용한 완화와 이후 변경

- `store_frames=False + on_frame` 구조로 전체 frame list 보관을 제거했다.
- 한때 GUI preview byte limit을 256 MiB로 두었으나, 이후 사용 요청에 따라 display byte limit은 제거했다.
- 현재는 frame list 중복은 제거되어 있지만, preview 자체는 제한 없이 누적되므로 장시간/고속 capture에서는 여전히 메모리 사용량을 주의해야 한다.

관련 commit:

- `0677bea fix: bound GUI capture preview memory`
- `eaefbaf fix: settle camera settings before capture`

## 원인 후보 3: capture 직전 설정 write 직후 바로 AcquisitionStart

### 의심 경로

기존 `CaptureWorker.run()`은 capture 직전에 설정을 다시 썼다.

```python
self.camera.exposure_time = ...
self.camera.acquisition_line_rate = ...
self.camera.height = ...
self.camera.trigger_mode = ...
...
self.camera.capture(...)
```

특히 `Height`는 payload size를 바꾸는 설정이다. 설정 직후 카메라 내부 GenICam node/payload size/stream state가 완전히 안정화되기 전에 `GetPayloadSize()`, `StreamEnable()`, `AcquisitionStart()`가 이어지면 eBUS native SDK 내부에서 race가 날 수 있다.

가능한 문제 흐름:

```text
Height/line-rate/trigger 설정
→ 카메라 내부 반영 완료 전
→ pipeline buffer size 계산
→ StreamEnable / AcquisitionStart
→ 실제 payload와 pipeline buffer 상태 불일치 또는 SDK state race
→ SIGBUS/SIGSEGV 가능
```

### 적용한 완화

- capture 직전 worker thread에서 설정을 다시 쓰는 부분 제거
- `camera.capture(trigger_mode=...)` 호출도 제거하여 capture 직전 `TriggerMode` 재설정을 피함
- 설정 변경 시:
  - 250 ms debounce 후 설정 적용
  - 설정 적용 후 1초 settle time 동안 Capture 버튼 비활성화
  - settle 완료 후 Capture 버튼 enable
- 카메라 open 직후에도 기본 설정 적용 후 1초 대기한 뒤 Capture 가능하게 변경

관련 commit:

- `eaefbaf fix: settle camera settings before capture`

## 원인 후보 4: eBUS native 객체의 thread-safety / thread affinity 문제

### 현재 구조상 의심점

Qt GUI는 다음 형태로 동작한다.

```text
Qt main thread:
  LineScanCamera 생성/open
  일부 설정 적용

QThread worker:
  camera.capture()
  PvPipeline 생성
  RetrieveNextBuffer / ReleaseBuffer
```

즉 `PvDevice`, `PvStream` 등 eBUS native 객체가 생성/open된 thread와 실제 streaming/capture가 수행되는 thread가 다르다.

Pleora eBUS Python binding은 SWIG 기반 native C++ wrapper이므로, SDK가 명확히 thread-safe를 보장하지 않는다면 다음 문제가 가능하다.

- device/stream/pipeline 객체 thread affinity 위반
- native 객체 lifecycle race
- Python exception 대신 SIGSEGV/SIGBUS로 프로세스 종료

### 권장 해결 방향

단계별로 다음 중 하나를 고려한다.

#### 1단계: 같은 worker thread에서 open → 설정 → settle → capture → close

현재보다 안정적인 구조:

```text
worker thread:
  LineScanCamera 생성
  open
  설정 적용
  1초 settle
  capture
  close
```

이 방식은 eBUS 객체 thread affinity 문제를 줄일 수 있다. 그러나 여전히 같은 Python 프로세스 안이므로 native crash가 나면 GUI도 같이 종료된다.

#### 2단계: eBUS 작업을 별도 프로세스로 격리

가장 강한 안정화 패턴:

```text
GUI process:
  PySide6 UI만 담당
  설정값 전달
  worker subprocess 실행
  결과 .npy/.json 로드
  worker crash signal 표시

Capture worker process:
  eBUS import
  LineScanCamera open
  설정 적용
  settle
  capture
  결과 저장
  close
  exit
```

장점:

- eBUS worker가 `SIGBUS`/`SIGSEGV`로 죽어도 GUI는 살아 있음
- GUI에서 `returncode == -7`이면 SIGBUS, `returncode == -11`이면 SIGSEGV처럼 표시 가능
- eBUS native 객체 lifecycle이 capture process 안에서 깨끗하게 끝남

단점:

- capture마다 open/close 비용 발생
- 결과 전달을 위한 파일/IPC 설계 필요
- 실시간 streaming UI에는 별도 장기 실행 service 구조가 필요

현재 버튼식 capture GUI에는 우선 `subprocess per capture` 방식이 가장 현실적인 다음 단계이다.

## 현재까지 적용된 코드 변경 요약

1. GUI capture에서 전체 frame list 저장 제거
   - `store_frames=False`
   - `on_frame`에서 preview data 즉시 복사

2. unsafe payload fallback 제거
   - GUI 변환 helper에서 unknown native pointer-like object에 대한 임의 `bytes(payload)` 시도 제거

3. capture 직전 설정 재적용 제거
   - worker thread에서 `ExposureTime`, `AcquisitionLineRate`, `Height`, `TriggerMode`, `TriggerSource` 재설정 제거
   - `camera.capture(trigger_mode=...)` 제거

4. 설정 적용 후 1초 settle time 추가
   - 설정 변경 후 Capture 버튼 비활성화
   - 1초 안정화 후 Capture 버튼 enable

5. display byte limit 제거
   - preview cap은 제거됨
   - frame list 중복은 제거되었지만 preview 누적 메모리는 capture 조건에 따라 커질 수 있음

## 추가 진단 권장 사항

### 실행 로그 수집

```bash
PYTHONFAULTHANDLER=1 python linescan_gui.py 2>&1 | tee bus-error.log
```

SIGBUS/SIGSEGV 발생 시 마지막 Python frame이 다음 근처인지 확인한다.

- `linescan_module._extract_image_payload()`
- `LineScanCamera._frame_from_buffer()`
- `LineScanCamera.capture()`
- `linescan_gui.DisplayImageAccumulator.add_frame()`
- `ImageView.set_array()`

### capture 조건별 비교

다음 조건을 바꿔가며 crash 빈도를 비교한다.

- `Height=1` vs `Height=64/128/256`
- 낮은 line rate vs 높은 line rate
- 짧은 duration vs 긴 duration
- TriggerMode Off vs On
- GUI preview 사용 vs worker-only benchmark

### eBUS/network 상태 확인

`result.debug_summary()` 또는 benchmark script에서 다음 counter를 본다.

- `lost_packets`
- `missing_packet_ids`
- `recovered_packets`
- `resend_packets_requested`
- `block_gaps`
- `operational_errors`
- `retrieve_errors`

이 값들이 많으면 native crash와 별개로 packet loss / resend pressure / receiver overflow도 같이 해결해야 한다.

## 다음 구현 후보

우선순위 높은 다음 작업:

1. `linescan_capture_worker.py` 추가
   - CLI 인자로 설정 수신
   - eBUS open/config/settle/capture/close 전담
   - image `.npy`, stats `.json` 저장
   - SIGBUS/SIGSEGV 발생 시 GUI가 worker return code로 감지

2. `linescan_gui.py`에서 eBUS 직접 사용 제거 또는 최소화
   - GUI process는 eBUS import를 피하는 것이 이상적
   - worker process만 eBUS import

3. 필요 시 장기 실행 capture service로 확장
   - open/close 비용이 너무 크면 subprocess-per-capture 대신 persistent worker process + IPC 구조로 전환
