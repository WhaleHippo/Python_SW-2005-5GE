"""PySide6 GUI for the SW-2005/4005 5GigE line-scan camera wrapper.

Run with a Python environment that has PySide6==6.4.3 and numpy<2 installed:

    uv venv .venv
    . .venv/bin/activate
    uv pip install -r requirements-gui.txt
    python linescan_gui.py

The GUI intentionally uses the public, explicit properties exposed by
``linescan_module.LineScanCamera`` and converts captured grayscale bytes into a
2D numpy array for display. Camera open/configure/capture runs in a dedicated
child process; the Qt UI communicates with that process through a Pipe and polls
responses with a QTimer so vendor SDK objects never live in the GUI process.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
import traceback
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path

import numpy as np

from linescan_module import DEVICE_IP_ADDR, CaptureResult, LineScanCamera, LineScanFrame


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


@dataclass(frozen=True)
class GuiCapturePayload:
    result: CaptureResult
    image_array: np.ndarray
    preview_bytes: int


def _settings_to_ipc(settings: CaptureSettings) -> dict[str, object]:
    """Convert settings to plain primitives so spawn-mode processes can unpickle them."""
    return {
        "exposure_us": settings.exposure_us,
        "line_rate_hz": settings.line_rate_hz,
        "trigger_mode": settings.trigger_mode,
        "trigger_source": settings.trigger_source,
        "frame_height": settings.frame_height,
        "duration_s": settings.duration_s,
    }


def _settings_from_ipc(value: object) -> CaptureSettings:
    if isinstance(value, CaptureSettings):
        return value
    if not isinstance(value, dict):
        raise TypeError(f"invalid capture settings payload: {type(value).__name__}")
    return CaptureSettings(
        exposure_us=float(value["exposure_us"]),
        line_rate_hz=float(value["line_rate_hz"]),
        trigger_mode=str(value["trigger_mode"]),
        trigger_source=str(value["trigger_source"]),
        frame_height=int(value["frame_height"]),
        duration_s=float(value["duration_s"]),
    )


def _apply_settings_to_camera(camera: LineScanCamera, settings: CaptureSettings) -> None:
    """Apply GUI settings inside the camera-owner process."""
    camera.exposure_time = settings.exposure_us
    camera.acquisition_line_rate = settings.line_rate_hz
    camera.height = settings.frame_height
    camera.trigger_mode = settings.trigger_mode == "On"
    if settings.trigger_mode == "On":
        camera.trigger_selector = "LineStart"
        camera.trigger_source = settings.trigger_source


def _camera_limit_snapshot(camera: LineScanCamera) -> dict[str, float]:
    """Read timing slider limits from the process-owned camera."""
    return {
        "exposure_time_max": float(camera.exposure_time_max),
        "acquisition_line_rate_max": float(camera.acquisition_line_rate_max),
    }


class DisplayImageAccumulator:

    """Accumulate a GUI preview array while capture buffers are still valid."""

    def __init__(self):
        self.width: int | None = None
        self.rows = 0
        self._data = bytearray()

    @property
    def bytes_used(self) -> int:
        return len(self._data)

    def add_frame(self, frame: LineScanFrame) -> None:
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


def camera_process_main(conn: Connection) -> None:
    """Own LineScanCamera in a child process and handle dict-based IPC commands."""
    camera: LineScanCamera | None = None
    try:
        while True:
            try:
                command = conn.recv()
            except EOFError:
                break

            request_id = command.get("id")
            action = command.get("action")

            try:
                if action == "open":
                    if camera is None:
                        camera = LineScanCamera(
                            device_ip_addr=command.get("device_ip_addr", DEVICE_IP_ADDR),
                            copy_frames=True,
                            debug=False,
                        )
                    camera.open()
                    settings_payload = command.get("settings")
                    if settings_payload is not None:
                        _apply_settings_to_camera(camera, _settings_from_ipc(settings_payload))
                    conn.send(
                        {
                            "id": request_id,
                            "event": "opened",
                            "limits": _camera_limit_snapshot(camera),
                            "message": f"connected: {camera.device_ip_addr}",
                        }
                    )
                elif action == "apply_settings":
                    if camera is None or not camera.is_open:
                        raise RuntimeError("camera is not open")
                    _apply_settings_to_camera(camera, _settings_from_ipc(command["settings"]))
                    conn.send(
                        {
                            "id": request_id,
                            "event": "settings_applied",
                            "limits": _camera_limit_snapshot(camera),
                        }
                    )
                elif action == "capture":
                    if camera is None or not camera.is_open:
                        raise RuntimeError("camera is not open")
                    settings = _settings_from_ipc(command["settings"])
                    conn.send(
                        {
                            "id": request_id,
                            "event": "progress",
                            "message": f"{settings.duration_s:.3f}초 capture 중...",
                        }
                    )
                    accumulator = DisplayImageAccumulator()

                    def on_frame(frame: LineScanFrame) -> None:
                        # Convert while the eBUS pipeline buffer is still checked out.
                        # This avoids later GUI access to a released native pointer.
                        accumulator.add_frame(frame)

                    result = camera.capture(
                        duration_s=settings.duration_s,
                        store_frames=False,
                        copy_frames=True,
                        on_frame=on_frame,
                        debug=False,
                    )
                    conn.send(
                        {
                            "id": request_id,
                            "event": "capture_finished",
                            "payload": {
                                "result": result,
                                "image_array": accumulator.to_array(),
                                "preview_bytes": accumulator.bytes_used,
                            },
                        }
                    )
                elif action == "close":
                    if camera is not None:
                        camera.close()
                    camera = None
                    conn.send({"id": request_id, "event": "closed"})
                elif action == "shutdown":
                    if camera is not None:
                        camera.close()
                    conn.send({"id": request_id, "event": "shutdown"})
                    break
                else:
                    raise ValueError(f"unknown camera process action: {action!r}")
            except Exception:
                conn.send({"id": request_id, "event": "error", "action": action, "error": traceback.format_exc()})
    finally:
        if camera is not None:
            try:
                camera.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


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

    class CameraProcessClient(QtCore.QObject):
        event_received = QtCore.Signal(object)
        process_failed = QtCore.Signal(str)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._ctx = mp.get_context("spawn")
            self._conn: Connection | None = None
            self._process: mp.Process | None = None
            self._next_request_id = 1
            self._poll_timer = QtCore.QTimer(self)
            self._poll_timer.setInterval(30)
            self._poll_timer.timeout.connect(self._poll)

        def is_running(self) -> bool:
            return self._process is not None and self._process.is_alive()

        def send(self, action: str, **payload) -> int:
            self._ensure_process()
            request_id = self._next_request_id
            self._next_request_id += 1
            message = {"id": request_id, "action": action}
            for key, value in payload.items():
                message[key] = _settings_to_ipc(value) if isinstance(value, CaptureSettings) else value
            try:
                assert self._conn is not None
                self._conn.send(message)
            except Exception:
                self.process_failed.emit(traceback.format_exc())
            return request_id

        def shutdown(self, timeout_ms: int = 1500) -> None:
            if self._conn is not None:
                try:
                    self._conn.send({"id": self._next_request_id, "action": "shutdown"})
                    self._next_request_id += 1
                except Exception:
                    pass
            if self._process is not None:
                self._process.join(timeout_ms / 1000.0)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(1.0)
            self._poll_timer.stop()
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None
            self._process = None

        def _ensure_process(self) -> None:
            if self.is_running():
                return
            parent_conn, child_conn = self._ctx.Pipe(duplex=True)
            process = self._ctx.Process(target=camera_process_main, args=(child_conn,), daemon=True)
            process.start()
            child_conn.close()
            self._conn = parent_conn
            self._process = process
            self._poll_timer.start()

        def _poll(self) -> None:
            if self._conn is None:
                return
            try:
                while self._conn.poll():
                    self.event_received.emit(self._conn.recv())
            except EOFError:
                self.process_failed.emit("카메라 프로세스와의 연결이 종료되었습니다.")
                self.shutdown(timeout_ms=0)
            except Exception:
                self.process_failed.emit(traceback.format_exc())
                self.shutdown(timeout_ms=0)

            if self._process is not None and not self._process.is_alive() and self._conn is not None:
                exitcode = self._process.exitcode
                self.process_failed.emit(f"카메라 프로세스가 종료되었습니다. exitcode={exitcode}")
                self.shutdown(timeout_ms=0)

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("SW-2005/4005 5GE Line Scan Capture")
            self.resize(1200, 760)
            self.camera_client = CameraProcessClient(self)
            self.camera_client.event_received.connect(self._handle_camera_event)
            self.camera_client.process_failed.connect(self._handle_camera_process_failed)
            self.camera_open = False
            self.camera_busy_action: str | None = None
            self._capture_settings_in_progress: CaptureSettings | None = None
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
            self._settings_apply_timer.stop()
            self._settings_settle_timer.stop()
            self.camera_client.shutdown()
            self.camera_open = False
            self.camera_busy_action = None
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
            return self.camera_busy_action == "capture"

        def _schedule_settings_apply(self, *_args) -> None:
            if not self.camera_open or self.camera_busy_action is not None:
                return
            self._settings_settle_timer.stop()
            self.capture_button.setEnabled(False)
            self._settings_apply_timer.start(SETTINGS_APPLY_DEBOUNCE_MS)

        def _settings_settled(self) -> None:
            self._update_control_enabled_state()
            if self.camera_open and self.camera_busy_action is None:
                self.status_label.setText("설정 안정화 완료. Capture 가능.")

        def _settings_ready_for_capture(self) -> bool:
            return not self._settings_apply_timer.isActive() and not self._settings_settle_timer.isActive()

        def _apply_pending_settings(self) -> None:
            if not self.camera_open or self.camera_busy_action is not None:
                return
            settings = self._settings()
            self.camera_busy_action = "apply_settings"
            self.capture_button.setEnabled(False)
            self._update_control_enabled_state()
            self.status_label.setText(
                "카메라 프로세스에 설정 적용 중: "
                f"exposure={settings.exposure_us:g} µs, "
                f"line_rate={settings.line_rate_hz:g} Hz, "
                f"height={settings.frame_height}, "
                f"trigger={settings.trigger_mode}"
            )
            self.camera_client.send("apply_settings", settings=settings)

        def _sync_timing_slider_maximums_from_limits(self, limits: object) -> None:
            if not isinstance(limits, dict):
                return
            exposure_max = max(BASE_EXPOSURE_US_MIN, int(limits.get("exposure_time_max", BASE_EXPOSURE_US_MAX)))
            line_rate_max = max(BASE_LINE_RATE_HZ_MIN, int(limits.get("acquisition_line_rate_max", BASE_LINE_RATE_HZ_MAX)))

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
            busy = self.camera_busy_action is not None
            for widget in self._control_widgets():
                widget.setEnabled(controls_enabled and not busy)
            self.capture_button.setEnabled(
                controls_enabled and not busy and self._settings_ready_for_capture()
            )
            self.open_close_button.setEnabled(not self._capture_running())
            self.trigger_source_combo.setEnabled(
                trigger_source_enabled(
                    camera_open=self.camera_open and not busy,
                    trigger_mode=self.trigger_mode_combo.currentText(),
                )
            )

        def toggle_camera(self) -> None:
            if self.camera_busy_action is not None:
                self.status_label.setText("카메라 프로세스 작업이 끝날 때까지 기다려 주세요.")
                return
            if self.camera_open:
                self._close_camera()
                return

            self.camera_busy_action = "open"
            self.status_label.setText("별도 카메라 프로세스 시작 및 카메라 연결 중...")
            self.summary_text.clear()
            self._update_control_enabled_state()
            self.camera_client.send("open", device_ip_addr=DEVICE_IP_ADDR, settings=self._settings())

        def _close_camera(self) -> None:
            self._settings_apply_timer.stop()
            self._settings_settle_timer.stop()
            if self._capture_running():
                self.status_label.setText("Capture 중에는 close할 수 없습니다.")
                return
            if self.camera_client.is_running():
                self.camera_busy_action = "close"
                self.status_label.setText("카메라 프로세스에서 카메라 닫는 중...")
                self._update_control_enabled_state()
                self.camera_client.send("close")
                return

            self.camera_open = False
            self.camera_busy_action = None
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

        def start_capture(self) -> None:
            if not self.camera_open:
                self.status_label.setText("먼저 카메라를 open 해주세요.")
                return
            if self.camera_busy_action is not None:
                self.status_label.setText("이미 카메라 프로세스 작업이 진행 중입니다.")
                return
            if not self._settings_ready_for_capture():
                self.status_label.setText("설정 적용/안정화 중입니다. 잠시 후 capture 해주세요.")
                return

            self._settings_apply_timer.stop()
            self._capture_settings_in_progress = self._settings()
            self.camera_busy_action = "capture"
            self.capture_button.setEnabled(False)
            self.open_close_button.setEnabled(False)
            self._update_control_enabled_state()
            self.summary_text.clear()
            self.camera_client.send("capture", settings=self._capture_settings_in_progress)

        @QtCore.Slot(object)
        def _handle_camera_event(self, event: dict[str, object]) -> None:
            event_name = event.get("event")

            if event_name == "progress":
                self.status_label.setText(str(event.get("message", "카메라 프로세스 작업 중...")))
                return

            if event_name == "opened":
                self.camera_open = True
                self.camera_busy_action = None
                self.open_close_button.setText("Close Camera")
                self._sync_timing_slider_maximums_from_limits(event.get("limits"))
                self.status_label.setText(f"연결됨: {DEVICE_IP_ADDR}. 설정 안정화 대기 중...")
                self.capture_button.setEnabled(False)
                self._settings_settle_timer.start(SETTINGS_SETTLE_MS)
                self._update_control_enabled_state()
                return

            if event_name == "settings_applied":
                self.camera_busy_action = None
                self._sync_timing_slider_maximums_from_limits(event.get("limits"))
                settings = self._settings()
                self.status_label.setText(
                    "설정 적용됨. 1초 안정화 대기 중: "
                    f"exposure={settings.exposure_us:g} µs, "
                    f"line_rate={settings.line_rate_hz:g} Hz, "
                    f"height={settings.frame_height}, "
                    f"trigger={settings.trigger_mode}"
                )
                self._settings_settle_timer.start(SETTINGS_SETTLE_MS)
                self._update_control_enabled_state()
                return

            if event_name == "capture_finished":
                payload = event.get("payload")
                self.camera_busy_action = None
                if isinstance(payload, dict):
                    payload = GuiCapturePayload(
                        result=payload["result"],
                        image_array=payload["image_array"],
                        preview_bytes=int(payload["preview_bytes"]),
                    )
                self._capture_finished(payload if isinstance(payload, GuiCapturePayload) else None, None)
                return

            if event_name == "closed":
                self.camera_open = False
                self.camera_busy_action = None
                self.camera_client.shutdown(timeout_ms=200)
                self.open_close_button.setText("Open Camera")
                self._update_control_enabled_state()
                self.status_label.setText("카메라가 닫혀 있습니다.")
                return

            if event_name == "error":
                action = event.get("action")
                error_text = str(event.get("error", "unknown camera process error"))
                if action == "open":
                    self.camera_open = False
                    self.camera_client.shutdown(timeout_ms=200)
                    self.open_close_button.setText("Open Camera")
                self.camera_busy_action = None
                self._update_control_enabled_state()
                self.status_label.setText(f"카메라 프로세스 작업 실패: {action}")
                self.summary_text.setPlainText(error_text)
                return

            self.summary_text.setPlainText(f"알 수 없는 카메라 프로세스 이벤트: {event!r}")

        @QtCore.Slot(str)
        def _handle_camera_process_failed(self, error_text: str) -> None:
            self.camera_open = False
            self.camera_busy_action = None
            self.open_close_button.setText("Open Camera")
            self._settings_apply_timer.stop()
            self._settings_settle_timer.stop()
            self._update_control_enabled_state()
            self.status_label.setText("카메라 프로세스 실패")
            self.summary_text.setPlainText(error_text)

        def _capture_finished(self, payload: GuiCapturePayload | None, error_text: str | None) -> None:
            self.open_close_button.setEnabled(True)
            capture_settings = self._capture_settings_in_progress
            self._capture_settings_in_progress = None
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
