#!/usr/bin/env python3
"""
High-throughput eBUS PvPipeline receiver/diagnostic sample for SW-4005M-5GE.

This is a benchmark-oriented replacement for eBUS_examples/PvPipelineSample.py:
- no OpenCV display
- no per-frame printing
- larger pipeline buffer count
- optional camera transport/acquisition tuning
- prints stream bandwidth plus local acquired-byte throughput
- prints packet loss/resend counters

Example:
    python3 high_throughput_pipeline.py --duration 10 --buffer-count 128 \
        --pixel-format Mono8 --width 4096 --height 256 --line-rate 84000 \
        --packet-size 7976 --safety-margin 92 --scp-delay 0 --trigger-off
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

# eBUS is imported lazily in main() after argparse has handled --help.
# This lets `python3 high_throughput_pipeline.py --help` work even on a host
# where the proprietary `_ebus_python` extension is not installed in PYTHONPATH.
eb = None
psu = None


DEFAULT_BUFFER_COUNT = 64
DEFAULT_STATS_INTERVAL = 1.0

STOP_REQUESTED = False


def _handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
    global STOP_REQUESTED
    STOP_REQUESTED = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


@dataclass
class Counters:
    frames: int = 0
    bytes_acquired: int = 0
    lost: int = 0
    recovered: int = 0
    recovered_single: int = 0
    resend_groups: int = 0
    resend_packets: int = 0
    missing_ids: int = 0
    operational_errors: int = 0
    retrieve_errors: int = 0
    block_gaps: int = 0
    last_block_id: int | None = None

    def update_from_buffer(self, pvbuffer: Any) -> None:
        self.frames += 1
        self.bytes_acquired += _safe_int_call(pvbuffer, "GetAcquiredSize")
        self.lost += _safe_int_call(pvbuffer, "GetLostPacketCount")
        self.recovered += _safe_int_call(pvbuffer, "GetPacketsRecoveredCount")
        self.recovered_single += _safe_int_call(pvbuffer, "GetPacketsRecoveredSingleResendCount")
        self.resend_groups += _safe_int_call(pvbuffer, "GetResendGroupRequestedCount")
        self.resend_packets += _safe_int_call(pvbuffer, "GetResendPacketRequestedCount")
        self.missing_ids += _safe_int_call(pvbuffer, "GetMissingPacketIdsCount")

        block_id = _safe_int_call(pvbuffer, "GetBlockID", default=-1)
        if block_id >= 0:
            if self.last_block_id is not None and block_id > self.last_block_id + 1:
                self.block_gaps += block_id - self.last_block_id - 1
            self.last_block_id = block_id


def _safe_int_call(obj: Any, method_name: str, default: int = 0) -> int:
    method = getattr(obj, method_name, None)
    if method is None:
        return default
    try:
        return int(method())
    except Exception:
        return default


def _result_ok(result: Any) -> bool:
    return result is not None and hasattr(result, "IsOK") and result.IsOK()


def _describe_result(result: Any) -> str:
    if result is None:
        return "None"
    code = result.GetCodeString() if hasattr(result, "GetCodeString") else "UNKNOWN"
    desc = result.GetDescription() if hasattr(result, "GetDescription") else ""
    return f"{code}: {desc}" if desc else code


def get_param(params: Any, name: str) -> Any | None:
    try:
        return params.Get(name)
    except Exception:
        return None


def read_param(params: Any, name: str) -> str:
    param = get_param(params, name)
    if param is None:
        return "<not found>"

    try:
        if hasattr(param, "GetValueString"):
            result, value = param.GetValueString()
            if _result_ok(result):
                return str(value)
    except Exception:
        pass

    try:
        result, value = param.GetValue()
        if _result_ok(result):
            return str(value)
        return f"<read failed: {_describe_result(result)}>"
    except Exception as exc:
        return f"<read exception: {exc}>"


def set_param(params: Any, name: str, value: Any, *, required: bool = False) -> bool:
    param = get_param(params, name)
    if param is None:
        message = f"{name}: parameter not found"
        if required:
            raise RuntimeError(message)
        print(f"WARN: {message}")
        return False

    try:
        result = param.SetValue(value)
    except Exception as exc:
        if required:
            raise RuntimeError(f"{name}: SetValue({value!r}) raised {exc}") from exc
        print(f"WARN: {name}: SetValue({value!r}) raised {exc}")
        return False

    if not _result_ok(result):
        message = f"{name}: SetValue({value!r}) failed: {_describe_result(result)}"
        if required:
            raise RuntimeError(message)
        print(f"WARN: {message}")
        return False

    print(f"SET: {name} = {value}")
    return True


def execute_command(params: Any, name: str, *, required: bool = False) -> bool:
    command = get_param(params, name)
    if command is None:
        message = f"{name}: command not found"
        if required:
            raise RuntimeError(message)
        print(f"WARN: {message}")
        return False

    try:
        result = command.Execute()
    except Exception as exc:
        if required:
            raise RuntimeError(f"{name}: Execute() raised {exc}") from exc
        print(f"WARN: {name}: Execute() raised {exc}")
        return False

    if not _result_ok(result):
        message = f"{name}: Execute() failed: {_describe_result(result)}"
        if required:
            raise RuntimeError(message)
        print(f"WARN: {message}")
        return False

    print(f"EXEC: {name}")
    return True


def dump_params(title: str, params: Any, names: Iterable[str]) -> None:
    print(f"\n[{title}]")
    for name in names:
        print(f"  {name}: {read_param(params, name)}")


def connect_to_device(connection_id: str) -> Any:
    print("Connecting to device...")
    result, device = eb.PvDevice.CreateAndConnect(connection_id)
    if device is None or not _result_ok(result):
        raise RuntimeError(f"Unable to connect to device: {_describe_result(result)}")
    return device


def open_stream(connection_id: str) -> Any:
    print("Opening stream...")
    result, stream = eb.PvStream.CreateAndOpen(connection_id)
    if stream is None or not _result_ok(result):
        raise RuntimeError(f"Unable to open stream: {_describe_result(result)}")
    return stream


def configure_device(device: Any, stream: Any, args: argparse.Namespace) -> None:
    params = device.GetParameters()

    # Stop acquisition if the device was left running from a previous test.
    execute_command(params, "AcquisitionStop", required=False)

    if args.trigger_off:
        set_param(params, "TriggerMode", "Off")
    if args.acquisition_mode:
        set_param(params, "AcquisitionMode", args.acquisition_mode)
    if args.pixel_format:
        set_param(params, "PixelFormat", args.pixel_format)
    if args.width is not None:
        set_param(params, "Width", args.width)
    if args.height is not None:
        set_param(params, "Height", args.height)
    if args.line_rate is not None:
        set_param(params, "AcquisitionLineRate", float(args.line_rate))

    if isinstance(device, eb.PvDeviceGEV):
        if args.safety_margin is not None:
            set_param(params, "NetworkThroughputSafetyMargin", int(args.safety_margin))
        if args.scp_delay is not None:
            set_param(params, "GevSCPD", int(args.scp_delay))

        # Negotiate first so eBUS discovers the host/network capability.
        result = device.NegotiatePacketSize()
        if not _result_ok(result):
            print(f"WARN: NegotiatePacketSize failed: {_describe_result(result)}")
        else:
            print("OK: NegotiatePacketSize")

        # Optional manual override for reproducing the manual's SCPS=7976 condition.
        if args.packet_size is not None:
            set_param(params, "GevSCPSPacketSize", int(args.packet_size))

        result = device.SetStreamDestination(stream.GetLocalIPAddress(), stream.GetLocalPort())
        if not _result_ok(result):
            raise RuntimeError(f"SetStreamDestination failed: {_describe_result(result)}")
        print(f"OK: SetStreamDestination {stream.GetLocalIPAddress()}:{stream.GetLocalPort()}")


def create_pipeline(device: Any, stream: Any, buffer_count: int) -> Any:
    pipeline = eb.PvPipeline(stream)
    if not pipeline:
        raise RuntimeError("PvPipeline creation failed")

    payload_size = device.GetPayloadSize()
    if payload_size <= 0:
        raise RuntimeError(f"Invalid payload size: {payload_size}")

    pipeline.SetBufferCount(buffer_count)
    pipeline.SetBufferSize(payload_size)
    print(f"Pipeline: buffer_count={buffer_count}, payload_size={payload_size} bytes")
    return pipeline


def print_static_snapshot(device: Any, stream: Any) -> None:
    device_params = device.GetParameters()
    stream_params = stream.GetParameters()

    dump_params(
        "Device acquisition/image/transport snapshot",
        device_params,
        [
            "DeviceModelName",
            "DeviceLinkSpeed",
            "Width",
            "Height",
            "PixelFormat",
            "AcquisitionMode",
            "TriggerMode",
            "AcquisitionLineRate",
            "ExposureMode",
            "ExposureTime",
            "NetworkThroughputSafetyMargin",
            "GevSCPSPacketSize",
            "GevSCPSDoNotFragment",
            "GevSCPD",
            "GevSCCFGExtendedChunkData",
            "ChunkModeActive",
        ],
    )

    dump_params(
        "Stream receiver snapshot",
        stream_params,
        [
            "AcquisitionRate",
            "Bandwidth",
            "PayloadSize",
            "QueuedBufferCount",
            "QueuedBufferMaximum",
            "LostPacketCount",
            "ResendPacketRequestedCount",
        ],
    )


def acquire(device: Any, stream: Any, pipeline: Any, args: argparse.Namespace) -> Counters:
    device_params = device.GetParameters()
    stream_params = stream.GetParameters()
    start = get_param(device_params, "AcquisitionStart")
    stop = get_param(device_params, "AcquisitionStop")
    frame_rate_param = get_param(stream_params, "AcquisitionRate")
    bandwidth_param = get_param(stream_params, "Bandwidth")

    if start is None or stop is None:
        raise RuntimeError("AcquisitionStart/AcquisitionStop command not found")

    counters = Counters()

    print("\nStarting pipeline...")
    pipeline.Start()

    print("Enabling stream and starting acquisition...")
    result = device.StreamEnable()
    if not _result_ok(result):
        raise RuntimeError(f"StreamEnable failed: {_describe_result(result)}")
    result = start.Execute()
    if not _result_ok(result):
        raise RuntimeError(f"AcquisitionStart failed: {_describe_result(result)}")

    t0 = time.monotonic()
    last_t = t0
    last_frames = 0
    last_bytes = 0

    print("\n[Streaming statistics]")
    print("Press Ctrl-C to stop. Values are interval-local except cumulative error counters.")

    try:
        while not STOP_REQUESTED:
            now = time.monotonic()
            if args.duration is not None and now - t0 >= args.duration:
                break

            result, pvbuffer, operational_result = pipeline.RetrieveNextBuffer(args.timeout_ms)
            if not _result_ok(result):
                counters.retrieve_errors += 1
                continue

            try:
                if _result_ok(operational_result):
                    counters.update_from_buffer(pvbuffer)
                else:
                    counters.operational_errors += 1
            finally:
                pipeline.ReleaseBuffer(pvbuffer)

            now = time.monotonic()
            if now - last_t >= args.stats_interval:
                dt = now - last_t
                frames_delta = counters.frames - last_frames
                bytes_delta = counters.bytes_acquired - last_bytes
                local_mbps = (bytes_delta * 8.0) / dt / 1_000_000.0

                ebus_fps = read_numeric_param(frame_rate_param)
                ebus_bw = read_numeric_param(bandwidth_param)
                ebus_bw_mbps = ebus_bw / 1_000_000.0 if ebus_bw is not None else None

                fps_text = f"{ebus_fps:.1f}" if ebus_fps is not None else "n/a"
                ebus_bw_text = f"{ebus_bw_mbps:.1f}" if ebus_bw_mbps is not None else "n/a"

                print(
                    "  "
                    f"local={local_mbps:8.1f} Mb/s, "
                    f"frames={frames_delta:6d}, "
                    f"eBUS_FPS={fps_text:>8}, "
                    f"eBUS_BW={ebus_bw_text:>8} Mb/s, "
                    f"lost={counters.lost}, recovered={counters.recovered}, "
                    f"resend_pkts={counters.resend_packets}, gaps={counters.block_gaps}, "
                    f"op_err={counters.operational_errors}, ret_err={counters.retrieve_errors}"
                )

                last_t = now
                last_frames = counters.frames
                last_bytes = counters.bytes_acquired
    finally:
        print("\nStopping acquisition...")
        try:
            stop.Execute()
        except Exception:
            pass
        try:
            device.StreamDisable()
        except Exception:
            pass
        try:
            pipeline.Stop()
        except Exception:
            pass

    total_dt = max(time.monotonic() - t0, 1e-9)
    avg_mbps = (counters.bytes_acquired * 8.0) / total_dt / 1_000_000.0
    print("\n[Summary]")
    print(f"  elapsed_s: {total_dt:.3f}")
    print(f"  frames: {counters.frames}")
    print(f"  avg_local_bandwidth_mbps: {avg_mbps:.1f}")
    print(f"  bytes_acquired: {counters.bytes_acquired}")
    print(f"  lost_packets: {counters.lost}")
    print(f"  recovered_packets: {counters.recovered}")
    print(f"  recovered_single_resend_packets: {counters.recovered_single}")
    print(f"  resend_groups_requested: {counters.resend_groups}")
    print(f"  resend_packets_requested: {counters.resend_packets}")
    print(f"  missing_packet_ids: {counters.missing_ids}")
    print(f"  block_gaps: {counters.block_gaps}")
    print(f"  operational_errors: {counters.operational_errors}")
    print(f"  retrieve_errors: {counters.retrieve_errors}")
    return counters


def read_numeric_param(param: Any | None) -> float | None:
    if param is None:
        return None
    try:
        result, value = param.GetValue()
        if _result_ok(result):
            return float(value)
    except Exception:
        return None
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark-oriented PvPipeline receiver for SW-4005M-5GE/eBUS."
    )
    parser.add_argument("--connection-id", help="Optional eBUS connection ID. If omitted, PvSelectDevice() is used.")
    parser.add_argument("--duration", type=float, default=None, help="Streaming duration in seconds. Default: until Ctrl-C.")
    parser.add_argument("--buffer-count", type=int, default=DEFAULT_BUFFER_COUNT, help="PvPipeline buffer count.")
    parser.add_argument("--stats-interval", type=float, default=DEFAULT_STATS_INTERVAL, help="Statistics print interval in seconds.")
    parser.add_argument("--timeout-ms", type=int, default=1000, help="RetrieveNextBuffer timeout in ms.")

    parser.add_argument("--trigger-off", action="store_true", help="Set TriggerMode=Off before acquisition.")
    parser.add_argument("--acquisition-mode", default="Continuous", help="Set AcquisitionMode. Use empty string to skip.")
    parser.add_argument("--pixel-format", help="Set PixelFormat, e.g. Mono8, Mono10Packed.")
    parser.add_argument("--width", type=int, help="Set Width.")
    parser.add_argument("--height", type=int, help="Set Height.")
    parser.add_argument("--line-rate", type=float, help="Set AcquisitionLineRate in Hz.")

    parser.add_argument("--packet-size", type=int, help="Set GevSCPSPacketSize after negotiation, e.g. 7976.")
    parser.add_argument("--safety-margin", type=int, help="Set NetworkThroughputSafetyMargin, e.g. 92.")
    parser.add_argument("--scp-delay", type=int, help="Set GevSCPD packet delay, e.g. 0.")
    parser.add_argument("--no-display", action="store_true", help="Accepted for clarity; this program never displays images.")

    args = parser.parse_args(argv)
    if args.buffer_count <= 0:
        parser.error("--buffer-count must be positive")
    if args.stats_interval <= 0:
        parser.error("--stats-interval must be positive")
    if args.acquisition_mode == "":
        args.acquisition_mode = None
    return args


def main(argv: list[str]) -> int:
    global eb, psu

    args = parse_args(argv)

    try:
        import eBUS as eb_module
        import lib.PvSampleUtils as psu_module
    except ModuleNotFoundError as exc:
        print(
            "ERROR: eBUS Python extension is not importable. "
            "Check that Pleora eBUS SDK Python bindings are installed and "
            "PYTHONPATH/LD_LIBRARY_PATH include the directory containing _ebus_python.",
            file=sys.stderr,
        )
        print(f"Import error: {exc}", file=sys.stderr)
        return 2

    eb = eb_module
    psu = psu_module

    connection_id = args.connection_id
    if not connection_id:
        connection_id = psu.PvSelectDevice()
    if not connection_id:
        print("No device selected.")
        return 1

    device = None
    stream = None
    try:
        device = connect_to_device(connection_id)
        stream = open_stream(connection_id)
        configure_device(device, stream, args)
        print_static_snapshot(device, stream)
        pipeline = create_pipeline(device, stream, args.buffer_count)
        acquire(device, stream, pipeline, args)
        return 0
    finally:
        if stream is not None:
            try:
                print("Closing stream...")
                stream.Close()
            except Exception:
                pass
            try:
                eb.PvStream.Free(stream)
            except Exception:
                pass
        if device is not None:
            try:
                print("Disconnecting device...")
                device.Disconnect()
            except Exception:
                pass
            try:
                eb.PvDevice.Free(device)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
