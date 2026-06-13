# 개요
라인스캔 카메라 SW-4005M-5GE의 동작을 위한 Python 라이브러리입니다.
JAI사의 기초적인 Python wrapper인 eBUS.py를 보다 효율적인 구조로 사용하기 위한 추가 wrapper 라이브러리입니다.

# 타겟 카메라
- 모델명: SW-4005M-5GE
- 제조사: JAI
- 인터페이스: 5GigE Vision
- 용도: 라인스캔 카메라 제어 및 영상 취득

# 자료 설명
    1. eBUS.py : SW-4005M-5GE의 동작을 위한 JAI의 Python wrapper (git으로 관리되지 않음)
    2. eBUS_examples : eBUS.py를 이용한 기초적인 예제 코드 (git으로 관리되지 않음)
    3. SW-4005M-5GE_usermanual.pdf : SW-4005M-5GE의 사용 설명서 (git으로 관리되지 않음)
    4. docs : 개발을 위한 문서자료 모음
    5. linescan_module.py : 개발할 wrapper 라이브러리
    6. linescan_gui.py : PySide6 6.4.3 기반 라인스캔 capture GUI

# GUI 실행
PySide6 6.4.3은 NumPy 1.x 계열과 함께 사용하는 것이 안전하므로 GUI용 의존성은 별도 파일에 고정했습니다.

```bash
uv venv .venv
. .venv/bin/activate
uv pip install -r requirements-gui.txt
python linescan_gui.py
```

GUI 왼쪽 패널에서 카메라 open/close, 노출 시간, line rate, trigger mode/source, capture duration을 설정하고 `Capture`를 누르면 `linescan_module.py`의 `LineScanCamera.capture()` 결과 bytes를 NumPy 2차원 `uint8` array로 변환해 오른쪽 패널에 grayscale로 표시합니다. 카메라가 open되기 전에는 설정/캡처 위젯이 비활성화되며, trigger source는 trigger mode가 `On`일 때만 활성화됩니다. 노출 시간과 line rate slider는 `line period = 1 / line rate` 관계에 맞춰 서로의 최대값을 자동 제한합니다.

# 주의사항
- 본 라이브러리는 Pleora/JAI eBUS Python binding(`eBUS.py`, `_ebus_python`)을 내부에서 사용합니다. 이 native wrapper는 thread-safe 동작을 보장한다고 보기 어렵기 때문에, eBUS 관련 객체(`LineScanCamera`, `PvDevice`, `PvStream`, `PvPipeline` 등)를 여러 thread에서 공유하거나 thread 경계를 넘어 전달하지 않는 것을 권장합니다.
- GUI에서는 eBUS 객체의 thread affinity 문제를 피하기 위해 `LineScanCamera` 생성, open/close, 설정 적용, capture를 하나의 `QThread` 안에서 수행하는 구조를 사용합니다. 이후 수정 시에도 eBUS 객체는 생성된 thread 안에서만 lifecycle을 관리해야 합니다.
- native binding에서 발생하는 `Segmentation fault`/`Bus error`는 Python exception으로 잡히지 않을 수 있습니다. thread 경계를 넘어 eBUS 객체를 사용해야 하는 구조라면 별도 프로세스 격리 같은 더 강한 경계를 검토하세요.
    
# 자료 출처
https://www.jai.com/support-software/jai-software
https://www.jai.com/support-software/ubuntu-x86/

    
