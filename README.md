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

GUI 왼쪽 패널에서 카메라 open/close, 노출 시간, line rate, trigger mode/source, capture duration을 설정하고 `Capture`를 누르면 `linescan_module.py`의 `LineScanCamera.capture()` 결과 bytes를 NumPy 2차원 `uint8` array로 변환해 오른쪽 패널에 grayscale로 표시합니다.
    
# 자료 출처
https://www.jai.com/support-software/jai-software
https://www.jai.com/support-software/ubuntu-x86/

    
