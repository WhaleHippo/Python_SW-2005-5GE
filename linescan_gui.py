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
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

import numpy as np


TRIGGER_MODE_ITEMS = ["Off", "On"]
TRIGGER_SOURCE_ITEMS = ["Line4", "Software", "Line1", "Line2", "Line3", "Line5", "Line6"]
FRAME_HEIGHT_ITEMS = ["1", "2", "4", "8", "16", "32", "64", "128"]
BASE_EXPOSURE_US_MIN = 1
BASE_EXPOSURE_US_MAX = 100
BASE_LINE_RATE_HZ_MIN = 66
BASE_LINE_RATE_HZ_MAX = 10_000
SETTINGS_APPLY_DEBOUNCE_MS = 250
SETTINGS_SETTLE_MS = 1000
"""Time to wait after applying camera settings before enabling capture."""
DEFAULT_DEVICE_IP_ADDR = "192.168.1.200"
"""Default camera IP used by the GUI. The camera service imports linescan_module lazily."""


def controls_enabled_after_open(camera_open: bool) -> bool:
    return bool(camera_open)


def trigger_source_enabled(*, camera_open: bool, trigger_mode: str) -> bool:
    return bool(camera_open) and trigger_mode == "On"


def _coerce_payload_to_uint8(payload: object) -> np.ndarray:
    """Return a 1D uint8 array from safe bytes-like or sequence payloads.

    Avoid arbitrary ``bytes(payload)`` fallback: eBUS/SWIG may expose raw native
    pointers whose memory becomes invalid as soon as the pipeline buffer is
    released. Calling Python's conversion protocol on such objects after release
    can segfault instead of raising a Python exception.
    """
    if payload is None:
        return np.empty((0,), dtype=np.uint8)
    if isinstance(payload, np.ndarray):
        return np.asarray(payload, dtype=np.uint8).reshape(-1)
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return np.frombuffer(payload, dtype=np.uint8)
    if isinstance(payload, (list, tuple)):
        try:
            return np.asarray(payload, dtype=np.uint8).reshape(-1)
        except Exception:
            return np.empty((0,), dtype=np.uint8)
    return np.empty((0,), dtype=np.uint8)


def frame_to_array(frame: Any) -> np.ndarray:
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


def capture_result_to_array(result: Any) -> np.ndarray:
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


@dataclass(frozen=True)
class GuiCapturePayload:
    result: Any
    image_array: np.ndarray
    preview_bytes: int


@dataclass(frozen=True)
class CameraCommand:
    """Plain camera command message usable by QThread today and a Process later."""

    action: str
    settings: CaptureSettings | None = None
    command_id: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CameraEvent:
    """Plain camera event message emitted from the isolated camera service."""

    kind: str
    command_id: int = 0
    payload: Any = None
    error_text: str | None = None


class CameraServiceCore:
    """Own the full eBUS camera lifecycle without depending on Qt widgets.

    This class is intentionally plain Python: the GUI sends ``CameraCommand``
    messages and receives ``CameraEvent`` messages.  The current GUI runs it in
    one QThread, but the same command/event boundary can later be moved into a
    multiprocessing/subprocess worker without passing Qt or eBUS objects across
    the boundary.
    """

    def __init__(
        self,
        *,
        camera_factory: Callable[[], Any] | None = None,
        device_ip_addr: str = DEFAULT_DEVICE_IP_ADDR,
    ):
        self._camera_factory = camera_factory
        self.device_ip_addr = device_ip_addr
        self.camera: Any | None = None

    @property
    def is_open(self) -> bool:
        return bool(self.camera is not None and getattr(self.camera, "is_open", False))

    def handle(self, command: CameraCommand) -> list[CameraEvent]:
        try:
            if command.action == "open":
                return [self._open(command)]
            if command.action == "config":
                return [self._config(command)]
            if command.action == "capture":
                return self._capture(command)
            if command.action == "close":
                return [self._close(command)]
            if command.action == "shutdown":
                event = self._close(command)
                return [CameraEvent("shutdown", command.command_id, event.payload)]
            raise ValueError(f"unknown camera command: {command.action}")
        except Exception:
            return [CameraEvent("error", command.command_id, error_text=traceback.format_exc())]

    def _make_camera(self) -> Any:
        if self._camera_factory is not None:
            return self._camera_factory()
        from linescan_module import LineScanCamera

        return LineScanCamera(device_ip_addr=self.device_ip_addr, copy_frames=True, debug=False)

    def _open(self, command: CameraCommand) -> CameraEvent:
        if self.camera is None:
            self.camera = self._make_camera()
        self.camera.open()
        if command.settings is not None:
            self._apply_settings(command.settings)
        return CameraEvent("opened", command.command_id, self._camera_info())

    def _config(self, command: CameraCommand) -> CameraEvent:
        self._require_open()
        if command.settings is None:
            raise ValueError("config command requires settings")
        self._apply_settings(command.settings)
        return CameraEvent("config_applied", command.command_id, self._camera_info())

    def _capture(self, command: CameraCommand) -> list[CameraEvent]:
        self._require_open()
        if command.settings is None:
            raise ValueError("capture command requires settings")
        accumulator = DisplayImageAccumulator()

        def on_frame(frame: Any) -> None:
            # Convert while the eBUS pipeline buffer is still checked out.
            accumulator.add_frame(frame)

        started = CameraEvent("capture_started", command.command_id, command.settings)
        result = self.camera.capture(
            duration_s=command.settings.duration_s,
            store_frames=False,
            copy_frames=True,
            on_frame=on_frame,
            debug=False,
        )
        payload = GuiCapturePayload(
            result=result,
            image_array=accumulator.to_array(),
            preview_bytes=accumulator.bytes_used,
        )
        return [started, CameraEvent("capture_finished", command.command_id, payload)]

    def _close(self, command: CameraCommand) -> CameraEvent:
        if self.camera is not None:
            try:
                self.camera.close()
            finally:
                self.camera = None
        return CameraEvent("closed", command.command_id, {"device_ip_addr": self.device_ip_addr})

    def _require_open(self) -> None:
        if self.camera is None or not getattr(self.camera, "is_open", False):
            raise RuntimeError("camera is not open")

    def _apply_settings(self, settings: CaptureSettings) -> None:
        self._require_open()
        self.camera.exposure_time = settings.exposure_us
        self.camera.acquisition_line_rate = settings.line_rate_hz
        self.camera.height = settings.frame_height
        self.camera.trigger_mode = settings.trigger_mode == "On"
        if settings.trigger_mode == "On":
            self.camera.trigger_selector = "LineStart"
            self.camera.trigger_source = settings.trigger_source

    def _camera_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"device_ip_addr": self.device_ip_addr, "is_open": self.is_open}
        if self.camera is not None:
            for key in ("exposure_time_max", "acquisition_line_rate_max"):
                try:
                    info[key] = float(getattr(self.camera, key))
                except Exception:
                    pass
        return info


class DisplayImageAccumulator:
    """Accumulate a GUI preview array while capture buffers are still valid."""

    def __init__(self):
        self.width: int | None = None
        self.rows = 0
        self._data = bytearray()

    @property
    def bytes_used(self) -> int:
        return len(self._data)

    def add_frame(self, frame: Any) -> None:
        array = frame_to_array(frame)
        if array.size == 0 or array.ndim != 2 or array.shape[1] <= 0:
            return

        array = np.ascontiguousarray(array, dtype=np.uint8)
        _height, frame_width = array.shape
        if self.width is None:
            self.width = int(frame_width)
        elif frame_width != self.width:
            new_width = min(self.width, int(frame_width))
            if new_width <= 0:
                return
            if new_width < self.width and self.rows > 0:
                existing = np.frombuffer(self._data, dtype=np.uint8).reshape(self.rows, self.width)
                self._data = bytearray(np.ascontiguousarray(existing[:, :new_width]).tobytes())
                self.width = new_width
            array = array[:, : self.width]

        self._data.extend(array[:, : self.width].tobytes())
        self.rows += int(array.shape[0])

    def to_array(self) -> np.ndarray:
        if self.width is None or self.rows <= 0:
            return np.empty((0, 0), dtype=np.uint8)
        return np.frombuffer(self._data, dtype=np.uint8).reshape(self.rows, self.width).copy()


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

    class CameraServiceWorker(QtCore.QObject):
        """Qt adapter around the plain camera service command queue."""

        event = QtCore.Signal(object)

        def __init__(self, core: CameraServiceCore | None = None):
            super().__init__()
            self.core = core or CameraServiceCore()
            self._commands: Queue[CameraCommand] = Queue()
            self._processing = False

        @QtCore.Slot(object)
        def submit(self, command: CameraCommand) -> None:
            self._commands.put(command)
            self._drain_commands()

        @QtCore.Slot()
        def shutdown_now(self) -> None:
            for event in self.core.handle(CameraCommand("shutdown")):
                self.event.emit(event)

        def _drain_commands(self) -> None:
            if self._processing:
                return
            self._processing = True
            try:
                while True:
                    try:
                        command = self._commands.get_nowait()
                    except Empty:
                        break
                    for event in self.core.handle(command):
                        self.event.emit(event)
            finally:
                self._processing = False

    class MainWindow(QtWidgets.QMainWindow):
        camera_command_requested = QtCore.Signal(object)

        def __init__(self):
            super().__init__()
            self.setWindowTitle("SW-2005/4005 5GE Line Scan Capture")
            self.resize(1200, 760)
            self.camera_open = False
            self._camera_busy_action: str | None = None
            self._command_id = 0
            self._pending_capture_settings: CaptureSettings | None = None
            self.camera_thread = QtCore.QThread(self)
            self.camera_worker = CameraServiceWorker()
            self.camera_worker.moveToThread(self.camera_thread)
            self.camera_command_requested.connect(self.camera_worker.submit)
            self.camera_worker.event.connect(self._camera_event_received)
            self.camera_thread.start()
            self._settings_apply_timer = QtCore.QTimer(self)
            self._settings_apply_timer.setSingleShot(True)
            self._settings_apply_timer.timeout.connect(self._apply_pending_settings)
            self._settings_settle_timer = QtCore.QTimer(self)
            self._settings_settle_timer.setSingleShot(True)
            self._settings_settle_timer.timeout.connect(self._settings_settled)

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
            if self._capture_running():
                self.status_label.setText("Capture 중에는 창을 닫을 수 없습니다.")
                event.ignore()
                return
            self._settings_apply_timer.stop()
            self._settings_settle_timer.stop()
            if self.camera_thread.isRunning():
                QtCore.QMetaObject.invokeMethod(
                    self.camera_worker,
                    "shutdown_now",
                    QtCore.Qt.BlockingQueuedConnection,
                )
                self.camera_thread.quit()
                self.camera_thread.wait(2000)
            super().closeEvent(event)

        def _update_exposure_label(self, value: int) -> None:
            self.exposure_label.setText(f"Exposure time: {value} µs")

        def _update_line_rate_label(self, value: int) -> None:
            self.line_rate_label.setText(f"Line rate: {value:,} Hz")

        def _exposure_changed(self, value: int) -> None:
            if self._timing_update_in_progress:
                return
            self._update_exposure_label(value)
            self._schedule_settings_apply()

        def _line_rate_changed(self, value: int) -> None:
            if self._timing_update_in_progress:
                return
            self._update_line_rate_label(value)
            self._schedule_settings_apply()

        def _trigger_mode_changed(self, *_args) -> None:
            self._update_control_enabled_state()
            self._schedule_settings_apply()

        def _capture_running(self) -> bool:
            return self._camera_busy_action == "capture"

        def _camera_busy(self) -> bool:
            return self._camera_busy_action is not None

        def _next_command_id(self) -> int:
            self._command_id += 1
            return self._command_id

        def _send_camera_command(self, action: str, settings: CaptureSettings | None = None) -> None:
            self._camera_busy_action = action
            self._update_control_enabled_state()
            self.camera_command_requested.emit(
                CameraCommand(action=action, settings=settings, command_id=self._next_command_id())
            )

        def _schedule_settings_apply(self, *_args) -> None:
            if not self.camera_open or self._camera_busy():
                return
            self._settings_settle_timer.stop()
            self.capture_button.setEnabled(False)
            self._settings_apply_timer.start(SETTINGS_APPLY_DEBOUNCE_MS)

        def _settings_settled(self) -> None:
            self._update_control_enabled_state()
            if self.camera_open and not self._camera_busy():
                self.status_label.setText("설정 안정화 완료. Capture 가능.")

        def _settings_ready_for_capture(self) -> bool:
            return not self._settings_apply_timer.isActive() and not self._settings_settle_timer.isActive()

        def _apply_pending_settings(self) -> None:
            if not self.camera_open or self._camera_busy():
                return
            settings = self._settings()
            self.status_label.setText(
                "설정 적용 중: "
                f"exposure={settings.exposure_us:g} µs, "
                f"line_rate={settings.line_rate_hz:g} Hz, "
                f"height={settings.frame_height}, "
                f"trigger={settings.trigger_mode}"
            )
            self._send_camera_command("config", settings)

        def _sync_timing_slider_maximums_from_camera_info(self, info: dict[str, Any] | None) -> None:
            info = info or {}
            exposure_max = max(BASE_EXPOSURE_US_MIN, int(info.get("exposure_time_max", BASE_EXPOSURE_US_MAX)))
            line_rate_max = max(
                BASE_LINE_RATE_HZ_MIN,
                int(info.get("acquisition_line_rate_max", BASE_LINE_RATE_HZ_MAX)),
            )

            self._timing_update_in_progress = True
            try:
                self.exposure_slider.setMaximum(exposure_max)
                self.line_rate_slider.setMaximum(line_rate_max)
            finally:
                self._timing_update_in_progress = False
            self._update_exposure_label(self.exposure_slider.value())
            self._update_line_rate_label(self.line_rate_slider.value())

        def _control_widgets(self):
            return (
                self.exposure_slider,
                self.line_rate_slider,
                self.trigger_mode_combo,
                self.frame_height_combo,
                self.duration_spin,
            )

        def _update_control_enabled_state(self) -> None:
            controls_enabled = controls_enabled_after_open(self.camera_open)
            busy = self._camera_busy()
            for widget in self._control_widgets():
                widget.setEnabled(controls_enabled and not busy)
            self.capture_button.setEnabled(
                controls_enabled and not busy and self._settings_ready_for_capture()
            )
            self.open_close_button.setEnabled(not busy)
            self.trigger_source_combo.setEnabled(
                trigger_source_enabled(
                    camera_open=self.camera_open and not busy,
                    trigger_mode=self.trigger_mode_combo.currentText(),
                )
            )

        def toggle_camera(self) -> None:
            if self.camera_open:
                self._close_camera()
                return
            self.status_label.setText("카메라 연결 중...")
            self.summary_text.clear()
            self.open_close_button.setEnabled(False)
            self._send_camera_command("open", self._settings())

        def _close_camera(self) -> None:
            self._settings_apply_timer.stop()
            self._settings_settle_timer.stop()
            if self._capture_running():
                self.status_label.setText("Capture 중에는 close할 수 없습니다.")
                return
            if not self.camera_open:
                return
            self.status_label.setText("카메라 닫는 중...")
            self._send_camera_command("close")

        def _settings(self) -> CaptureSettings:
            return CaptureSettings(
                exposure_us=float(self.exposure_slider.value()),
                line_rate_hz=float(self.line_rate_slider.value()),
                trigger_mode=self.trigger_mode_combo.currentText(),
                trigger_source=self.trigger_source_combo.currentText(),
                frame_height=int(self.frame_height_combo.currentText()),
                duration_s=float(self.duration_spin.value()),
            )

        def start_capture(self) -> None:
            if not self.camera_open:
                self.status_label.setText("먼저 카메라를 open 해주세요.")
                return
            if self._camera_busy():
                self.status_label.setText("카메라 작업이 이미 진행 중입니다.")
                return
            if not self._settings_ready_for_capture():
                self.status_label.setText("설정 적용/안정화 중입니다. 잠시 후 capture 해주세요.")
                return

            self._settings_apply_timer.stop()
            settings = self._settings()
            self._pending_capture_settings = settings
            self.capture_button.setEnabled(False)
            self.open_close_button.setEnabled(False)
            self.summary_text.clear()
            self.status_label.setText(f"{settings.duration_s:.3f}초 capture 요청 중...")
            self._send_camera_command("capture", settings)

        @QtCore.Slot(object)
        def _camera_event_received(self, event: CameraEvent) -> None:
            if event.kind == "opened":
                self.camera_open = True
                self._camera_busy_action = None
                self.open_close_button.setText("Close Camera")
                self._sync_timing_slider_maximums_from_camera_info(event.payload)
                self.status_label.setText(
                    f"연결됨: {event.payload.get('device_ip_addr', DEFAULT_DEVICE_IP_ADDR)}. "
                    "설정 안정화 대기 중..."
                )
                self.capture_button.setEnabled(False)
                self._settings_settle_timer.start(SETTINGS_SETTLE_MS)
                self._update_control_enabled_state()
                return

            if event.kind == "config_applied":
                self._camera_busy_action = None
                self._sync_timing_slider_maximums_from_camera_info(event.payload)
                settings = self._settings()
                self.status_label.setText(
                    "설정 적용됨. 1초 안정화 대기 중: "
                    f"exposure={settings.exposure_us:g} µs, "
                    f"line_rate={settings.line_rate_hz:g} Hz, "
                    f"height={settings.frame_height}, "
                    f"trigger={settings.trigger_mode}"
                )
                self.capture_button.setEnabled(False)
                self._settings_settle_timer.start(SETTINGS_SETTLE_MS)
                self._update_control_enabled_state()
                return

            if event.kind == "capture_started":
                settings = event.payload
                self.status_label.setText(f"{settings.duration_s:.3f}초 capture 중...")
                return

            if event.kind == "capture_finished":
                self._camera_busy_action = None
                settings = self._pending_capture_settings
                self._pending_capture_settings = None
                self._capture_finished(event.payload, None, settings)
                return

            if event.kind == "closed":
                self.camera_open = False
                self._camera_busy_action = None
                self._pending_capture_settings = None
                self.open_close_button.setText("Open Camera")
                self.status_label.setText("카메라가 닫혀 있습니다.")
                self._update_control_enabled_state()
                return

            if event.kind == "shutdown":
                self.camera_open = False
                self._camera_busy_action = None
                return

            if event.kind == "error":
                failed_action = self._camera_busy_action
                self._camera_busy_action = None
                if failed_action == "open":
                    self.camera_open = False
                    self.open_close_button.setText("Open Camera")
                    self.status_label.setText("연결 실패")
                elif failed_action == "config":
                    self.status_label.setText("설정 적용 실패")
                elif failed_action == "capture":
                    self.status_label.setText("Capture 실패")
                    self._pending_capture_settings = None
                elif failed_action == "close":
                    self.status_label.setText("카메라 close 실패")
                else:
                    self.status_label.setText("카메라 작업 실패")
                self.summary_text.setPlainText(str(event.error_text or "unknown error"))
                self._update_control_enabled_state()
                return

        def _capture_finished(
            self,
            payload: GuiCapturePayload | None,
            error_text: str | None,
            capture_settings: CaptureSettings | None,
        ) -> None:
            self.open_close_button.setEnabled(True)
            self._update_control_enabled_state()

            if error_text:
                self.status_label.setText("Capture 실패")
                self.summary_text.setPlainText(str(error_text))
                return
            if payload is None:
                self.status_label.setText("Capture 결과가 없습니다.")
                return

            result = payload.result
            image_array = payload.image_array
            self.image_view.set_array(image_array, capture_settings)
            self.status_label.setText(
                f"Capture 완료: array shape={tuple(image_array.shape)}, "
                f"frames={result.stats.frames}, preview={payload.preview_bytes / 1024 / 1024:.1f} MiB"
            )
            self.summary_text.setPlainText(
                result.debug_summary()
                + "\n"
                + f"numpy_array_shape: {tuple(image_array.shape)}\n"
                + f"numpy_array_dtype: {image_array.dtype}\n"
                + f"preview_bytes: {payload.preview_bytes}"
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
