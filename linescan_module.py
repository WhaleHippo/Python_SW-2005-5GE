"""SW-2005/4005 5GigE 라인스캔 카메라용 단순 capture wrapper.

이 모듈은 JAI/Pleora eBUS Python binding 위에 얇은 안전 API를 제공합니다.
공개 API는 `LineScanCamera`의 명시적 property와 `capture(duration_s=...)`에
집중하며, 임의 GenICam feature를 외부에서 수정하는 generic setter는 노출하지
않습니다.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

DEVICE_IP_ADDR = "192.168.1.200"
"""기본 카메라 IP. `force_ip=True`일 때 카메라에 강제 할당할 목표 IP입니다."""

PIPELINE_BUFFER_COUNT = 64
"""PvPipeline에 queue할 기본 buffer 개수입니다. 고속 취득을 위해 넉넉하게 둡니다."""

DEFAULT_SUBNET_MASK = "255.255.255.0"
"""Force-IP 시 사용할 기본 subnet mask입니다."""

DEFAULT_GATEWAY = "0.0.0.0"
"""Force-IP 시 사용할 기본 gateway입니다."""


class LineScanError(RuntimeError):
    """라인스캔 wrapper 작업 실패를 나타내는 기본 예외입니다."""


class EBusResultError(LineScanError):
    """eBUS `PvResult`가 실패를 반환했을 때 발생하는 예외입니다."""


@dataclass
class LineScanFrame:
    """capture loop에서 얻은 단일 frame/block 정보.

    Attributes:
        image: `copy_frames=True`이면 bytes/bytearray처럼 buffer release 후에도 안전한
            객체, `False`이면 eBUS image data pointer 원본일 수 있습니다.
        block_id: GigE Vision block ID. 장치/버퍼가 제공하지 않으면 None입니다.
        width: image width(pixel). 제공되지 않으면 None입니다.
        height: line-scan frame에 묶인 line 수. 제공되지 않으면 None입니다.
        acquired_size: eBUS buffer에서 실제 취득한 byte 수입니다.
        timestamp_ns: Python monotonic clock 기준 수신 시각(ns)입니다.
        ok: retrieve와 operational result가 모두 성공했는지 여부입니다.
        result_code: retrieve result code 문자열입니다.
        operational_code: payload operational result code 문자열입니다.
    """

    image: Any
    block_id: int | None
    width: int | None
    height: int | None
    acquired_size: int
    timestamp_ns: int
    ok: bool
    result_code: str | None = None
    operational_code: str | None = None


@dataclass
class LineScanStats:
    """capture 결과 debug/stat counter.

    packet/error counter 이름은 `high_throughput_pipeline.py`의 출력과 맞춰 두었습니다.
    `elapsed_s`가 설정되면 평균 bandwidth와 error rate 계산에 사용됩니다.
    """

    frames: int = 0
    bytes_acquired: int = 0
    lost_packets: int = 0
    recovered_packets: int = 0
    recovered_single_resend_packets: int = 0
    resend_groups_requested: int = 0
    resend_packets_requested: int = 0
    missing_packet_ids: int = 0
    operational_errors: int = 0
    retrieve_errors: int = 0
    block_gaps: int = 0
    last_block_id: int | None = None
    elapsed_s: float = 0.0
    ebus_fps: float | None = None
    ebus_bandwidth_mbps: float | None = None

    @property
    def avg_bandwidth_mbps(self) -> float:
        """전체 capture 구간의 local acquired-byte 평균 bandwidth(Mb/s)."""
        if self.elapsed_s <= 0:
            return 0.0
        return (self.bytes_acquired * 8.0) / self.elapsed_s / 1_000_000.0

    @property
    def error_count(self) -> int:
        """packet/retrieve/operational/block gap을 모두 더한 누적 오류 개수."""
        return (
            self.lost_packets
            + self.missing_packet_ids
            + self.operational_errors
            + self.retrieve_errors
            + self.block_gaps
        )

    @property
    def error_rate(self) -> float:
        """frame 수 대비 오류 비율. frame이 없으면 0.0을 반환합니다."""
        if self.frames <= 0:
            return 0.0
        return self.error_count / self.frames

    def update_from_buffer(self, pvbuffer: Any) -> None:
        """eBUS pvbuffer에서 frame/packet counter를 누적합니다."""
        self.frames += 1
        self.bytes_acquired += _safe_int_call(pvbuffer, "GetAcquiredSize")
        self.lost_packets += _safe_int_call(pvbuffer, "GetLostPacketCount")
        self.recovered_packets += _safe_int_call(pvbuffer, "GetPacketsRecoveredCount")
        self.recovered_single_resend_packets += _safe_int_call(
            pvbuffer, "GetPacketsRecoveredSingleResendCount"
        )
        self.resend_groups_requested += _safe_int_call(pvbuffer, "GetResendGroupRequestedCount")
        self.resend_packets_requested += _safe_int_call(pvbuffer, "GetResendPacketRequestedCount")
        self.missing_packet_ids += _safe_int_call(pvbuffer, "GetMissingPacketIdsCount")

        block_id = _get_block_id(pvbuffer)
        if block_id is not None:
            if self.last_block_id is not None and block_id > self.last_block_id + 1:
                self.block_gaps += block_id - self.last_block_id - 1
            self.last_block_id = block_id


@dataclass
class CaptureResult:
    """`LineScanCamera.capture()`의 반환값.

    `store_frames=False`이면 `frames`는 빈 list이며, 사용자는 `on_frame` callback에서
    직접 저장/처리해야 합니다.
    """

    frames: list[LineScanFrame]
    stats: LineScanStats
    settings: dict[str, Any] = field(default_factory=dict)

    def debug_summary(self) -> str:
        """사람이 읽기 쉬운 capture summary 문자열을 반환합니다."""
        lines = [
            f"elapsed_s: {self.stats.elapsed_s:.3f}",
            f"frames: {self.stats.frames}",
            f"avg_local_bandwidth_mbps: {self.stats.avg_bandwidth_mbps:.1f}",
            f"ebus_fps: {_format_optional_float(self.stats.ebus_fps)}",
            f"ebus_bandwidth_mbps: {_format_optional_float(self.stats.ebus_bandwidth_mbps)}",
            f"bytes_acquired: {self.stats.bytes_acquired}",
            f"lost_packets: {self.stats.lost_packets}",
            f"recovered_packets: {self.stats.recovered_packets}",
            f"recovered_single_resend_packets: {self.stats.recovered_single_resend_packets}",
            f"resend_groups_requested: {self.stats.resend_groups_requested}",
            f"resend_packets_requested: {self.stats.resend_packets_requested}",
            f"missing_packet_ids: {self.stats.missing_packet_ids}",
            f"block_gaps: {self.stats.block_gaps}",
            f"operational_errors: {self.stats.operational_errors}",
            f"retrieve_errors: {self.stats.retrieve_errors}",
        ]
        return "\n".join(lines)


class LineScanCamera:
    """SW-2005/4005 5GigE 라인스캔 카메라의 단순 time-based capture wrapper.

    생성자는 설정값만 보관하고, 실제 eBUS import/장치 연결은 `open()` 또는 context
    manager 진입 시 수행합니다. `force_ip=True`이면 discovery로 찾은 첫 GEV 장치의
    MAC 주소에 `device_ip_addr`를 강제 할당한 뒤 해당 IP로 연결을 시도합니다. 이 작업은
    장치 네트워크 설정에 영향을 주는 hardware side effect입니다.
    """

    def __init__(
        self,
        device_ip_addr: str = DEVICE_IP_ADDR,
        pipeline_buffer_count: int = PIPELINE_BUFFER_COUNT,
        timeout_ms: int = 1000,
        copy_frames: bool = True,
        force_ip: bool = True,
        debug: bool = False,
        debug_interval_s: float = 1.0,
        *,
        eb_module: Any | None = None,
    ):
        if pipeline_buffer_count <= 0:
            raise ValueError("pipeline_buffer_count must be positive")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        if debug_interval_s <= 0:
            raise ValueError("debug_interval_s must be positive")

        self.device_ip_addr = device_ip_addr
        self.pipeline_buffer_count = pipeline_buffer_count
        self.timeout_ms = timeout_ms
        self.copy_frames = copy_frames
        self.force_ip = force_ip
        self.debug = debug
        self.debug_interval_s = debug_interval_s

        self._eb: Any = eb_module
        self._device: Any = None
        self._stream: Any = None
        self._pipeline: Any = None
        self._is_open = False
        self._is_acquiring = False
        self._connection_id: Any | None = None
        self._force_ip_attempted = False
        self._force_ip_succeeded = False
        self._force_ip_message: str | None = None

    def open(self) -> "LineScanCamera":
        """eBUS를 lazy import하고 device/stream을 열어 capture 준비를 완료합니다.

        `force_ip=True`이면 먼저 force-IP를 시도합니다. 이후 `PvDevice.CreateAndConnect()`와
        `PvStream.CreateAndOpen()`을 사용해 지정 IP에 연결하고, GigE Vision stream
        destination까지 설정합니다.
        """
        if self._is_open:
            return self

        self._eb = self._eb or importlib.import_module("eBUS")
        try:
            if self.force_ip:
                self.force_device_ip(self.device_ip_addr)

            self._connection_id = self.device_ip_addr
            result, device = self._eb.PvDevice.CreateAndConnect(self._connection_id)
            if device is None or not _result_ok(result):
                raise EBusResultError(f"PvDevice.CreateAndConnect failed: {_describe_result(result)}")
            self._device = device

            result, stream = self._eb.PvStream.CreateAndOpen(self._connection_id)
            if stream is None or not _result_ok(result):
                raise EBusResultError(f"PvStream.CreateAndOpen failed: {_describe_result(result)}")
            self._stream = stream

            self._configure_gige_stream()
            self._is_open = True
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """acquisition/stream/pipeline/device를 안전하게 정리합니다.

        여러 번 호출해도 안전합니다. capture 중이면 AcquisitionStop, StreamDisable,
        pipeline.Stop 순서로 정리하고, stream/device handle도 free합니다.
        """
        device = self._device
        stream = self._stream
        pipeline = self._pipeline

        if device is not None:
            try:
                self._execute_feature("AcquisitionStop", required=False)
            except Exception:
                pass
            try:
                device.StreamDisable()
            except Exception:
                pass
            self._is_acquiring = False

        if pipeline is not None:
            try:
                pipeline.Stop()
            except Exception:
                pass
            self._pipeline = None

        if stream is not None:
            try:
                stream.Close()
            except Exception:
                pass
            try:
                self._eb.PvStream.Free(stream)
            except Exception:
                pass
            self._stream = None

        if device is not None:
            try:
                device.Disconnect()
            except Exception:
                pass
            try:
                self._eb.PvDevice.Free(device)
            except Exception:
                pass
            self._device = None

        self._is_open = False

    def __enter__(self) -> "LineScanCamera":
        """context manager 진입 시 자동으로 `open()`합니다."""
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """context manager 종료 시 예외 여부와 관계없이 `close()`합니다."""
        self.close()

    def capture(
        self,
        duration_s: float,
        *,
        trigger_mode: bool | None = None,
        store_frames: bool = True,
        copy_frames: bool | None = None,
        on_frame: Callable[[LineScanFrame], None] | None = None,
        debug: bool | None = None,
    ) -> CaptureResult:
        """일정 시간 동안 촬상하고 frame list와 debug stats를 반환합니다.

        Args:
            duration_s: 취득 시간(초). 0보다 커야 합니다.
            trigger_mode: None이 아니면 capture 시작 전 `TriggerMode`에 적용합니다.
                False는 내부 line-rate free-run, True는 외부/소프트웨어 trigger 수신에
                사용합니다.
            store_frames: True이면 `CaptureResult.frames`에 frame을 저장합니다.
                장시간 고속 취득에서는 False와 `on_frame` callback 사용을 권장합니다.
            copy_frames: None이면 생성자 기본값을 사용합니다. True이면 eBUS buffer를
                release한 뒤에도 안전하게 image payload를 복사합니다.
            on_frame: 각 정상 frame 생성 직후 호출되는 callback입니다.
            debug: None이면 생성자 기본값을 사용합니다. True이면 interval마다 bandwidth,
                FPS, packet/error 정보를 stdout에 출력합니다.
        """
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        self.open()

        if trigger_mode is not None:
            self.trigger_mode = trigger_mode

        effective_copy = self.copy_frames if copy_frames is None else copy_frames
        effective_debug = self.debug if debug is None else debug
        frames: list[LineScanFrame] = []
        stats = LineScanStats()
        stream_params = self._stream.GetParameters() if self._stream is not None else None
        frame_rate_param = _get_param(stream_params, "AcquisitionRate") if stream_params is not None else None
        bandwidth_param = _get_param(stream_params, "Bandwidth") if stream_params is not None else None

        pipeline = self._create_pipeline()
        start_param = self._get_feature("AcquisitionStart")
        stop_param = self._get_feature("AcquisitionStop")

        start_time = time.monotonic()
        end_time = start_time + duration_s
        last_debug_time = start_time
        last_debug_frames = 0
        last_debug_bytes = 0

        pipeline_started = False
        stream_enabled = False
        try:
            pipeline.Start()
            pipeline_started = True
            result = self._device.StreamEnable()
            if not _result_ok(result):
                raise EBusResultError(f"StreamEnable failed: {_describe_result(result)}")
            stream_enabled = True
            result = start_param.Execute()
            if not _result_ok(result):
                raise EBusResultError(f"AcquisitionStart failed: {_describe_result(result)}")
            self._is_acquiring = True

            while time.monotonic() < end_time:
                result, pvbuffer, operational_result = pipeline.RetrieveNextBuffer(self.timeout_ms)
                if not _result_ok(result):
                    stats.retrieve_errors += 1
                    self._maybe_print_debug(
                        effective_debug,
                        stats,
                        frame_rate_param,
                        bandwidth_param,
                        start_time,
                        last_debug_time,
                        last_debug_frames,
                        last_debug_bytes,
                    )
                    now = time.monotonic()
                    if effective_debug and now - last_debug_time >= self.debug_interval_s:
                        last_debug_time = now
                        last_debug_frames = stats.frames
                        last_debug_bytes = stats.bytes_acquired
                    continue

                try:
                    ok = _result_ok(operational_result)
                    if ok:
                        stats.update_from_buffer(pvbuffer)
                        frame = self._frame_from_buffer(
                            pvbuffer,
                            result,
                            operational_result,
                            copy_image=effective_copy,
                        )
                        if store_frames:
                            frames.append(frame)
                        if on_frame is not None:
                            on_frame(frame)
                    else:
                        stats.operational_errors += 1
                finally:
                    pipeline.ReleaseBuffer(pvbuffer)

                now = time.monotonic()
                if effective_debug and now - last_debug_time >= self.debug_interval_s:
                    self._print_debug_interval(
                        stats,
                        frame_rate_param,
                        bandwidth_param,
                        now - last_debug_time,
                        stats.frames - last_debug_frames,
                        stats.bytes_acquired - last_debug_bytes,
                    )
                    last_debug_time = now
                    last_debug_frames = stats.frames
                    last_debug_bytes = stats.bytes_acquired
        finally:
            stats.elapsed_s = max(time.monotonic() - start_time, 0.0)
            stats.ebus_fps = _read_numeric_param(frame_rate_param)
            ebus_bw = _read_numeric_param(bandwidth_param)
            stats.ebus_bandwidth_mbps = ebus_bw / 1_000_000.0 if ebus_bw is not None else None

            try:
                stop_param.Execute()
            except Exception:
                pass
            self._is_acquiring = False
            if stream_enabled:
                try:
                    self._device.StreamDisable()
                except Exception:
                    pass
            if pipeline_started:
                try:
                    pipeline.Stop()
                except Exception:
                    pass
            self._pipeline = None

        return CaptureResult(frames=frames, stats=stats, settings=self.settings_snapshot())

    def force_device_ip(
        self,
        ip_addr: str | None = None,
        *,
        subnet_mask: str = DEFAULT_SUBNET_MASK,
        gateway: str = DEFAULT_GATEWAY,
    ) -> bool:
        """첫 번째 발견된 GigE Vision 카메라에 IP를 강제 할당합니다.

        eBUS `PvDeviceGEV.SetIPConfiguration(mac, ip, subnet, gateway)`를 사용합니다.
        장치 네트워크 설정을 바꾸는 side effect가 있으므로 실패하면 `LineScanError`를
        발생시킵니다. 이미 목표 IP인 경우에도 성공으로 기록합니다.
        """
        self._eb = self._eb or importlib.import_module("eBUS")
        target_ip = ip_addr or self.device_ip_addr
        self._force_ip_attempted = True
        device_info = self._find_first_gev_device_info(prefer_ip=target_ip)
        if device_info is None:
            raise LineScanError("force_ip failed: no GigE Vision device discovered")

        current_ip = _safe_call(device_info, "GetIPAddress")
        mac = _safe_call(device_info, "GetMACAddress")
        if not mac:
            raise LineScanError("force_ip failed: discovered device has no MAC address")
        if current_ip == target_ip:
            self._force_ip_succeeded = True
            self._force_ip_message = f"already configured: {target_ip}"
            return True

        result = self._eb.PvDeviceGEV.SetIPConfiguration(mac, target_ip, subnet_mask, gateway)
        if not _result_ok(result):
            raise EBusResultError(
                f"force_ip failed for MAC {mac} -> {target_ip}: {_describe_result(result)}"
            )
        self._force_ip_succeeded = True
        self._force_ip_message = f"{mac} -> {target_ip}/{subnet_mask} gw={gateway}"
        return True

    # ---- 제한적으로 공개하는 GenICam property ----

    @property
    def trigger_mode(self) -> bool:
        """`TriggerMode` On/Off. False=내부 line-rate, True=외부/소프트 trigger."""
        return str(self._read_feature("TriggerMode")).lower() == "on"

    @trigger_mode.setter
    def trigger_mode(self, enabled: bool) -> None:
        self._write_feature("TriggerMode", "On" if enabled else "Off")

    @property
    def trigger_selector(self) -> str:
        """`TriggerSelector` 문자열. trigger on에서 보통 `LineStart`를 사용합니다."""
        return str(self._read_feature("TriggerSelector"))

    @trigger_selector.setter
    def trigger_selector(self, value: str) -> None:
        self._write_feature("TriggerSelector", value)

    @property
    def trigger_source(self) -> str:
        """`TriggerSource` 문자열. 예: `Line4`, `Software`, encoder source 등."""
        return str(self._read_feature("TriggerSource"))

    @trigger_source.setter
    def trigger_source(self, value: str) -> None:
        self._write_feature("TriggerSource", value)

    @property
    def trigger_activation(self) -> str:
        """`TriggerActivation` 문자열. 예: `RisingEdge`, `FallingEdge`, `LevelHigh`."""
        return str(self._read_feature("TriggerActivation"))

    @trigger_activation.setter
    def trigger_activation(self, value: str) -> None:
        self._write_feature("TriggerActivation", value)

    @property
    def gain(self) -> float:
        """`Gain` 값. setter는 먼저 `GainSelector=DigitalAll`을 시도합니다."""
        return float(self._read_feature("Gain"))

    @gain.setter
    def gain(self, value: float) -> None:
        self._write_feature("GainSelector", "DigitalAll", required=False)
        self._write_feature("Gain", float(value))

    @property
    def exposure_time(self) -> float:
        """`ExposureTime` 노출 시간(µs). 장치 설정에 즉시 반영됩니다."""
        return float(self._read_feature("ExposureTime"))

    @exposure_time.setter
    def exposure_time(self, value: float) -> None:
        self._write_feature("ExposureTime", float(value))

    @property
    def acquisition_line_rate(self) -> float:
        """`AcquisitionLineRate` 내부 line-rate(Hz). TriggerMode=Off에서 의미가 큽니다."""
        return float(self._read_feature("AcquisitionLineRate"))

    @acquisition_line_rate.setter
    def acquisition_line_rate(self, value: float) -> None:
        self._write_feature("AcquisitionLineRate", float(value))

    @property
    def gev_scps_packet_size(self) -> int:
        """`GevSCPSPacketSize` GigE packet size(byte). jumbo frame 튜닝용입니다."""
        return int(self._read_feature("GevSCPSPacketSize"))

    @gev_scps_packet_size.setter
    def gev_scps_packet_size(self, value: int) -> None:
        self._write_feature("GevSCPSPacketSize", int(value))

    @property
    def network_throughput_safety_margin(self) -> int:
        """`NetworkThroughputSafetyMargin` throughput safety margin 설정값."""
        return int(self._read_feature("NetworkThroughputSafetyMargin"))

    @network_throughput_safety_margin.setter
    def network_throughput_safety_margin(self, value: int) -> None:
        self._write_feature("NetworkThroughputSafetyMargin", int(value))

    @property
    def device_link_speed(self) -> int | str:
        """`DeviceLinkSpeed` read-only link speed 확인값."""
        return self._read_feature("DeviceLinkSpeed")

    @property
    def height(self) -> int:
        """`Height` frame/block 하나에 묶을 line 수. bandwidth/FPS에 직접 영향."""
        return int(self._read_feature("Height"))

    @height.setter
    def height(self, value: int) -> None:
        self._write_feature("Height", int(value))

    @property
    def width(self) -> int:
        """`Width` line width(pixel)."""
        return int(self._read_feature("Width"))

    @width.setter
    def width(self, value: int) -> None:
        self._write_feature("Width", int(value))

    @property
    def is_open(self) -> bool:
        """device/stream handle이 열린 상태인지 반환합니다."""
        return self._is_open

    def settings_snapshot(self) -> dict[str, Any]:
        """현재 wrapper 설정과 가능한 GenICam 값을 dict로 반환합니다."""
        snapshot: dict[str, Any] = {
            "device_ip_addr": self.device_ip_addr,
            "pipeline_buffer_count": self.pipeline_buffer_count,
            "timeout_ms": self.timeout_ms,
            "copy_frames": self.copy_frames,
            "force_ip": self.force_ip,
            "force_ip_attempted": self._force_ip_attempted,
            "force_ip_succeeded": self._force_ip_succeeded,
            "force_ip_message": self._force_ip_message,
        }
        if self._device is not None:
            for key, feature in [
                ("trigger_mode", "TriggerMode"),
                ("width", "Width"),
                ("height", "Height"),
                ("acquisition_line_rate", "AcquisitionLineRate"),
                ("exposure_time", "ExposureTime"),
                ("gain", "Gain"),
                ("device_link_speed", "DeviceLinkSpeed"),
            ]:
                try:
                    snapshot[key] = self._read_feature(feature)
                except Exception as exc:
                    snapshot[key] = f"<unavailable: {exc}>"
        return snapshot

    # ---- private eBUS helpers: public generic GenICam API로 노출하지 않음 ----

    def _find_first_gev_device_info(self, *, prefer_ip: str | None = None) -> Any | None:
        system = self._eb.PvSystem()
        result = system.Find()
        if not _result_ok(result):
            raise EBusResultError(f"PvSystem.Find failed: {_describe_result(result)}")

        first_gev = None
        for i in range(int(system.GetInterfaceCount())):
            interface = system.GetInterface(i)
            for j in range(int(interface.GetDeviceCount())):
                info = interface.GetDeviceInfo(j)
                is_gev = _is_instance(info, getattr(self._eb, "PvDeviceInfoGEV", None))
                is_pleora = _is_instance(info, getattr(self._eb, "PvDeviceInfoPleoraProtocol", None))
                has_mac = hasattr(info, "GetMACAddress")
                if not (is_gev or is_pleora or has_mac):
                    continue
                if first_gev is None:
                    first_gev = info
                if prefer_ip and _safe_call(info, "GetIPAddress") == prefer_ip:
                    return info
        return first_gev

    def _configure_gige_stream(self) -> None:
        if self._device is None or self._stream is None:
            raise LineScanError("device/stream is not open")
        if not _is_instance(self._device, getattr(self._eb, "PvDeviceGEV", None)):
            return

        result = self._device.NegotiatePacketSize()
        if not _result_ok(result):
            raise EBusResultError(f"NegotiatePacketSize failed: {_describe_result(result)}")
        result = self._device.SetStreamDestination(
            self._stream.GetLocalIPAddress(), self._stream.GetLocalPort()
        )
        if not _result_ok(result):
            raise EBusResultError(f"SetStreamDestination failed: {_describe_result(result)}")

    def _create_pipeline(self) -> Any:
        if self._device is None or self._stream is None:
            raise LineScanError("camera is not open")
        payload_size = int(self._device.GetPayloadSize())
        if payload_size <= 0:
            raise LineScanError(f"invalid payload size: {payload_size}")
        pipeline = self._eb.PvPipeline(self._stream)
        if not pipeline:
            raise LineScanError("PvPipeline creation failed")
        pipeline.SetBufferCount(self.pipeline_buffer_count)
        pipeline.SetBufferSize(payload_size)
        self._pipeline = pipeline
        return pipeline

    def _get_feature(self, name: str) -> Any:
        self.open()
        params = self._device.GetParameters()
        param = _get_param(params, name)
        if param is None:
            raise LineScanError(f"GenICam feature not found: {name}")
        return param

    def _read_feature(self, name: str) -> Any:
        param = self._get_feature(name)
        try:
            if hasattr(param, "GetValueString"):
                result, value = param.GetValueString()
                if _result_ok(result):
                    return value
        except Exception:
            pass
        try:
            result, value = param.GetValue()
        except Exception as exc:
            raise LineScanError(f"{name}: GetValue raised {exc}") from exc
        if not _result_ok(result):
            raise EBusResultError(f"{name}: GetValue failed: {_describe_result(result)}")
        return value

    def _write_feature(self, name: str, value: Any, *, required: bool = True) -> bool:
        try:
            param = self._get_feature(name)
        except Exception:
            if required:
                raise
            return False
        try:
            result = param.SetValue(value)
        except Exception as exc:
            if required:
                raise LineScanError(f"{name}: SetValue({value!r}) raised {exc}") from exc
            return False
        if not _result_ok(result):
            if required:
                raise EBusResultError(
                    f"{name}: SetValue({value!r}) failed: {_describe_result(result)}"
                )
            return False
        return True

    def _execute_feature(self, name: str, *, required: bool = True) -> bool:
        try:
            param = self._get_feature(name)
        except Exception:
            if required:
                raise
            return False
        try:
            result = param.Execute()
        except Exception as exc:
            if required:
                raise LineScanError(f"{name}: Execute raised {exc}") from exc
            return False
        if not _result_ok(result):
            if required:
                raise EBusResultError(f"{name}: Execute failed: {_describe_result(result)}")
            return False
        return True

    def _frame_from_buffer(
        self,
        pvbuffer: Any,
        result: Any,
        operational_result: Any,
        *,
        copy_image: bool,
    ) -> LineScanFrame:
        image_obj = None
        width = None
        height = None
        try:
            image = pvbuffer.GetImage()
            width = _safe_optional_int_call(image, "GetWidth")
            height = _safe_optional_int_call(image, "GetHeight")
            image_obj = _extract_image_payload(image, copy_image=copy_image)
        except Exception:
            image_obj = None
        return LineScanFrame(
            image=image_obj,
            block_id=_get_block_id(pvbuffer),
            width=width,
            height=height,
            acquired_size=_safe_int_call(pvbuffer, "GetAcquiredSize"),
            timestamp_ns=time.monotonic_ns(),
            ok=_result_ok(result) and _result_ok(operational_result),
            result_code=_result_code(result),
            operational_code=_result_code(operational_result),
        )

    def _print_debug_interval(
        self,
        stats: LineScanStats,
        frame_rate_param: Any | None,
        bandwidth_param: Any | None,
        dt: float,
        frames_delta: int,
        bytes_delta: int,
    ) -> None:
        dt = max(dt, 1e-9)
        local_mbps = (bytes_delta * 8.0) / dt / 1_000_000.0
        ebus_fps = _read_numeric_param(frame_rate_param)
        ebus_bw = _read_numeric_param(bandwidth_param)
        ebus_bw_mbps = ebus_bw / 1_000_000.0 if ebus_bw is not None else None
        print(
            "  "
            f"local={local_mbps:8.1f} Mb/s, "
            f"frames={frames_delta:6d}, "
            f"eBUS_FPS={_format_optional_float(ebus_fps):>8}, "
            f"eBUS_BW={_format_optional_float(ebus_bw_mbps):>8} Mb/s, "
            f"lost={stats.lost_packets}, recovered={stats.recovered_packets}, "
            f"resend_pkts={stats.resend_packets_requested}, gaps={stats.block_gaps}, "
            f"op_err={stats.operational_errors}, ret_err={stats.retrieve_errors}"
        )

    def _maybe_print_debug(self, *args: Any, **kwargs: Any) -> None:
        # retrieve timeout만 반복되는 trigger-on 상황에서도 호출부를 단순하게 유지하기 위한 hook.
        return None


def _safe_int_call(obj: Any, method_name: str, default: int = 0) -> int:
    method = getattr(obj, method_name, None)
    if method is None:
        return default
    try:
        return int(method())
    except Exception:
        return default


def _safe_optional_int_call(obj: Any, method_name: str) -> int | None:
    method = getattr(obj, method_name, None)
    if method is None:
        return None
    try:
        return int(method())
    except Exception:
        return None


def _safe_call(obj: Any, method_name: str, default: Any = None) -> Any:
    method = getattr(obj, method_name, None)
    if method is None:
        return default
    try:
        return method()
    except Exception:
        return default


def _get_param(params: Any, name: str) -> Any | None:
    if params is None:
        return None
    try:
        return params.Get(name)
    except Exception:
        return None


def _result_ok(result: Any) -> bool:
    return result is not None and hasattr(result, "IsOK") and bool(result.IsOK())


def _result_code(result: Any) -> str | None:
    if result is None:
        return None
    if hasattr(result, "GetCodeString"):
        try:
            return str(result.GetCodeString())
        except Exception:
            return None
    return None


def _describe_result(result: Any) -> str:
    if result is None:
        return "None"
    code = _result_code(result) or "UNKNOWN"
    desc = ""
    if hasattr(result, "GetDescription"):
        try:
            desc = str(result.GetDescription())
        except Exception:
            desc = ""
    return f"{code}: {desc}" if desc else code


def _read_numeric_param(param: Any | None) -> float | None:
    if param is None:
        return None
    try:
        result, value = param.GetValue()
        if _result_ok(result):
            return float(value)
    except Exception:
        return None
    return None


def _get_block_id(pvbuffer: Any) -> int | None:
    block_id = _safe_optional_int_call(pvbuffer, "GetBlockID")
    if block_id is None or block_id < 0:
        return None
    return block_id


def _extract_image_payload(image: Any, *, copy_image: bool) -> Any:
    data = image.GetDataPointer()
    if not copy_image:
        return data

    acquired_size = _safe_optional_int_call(image, "GetAcquiredSize")
    if acquired_size is None:
        acquired_size = _safe_optional_int_call(image, "GetRequiredSize")

    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)

    # SWIG/Pleora binding은 환경에 따라 buffer protocol 객체, ctypes pointer, 또는
    # Python sequence를 반환할 수 있다. bytes(data)가 되면 가장 안전하고, 길이를
    # 따로 알아야 하는 pointer류는 원본을 반환해 사용자가 즉시 처리하게 한다.
    try:
        return bytes(data)
    except Exception:
        if acquired_size is not None and hasattr(data, "__getitem__"):
            try:
                return bytes(data[i] for i in range(acquired_size))
            except Exception:
                pass
    return data


def _format_optional_float(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _is_instance(obj: Any, cls: Any) -> bool:
    return cls is not None and isinstance(obj, cls)


__all__ = [
    "DEVICE_IP_ADDR",
    "PIPELINE_BUFFER_COUNT",
    "CaptureResult",
    "EBusResultError",
    "LineScanCamera",
    "LineScanError",
    "LineScanFrame",
    "LineScanStats",
]


def main() -> None:
    """이 파일을 직접 실행했을 때 라이브러리 사용 방법을 출력합니다.

    실제 카메라 capture는 hardware/network 설정에 영향을 줄 수 있으므로, 예제는
    자동 실행하지 않고 복사해서 쓰는 코드 형태로만 보여줍니다.
    """
    print(
        """
LineScanCamera 사용 방법
========================

1) 기본 TriggerMode=Off capture
-------------------------------
from linescan_module import LineScanCamera

with LineScanCamera() as cam:
    cam.width = 4096
    cam.height = 256          # line-scan에서 한 frame/block으로 묶을 line 수
    cam.trigger_mode = False  # 내부 line-rate 모드
    cam.acquisition_line_rate = 84000.0  # Hz
    cam.exposure_time = 5.0   # us
    cam.gain = 1.0

    result = cam.capture(duration_s=3.0, debug=True)

print(len(result.frames))
print(result.stats.avg_bandwidth_mbps)
print(result.debug_summary())


2) TriggerMode=On 외부 trigger capture
--------------------------------------
from linescan_module import LineScanCamera

with LineScanCamera() as cam:
    cam.width = 4096
    cam.height = 256
    cam.trigger_mode = True
    cam.trigger_selector = "LineStart"
    cam.trigger_source = "Line4"
    cam.trigger_activation = "RisingEdge"
    cam.exposure_time = 5.0   # us
    cam.gain = 1.0

    # 외부 trigger가 들어오는 동안 10초간 수신합니다.
    result = cam.capture(duration_s=10.0, debug=True)

print(result.debug_summary())


3) IP / buffer count 변경
-------------------------
from linescan_module import LineScanCamera

with LineScanCamera(
    device_ip_addr="192.168.1.200",
    pipeline_buffer_count=64,
    timeout_ms=1000,
    copy_frames=True,
    force_ip=True,
    debug=True,
) as cam:
    result = cam.capture(duration_s=5.0)


4) 장시간/고속 취득: callback으로 처리하고 frame list 저장 안 함
---------------------------------------------------------------
from linescan_module import LineScanCamera

with LineScanCamera(copy_frames=True) as cam:
    def on_frame(frame):
        # frame.image는 기본적으로 buffer release 후에도 안전하게 복사된 payload입니다.
        # 여기서 파일 저장, queue 전달, 분석 등을 수행합니다.
        pass

    result = cam.capture(
        duration_s=10.0,
        store_frames=False,
        on_frame=on_frame,
        debug=True,
    )

print(result.stats.frames)
print(result.debug_summary())


주의 사항
---------
- 기본 DEVICE_IP_ADDR는 "192.168.1.200"입니다.
- 기본 force_ip=True는 발견된 첫 GigE Vision 카메라에 해당 IP를 강제 할당하려고 합니다.
  네트워크 설정에 영향을 주므로 원치 않으면 LineScanCamera(force_ip=False)를 사용하세요.
- public generic set_param/get_param API는 없습니다. 필요한 설정은 명시된 property만 사용합니다.
- copy_frames=True가 기본값이라 안전하지만, 고속/장시간 취득에서는 메모리 사용량이 커질 수 있습니다.
  이 경우 store_frames=False + on_frame callback을 권장합니다.
- 실제 hardware 검증 전에는 `python3 -m py_compile linescan_module.py`로 문법 확인이 가능합니다.
""".strip()
    )


if __name__ == "__main__":
    main()
