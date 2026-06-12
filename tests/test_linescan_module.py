import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

# The real Pleora/JAI eBUS binding is only available on camera machines with the
# vendor runtime installed. Unit tests use fakes, so provide an import stub before
# importing linescan_module.
_ebus_stub = types.ModuleType("eBUS")
for _name in (
    "PvBuffer",
    "PvDevice",
    "PvDeviceGEV",
    "PvDeviceInfo",
    "PvDeviceInfoGEV",
    "PvDeviceInfoPleoraProtocol",
    "PvGenParameter",
    "PvGenParameterArray",
    "PvImage",
    "PvPipeline",
    "PvResult",
    "PvStream",
    "PvSystem",
):
    setattr(_ebus_stub, _name, object)
sys.modules.setdefault("eBUS", _ebus_stub)

import linescan_module as lm
from linescan_module import CaptureResult, LineScanCamera, LineScanFrame, LineScanStats
from linescan_gui import (
    CameraCommand,
    CameraServiceCore,
    CaptureSettings,
    DisplayImageAccumulator,
    controls_enabled_after_open,
    frame_to_array,
    next_capture_image_path,
    trigger_source_enabled,
)


class Result:
    def __init__(self, ok=True, code="OK", desc=""):
        self.ok = ok
        self.code = code
        self.desc = desc

    def IsOK(self):
        return self.ok

    def GetCodeString(self):
        return self.code

    def GetDescription(self):
        return self.desc


class Param:
    def __init__(self, value=None, minimum=None, maximum=None):
        self.value = value
        self.minimum = minimum
        self.maximum = maximum
        self.writes = []
        self.exec_count = 0

    def GetValue(self):
        return Result(), self.value

    def GetValueString(self):
        return Result(), str(self.value)

    def GetMin(self):
        return Result(), self.minimum

    def GetMax(self):
        return Result(), self.maximum

    def SetValue(self, value):
        self.value = value
        self.writes.append(value)
        return Result()

    def Execute(self):
        self.exec_count += 1
        return Result()


class Params:
    def __init__(self):
        self.params = {
            "TriggerMode": Param("Off"),
            "TriggerSelector": Param("LineStart"),
            "TriggerSource": Param("Line4"),
            "TriggerActivation": Param("RisingEdge"),
            "GainSelector": Param("AnalogAll"),
            "Gain": Param(1.0),
            "ExposureTime": Param(5.0, minimum=1.0, maximum=100.0),
            "AcquisitionLineRate": Param(84000.0, minimum=66.0, maximum=10000.0),
            "GevSCPSPacketSize": Param(7976),
            "NetworkThroughputSafetyMargin": Param(92),
            "DeviceLinkSpeed": Param(5000000000),
            "Height": Param(256),
            "Width": Param(4096),
            "AcquisitionStart": Param(),
            "AcquisitionStop": Param(),
            "AcquisitionRate": Param(10.0),
            "Bandwidth": Param(1000000.0),
        }

    def Get(self, name):
        return self.params.get(name)


class FakeImage:
    def __init__(self, data=b"abcd", width=4, height=1):
        self.data = data
        self.width = width
        self.height = height

    def GetDataPointer(self):
        return self.data

    def GetWidth(self):
        return self.width

    def GetHeight(self):
        return self.height

    def GetAcquiredSize(self):
        return len(self.data)


class FakeBuffer:
    def __init__(self, block_id=1, data=b"abcd"):
        self.block_id = block_id
        self.image = FakeImage(data=data)

    def GetImage(self):
        return self.image

    def GetBlockID(self):
        return self.block_id

    def GetAcquiredSize(self):
        return len(self.image.data)

    def GetLostPacketCount(self):
        return 0

    def GetPacketsRecoveredCount(self):
        return 0

    def GetPacketsRecoveredSingleResendCount(self):
        return 0

    def GetResendGroupRequestedCount(self):
        return 0

    def GetResendPacketRequestedCount(self):
        return 0

    def GetMissingPacketIdsCount(self):
        return 0


class FakeDeviceBase:
    pass


class FakeDeviceGEV(FakeDeviceBase):
    def __init__(self, params):
        self.params = params
        self.enabled = False
        self.disconnected = False

    def GetParameters(self):
        return self.params

    def GetPayloadSize(self):
        return 4

    def NegotiatePacketSize(self):
        return Result()

    def SetStreamDestination(self, ip, port):
        self.destination = (ip, port)
        return Result()

    def StreamEnable(self):
        self.enabled = True
        return Result()

    def StreamDisable(self):
        self.enabled = False
        return Result()

    def Disconnect(self):
        self.disconnected = True


class FakeStream:
    def __init__(self, params):
        self.params = params
        self.closed = False

    def GetParameters(self):
        return self.params

    def GetLocalIPAddress(self):
        return "192.168.1.10"

    def GetLocalPort(self):
        return 12345

    def Close(self):
        self.closed = True


class FakePipeline:
    def __init__(self, stream):
        self.stream = stream
        self.buffers = [FakeBuffer(1, b"abcd"), FakeBuffer(2, b"efgh")]
        self.started = False
        self.released = 0
        self.buffer_count = None
        self.buffer_size = None

    def SetBufferCount(self, value):
        self.buffer_count = value

    def SetBufferSize(self, value):
        self.buffer_size = value

    def Start(self):
        self.started = True

    def Stop(self):
        self.started = False

    def RetrieveNextBuffer(self, timeout_ms):
        if self.buffers:
            return Result(), self.buffers.pop(0), Result()
        return Result(False, "TIMEOUT"), None, Result(False, "TIMEOUT")

    def ReleaseBuffer(self, buffer):
        self.released += 1


class FakePvDevice:
    def __init__(self, eb):
        self.eb = eb

    def CreateAndConnect(self, connection_id):
        return Result(), self.eb.device

    def Free(self, device):
        self.eb.device_freed = True


class FakePvStream:
    def __init__(self, eb):
        self.eb = eb

    def CreateAndOpen(self, connection_id):
        return Result(), self.eb.stream

    def Free(self, stream):
        self.eb.stream_freed = True


class FakeEBus:
    PvDeviceGEV = FakeDeviceGEV

    def __init__(self):
        self.params = Params()
        self.device = FakeDeviceGEV(self.params)
        self.stream = FakeStream(self.params)
        self.device_freed = False
        self.stream_freed = False
        self.PvDevice = FakePvDevice(self)
        self.PvStream = FakePvStream(self)

    def PvPipeline(self, stream):
        self.pipeline = FakePipeline(stream)
        return self.pipeline


class LineScanModuleTests(unittest.TestCase):
    def make_camera(self):
        eb = FakeEBus()
        lm.eb = eb
        cam = LineScanCamera(force_ip=False, timeout_ms=1)
        return cam, eb

    def test_property_mapping_and_gain_selector(self):
        cam, eb = self.make_camera()
        cam.open()
        cam.trigger_mode = True
        cam.gain = 2.5
        cam.height = 128
        cam.width = 2048
        self.assertTrue(cam.trigger_mode)
        self.assertEqual(eb.params.params["TriggerMode"].value, "On")
        self.assertEqual(eb.params.params["GainSelector"].value, "DigitalAll")
        self.assertEqual(eb.params.params["Gain"].value, 2.5)
        self.assertEqual(cam.height, 128)
        self.assertEqual(cam.width, 2048)
        with self.assertRaises(AttributeError):
            cam.device_link_speed = 1  # type: ignore[misc]

    def test_timing_feature_limits_are_exposed_as_float_properties(self):
        cam, _eb = self.make_camera()
        cam.open()
        self.assertEqual(cam.acquisition_line_rate_min, 66.0)
        self.assertEqual(cam.acquisition_line_rate_max, 10000.0)
        self.assertEqual(cam.exposure_time_min, 1.0)
        self.assertEqual(cam.exposure_time_max, 100.0)

    def test_stats_block_gap(self):
        stats = LineScanStats()
        stats.update_from_buffer(FakeBuffer(1, b"aa"))
        stats.update_from_buffer(FakeBuffer(4, b"bbbb"))
        stats.elapsed_s = 2.0
        self.assertEqual(stats.frames, 2)
        self.assertEqual(stats.bytes_acquired, 6)
        self.assertEqual(stats.block_gaps, 2)
        self.assertAlmostEqual(stats.avg_bandwidth_mbps, 0.000024)

    def test_capture_stores_frames_and_cleans_up(self):
        cam, eb = self.make_camera()
        result = cam.capture(0.01)
        self.assertEqual(len(result.frames), 2)
        self.assertEqual(result.stats.frames, 2)
        self.assertGreaterEqual(result.stats.retrieve_errors, 0)
        self.assertEqual(eb.pipeline.released, 2)
        self.assertFalse(eb.pipeline.started)
        self.assertFalse(eb.device.enabled)
        self.assertIn("avg_local_bandwidth_mbps", result.debug_summary())
        cam.close()
        self.assertTrue(eb.stream.closed)
        self.assertTrue(eb.stream_freed)
        self.assertTrue(eb.device.disconnected)
        self.assertTrue(eb.device_freed)

    def test_gui_frame_to_array_uses_frame_dimensions(self):
        frame = LineScanFrame(
            image=bytes(range(8)),
            block_id=1,
            width=4,
            height=2,
            acquired_size=8,
            timestamp_ns=0,
            ok=True,
        )
        array = frame_to_array(frame)
        np.testing.assert_array_equal(array, np.array([[0, 1, 2, 3], [4, 5, 6, 7]], dtype=np.uint8))

    def test_gui_frame_to_array_ignores_unsafe_pointer_like_payloads(self):
        class PointerLikePayload:
            pass

        frame = LineScanFrame(
            image=PointerLikePayload(),
            block_id=1,
            width=4,
            height=1,
            acquired_size=4,
            timestamp_ns=0,
            ok=True,
        )
        self.assertEqual(frame_to_array(frame).shape, (0, 0))

    def test_display_accumulator_keeps_full_preview(self):
        accumulator = DisplayImageAccumulator()
        accumulator.add_frame(LineScanFrame(bytes(range(8)), 1, 4, 2, 8, 0, True))
        array = accumulator.to_array()
        np.testing.assert_array_equal(array, np.array([[0, 1, 2, 3], [4, 5, 6, 7]], dtype=np.uint8))
        self.assertEqual(accumulator.bytes_used, 8)

    def test_gui_controls_are_enabled_only_after_open_and_trigger_source_needs_trigger_on(self):
        self.assertFalse(controls_enabled_after_open(False))
        self.assertTrue(controls_enabled_after_open(True))
        self.assertFalse(trigger_source_enabled(camera_open=True, trigger_mode="Off"))
        self.assertFalse(trigger_source_enabled(camera_open=False, trigger_mode="On"))
        self.assertTrue(trigger_source_enabled(camera_open=True, trigger_mode="On"))

    def test_next_capture_image_path_uses_incrementing_number_and_settings(self):
        settings = CaptureSettings(
            exposure_us=5.0,
            line_rate_hz=66.0,
            trigger_mode="Off",
            trigger_source="Line4",
            frame_height=1,
            duration_s=1.0,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            captures_dir = Path(tmpdir)
            (captures_dir / "1_exposure_5us_linerate_66hz_trigger_Off.png").write_bytes(b"old")
            path = next_capture_image_path(captures_dir, settings)

        self.assertEqual(
            path.name,
            "2_exposure_5us_linerate_66hz_trigger_Off.png",
        )

    def test_camera_service_core_keeps_camera_lifecycle_inside_service(self):
        class FakeServiceCamera:
            def __init__(self):
                self.is_open = False
                self.calls = []
                self.exposure_time_max = 100.0
                self.acquisition_line_rate_max = 10000.0

            def open(self):
                self.calls.append("open")
                self.is_open = True
                return self

            def close(self):
                self.calls.append("close")
                self.is_open = False

            @property
            def exposure_time(self):
                return 0.0

            @exposure_time.setter
            def exposure_time(self, value):
                self.calls.append(("exposure_time", value))

            @property
            def acquisition_line_rate(self):
                return 0.0

            @acquisition_line_rate.setter
            def acquisition_line_rate(self, value):
                self.calls.append(("acquisition_line_rate", value))

            @property
            def height(self):
                return 0

            @height.setter
            def height(self, value):
                self.calls.append(("height", value))

            @property
            def trigger_mode(self):
                return False

            @trigger_mode.setter
            def trigger_mode(self, value):
                self.calls.append(("trigger_mode", value))

            @property
            def trigger_selector(self):
                return "LineStart"

            @trigger_selector.setter
            def trigger_selector(self, value):
                self.calls.append(("trigger_selector", value))

            @property
            def trigger_source(self):
                return "Line4"

            @trigger_source.setter
            def trigger_source(self, value):
                self.calls.append(("trigger_source", value))

            def capture(self, **kwargs):
                self.calls.append(("capture", kwargs["duration_s"]))
                if kwargs.get("on_frame"):
                    kwargs["on_frame"](LineScanFrame(bytes([1, 2, 3, 4]), 1, 2, 2, 4, 0, True))
                return CaptureResult(frames=[], stats=LineScanStats(frames=1, bytes_acquired=4))

        cameras = []

        def factory():
            camera = FakeServiceCamera()
            cameras.append(camera)
            return camera

        settings = CaptureSettings(5.0, 66.0, "On", "Line4", 2, 0.01)
        service = CameraServiceCore(camera_factory=factory)

        open_events = service.handle(CameraCommand("open", settings=settings))
        config_events = service.handle(CameraCommand("config", settings=settings))
        capture_events = service.handle(CameraCommand("capture", settings=settings))
        close_events = service.handle(CameraCommand("close"))

        self.assertEqual(len(cameras), 1)
        self.assertEqual([event.kind for event in open_events], ["opened"])
        self.assertEqual([event.kind for event in config_events], ["config_applied"])
        self.assertEqual([event.kind for event in capture_events], ["capture_started", "capture_finished"])
        self.assertEqual([event.kind for event in close_events], ["closed"])
        self.assertFalse(cameras[0].is_open)
        self.assertEqual(cameras[0].calls[0], "open")
        self.assertEqual(cameras[0].calls[-1], "close")
        self.assertEqual(sum(1 for call in cameras[0].calls if call == "open"), 1)
        self.assertIn(("capture", 0.01), cameras[0].calls)
        payload = capture_events[-1].payload
        np.testing.assert_array_equal(payload.image_array, np.array([[1, 2], [3, 4]], dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
