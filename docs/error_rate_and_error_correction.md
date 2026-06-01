# SW-4005M-5GE Error Rate / Error Correction 분석

이 문서는 현재 프로젝트의 소스코드(`linescan_module.py`, `eBUS.py`, `eBUS_examples/*`)와 `SW-2005-5GE_usermanual.pdf`를 기준으로, **SW-4005M-5GE에서 에러율을 어떻게 관찰하고, 에러를 어떻게 보정/완화할지** 정리한 문서입니다.

타겟 카메라: `SW-4005M-5GE`  
인터페이스: `5GigE Vision`  
주요 관심사: GigE Vision 스트리밍 패킷 손실, GVSP 오류, frame/block 누락, 카메라 내부 보정 기능

## 1. 결론 요약

현재 프로젝트의 직접 작성 소스인 `linescan_module.py`는 아직 아래처럼 wrapper 구현이 거의 없는 상태입니다.

```python
import eBUS as eb

# TODO
```

따라서 현재 코드에 이미 구현된 `error rate` 계산 로직이나 `error correction` 로직은 없습니다.  
다만 JAI/Pleora eBUS wrapper인 `eBUS.py`와 예제 코드에는 에러율 측정과 패킷 손실 대응에 필요한 API가 이미 노출되어 있습니다.

구현 시 핵심은 다음입니다.

- 수신 성공 여부는 `PvStream.RetrieveBuffer()`의 `result`와 `operational_result`를 모두 확인합니다.
- 각 buffer/frame의 손실 여부는 `PvBuffer.GetOperationResult()`와 packet 관련 counter로 확인합니다.
- frame/block 누락은 `PvBuffer.GetBlockID()` 증가값으로 확인합니다.
- GigE Vision 패킷 손실은 eBUS의 packet resend 기능으로 일부 복구됩니다.
- 복구되지 않은 손실은 `MISSING_PACKETS`, `RESENDS_FAILURE`, `TOO_MANY_RESENDS`, `TOO_MANY_CONSECUTIVE_RESENDS`, `IMAGE_ERROR` 등으로 드러납니다.
- 카메라 영상 품질 보정은 DSNU/PRNU/Shading Correction으로 수행하지만, 이것은 네트워크 패킷 에러 보정이 아니라 센서/광학/조명 편차 보정입니다.

## 2. 에러의 종류를 구분해야 함

이 프로젝트에서 “error”는 크게 두 종류로 나누어야 합니다.

### A. 전송/수신 에러

GigE Vision 스트리밍 중 발생하는 에러입니다.

예:

- UDP/GVSP packet loss
- packet resend 실패
- 너무 많은 resend
- buffer timeout
- buffer overflow
- block/frame 누락
- packet out-of-order
- GVSP error

이 문서의 `error rate`는 주로 이 전송/수신 에러율을 의미합니다.

### B. 영상 품질 편차 / 보정 대상

카메라 센서, 조명, 렌즈 때문에 발생하는 픽셀/밝기 편차입니다.

예:

- DSNU: dark signal non-uniformity
- PRNU: photo response non-uniformity
- shading: 렌즈/조명으로 인한 밝기 불균일
- gain/black level 편차

이것은 `error correction`이라는 이름으로 매뉴얼에 나오지만, 네트워크 패킷 손실을 복구하는 기능은 아닙니다.

## 3. 소스코드에서 확인한 스트리밍 에러 처리 구조

### 3.1 `PvStreamSample.py`의 기본 수신 흐름

예제 `eBUS_examples/PvStreamSample.py`는 다음 패턴으로 스트리밍합니다.

```python
result, pvbuffer, operational_result = stream.RetrieveBuffer(1000)
if result.IsOK():
    if operational_result.IsOK():
        # 정상 buffer 처리
        print(pvbuffer.GetBlockID())
    else:
        # Non-OK operational result
        print(operational_result.GetCodeString())
    stream.QueueBuffer(pvbuffer)
else:
    # RetrieveBuffer 자체 실패
    print(result.GetCodeString())
```

의미:

- `result`
  - host 쪽에서 buffer를 가져오는 API 호출 자체의 성공/실패입니다.
  - 예: timeout, stream close, retrieve 실패 등
- `operational_result`
  - 실제 acquisition 결과입니다.
  - buffer가 왔더라도 내부에 packet loss나 image error가 있으면 OK가 아닐 수 있습니다.
- 정상 처리가 끝난 buffer는 반드시 `stream.QueueBuffer(pvbuffer)`로 다시 queue에 넣어야 합니다.

### 3.2 `PvBuffer.GetOperationResult()`에서 가능한 에러

`eBUS.py`의 `PvBuffer.GetOperationResult()` 설명에 따르면 acquisition operation result에는 다음 코드들이 포함됩니다.

- `OK`
- `TIMEOUT`
- `ABORTED`
- `MISSING_PACKETS`
- `BUFFER_TOO_SMALL`
- `IMAGE_ERROR`
- `RESENDS_FAILURE`
- `TOO_MANY_RESENDS`
- `TOO_MANY_CONSECUTIVE_RESENDS`
- `INVALID_DATA_FORMAT`
- `AUTO_ABORTED`
- `ERR_OVERFLOW`

중요한 해석:

- `MISSING_PACKETS`: 최종적으로 채워지지 않은 packet 구간이 있음
- `RESENDS_FAILURE`: packet resend 요청으로도 복구 실패
- `TOO_MANY_RESENDS`: resend가 너무 많이 발생
- `TOO_MANY_CONSECUTIVE_RESENDS`: 연속 resend가 너무 많음
- `IMAGE_ERROR`: grabber 쪽 data overrun, missing line 등 이미지 자체 문제
- `ERR_OVERFLOW`: host 처리/queue/stream 쪽 overflow 가능성

### 3.3 block/frame 누락 확인: `GetBlockID()`

`PvBuffer.GetBlockID()`는 GigE Vision transmitter가 새 이미지마다 보통 1씩 증가시키는 ID입니다.  
`eBUS.py` 설명상 이 값으로 block이 순서대로 들어왔는지, 누락이 있는지 확인할 수 있습니다.

주의:

- BlockID는 wrap-around가 있습니다.
- legacy 16-bit Block ID 모드에서는 65536 근처에서 wrap됩니다.
- Extended Block ID 모드에서는 훨씬 큰 범위까지 증가합니다.
- 매뉴얼 기준 `GevGVSPExtendedIDMode` 기본값은 `On`입니다.

## 4. eBUS에서 사용할 수 있는 packet-level counter

`eBUS.py`의 `PvBuffer`에는 packet 손실/복구 상태를 확인할 수 있는 counter가 있습니다.

### Buffer 단위 counter

- `GetPacketsRecoveredCount()`
  - packet resend 요청으로 성공적으로 복구된 packet 수
- `GetPacketsRecoveredSingleResendCount()`
  - resend retry 없이 한 번의 resend 요청으로 복구된 packet 수
- `GetResendGroupRequestedCount()`
  - data receiver가 발행한 resend group 요청 수
- `GetResendPacketRequestedCount()`
  - resend 요청 대상 packet 수
- `GetLostPacketCount()`
  - 해당 buffer를 채우는 동안 성공적으로 전달되지 않은 packet 수
- `GetIgnoredPacketCount()`
  - 수신되었지만 해당 buffer에서 무시된 packet 수
- `GetRedundantPacketCount()`
  - 중복 수신 packet 수
- `GetPacketOutOfOrderCount()`
  - 순서가 뒤바뀌어 수신된 packet 수
- `GetMissingPacketIdsCount()`
  - buffer 내부에서 비어 있는 packet range 그룹 수
- `GetMissingPacketIds(index)`
  - 누락된 packet range의 시작/끝 packet ID

### missing packet range 확인 절차

`eBUS.py` 설명 기준 절차:

1. stream 쪽에서 missing packet list 추적 기능을 켭니다.
   - `PvStream`의 `EnableMissingPacketsList` property
2. 필요하면 packet resend 요청 기능을 끕니다.
   - `PvStream`의 `RequestMissingPackets` property
   - 일반 운용에서는 끄기보다 켜둔 상태에서 복구율을 모니터링하는 편이 안전합니다.
3. buffer 수신 후 `PvBuffer.GetOperationResult()` 또는 `operational_result`를 확인합니다.
4. 결과가 `MISSING_PACKETS`이면 `GetMissingPacketIdsCount()`를 호출합니다.
5. 각 index에 대해 `GetMissingPacketIds(index)`로 누락 packet range를 확인합니다.
6. packet size와 payload layout을 이용해 이미지 어느 영역이 비었는지 추정합니다.

주의:

- 이 기능은 GigE Vision 전송 구조를 이해해야 하는 advanced 기능입니다.
- USB3 Vision에는 packet resend 개념이 없지만, 이 프로젝트 타겟은 5GigE이므로 관련 있습니다.

## 5. Error Rate 계산 방법

매뉴얼이나 현재 wrapper 코드에 “error rate”라는 단일 feature가 직접 정의되어 있지는 않습니다.  
따라서 wrapper에서 직접 계산해야 합니다.

### 5.1 Frame/Block 기준 error rate

가장 구현하기 쉬운 지표입니다.

```text
frame_error_rate = error_frame_count / received_frame_count
```

`error_frame_count`에 포함할 항목:

- `operational_result.IsOK() == False`
- `PvBuffer.GetOperationResult().IsOK() == False`
- `GetLostPacketCount() > 0`
- `GetMissingPacketIdsCount() > 0`
- BlockID가 예상값보다 건너뜀

추천:

```text
received_frame_count = 정상/비정상 상관없이 RetrieveBuffer로 받은 buffer 수
ok_frame_count = operational_result OK인 buffer 수
error_frame_count = received_frame_count - ok_frame_count
missing_block_count = BlockID gap으로 추정한 누락 block 수
frame_error_rate = (error_frame_count + missing_block_count) / (received_frame_count + missing_block_count)
```

### 5.2 Packet 기준 loss/recovery rate

packet counter를 이용한 지표입니다.

```text
packet_loss_count = sum(buffer.GetLostPacketCount())
packet_recovered_count = sum(buffer.GetPacketsRecoveredCount())
packet_resend_requested_count = sum(buffer.GetResendPacketRequestedCount())
packet_out_of_order_count = sum(buffer.GetPacketOutOfOrderCount())
packet_redundant_count = sum(buffer.GetRedundantPacketCount())
```

추천 계산식:

```text
packet_recovery_rate = packet_recovered_count / packet_resend_requested_count
unrecovered_packet_rate = packet_loss_count / estimated_total_packet_count
resend_pressure = packet_resend_requested_count / received_frame_count
```

`estimated_total_packet_count` 계산:

```text
payload_size = Width x Height x bytes_per_pixel + leader/trailer/chunk overhead
packet_payload_size ≈ GevSCPSPacketSize - UDP/IP/GVSP overhead
estimated_packets_per_frame = ceil(payload_size / packet_payload_size)
estimated_total_packet_count = estimated_packets_per_frame x received_frame_count
```

실제 overhead는 transport layer 설정에 따라 달라질 수 있으므로, 초기 구현에서는 frame 기준 error rate를 먼저 넣고 packet-level 지표는 보조 통계로 두는 것을 권장합니다.

### 5.3 BlockID gap 기준 누락률

```text
expected_next_block_id = previous_block_id + 1
if current_block_id != expected_next_block_id:
    missing_blocks += gap_size
```

주의:

- wrap-around 처리 필요
- 첫 frame은 기준값으로만 저장
- `GevGVSPExtendedIDMode = On`인 경우 긴 ID를 기준으로 처리

## 6. Error Correction / 완화 방법

## 6.1 네트워크 packet resend

GigE Vision은 UDP 기반이라 packet loss가 생길 수 있습니다.  
eBUS data receiver는 lost packet에 대해 resend를 요청할 수 있고, `PvBuffer`는 복구된 packet 수를 제공합니다.

관련 eBUS API:

- `GetPacketsRecoveredCount()`
- `GetPacketsRecoveredSingleResendCount()`
- `GetResendGroupRequestedCount()`
- `GetResendPacketRequestedCount()`
- `GetLostPacketCount()`
- `GetMissingPacketIdsCount()`

관련 카메라 feature:

- `GevSCCFGPacketResendDestination`
  - packet resend request에 따른 resend packet의 alternate IP destination을 사용할지 설정
- `GevSCPSPacketSize`
  - stream packet size
- `GevSCPSDoNotFragment`
  - IP fragmentation 방지
- `GevSCPD`
  - packet 간 delay
- `NetworkThroughputSafetyMargin`
  - stream bandwidth 안전 여유율

매뉴얼에서 확인한 기본/범위:

- `GevSCPSPacketSize`: `1476 ~ 8192 byte`, default `1476`
- `GevSCPSDoNotFragment`: default `True`
- `GevSCPD`: default `0`
- `NetworkThroughputSafetyMargin`: `10 ~ 100`, default `92`
- `GevMCRC`: message channel retry count, default `3`

주의:

- packet resend는 일부 손실을 복구하지만, 과도한 손실은 `TOO_MANY_RESENDS` 또는 `TOO_MANY_CONSECUTIVE_RESENDS`가 됩니다.
- error correction을 resend에만 의존하면 안 됩니다. 근본적으로 packet loss가 낮아지도록 네트워크 대역폭/packet size/host buffer를 조정해야 합니다.

## 6.2 Packet size 조정

예제 `PvStreamSample.py`는 GigE Vision device일 때 다음을 수행합니다.

```python
if isinstance(device, eb.PvDeviceGEV):
    device.NegotiatePacketSize()
    device.SetStreamDestination(stream.GetLocalIPAddress(), stream.GetLocalPort())
```

의미:

- `NegotiatePacketSize()`로 카메라와 host/network가 지원하는 packet size를 협상합니다.
- 이후 stream destination을 host IP/port로 설정합니다.

추천:

- 기본 구현에서도 반드시 `NegotiatePacketSize()`를 호출합니다.
- 5GigE NIC와 jumbo frame이 제대로 설정되어 있다면 큰 packet size를 쓰는 것이 CPU/packet overhead에 유리할 수 있습니다.
- packet loss가 늘면 packet size를 줄여 비교 테스트합니다.

주의:

- `GevSCPSPacketSize`와 receiver의 `DeviceStreamChannelPacketSize`는 동기화되어야 합니다.
- `GevSCPSDoNotFragment = True`이므로 MTU보다 큰 packet은 문제가 됩니다.
- OS/NIC/switch/jumbo frame 설정이 맞지 않으면 큰 packet size가 오히려 손실을 증가시킬 수 있습니다.

## 6.3 NetworkThroughputSafetyMargin 조정

매뉴얼은 `NetworkThroughputSafetyMargin`에 대해 다음 주의사항을 제공합니다.

- LinkSpeed에 대해 camera stream bandwidth 제한 비율을 설정합니다.
- 기본값은 `92`입니다.
- 값을 높이면 frame rate를 높일 수 있습니다.
- 그러나 `92`보다 크게 설정하면 PC와 환경에 따라 abnormal image가 관찰될 수 있습니다.
- abnormal image가 발생하면 기본값 `92`로 되돌리라고 명시되어 있습니다.

추천:

- 안정성 우선: `92` 유지
- error rate가 높으면 값을 높이기보다 먼저 낮춰서 여유 bandwidth를 확보합니다.
- 최대 라인레이트 테스트는 별도 validation 환경에서만 수행합니다.

## 6.4 GevSCPD / packet delay 조정

`GevSCPD`는 stream channel의 packet 사이에 삽입할 delay입니다.

활용:

- NIC/CPU가 burst packet을 처리하지 못해 packet loss가 생길 때 packet 사이 delay를 늘립니다.
- line rate를 낮추기 어렵지만 packet loss가 생기는 경우 우선 검토합니다.

주의:

- delay를 늘리면 effective throughput이 줄어들 수 있습니다.
- 최대 라인레이트와 동시에 달성하기 어려울 수 있습니다.

## 6.5 Buffer queue / pipeline 관리

예제는 `BUFFER_COUNT = 16`으로 buffer를 여러 개 할당한 뒤 모두 stream에 queue합니다.

구현 시 권장:

- 충분한 buffer 수 확보
- 처리 thread가 느려져 queue가 비지 않도록 image processing과 acquisition 분리
- 수신 즉시 필요한 metadata/error counter를 기록하고 buffer를 빠르게 requeue
- 장시간 저장/처리는 별도 queue/thread/process로 분리

에러 징후:

- `ERR_OVERFLOW`
- `BUFFER_TOO_SMALL`
- `TIMEOUT`
- frame/block gap 증가
- bandwidth는 높지 않은데 packet loss가 증가

## 6.6 라인레이트 / PixelFormat / ROI 낮추기

SW-4005M-5GE는 최대 `84 kHz`를 지원하지만, 다음 조건에서 대역폭 부담이 달라집니다.

- `Width`
- `Height`
- `PixelFormat`
- `AcquisitionLineRate`
- chunk/event 사용 여부
- packet size

매뉴얼 기준 SW-4005M-5GE 최대 라인레이트:

| Width | Mono8 | Mono10 / Mono12 | Mono10Packed / Mono12Packed |
|---:|---:|---:|---:|
| 4096 Full | 84 kHz | 72 kHz | 84 kHz |
| 3072 | 84 kHz | 84 kHz | 84 kHz |
| 2048 | 84 kHz | 84 kHz | 84 kHz |
| 1024 | 84 kHz | 84 kHz | 84 kHz |

권장 완화 순서:

1. `PixelFormat = Mono8` 또는 packed format 사용
2. `Width` ROI 축소
3. `Height`를 필요한 frame 단위로만 설정
4. `AcquisitionLineRate` 낮추기
5. `NetworkThroughputSafetyMargin` 기본값 또는 낮은 값으로 안정화
6. `GevSCPD` 증가
7. host buffer/pipeline 개선

## 6.7 Event 기능 주의

매뉴얼은 Event 기능에 강한 라인레이트 제한이 있다고 명시합니다.

`AcquisitionStart`, `AcquisitionStop` 외 Event를 켠 경우:

- 추가 event 1개 활성화: 최대 라인레이트 `6 kHz`
- 추가 event 2개 활성화: 최대 라인레이트 `4 kHz`
- `AcquisitionStart`/`AcquisitionStop` 외 event는 3개 초과 활성화를 권장하지 않음

주의:

- `EventExposureStart`, `EventExposureEnd`, `EventLVALStart`, `EventLVALEnd` 같은 event를 디버깅 목적으로 켠 상태에서 84 kHz 운용을 시도하면 안 됩니다.
- 고속 line scan 운용 중에는 Event는 최소화하고, 필요하면 짧은 진단 구간에서만 켭니다.

## 7. 카메라 내부 영상 보정 기능

이 섹션은 전송 에러 correction이 아니라 영상 품질 correction입니다.

### 7.1 DSNU Correction / Pixel Black Correct

목적:

- dark 영역의 픽셀별 black level 편차 보정

절차:

1. 센서 보호 캡 장착
2. `AcquisitionStart`
3. `PixelBlackCorrectionMode`에서 `User1 ~ User3` 선택
4. `CalibratePixelBlackCorrection` 실행
5. `PixelBlackCalibrationResult` 확인

실패 조건:

- image too bright
- image too dark
- image output이 없음
- `TestPattern != Off`
- `PixelBlackCorrectionMode = Off` 또는 `Default`

주의:

- line rate가 느리거나 노출 시간이 길면 dark current가 바뀌므로 보정 재수행이 필요할 수 있습니다.

### 7.2 PRNU Correction / Pixel Gain Correct

목적:

- 밝은 조건에서 픽셀별 감도 편차 보정

절차:

1. 필요하면 `Width`, `OffsetX`로 보정 계산 ROI 설정
2. `PixelGainCorrectionMode`에서 저장 영역 또는 ROI 모드 선택
3. 균일한 밝기의 기준 영상 준비
4. `CalibratePixelGainCorrection` 실행
5. `PixelGainCalibrationResult` 확인

특징:

- 너무 밝거나 너무 어두운 경우 normal correction 대신 `Best Effort` correction이 수행될 수 있습니다.

주의:

- 조명/노출/라인레이트 조건이 바뀌면 PRNU 상태도 달라질 수 있습니다.

### 7.3 Shading Correction

목적:

- 렌즈/조명으로 인한 밝기 불균일 보정

SW-4005M-5GE는 흑백 모델이므로 flat shading 계열을 중심으로 봅니다.

절차:

1. 필요 시 `Width`, `OffsetX`로 correction 계산 영역 설정
2. `ShadingCorrectionMode` 선택
3. 저장 영역 `User1 ~ User3` 선택
4. 균일 조명 아래 white chart 표시
5. `CalibrateShadingCorrection` 실행
6. `ShadingDetectResult` 확인

주의:

- 너무 밝거나 너무 어두우면 실패 또는 best effort 결과가 나올 수 있습니다.

## 8. wrapper에 넣을 권장 설계

### 8.1 ErrorStats 구조체/클래스

wrapper에는 최소한 아래 통계를 누적하는 객체를 둡니다.

```python
@dataclass
class StreamErrorStats:
    received_buffers: int = 0
    ok_buffers: int = 0
    error_buffers: int = 0
    retrieve_failures: int = 0
    missing_blocks: int = 0
    lost_packets: int = 0
    recovered_packets: int = 0
    resend_packet_requests: int = 0
    resend_group_requests: int = 0
    ignored_packets: int = 0
    redundant_packets: int = 0
    out_of_order_packets: int = 0
    last_block_id: int | None = None
```

### 8.2 수신 루프에서 해야 할 일

```python
result, pvbuffer, operational_result = stream.RetrieveBuffer(timeout_ms)

if not result.IsOK():
    stats.retrieve_failures += 1
    log(result.GetCodeString(), result.GetDescription())
    return

stats.received_buffers += 1

if operational_result.IsOK():
    stats.ok_buffers += 1
else:
    stats.error_buffers += 1
    log(operational_result.GetCodeString(), operational_result.GetDescription())

block_id = pvbuffer.GetBlockID()
update_block_gap(stats, block_id)

stats.lost_packets += pvbuffer.GetLostPacketCount()
stats.recovered_packets += pvbuffer.GetPacketsRecoveredCount()
stats.resend_packet_requests += pvbuffer.GetResendPacketRequestedCount()
stats.resend_group_requests += pvbuffer.GetResendGroupRequestedCount()
stats.ignored_packets += pvbuffer.GetIgnoredPacketCount()
stats.redundant_packets += pvbuffer.GetRedundantPacketCount()
stats.out_of_order_packets += pvbuffer.GetPacketOutOfOrderCount()

stream.QueueBuffer(pvbuffer)
```

주의:

- `QueueBuffer()`는 예외 상황에서도 가능한 한 호출해 buffer starvation을 막습니다.
- 단, `pvbuffer`가 `None`이면 호출하면 안 됩니다.
- 오류 통계 기록은 buffer requeue보다 오래 걸리지 않게 합니다.

### 8.3 Error rate 계산 property

```python
frame_error_rate = error_buffers / received_buffers
retrieve_failure_rate = retrieve_failures / (received_buffers + retrieve_failures)
packet_recovery_rate = recovered_packets / resend_packet_requests
block_missing_rate = missing_blocks / (received_buffers + missing_blocks)
```

분모가 0이면 0 또는 `None`으로 처리합니다.

## 9. 진단 기준 제안

초기 개발/실험에서는 아래 기준으로 상태를 분류합니다.

### 정상

- `operational_result`가 대부분 OK
- `lost_packets = 0`
- `missing_blocks = 0`
- `out_of_order_packets`가 0 또는 매우 낮음
- `resend_packet_requests`가 0 또는 간헐적

### 주의

- `resend_packet_requests`가 지속적으로 증가하지만 `lost_packets = 0`
- 즉, packet loss가 발생했지만 resend로 복구되는 상태
- 원인: 네트워크 여유 부족, packet burst, CPU 처리 지연 가능성

대응:

- packet size 협상 상태 확인
- NIC MTU/jumbo frame 확인
- `GevSCPD` 증가 테스트
- ROI/line rate 감소 테스트

### 위험

- `MISSING_PACKETS`
- `RESENDS_FAILURE`
- `TOO_MANY_RESENDS`
- `TOO_MANY_CONSECUTIVE_RESENDS`
- `IMAGE_ERROR`
- BlockID gap 발생
- `lost_packets > 0`

대응:

- 라인레이트 즉시 낮춤
- `PixelFormat`을 `Mono8` 또는 packed로 변경
- ROI 축소
- Event 기능 비활성화
- host buffer 수 증가
- image processing을 acquisition thread에서 분리
- NIC/switch/cable/MTU 확인

## 10. 구현 우선순위

### 1단계: 현재 wrapper에 최소 error monitor 추가

- `RetrieveBuffer` 결과 확인
- `operational_result` 확인
- `GetBlockID()` gap 확인
- `GetLostPacketCount()` / `GetPacketsRecoveredCount()` 누적
- 1초마다 error summary 출력

### 2단계: 안정화 옵션 추가

- `NegotiatePacketSize()` 호출 보장
- `NetworkThroughputSafetyMargin` 설정 helper
- `GevSCPD` 설정 helper
- `PixelFormat`, `Width`, `Height`, `AcquisitionLineRate` 조합별 error test

### 3단계: correction/calibration helper 추가

- DSNU calibration helper
- PRNU calibration helper
- Shading correction helper
- calibration 결과 enum/string 읽기

### 4단계: 장시간 테스트 리포트

- runtime seconds
- received buffers
- frame error rate
- missing block count
- lost/recovered packet count
- resend pressure
- bandwidth
- acquisition rate
- 설정 snapshot

## 11. 주의할 점

- 현재 `linescan_module.py`에는 아직 구현이 없으므로, 이 문서는 구현 설계 기준입니다.
- `eBUS.py`는 SWIG wrapper라 실제 내부 동작은 Pleora eBUS SDK에 있습니다.
- 매뉴얼의 correction 기능은 대부분 이미지 품질 보정이며, 네트워크 packet loss를 고치는 기능이 아닙니다.
- 전송 에러는 resend와 네트워크/host 튜닝으로 줄이고, 영상 편차는 DSNU/PRNU/Shading으로 보정해야 합니다.
- 고속 운용에서는 Event 기능을 켜지 않는 것이 좋습니다. AcquisitionStart/Stop 외 event는 라인레이트를 6 kHz 또는 4 kHz 수준으로 크게 제한할 수 있습니다.
- `NetworkThroughputSafetyMargin`을 기본값 92보다 높이면 abnormal image가 생길 수 있다고 매뉴얼에 명시되어 있습니다.
- 에러율 로그는 반드시 설정값과 함께 저장해야 원인 분석이 가능합니다.

## 12. 참고한 주요 파일

- `linescan_module.py`
  - 현재 사용자 wrapper 시작점, 아직 TODO 상태
- `eBUS.py`
  - `PvBuffer.GetOperationResult()`
  - `PvBuffer.GetBlockID()`
  - `PvBuffer.GetPacketsRecoveredCount()`
  - `PvBuffer.GetLostPacketCount()`
  - `PvBuffer.GetMissingPacketIdsCount()`
  - `PvBuffer.GetMissingPacketIds()`
- `eBUS_examples/PvStreamSample.py`
  - `PvStream.RetrieveBuffer()` 사용 예
  - `operational_result` 확인 예
  - `NegotiatePacketSize()` / `SetStreamDestination()` 예
- `SW-2005-5GE_usermanual.pdf`
  - Transport Layer Control
  - Event Control Function
  - DSNU Correction
  - PRNU Correction
  - Shading Correction
