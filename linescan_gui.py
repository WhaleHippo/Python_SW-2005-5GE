"""PySide6 GUI for the SW-2005/4005 5GigE line-scan camera wrapper.

Run with a Python environment that has PySide6==6.4.3 and numpy<2 installed:

    uv venv .venv
    . .venv/bin/activate
    uv pip install -r requirements-gui.txt
    python linescan_gui.py

The GUI intentionally uses the public, explicit properties exposed by
``linescan_module.LineScanCamera`` and converts captured grayscale bytes into a
2D numpy array for display.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from linescan_module import DEVICE_IP_ADDR, CaptureResult, LineScanCamera, LineScanFrame


TRIGGER_MODE_ITEMS = ["Off", "On"]
TRIGGER_SOURCE_ITEMS = ["Line4", "Software", "Line1", "Line2", "Line3", "Line5", "Line6"]
FRAME_HEIGHT_ITEMS = ["1", "2", "4", "8", "16", "32", "64", "128"]
BASE_EXPOSURE_US_MIN = 1
BASE_EXPOSURE_US_MAX = 1000
BASE_LINE_RATE_HZ_MIN = 1
BASE_LINE_RATE_HZ_MAX = 100_000
SECONDS_TO_MICROSECONDS = 1_000_000
SETTINGS_APPLY_DEBOUNCE_MS = 250


@dataclass(frozen=True)
class TimingLimits:
    exposure_us: int
    exposure_us_max: int
    line_rate_hz: int
    line_rate_hz_max: int


def _safe_reciprocal_limit(value: float, base_max: int) -> int:
    if value <= 0:
        return base_max
    return max(1, min(base_max, int(SECONDS_TO_MICROSECONDS // value)))


def timing_limits(exposure_us: float, line_rate_hz: float) -> TimingLimits:
    """Return mutually constrained exposure/line-rate values and maxima.

    A line period is ``1 / line_rate`` seconds, so exposure time cannot be
    longer than that period. Likewise, a selected exposure time limits the
    maximum line rate to ``1_000_000 / exposure_us`` Hz.
    """
    exposure_us_max = _safe_reciprocal_limit(line_rate_hz, BASE_EXPOSURE_US_MAX)
    line_rate_hz_max = _safe_reciprocal_limit(exposure_us, BASE_LINE_RATE_HZ_MAX)
    exposure = max(BASE_EXPOSURE_US_MIN, min(int(round(exposure_us)), exposure_us_max))
    line_rate = max(BASE_LINE_RATE_HZ_MIN, min(int(round(line_rate_hz)), line_rate_hz_max))
    return TimingLimits(
        exposure_us=exposure,
        exposure_us_max=exposure_us_max,
        line_rate_hz=line_rate,
        line_rate_hz_max=line_rate_hz_max,
    )


def controls_enabled_after_open(camera_open: bool) -> bool:
    return bool(camera_open)


def trigger_source_enabled(*, camera_open: bool, trigger_mode: str) -> bool:
    return bool(camera_open) and trigger_mode == "On"


def _coerce_payload_to_uint8(payload: object) -> np.ndarray:
    """Return a 1D uint8 array from bytes-like or sequence payloads."""
    if payload is None:
        return np.empty((0,), dtype=np.uint8)
    if isinstance(payload, np.ndarray):
        return np.asarray(payload, dtype=np.uint8).reshape(-1)
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return np.frombuffer(payload, dtype=np.uint8)
    try:
        return np.frombuffer(bytes(payload), dtype=np.uint8)
    except Exception:
        try:
            return np.asarray(list(payload), dtype=np.uint8).reshape(-1)  # type: ignore[arg-type]
        except Exception:
            return np.empty((0,), dtype=np.uint8)


def frame_to_array(frame: LineScanFrame) -> np.ndarray:
    """Convert one LineScanFrame's grayscale bytes into a 2D numpy array.

    Width/height metadata from the camera is preferred. If height is missing but
    width is known, the payload is reshaped into as many complete lines as fit.
    Empty or non-convertible payloads return ``shape == (0, 0)``.
    """
    payload = _coerce_payload_to_uint8(frame.image)
    if payload.size == 0:
        return np.empty((0, 0), dtype=np.uint8)

    width = int(frame.width or 0)
    height = int(frame.height or 0)
    if width > 0 and height > 0 and payload.size >= width * height:
        return payload[: width * height].reshape(height, width).copy()
    if width > 0:
        complete_lines = payload.size // width
        if complete_lines > 0:
            return payload[: complete_lines * width].reshape(complete_lines, width).copy()
    return payload.reshape(1, -1).copy()


def capture_result_to_array(result: CaptureResult) -> np.ndarray:
    """Stack captured grayscale frames vertically into one 2D image array."""
    arrays = [arr for arr in (frame_to_array(frame) for frame in result.frames) if arr.size]
    if not arrays:
        return np.empty((0, 0), dtype=np.uint8)

    min_width = min(arr.shape[1] for arr in arrays)
    if min_width <= 0:
        return np.empty((0, 0), dtype=np.uint8)
    arrays = [arr[:, :min_width] for arr in arrays]
    return np.vstack(arrays).astype(np.uint8, copy=False)


@dataclass(frozen=True)
class CaptureSettings:
    exposure_us: float
    line_rate_hz: float
    trigger_mode: str
    trigger_source: str
    frame_height: int
    duration_s: float


def _format_filename_number(value: float, unit: str) -> str:
    number = f"{value:g}".replace(".", "p")
    return f"{number}{unit}"


def _safe_filename_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)


def next_capture_image_path(captures_dir: Path, settings: CaptureSettings) -> Path:
    """Return the next auto-numbered capture image path for the given settings."""
    next_number = 1
    if captures_dir.exists():
        for path in captures_dir.glob("*.png"):
            number_text = path.stem.split("_", 1)[0]
            if number_text.isdigit():
                next_number = max(next_number, int(number_text) + 1)

    exposure = _format_filename_number(settings.exposure_us, "us")
    line_rate = _format_filename_number(settings.line_rate_hz, "hz")
    trigger_mode = _safe_filename_part(settings.trigger_mode)
    filename = f"{next_number}_exposure_{exposure}_linerate_{line_rate}_trigger_{trigger_mode}.png"
    return captures_dir / filename


def load_qt():
    """Import PySide6 lazily so helper functions can be unit-tested headlessly."""
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "PySide6가 설치되어 있지 않습니다. 다음처럼 PySide6 6.4.3을 설치한 뒤 실행하세요:\n"
            "  uv venv .venv\n"
            "  . .venv/bin/activate\n"
            "  uv pip install -r requirements-gui.txt\n"
            "  python linescan_gui.py"
        ) from exc
    return QtCore, QtGui, QtWidgets


def make_application_classes(QtCore, QtGui, QtWidgets):
    class ImageView(QtWidgets.QLabel):
        def __init__(self):
            super().__init__("Capture 결과가 여기에 표시됩니다.")
            self.setAlignment(QtCore.Qt.AlignCenter)
            self.setMinimumSize(640, 480)
            self.setStyleSheet("background: #111; color: #bbb; border: 1px solid #444;")
            self._array = np.empty((0, 0), dtype=np.uint8)
            self._pixmap = QtGui.QPixmap()
            self._capture_settings: CaptureSettings | None = None
            self._captures_dir = Path(__file__).resolve().parent / "captures"

        def set_array(self, array: np.ndarray, settings: CaptureSettings | None = None) -> None:
            self._array = np.asarray(array, dtype=np.uint8)
            if self._array.size == 0 or self._array.ndim != 2:
                self._pixmap = QtGui.QPixmap()
                self._capture_settings = None
                self.clear()
                self.setText("표시할 capture 데이터가 없습니다.")
                return

            self._capture_settings = settings

            height, width = self._array.shape
            contiguous = np.ascontiguousarray(self._array)
            bytes_per_line = int(contiguous.strides[0])
            image = QtGui.QImage(
                contiguous.data,
                width,
                height,
                bytes_per_line,
                QtGui.QImage.Format_Grayscale8,
            ).copy()
            self._pixmap = QtGui.QPixmap.fromImage(image)
            self._rescale_pixmap()

        def resizeEvent(self, event):  # noqa: N802 - Qt override
            super().resizeEvent(event)
            self._rescale_pixmap()

        def save_current_image(self) -> None:
            if self._pixmap.isNull():
                QtWidgets.QMessageBox.information(self, "이미지 저장", "저장할 이미지가 없습니다.")
                return

            if self._capture_settings is None:
                QtWidgets.QMessageBox.warning(
                    self,
                    "이미지 저장 실패",
                    "이미지 저장에 필요한 capture 설정 정보가 없습니다.",
                )
                return

            try:
                self._captures_dir.mkdir(parents=True, exist_ok=True)
                path = next_capture_image_path(self._captures_dir, self._capture_settings)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(
                    self,
                    "이미지 저장 실패",
                    f"저장 경로를 만들 수 없습니다:\n{exc}",
                )
                return

            if not self._pixmap.save(str(path)):
                QtWidgets.QMessageBox.warning(self, "이미지 저장 실패", f"이미지를 저장할 수 없습니다:\n{path}")
                return
            QtWidgets.QMessageBox.information(self, "이미지 저장", f"이미지를 저장했습니다:\n{path}")

        def _rescale_pixmap(self) -> None:
            if self._pixmap.isNull():
                return
            scaled = self._pixmap.scaled(
                self.size(),
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.FastTransformation,
            )
            self.setPixmap(scaled)

    class CaptureWorker(QtCore.QObject):
        finished = QtCore.Signal(object, object)  # CaptureResult | None, error_text | None
        progress = QtCore.Signal(str)

        def __init__(self, camera: LineScanCamera, settings: CaptureSettings):
            super().__init__()
            self.camera = camera
            self.settings = settings

        @QtCore.Slot()
        def run(self) -> None:
            try:
                self.progress.emit("카메라 설정 적용 중...")
                self.camera.exposure_time = self.settings.exposure_us
                self.camera.acquisition_line_rate = self.settings.line_rate_hz
                self.camera.height = self.settings.frame_height
                self.camera.trigger_mode = self.settings.trigger_mode == "On"
                if self.settings.trigger_mode == "On":
                    self.camera.trigger_selector = "LineStart"
                    self.camera.trigger_source = self.settings.trigger_source

                self.progress.emit(f"{self.settings.duration_s:.3f}초 capture 중...")
                result = self.camera.capture(
                    duration_s=self.settings.duration_s,
                    trigger_mode=self.settings.trigger_mode == "On",
                    store_frames=True,
                    copy_frames=True,
                    debug=False,
                )
                self.finished.emit(result, None)
            except Exception:
                self.finished.emit(None, traceback.format_exc())

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("SW-2005/4005 5GE Line Scan Capture")
            self.resize(1200, 760)
            self.camera: LineScanCamera | None = None
            self.capture_thread: QtCore.QThread | None = None
            self.capture_worker: CaptureWorker | None = None
            self._settings_apply_timer = QtCore.QTimer(self)
            self._settings_apply_timer.setSingleShot(True)
            self._settings_apply_timer.timeout.connect(self._apply_pending_settings)

            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            root = QtWidgets.QHBoxLayout(central)

            controls = QtWidgets.QWidget()
            controls.setMaximumWidth(360)
            form = QtWidgets.QVBoxLayout(controls)
            form.setAlignment(QtCore.Qt.AlignTop)
            root.addWidget(controls)

            self.open_close_button = QtWidgets.QPushButton("Open Camera")
            self.open_close_button.clicked.connect(self.toggle_camera)
            form.addWidget(self.open_close_button)

            self.exposure_label = QtWidgets.QLabel()
            self.exposure_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.exposure_slider.setRange(BASE_EXPOSURE_US_MIN, BASE_EXPOSURE_US_MAX)
            self.exposure_slider.setValue(5)
            self.exposure_slider.valueChanged.connect(self._update_exposure_label)
            self.exposure_slider.valueChanged.connect(self._exposure_changed)
            form.addWidget(self.exposure_label)
            form.addWidget(self.exposure_slider)

            self.line_rate_label = QtWidgets.QLabel()
            self.line_rate_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.line_rate_slider.setRange(BASE_LINE_RATE_HZ_MIN, BASE_LINE_RATE_HZ_MAX)
            self.line_rate_slider.setSingleStep(100)
            self.line_rate_slider.setPageStep(1000)
            self.line_rate_slider.setValue(66)
            self.line_rate_slider.valueChanged.connect(self._update_line_rate_label)
            self.line_rate_slider.valueChanged.connect(self._line_rate_changed)
            form.addWidget(self.line_rate_label)
            form.addWidget(self.line_rate_slider)

            form.addWidget(QtWidgets.QLabel("Trigger mode"))
            self.trigger_mode_combo = QtWidgets.QComboBox()
            self.trigger_mode_combo.addItems(TRIGGER_MODE_ITEMS)
            self.trigger_mode_combo.currentTextChanged.connect(self._trigger_mode_changed)
            form.addWidget(self.trigger_mode_combo)

            form.addWidget(QtWidgets.QLabel("Trigger source"))
            self.trigger_source_combo = QtWidgets.QComboBox()
            self.trigger_source_combo.addItems(TRIGGER_SOURCE_ITEMS)
            self.trigger_source_combo.currentTextChanged.connect(self._schedule_settings_apply)
            form.addWidget(self.trigger_source_combo)

            form.addWidget(QtWidgets.QLabel("Frame height"))
            self.frame_height_combo = QtWidgets.QComboBox()
            self.frame_height_combo.addItems(FRAME_HEIGHT_ITEMS)
            self.frame_height_combo.setCurrentText("1")
            self.frame_height_combo.currentTextChanged.connect(self._schedule_settings_apply)
            form.addWidget(self.frame_height_combo)

            form.addWidget(QtWidgets.QLabel("Capture duration (s)"))
            self.duration_spin = QtWidgets.QDoubleSpinBox()
            self.duration_spin.setRange(0.01, 3600.0)
            self.duration_spin.setDecimals(3)
            self.duration_spin.setSingleStep(0.1)
            self.duration_spin.setValue(1.0)
            form.addWidget(self.duration_spin)

            self.capture_button = QtWidgets.QPushButton("Capture")
            self.capture_button.setEnabled(False)
            self.capture_button.clicked.connect(self.start_capture)
            form.addWidget(self.capture_button)

            self.status_label = QtWidgets.QLabel("카메라가 닫혀 있습니다.")
            self.status_label.setWordWrap(True)
            form.addWidget(self.status_label)

            self.summary_text = QtWidgets.QPlainTextEdit()
            self.summary_text.setReadOnly(True)
            self.summary_text.setMaximumHeight(230)
            form.addWidget(self.summary_text)

            image_panel = QtWidgets.QWidget()
            image_layout = QtWidgets.QVBoxLayout(image_panel)
            image_layout.setContentsMargins(0, 0, 0, 0)
            self.image_view = ImageView()
            image_layout.addWidget(self.image_view, stretch=1)
            self.save_image_button = QtWidgets.QPushButton("이미지 저장")
            self.save_image_button.clicked.connect(self.image_view.save_current_image)
            image_layout.addWidget(self.save_image_button)
            root.addWidget(image_panel, stretch=1)

            self._timing_update_in_progress = False
            self._update_exposure_label(self.exposure_slider.value())
            self._update_line_rate_label(self.line_rate_slider.value())
            self._exposure_changed(self.exposure_slider.value())
            self._update_control_enabled_state()

        def closeEvent(self, event):  # noqa: N802 - Qt override
            self._settings_apply_timer.stop()
            self._close_camera()
            super().closeEvent(event)

        def _update_exposure_label(self, value: int) -> None:
            self.exposure_label.setText(f"Exposure time: {value} µs")

        def _update_line_rate_label(self, value: int) -> None:
            self.line_rate_label.setText(f"Line rate: {value:,} Hz")

        def _exposure_changed(self, value: int) -> None:
            if self._timing_update_in_progress:
                return
            self._timing_update_in_progress = True
            try:
                max_line_rate = _safe_reciprocal_limit(value, BASE_LINE_RATE_HZ_MAX)
                self.line_rate_slider.setMaximum(max(BASE_LINE_RATE_HZ_MIN, max_line_rate))
                if self.line_rate_slider.value() > self.line_rate_slider.maximum():
                    self.line_rate_slider.setValue(self.line_rate_slider.maximum())
                max_exposure = _safe_reciprocal_limit(
                    self.line_rate_slider.value(), BASE_EXPOSURE_US_MAX
                )
                self.exposure_slider.setMaximum(max(BASE_EXPOSURE_US_MIN, max_exposure))
            finally:
                self._timing_update_in_progress = False
            self._update_exposure_label(self.exposure_slider.value())
            self._update_line_rate_label(self.line_rate_slider.value())
            self._schedule_settings_apply()

        def _line_rate_changed(self, value: int) -> None:
            if self._timing_update_in_progress:
                return
            self._timing_update_in_progress = True
            try:
                max_exposure = _safe_reciprocal_limit(value, BASE_EXPOSURE_US_MAX)
                self.exposure_slider.setMaximum(max(BASE_EXPOSURE_US_MIN, max_exposure))
                if self.exposure_slider.value() > self.exposure_slider.maximum():
                    self.exposure_slider.setValue(self.exposure_slider.maximum())
                max_line_rate = _safe_reciprocal_limit(
                    self.exposure_slider.value(), BASE_LINE_RATE_HZ_MAX
                )
                self.line_rate_slider.setMaximum(max(BASE_LINE_RATE_HZ_MIN, max_line_rate))
            finally:
                self._timing_update_in_progress = False
            self._update_exposure_label(self.exposure_slider.value())
            self._update_line_rate_label(self.line_rate_slider.value())
            self._schedule_settings_apply()

        def _trigger_mode_changed(self, *_args) -> None:
            self._update_control_enabled_state()
            self._schedule_settings_apply()

        def _capture_running(self) -> bool:
            return self.capture_thread is not None and self.capture_thread.isRunning()

        def _schedule_settings_apply(self, *_args) -> None:
            if self.camera is None or not self.camera.is_open or self._capture_running():
                return
            self._settings_apply_timer.start(SETTINGS_APPLY_DEBOUNCE_MS)

        def _apply_pending_settings(self) -> None:
            if self.camera is None or not self.camera.is_open or self._capture_running():
                return
            try:
                self._apply_current_settings()
                settings = self._settings()
                self.status_label.setText(
                    "설정 적용됨: "
                    f"exposure={settings.exposure_us:g} µs, "
                    f"line_rate={settings.line_rate_hz:g} Hz, "
                    f"height={settings.frame_height}, "
                    f"trigger={settings.trigger_mode}"
                )
            except Exception:
                self.status_label.setText("설정 적용 실패")
                self.summary_text.setPlainText(traceback.format_exc())

        def _control_widgets(self):
            return (
                self.exposure_slider,
                self.line_rate_slider,
                self.trigger_mode_combo,
                self.frame_height_combo,
                self.duration_spin,
                self.capture_button,
            )

        def _update_control_enabled_state(self) -> None:
            camera_open = self.camera is not None and self.camera.is_open
            controls_enabled = controls_enabled_after_open(camera_open)
            capture_running = self._capture_running()
            for widget in self._control_widgets():
                widget.setEnabled(controls_enabled and not capture_running)
            self.trigger_source_combo.setEnabled(
                trigger_source_enabled(
                    camera_open=camera_open and not capture_running,
                    trigger_mode=self.trigger_mode_combo.currentText(),
                )
            )

        def toggle_camera(self) -> None:
            if self.camera is not None and self.camera.is_open:
                self._close_camera()
                return
            try:
                self.status_label.setText("카메라 연결 중...")
                QtWidgets.QApplication.processEvents()
                self.camera = LineScanCamera(
                    device_ip_addr=DEVICE_IP_ADDR,
                    copy_frames=True,
                    force_ip=True,
                    debug=False,
                )
                self.camera.open()
                self.open_close_button.setText("Close Camera")
                self._update_control_enabled_state()
                self.status_label.setText(f"연결됨: {DEVICE_IP_ADDR}")
                self._apply_current_settings()
            except Exception as exc:
                self._close_camera()
                self.status_label.setText(f"연결 실패: {exc}")
                self.summary_text.setPlainText(traceback.format_exc())

        def _close_camera(self) -> None:
            self._settings_apply_timer.stop()
            if self.capture_thread is not None and self.capture_thread.isRunning():
                self.status_label.setText("Capture 중에는 close할 수 없습니다.")
                return
            if self.camera is not None:
                try:
                    self.camera.close()
                except Exception:
                    pass
            self.camera = None
            self.open_close_button.setText("Open Camera")
            self._update_control_enabled_state()
            self.status_label.setText("카메라가 닫혀 있습니다.")

        def _settings(self) -> CaptureSettings:
            return CaptureSettings(
                exposure_us=float(self.exposure_slider.value()),
                line_rate_hz=float(self.line_rate_slider.value()),
                trigger_mode=self.trigger_mode_combo.currentText(),
                trigger_source=self.trigger_source_combo.currentText(),
                frame_height=int(self.frame_height_combo.currentText()),
                duration_s=float(self.duration_spin.value()),
            )

        def _apply_current_settings(self) -> None:
            if self.camera is None:
                return
            settings = self._settings()
            self.camera.exposure_time = settings.exposure_us
            self.camera.acquisition_line_rate = settings.line_rate_hz
            self.camera.height = settings.frame_height
            self.camera.trigger_mode = settings.trigger_mode == "On"
            if settings.trigger_mode == "On":
                self.camera.trigger_selector = "LineStart"
                self.camera.trigger_source = settings.trigger_source

        def start_capture(self) -> None:
            if self.camera is None or not self.camera.is_open:
                self.status_label.setText("먼저 카메라를 open 해주세요.")
                return
            if self.capture_thread is not None and self.capture_thread.isRunning():
                self.status_label.setText("이미 capture 중입니다.")
                return

            self._settings_apply_timer.stop()
            self.capture_button.setEnabled(False)
            self.open_close_button.setEnabled(False)
            self._update_control_enabled_state()
            self.summary_text.clear()

            self.capture_thread = QtCore.QThread(self)
            self.capture_worker = CaptureWorker(self.camera, self._settings())
            self.capture_worker.moveToThread(self.capture_thread)
            self.capture_thread.started.connect(self.capture_worker.run)
            self.capture_worker.progress.connect(self.status_label.setText)
            self.capture_worker.finished.connect(self._capture_finished)
            self.capture_worker.finished.connect(self.capture_thread.quit)
            self.capture_worker.finished.connect(self.capture_worker.deleteLater)
            self.capture_thread.finished.connect(self.capture_thread.deleteLater)
            self.capture_thread.start()

        @QtCore.Slot(object, object)
        def _capture_finished(self, result: CaptureResult | None, error_text: str | None) -> None:
            self.open_close_button.setEnabled(True)
            capture_settings = self.capture_worker.settings if self.capture_worker is not None else None
            self.capture_thread = None
            self.capture_worker = None
            self._update_control_enabled_state()

            if error_text:
                self.status_label.setText("Capture 실패")
                self.summary_text.setPlainText(str(error_text))
                return
            if result is None:
                self.status_label.setText("Capture 결과가 없습니다.")
                return

            image_array = capture_result_to_array(result)
            self.image_view.set_array(image_array, capture_settings)
            self.status_label.setText(
                f"Capture 완료: array shape={tuple(image_array.shape)}, frames={result.stats.frames}"
            )
            self.summary_text.setPlainText(
                result.debug_summary()
                + "\n"
                + f"numpy_array_shape: {tuple(image_array.shape)}\n"
                + f"numpy_array_dtype: {image_array.dtype}"
            )

    return MainWindow


def main() -> int:
    QtCore, QtGui, QtWidgets = load_qt()
    app = QtWidgets.QApplication(sys.argv)
    MainWindow = make_application_classes(QtCore, QtGui, QtWidgets)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
