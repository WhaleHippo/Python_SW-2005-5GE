# SW-4005M-5GE 유저 매뉴얼 핵심 요약

이 문서는 `SW-2005-5GE_usermanual.pdf` 안에서 **타겟 카메라 `SW-4005M-5GE`에 해당하는 내용만** 골라 정리한 것입니다.  
특히 트리거 모드, 노출 시간, Gain 조절, 라인레이트, ROI, 보정 기능을 실제 제어 코드 작성 시 참고하기 쉽게 정리했습니다.

## 1. 타겟 카메라 기본 사양

- 모델명: `SW-4005M-5GE`
- 센서: Mono CMOS line sensor
- 유효 픽셀: `4096 x 1`
- 픽셀 크기: `3.5 μm x 3.5 μm`
- 최대 라인레이트: `84 kHz`
- 인터페이스: `5GBASE-T`, 5GigE Vision
- 렌즈 마운트: C-mount
- 전원:
  - PoE 지원
  - 또는 DC IN/TRIG 12핀 커넥터의 `DC 10.8 ~ 26.4 V`
- 흑백 모델이므로 색상 전용 기능은 사용 대상이 아닙니다.
  - White Balance
  - Color Space Conversion
  - Color Shading
  - RGB 개별 Gain
  - TL 모델 전용 ExposureTimeMode Individual

## 2. SW-4005M-5GE에서 중요한 Feature 목록

### Acquisition / 라인 취득

주요 GenICam feature:

- `AcquisitionMode`
  - `SingleFrame`
  - `MultiFrame`
  - `Continuous` 기본값
- `AcquisitionStart`
- `AcquisitionStop`
- `AcquisitionFrameCount`
- `AcquisitionLineRate`
- `TriggerSelector`
- `TriggerMode`
- `TriggerSource`
- `TriggerActivation`
- `ExposureMode`
- `ExposureTime`
- `ExposureModeOption`

주의:

- 외부 트리거를 쓰더라도 카메라는 먼저 `AcquisitionStart` 상태가 되어야 이미지를 출력합니다.
- `TriggerMode = On`일 때는 트리거 설정과 노출 설정이 서로 강하게 연동됩니다.
- `Frame Rate = Line Rate / Height`로 계산됩니다.

## 3. 라인레이트 설정

### 내부 라인레이트 모드

외부 트리거 없이 카메라 내부 주기로 스캔하려면:

```text
TriggerMode = Off
AcquisitionLineRate = 원하는 라인레이트 Hz
```

- `AcquisitionLineRate` 최소값: `66 Hz`
- 최대값은 `PixelFormat`, ROI `Width`, 네트워크 설정, binning 등에 따라 달라집니다.

### SW-4005M-5GE 최대 라인레이트

매뉴얼 기준 조건:

- Bandwidth: `5000 Mbps`
- ExtendedIDMode: `Off`
- SCPS: `7976 bytes`
- NetworkThroughputSafetyMargin: `92`

| Width | Mono8 | Mono10 / Mono12 | Mono10Packed / Mono12Packed |
|---:|---:|---:|---:|
| 4096 Full | 84 kHz | 72 kHz | 84 kHz |
| 3072 3/4 | 84 kHz | 84 kHz | 84 kHz |
| 2048 1/2 | 84 kHz | 84 kHz | 84 kHz |
| 1024 1/4 | 84 kHz | 84 kHz | 84 kHz |

주의:

- `84 kHz`는 SW-4005M-5GE 모델의 센서 제한입니다.
- Full width에서 `Mono10` 또는 `Mono12`를 쓰면 최대 라인레이트가 `72 kHz`로 내려갑니다.
- 높은 라인레이트가 필요하면 `Mono8` 또는 packed 포맷을 우선 검토합니다.
- ROI Width를 줄이면 포맷에 따른 제한이 완화될 수 있습니다.

## 4. ROI / Width / Height

SW-4005 계열 기준 ROI 범위:

- `Width`
  - Binning Off: `128 ~ (4096 - OffsetX)`, step `8`
  - Binning On: `64 ~ (2048 - OffsetX)`, step `8`
- `OffsetX`
  - Binning Off: `0 ~ (4096 - Width)`, step `8`
  - Binning On: `0 ~ (2048 - Width)`, step `8`
- `Height`
  - `1 ~ (4096 - OffsetY)`, step `1`
- `OffsetY`
  - `0 ~ (4096 - Height)`, step `1`

주의:

- 이 카메라는 vertical binning을 지원하지 않습니다.
- `BinningHorizontal`은 `Height`, `OffsetY`에는 영향을 주지 않습니다.
- 카메라는 `Width x Height` 데이터를 하나의 block/frame으로 전송합니다.
- ROI는 최대 라인레이트와 PayloadSize에 직접 영향을 줍니다.

## 5. PixelFormat

SW-4005M-5GE는 monochrome 모델이므로 선택 가능한 PixelFormat은 다음과 같습니다.

- `Mono8` 기본값
- `Mono10`
- `Mono12`
- `Mono10Packed`
- `Mono12Packed`

주의:

- 대역폭/라인레이트 관점에서는 `Mono8` 또는 packed 포맷이 유리합니다.
- Full width에서 `Mono10`, `Mono12`는 최대 라인레이트가 72 kHz로 제한됩니다.
- 이미지 처리 정밀도가 필요하면 `Mono10`/`Mono12`를 쓰되 라인레이트 제한을 함께 고려해야 합니다.

## 6. 트리거 모드

### TriggerSelector 종류

주요 옵션:

- `AcquisitionStart`
  - 외부 트리거 입력으로 acquisition 시작
- `AcquisitionEnd`
  - 외부 트리거 입력으로 acquisition 정지
- `LineStart`
  - 외부 트리거 1회에 라인 1개 취득
  - 라인스캔 제어에서 가장 자주 사용할 가능성이 큼
- `FrameStart`
  - 외부 트리거 1회에 frame 취득
- `AcquisitionTransferStart`
  - 내부에 저장된 이미지를 지정 타이밍에 host로 전송

### 외부 트리거 + 지정 노출시간

라인마다 외부 트리거를 받고, 노출시간은 `ExposureTime`으로 지정하는 방식입니다.

```text
TriggerMode = On
TriggerSelector = LineStart
TriggerSource = 원하는 입력 라인 또는 내부 신호
TriggerActivation = RisingEdge 또는 FallingEdge
ExposureMode = Timed
ExposureTime = 원하는 값
```

주의:

- 외부 트리거 사용 시 라인레이트는 트리거 주기로 결정됩니다.
- `ExposureTime`은 트리거 주기보다 길 수 없습니다.
- 실제 노출 시간은 설정한 `ExposureTime`에 센서 offset이 더해집니다.

### 외부 트리거 + 노출시간 미지정

```text
TriggerMode = On
TriggerSelector = LineStart
TriggerActivation = RisingEdge 또는 FallingEdge
ExposureMode = Off
```

특징:

- 외부 트리거 주기가 라인레이트가 됩니다.
- 노출은 `1 / line rate` 기반으로 계산됩니다.

주의:

- 물체 속도와 트리거 주기가 흔들리면 노출도 함께 변할 수 있습니다.

### 외부 트리거 pulse width로 노출 제어

```text
TriggerMode = On
TriggerSelector = LineStart
TriggerActivation = LevelHigh 또는 LevelLow
ExposureMode = TriggerWidth
```

특징:

- 트리거 입력 신호의 high 또는 low 지속 시간이 노출 시간이 됩니다.
- 긴 노출이 필요할 때 사용할 수 있습니다.

주의:

- 실제 노출 시간은 트리거 pulse width에 센서 offset이 더해집니다.
- 매뉴얼의 최소 pulse width:
  - Camera internal logic: `0.11 μs`
  - TTL High Active: `0.53 μs`
  - TTL Low Active: `0.17 μs`

### 내부 라인레이트 + 지정 노출시간

```text
TriggerMode = Off
ExposureMode = Timed
ExposureTime = 원하는 값
AcquisitionLineRate = 원하는 값
```

주의:

- `ExposureTime`은 line period보다 길 수 없습니다.
- line period는 `1 / AcquisitionLineRate`입니다.

### 내부 라인레이트 + 노출시간 미지정

```text
TriggerMode = Off
ExposureMode = Off
AcquisitionLineRate = 원하는 값
```

특징:

- 노출은 `1 / line rate` 기반으로 자동 계산됩니다.

주의:

- 라인레이트를 높이면 노출 시간이 짧아져 영상이 어두워질 수 있습니다.

## 7. 노출 시간 조절

### ExposureMode

- `Off`
  - 별도 노출 제어 없음
  - line rate 기반으로 노출이 계산됨
- `Timed` 기본값
  - `ExposureTime` feature로 노출 시간 지정
- `TriggerWidth`
  - 트리거 입력 pulse width로 노출 시간 결정

### ExposureTime

- 최소 설정값: `0.11 μs`
- step: `0.01 μs`
- 최대값은 설정 상태에 따라 달라집니다.

### SW-4005M-5GE 실제 노출 시간 offset

SW-4005M-5GE의 exposure offset은:

```text
2.06 μs
```

따라서:

```text
ExposureMode = Timed일 때
Actual Exposure Time = ExposureTime + 2.06 μs

ExposureMode = TriggerWidth일 때
Actual Exposure Time = Trigger pulse width + 2.06 μs
```

예시:

```text
ExposureTime = 1.00 μs
Actual Exposure Time = 3.06 μs
```

주의:

- 실제 노출은 설정값보다 항상 offset만큼 길어집니다.
- 고속 라인레이트에서는 이 offset까지 고려해서 trigger period/line period를 잡아야 합니다.

### ExposureModeOption

- `PrioritizeExposureTime` 기본값
  - 설정한 `ExposureTime`을 우선합니다.
  - 현재 ExposureTime보다 짧은 line period가 필요한 라인레이트는 설정할 수 없습니다.
  - 더 빠른 라인레이트가 필요하면 먼저 `ExposureTime`을 줄여야 합니다.
- `PrioritizeLineRate`
  - 라인레이트를 우선합니다.
  - 라인레이트를 높였을 때 현재 `ExposureTime`이 너무 길면 카메라가 `ExposureTime`을 더 짧은 값으로 덮어쓸 수 있습니다.
  - 더 긴 노출을 원하면 먼저 `AcquisitionLineRate`를 낮춰야 합니다.

## 8. 트리거 입력 지연 / 타이밍 주의사항

`TriggerMode = On`일 때 외부 트리거 입력부터 실제 노출 시작까지 delay가 있습니다.

| 입력 조건 | ExposureMode Off | ExposureMode Timed | ExposureMode TriggerWidth |
|---|---:|---:|---:|
| Camera Internal Logic | 2.00 μs | 0.13 μs | 0.91 μs |
| TTL High Active | 2.13 μs | 0.25 μs | 0.21 μs |
| TTL Low Active | 2.41 μs | 0.54 μs | 0.50 μs |
| Opto High Active | 2.0 μs | 0.2 μs | 0.16 μs |
| Opto Low Active | 2.0 μs | 0.2 μs | 0.16 μs |

주의:

- 물체 위치 동기화가 중요하면 위 지연을 고려해야 합니다.
- encoder/센서 위치와 카메라 촬상 위치가 떨어져 있으면 `ImageOutputDelay`를 사용할 수 있습니다.
- `ImageOutputDelay`는 trigger 입력 후 host로 이미지가 출력되기까지의 라인 수 지연을 설정합니다.

## 9. GPIO / 트리거 입력 라인

DC IN/TRIG 12핀 커넥터 주요 핀:

- Pin 2, 11: `DC In`, `10.8 ~ 26.4 V`
- Pin 4: `TTL In 4`, `Line14`
- Pin 5, 6: `Opto In 1`, `Line5`
- Pin 7: `TTL Out 4`, `Line12`
- Pin 9: `TTL Out 1`, `Line1`
- Pin 10: `TTL In 1`, `Line4`
- Pin 1, 3, 12: GND

TTL signal specification:

- TTL Output Low: `0.0 V`
- TTL Output High: `5.0 V`
- TTL Input Low: `0.0 ~ 0.7 V`
- TTL Input High: `2.0 ~ 5.5 V`

TriggerSource 후보로 중요한 것:

- `Line4 TTL In1` 기본값
- `Line5 Opt In1`
- `Line14 TTL In4`
- `Software`
- `PulseGenerator0 ~ 3`
- `UserOutput0 ~ 3`
- `LogicBlock0 ~ 3`
- `EncoderTrigger`

주의:

- 이 카메라의 pin assignment는 다른 JAI 카메라와 다르다고 명시되어 있습니다.
- 실제 배선 전 반드시 핀 번호와 Line 번호를 혼동하지 않아야 합니다.

## 10. Encoder 관련 기능

카메라는 rotary encoder 직접 연결을 지원합니다.

주요 기능:

- `EncoderTrigger`
- `EncoderDirection`
- `EncoderDivider`
- `EdgeDetection`
- `EncoderOutputMode`
- `ObjectDirection`

활용:

- 컨베이어 속도와 line trigger를 동기화할 때 사용합니다.
- encoder 기반 LineStart trigger를 만들 수 있습니다.
- `EdgeDetection` 옵션은 A/B상 rising/falling edge를 활용해 n번째 edge마다 trigger를 만들 수 있습니다.

주의:

- encoder output interval이 크게 흔들리면 카메라 내부 trigger도 크게 흔들릴 수 있습니다.
- 벨트가 오래 멈췄다가 다시 움직이는 경우 `EncoderDivider` 계산 특성 때문에 trigger가 바로 나오지 않을 수 있습니다.
- 이 경우 `EncoderAveragingInterval`, `EncoderMaxIntervalForNonDecimationMode` 같은 보호 설정을 검토해야 합니다.

## 11. Gain 조절

SW-4005M-5GE는 흑백 모델이므로 gain은 `DigitalAll` 기준으로 조절합니다.

### 수동 Gain

```text
GainSelector = DigitalAll
Gain = 원하는 배율
```

범위:

```text
Mono model Gain range = 1.00 ~ 64.00
step = 0.01
default = 1.00
```

추가 옵션:

```text
InGainBypassMode = On 또는 Off
```

- `InGainBypassMode = On`
  - 카메라 내부 fixed gain, 즉 InGain을 비활성화하고 사용자 설정 gain만 적용합니다.
- 기본값은 `Off`입니다.

주의:

- Gain 값은 dB가 아니라 배율 단위입니다.
- Gain을 올리면 밝기는 증가하지만 노이즈도 증가합니다.
- SW-4005 모델은 `SensorGainMode`를 지원하지 않습니다. 이 기능은 SW-2005 일부 firmware 전용입니다.

### 자동 Gain

주요 feature:

```text
GainAuto = Once
GainAutoWidth
GainAutoOffsetX
AGCReference = 30 ~ 95 %, default 50 %
AGCOnceStatus
```

동작:

- `GainAuto = Once`로 설정하면 한 번 자동 조절합니다.
- 수렴이 끝나면 `GainAuto`는 자동으로 `Off`로 돌아갑니다.

주의:

- `GainAuto`는 `IndividualGainMode = Off`일 때만 사용 가능합니다.
- SW-4005M-5GE는 흑백 모델이라 RGB 개별 gain 관련 항목은 사용 대상이 아닙니다.

## 12. 영상 품질 보정 기능

### DSNU Correction / Pixel Black Correct

목적:

- dark 영역에서 픽셀별 black level 편차를 보정합니다.

절차 요약:

1. 센서 보호 캡을 장착합니다.
2. `AcquisitionStart`로 영상 취득을 시작합니다.
3. `PixelBlackCorrectionMode`에서 저장 영역 `User1 ~ User3`를 선택합니다.
4. `CalibratePixelBlackCorrection`을 실행합니다.
5. `PixelBlackCalibrationResult`를 확인합니다.

주의:

- line rate를 낮추거나 긴 노출을 사용하면 dark current가 변해 DSNU 상태가 바뀔 수 있습니다.
- 조명/노출/라인레이트 조건이 바뀌면 재보정이 필요할 수 있습니다.
- `TestPattern`이 `Off`가 아니거나 correction mode가 `Off`/`Default`이면 보정 실패 가능성이 있습니다.

### PRNU Correction / Pixel Gain Correct

목적:

- 밝은 조건에서 픽셀별 감도 편차를 보정합니다.

절차 요약:

1. 필요하면 `Width`, `OffsetX`로 보정 계산 영역을 설정합니다.
2. `PixelGainCorrectionMode`에서 저장 영역 또는 ROI 기반 모드를 선택합니다.
3. 균일한 밝기의 기준 영상을 준비합니다.
4. `CalibratePixelGainCorrection`을 실행합니다.
5. `PixelGainCalibrationResult`를 확인합니다.

주의:

- line rate가 느리거나 노출 시간이 길면 dark current 변화로 PRNU 상태도 달라질 수 있습니다.
- 너무 밝거나 너무 어두운 영상에서는 best effort correction 또는 실패가 발생할 수 있습니다.

### Shading Correction

목적:

- 렌즈/조명으로 인한 밝기 불균일을 보정합니다.

SW-4005M-5GE에서는 흑백 모델이므로 주로 flat shading 계열만 고려합니다.

절차 요약:

1. 필요하면 `Width`, `OffsetX`로 correction 계산 영역을 설정합니다.
2. `ShadingCorrectionMode`를 선택합니다.
3. 저장 영역 `User1 ~ User3`를 선택합니다.
4. 균일 조명 아래 white chart를 촬영합니다.
5. `CalibrateShadingCorrection`을 실행합니다.
6. `ShadingDetectResult`를 확인합니다.

주의:

- 영상이 너무 밝거나 어두우면 실패 또는 best effort 결과가 나올 수 있습니다.

## 13. Gamma / LUT

### Gamma

- `Gamma` 선택값:
  - `0.45`, `0.5`, `0.55`, `0.6`, `0.65`, `0.75`, `0.8`, `0.9`, `1.0`
- 사용 방법:
  - `Gamma` 값 선택
  - `LUTMode = Gamma`

### LUT

- 257개 index로 sensor output과 camera output 사이의 비선형 mapping을 설정합니다.
- `LUTIndex`: `0 ~ 256`
- `LUTValue`: `0 ~ 4095`

주의:

- 측정/검사 용도에서는 Gamma/LUT가 원시 밝기값을 변형할 수 있으므로 필요할 때만 켭니다.
- 정량 분석에서는 `LUTMode = Off` 또는 선형 조건을 우선 검토합니다.

## 14. UserSet 저장 / 로드

카메라 설정은 전원을 끄면 사라질 수 있으므로 필요한 설정은 UserSet에 저장합니다.

- 저장 가능 영역: `UserSet1`, `UserSet2`, `UserSet3`
- `Default`는 factory default이므로 덮어쓸 수 없습니다.

저장 절차:

1. image acquisition을 정지합니다.
2. `UserSetSelector`에서 저장 영역을 선택합니다.
3. `UserSetSave` 실행

로드 절차:

1. image acquisition을 정지합니다.
2. `UserSetSelector`에서 불러올 영역을 선택합니다.
3. `UserSetLoad` 실행

주의:

- UserSet 저장/로드는 image acquisition이 정지된 상태에서만 가능합니다.
- Control Tool/PC에 저장되는 것이 아니라 카메라 user memory에 저장됩니다.

## 15. 구현 시 추천 설정 패턴

### A. 컨베이어/encoder 기반 라인스캔

```text
AcquisitionMode = Continuous
TriggerMode = On
TriggerSelector = LineStart
TriggerSource = EncoderTrigger 또는 외부 입력 Line
TriggerActivation = RisingEdge/FallingEdge 또는 LevelHigh/LevelLow
ExposureMode = Timed 또는 TriggerWidth
PixelFormat = Mono8 또는 Mono10Packed
Width/OffsetX = 필요한 ROI
Height = 한 frame으로 묶을 라인 수
```

추천:

- 속도 동기화가 목적이면 encoder 기반 `LineStart`를 우선 검토합니다.
- 조명 안정성이 좋고 속도가 일정하면 `ExposureMode = Timed`가 제어하기 쉽습니다.
- trigger pulse width로 조명/노출을 직접 맞출 수 있으면 `ExposureMode = TriggerWidth`도 유용합니다.

주의:

- 트리거 주기보다 `ExposureTime + 2.06 μs`가 길어지지 않게 합니다.
- 실제 위치 동기화에는 trigger delay와 ImageOutputDelay를 고려합니다.

### B. 외부 트리거 없이 일정 속도 스캔

```text
AcquisitionMode = Continuous
TriggerMode = Off
ExposureMode = Timed
ExposureTime = 원하는 값
AcquisitionLineRate = 원하는 값
PixelFormat = Mono8 또는 Mono10Packed
```

주의:

- `ExposureTime`은 `1 / AcquisitionLineRate`보다 길 수 없습니다.
- `PrioritizeExposureTime` 기본값에서는 ExposureTime이 길면 라인레이트를 충분히 올리지 못할 수 있습니다.

### C. 밝기 우선 조정 순서

1. 조명 세기/조명 각도 조정
2. `ExposureTime` 증가
3. `Gain` 증가
4. 필요 시 `PRNU`, `DSNU`, `Shading Correction` 수행

주의:

- Gain은 마지막에 올리는 편이 좋습니다. 노이즈가 같이 증가합니다.
- 긴 노출이나 낮은 라인레이트를 쓰면 dark current 영향으로 보정 상태가 바뀔 수 있습니다.

## 16. SW-4005M-5GE에서 제외하거나 주의할 기능

- `SensorGainMode`
  - SW-4005 모델은 지원하지 않습니다.
  - SW-2005 일부 firmware 전용입니다.
- `WhiteBalance`, `BalanceWhiteAuto`
  - 색상 모델 전용입니다.
- `ColorTransformationControl`
  - 색상 모델 전용입니다.
- `IndividualGainMode`의 RGB 개별 조절
  - 색상 모델 전용입니다.
- `ExposureTimeMode = Individual`
  - TL 모델 전용입니다.
- `SpatialCompensation`
  - 색상 라인 센서의 RGB/Bilinear 보정 맥락이므로 SW-4005M-5GE 제어의 핵심 대상은 아닙니다.

## 17. 최소 제어 코드에서 우선 구현할 feature 후보

트리거/노출/gain 중심 wrapper를 만들 때 우선순위가 높은 feature입니다.

### 필수

- `AcquisitionMode`
- `AcquisitionStart`
- `AcquisitionStop`
- `TriggerMode`
- `TriggerSelector`
- `TriggerSource`
- `TriggerActivation`
- `ExposureMode`
- `ExposureTime`
- `AcquisitionLineRate`
- `PixelFormat`
- `Width`
- `Height`
- `OffsetX`
- `OffsetY`
- `GainSelector`
- `Gain`

### 있으면 좋은 기능

- `ExposureModeOption`
- `ImageOutputDelay`
- `LineSelector`
- `LineStatusAll`
- `LineSource`
- `UserSetSelector`
- `UserSetSave`
- `UserSetLoad`
- `GainAuto`
- `AGCReference`
- `PixelBlackCorrectionMode`
- `CalibratePixelBlackCorrection`
- `PixelGainCorrectionMode`
- `CalibratePixelGainCorrection`
- `ShadingCorrectionMode`
- `CalibrateShadingCorrection`

## 18. 핵심 주의사항 요약

- SW-4005M-5GE는 monochrome 모델입니다. 색상 전용 기능을 wrapper 기본 기능에 넣지 않아도 됩니다.
- 최대 라인레이트는 `84 kHz`지만 PixelFormat/ROI/네트워크 조건에 따라 낮아질 수 있습니다.
- Full width에서 `Mono10`, `Mono12`는 최대 `72 kHz`입니다.
- 외부 트리거 사용 시 라인레이트는 trigger period가 결정합니다.
- `ExposureTime`은 trigger period 또는 line period보다 길 수 없습니다.
- 실제 노출 시간은 `ExposureTime + 2.06 μs` 또는 `trigger pulse width + 2.06 μs`입니다.
- `TriggerMode = On`일 때 trigger 입력부터 노출 시작까지 delay가 있습니다.
- Gain 범위는 `1.00 ~ 64.00`, step `0.01`이며 gain 증가는 노이즈 증가를 동반합니다.
- `SensorGainMode`는 SW-4005 모델에서 지원하지 않습니다.
- UserSet 저장/로드는 acquisition 정지 상태에서만 가능합니다.
- DSNU/PRNU/Shading 보정은 조명, 노출, line rate 조건이 바뀌면 다시 해야 할 수 있습니다.
- 12핀 커넥터 pin assignment가 다른 JAI 카메라와 다르므로 배선 시 반드시 확인해야 합니다.
