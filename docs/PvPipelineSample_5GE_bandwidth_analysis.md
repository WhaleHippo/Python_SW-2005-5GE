# PvPipelineSample.py 5GigE 대역폭 미활용 원인 후보

대상:

- 카메라: `SW-4005M-5GE`
- 원본 예제: `eBUS_examples/PvPipelineSample.py`
- 증상: Ethernet link는 5Gbps인데 예제 실행 시 `Bandwidth`가 약 `700~800Mbps` 수준에서 머무름

이 문서는 `SW-2005-5GE_usermanual.pdf`, `docs/SW-4005M-5GE_manual_summary.md`, `docs/error_rate_and_error_correction.md`, `eBUS_examples/PvPipelineSample.py`, `eBUS_examples/PvStreamSample.py`, `eBUS.py`를 기준으로 작성했다.

## 1. 결론 요약

`5Gbps link`는 물리 링크의 최대 전송 능력일 뿐이고, 실제 카메라 stream bandwidth는 다음 조건들의 곱으로 결정된다.

```text
실제 영상 데이터 대역폭 ~= Width x Height x pixel_bits x frame_rate
frame_rate = line_rate / Height
```

따라서 `PvPipelineSample.py`에서 700~800Mbps가 보이면 가장 먼저 확인할 것은 네트워크 포트 자체보다 아래 항목이다.

1. `AcquisitionLineRate`가 84kHz 근처가 아니라 낮은 값으로 설정되어 있음
2. `Height`가 커서 frame rate가 낮아짐
3. `PixelFormat`, `Width`, `Height` 조합상 필요한 대역폭이 애초에 700~800Mbps 수준임
4. `GevSCPSPacketSize`가 jumbo packet이 아닌 기본 `1476 bytes` 수준으로 남아 CPU/packet overhead가 큼
5. `PvPipelineSample.py`가 화면 표시, per-frame print, Python loop, OpenCV 처리 때문에 고속 수신 benchmark로 부적합함
6. pipeline buffer가 `16`개뿐이라 host가 순간적으로 밀릴 때 여유가 작음
7. Event, Chunk, ExtendedIDMode, resend/loss 등 부가 기능 또는 손실 복구가 throughput을 갉아먹음

## 2. 매뉴얼에서 확인한 기준값

매뉴얼의 SW-4005M-5GE 최대 line rate 표는 다음 조건을 전제로 한다.

- `Bandwidth = 5000Mbps`
- `ExtendedIDMode = Off`
- `SCPS = 7976 bytes`
- `NetworkThroughputSafetyMargin = 92`

SW-4005M-5GE의 최대 line rate:

| Width | Mono8 | Mono10 / Mono12 | Mono10Packed / Mono12Packed |
|---:|---:|---:|---:|
| 4096 Full | 84kHz | 72kHz | 84kHz |
| 3072 3/4 | 84kHz | 84kHz | 84kHz |
| 2048 1/2 | 84kHz | 84kHz | 84kHz |
| 1024 1/4 | 84kHz | 84kHz | 84kHz |

중요한 점:

- `84kHz`는 line rate이다.
- `PvPipelineSample.py`의 `AcquisitionRate`는 보통 frame/block rate로 해석해야 한다.
- line scan에서 한 frame은 `Height`개의 line을 포함하므로 `frame_rate = line_rate / Height`가 된다.

예: `Width=4096`, `Height=256`, `Mono8`, `line_rate=84kHz`이면

```text
payload/frame = 4096 x 256 x 8 bit = 8,388,608 bit
frame_rate = 84,000 / 256 = 328.125 FPS
bandwidth ~= 8,388,608 x 328.125 = 2.75Gbps
```

`Height`가 더 작거나 line rate가 낮으면 bandwidth는 그만큼 내려간다.

## 3. 700~800Mbps가 나오는 주요 원인 후보

### 3.1 카메라 acquisition 설정이 5Gbps를 요구하지 않음

5Gbps 링크를 쓰더라도 현재 `Width`, `Height`, `PixelFormat`, `AcquisitionLineRate` 조합이 700~800Mbps만 요구하면 그 이상은 나오지 않는다.

확인할 feature:

- `Width`
- `Height`
- `PixelFormat`
- `AcquisitionLineRate`
- `AcquisitionMode`
- `TriggerMode`
- `ExposureMode`, `ExposureTime`

특히 `TriggerMode=On`이면 실제 line rate는 외부 trigger 주기에 묶인다.

### 3.2 Height 때문에 frame/block rate가 낮아짐

라인스캔 카메라는 `Height` line을 모아 하나의 block/frame으로 보낸다. 예제에서 보이는 FPS가 낮아도 line rate는 높을 수 있다.

계산식:

```text
line_rate = frame_rate x Height
bandwidth ~= Width x Height x bits_per_pixel x frame_rate
          ~= Width x bits_per_pixel x line_rate
```

`Bandwidth`가 낮으면 `AcquisitionRate`와 `Height`를 함께 기록해야 한다.

### 3.3 PixelFormat / packed format 차이

SW-4005M-5GE는 다음 monochrome format을 지원한다.

- `Mono8`
- `Mono10`
- `Mono12`
- `Mono10Packed`
- `Mono12Packed`

대역폭 관점:

- 최고 line rate 확인은 `Mono8` 또는 packed format이 유리하다.
- Full width에서 `Mono10`/`Mono12`는 최대 line rate가 `72kHz`로 제한된다.

### 3.4 packet size가 매뉴얼 기준 `SCPS=7976`이 아닐 수 있음

매뉴얼 최대 line rate 조건은 `SCPS = 7976 bytes`이다.

관련 feature:

- `GevSCPSPacketSize`: range `1476 ~ 8192`, default `1476`
- `GevSCPSDoNotFragment`: default `True`
- `GevSCPD`: default `0`

`PvPipelineSample.py`는 `device.NegotiatePacketSize()`를 호출하지만, 실제 협상 결과가 7976 근처인지 출력하지 않는다. OS/NIC/switch MTU가 맞지 않으면 1476 근처로 남을 수 있다.

확인:

```bash
ip -d link show <NIC>
ethtool <NIC>
```

권장:

- NIC MTU를 jumbo frame으로 설정
- 카메라 `GevSCPSPacketSize` 실제값 확인
- eBUS stream receiver의 packet size 관련 값도 같이 확인

주의: `GevSCPSDoNotFragment=True`이므로 MTU보다 큰 packet size는 문제를 만든다.

### 3.5 원본 PvPipelineSample.py 자체가 benchmark용이 아님

원본 예제는 매 frame마다 다음 작업을 한다.

- `print(..., end='')`로 block id, width, height, FPS, bandwidth 출력
- OpenCV가 있으면 `cv2.imshow()` 및 `cv2.waitKey(1)` 실행
- `image.GetDataPointer()`로 Python/Numpy 영역 접근
- payload type 분기와 decompression 관련 검사

Python에서 per-frame 출력과 GUI 처리는 고속 stream benchmark에 매우 불리하다. 실제 수신 성능을 보려면 화면 표시를 끄고, frame마다 출력하지 말고, 1초 단위 통계만 출력해야 한다.

### 3.6 pipeline buffer count가 16개뿐임

원본:

```python
BUFFER_COUNT = 16
pipeline.SetBufferCount(BUFFER_COUNT)
```

고속 5GigE stream에서는 Python loop, OS scheduling, GC, 화면 표시 등으로 순간 지연이 생길 수 있다. buffer가 적으면 수신 queue가 빨리 비거나 overflow/loss가 발생한다.

권장:

- benchmark 시 `64` 또는 `128`부터 테스트
- processing thread와 acquisition thread 분리
- buffer를 받은 즉시 통계만 기록하고 바로 `ReleaseBuffer()`

### 3.7 Event 기능 제한

매뉴얼은 `AcquisitionStart`/`AcquisitionStop` 외 Event 기능을 켜면 line rate가 크게 제한된다고 설명한다.

- 추가 event 1개 활성화: 최대 line rate `6kHz`
- 추가 event 2개 활성화: 최대 line rate `4kHz`

`PvPipelineSample.py`의 `register_events=False`는 pipeline event sink용 플래그라 카메라 GenICam Event feature 상태와는 별개다. 카메라 Event feature가 켜져 있지 않은지 확인해야 한다.

### 3.8 NetworkThroughputSafetyMargin 해석

매뉴얼 설명:

- LinkSpeed에 대한 camera stream bandwidth 제한 비율
- 기본값 `92`
- 값을 높이면 frame rate를 높일 수 있음
- 단, `92`보다 크게 설정하면 PC/환경에 따라 abnormal image가 생길 수 있음

따라서 무작정 `100`으로 올리는 것은 권장하지 않는다. 먼저 packet size, acquisition 설정, host 처리 병목을 확인한다.

### 3.9 packet loss / resend가 throughput을 낮춤

GigE Vision은 UDP 기반이라 packet loss가 생기면 resend가 발생한다. resend가 많으면 유효 throughput과 안정성이 떨어진다.

`PvBuffer`에서 확인 가능한 counter:

- `GetPacketsRecoveredCount()`
- `GetPacketsRecoveredSingleResendCount()`
- `GetResendGroupRequestedCount()`
- `GetResendPacketRequestedCount()`
- `GetLostPacketCount()`
- `GetMissingPacketIdsCount()`

원본 예제는 이 값을 출력하지 않아 `낮은 bandwidth`가 설정 문제인지 손실/복구 문제인지 구분하기 어렵다.

## 4. 진단 순서

1. 링크 확인
   - NIC가 실제 5000Mb/s로 link up인지 확인
   - `ethtool <NIC>`의 `Speed: 5000Mb/s` 확인
2. packet size 확인
   - OS MTU
   - `GevSCPSPacketSize`
   - 매뉴얼 기준인 7976 근처인지 확인
3. acquisition snapshot 기록
   - `Width`, `Height`, `PixelFormat`, `AcquisitionLineRate`, `TriggerMode`
4. 이론 대역폭 계산
   - `Width x Height x bits_per_pixel x FPS`
   - 또는 `Width x bits_per_pixel x line_rate`
5. 원본 예제가 아니라 화면 표시 없는 benchmark로 측정
6. lost/recovered/resend counter 확인
7. buffer count를 64/128로 늘려 비교
8. 필요 시 `PixelFormat=Mono8`, `Height` 축소, `AcquisitionLineRate` 증가 순서로 카메라가 실제로 5Gbps에 가까운 stream을 만들도록 설정

## 5. 이번 작업에서 만든 수정/진단 코드

프로젝트 루트에 다음 파일을 추가했다.

- `high_throughput_pipeline.py`

목적:

- 원본 `PvPipelineSample.py`에서 고속 측정에 불리한 GUI/per-frame 출력 제거
- buffer count 기본값을 `64`로 증가
- `GevSCPSPacketSize`, `NetworkThroughputSafetyMargin`, `GevSCPD` 등 주요 transport feature 설정/출력
- `Width`, `Height`, `PixelFormat`, `AcquisitionLineRate`, `TriggerMode` 등 acquisition feature snapshot 출력
- eBUS `Bandwidth`와 별도로 Python에서 `pvbuffer.GetAcquiredSize()` 누적 기반 throughput 계산
- lost/recovered/resend counter 누적 출력

예시 실행:

```bash
python3 high_throughput_pipeline.py --duration 10 --buffer-count 64 --no-display
```

5GigE 최대 조건에 가깝게 강제 테스트하려면, NIC/MTU가 준비되어 있다는 전제에서 다음처럼 시작한다.

```bash
python3 high_throughput_pipeline.py \
  --duration 10 \
  --buffer-count 128 \
  --pixel-format Mono8 \
  --width 4096 \
  --height 256 \
  --line-rate 84000 \
  --packet-size 7976 \
  --safety-margin 92 \
  --scp-delay 0 \
  --trigger-off
```

주의:

- `--packet-size 7976`은 NIC MTU가 맞을 때만 사용한다.
- 비정상 이미지가 생기면 `--safety-margin 92`로 되돌리고 packet loss counter를 먼저 확인한다.
- 실제 line rate가 올라가지 않으면 `ExposureTime`, trigger, event feature가 제한하고 있을 수 있다.

## 6. PvPipelineSample.py와 새 코드의 핵심 차이

| 항목 | PvPipelineSample.py | high_throughput_pipeline.py |
|---|---|---|
| buffer count | 16 | 기본 64, CLI로 조정 |
| 화면 표시 | OpenCV 있으면 표시 | 표시 안 함 |
| 출력 | 매 frame 출력 | interval 단위 출력 |
| packet size | negotiate만 수행, 결과 미출력 | 설정/협상 후 주요 값 출력 |
| throughput | eBUS `Bandwidth`만 표시 | eBUS 값 + acquired bytes 기반 로컬 계산 |
| loss/resend | 미표시 | 누적 lost/recovered/resend 출력 |
| acquisition 설정 | 현재 카메라 설정 그대로 사용 | CLI로 PixelFormat/Width/Height/LineRate 등 설정 가능 |

## 7. 가장 가능성이 높은 원인 조합

현재 증상만 놓고 보면 단일 원인보다는 아래 조합일 가능성이 높다.

1. 카메라가 현재 설정상 700~800Mbps 정도만 생성하고 있음
   - 특히 `AcquisitionLineRate`, `Height`, trigger 설정 확인 필요
2. packet size가 7976이 아니라 1476 근처라 5GigE 고속 수신 효율이 낮음
3. 원본 Python 예제의 per-frame 출력/OpenCV 처리 때문에 host loop가 benchmark 병목이 됨
4. buffer count 16으로는 순간 지연 흡수가 부족함

먼저 `high_throughput_pipeline.py`로 설정 snapshot과 loss/resend counter를 확인하면, `카메라가 적게 보내는 문제`인지 `host/network가 못 받는 문제`인지 분리할 수 있다.
