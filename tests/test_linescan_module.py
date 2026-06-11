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
    CaptureSettings,
    capture_result_to_array,
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
    def __init__(self, value=None):
        self.value = value
        self.writes = []
        self.exec_count = 0

    def GetValue(self):
        return Result(), self.value

    def GetValueString(self):
        return Result(), str(self.value)

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
            "ExposureTime": Param(5.0),
            "AcquisitionLineRate": Param(84000.0),
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

    def test_gui_capture_result_to_array_stacks_frames_vertically(self):
        frames = [
            LineScanFrame(bytes([1, 2, 3, 4]), 1, 2, 2, 4, 0, True),
            LineScanFrame(bytes([5, 6, 7, 8]), 2, 2, 2, 4, 0, True),
        ]
        result = CaptureResult(frames=frames, stats=LineScanStats())
        array = capture_result_to_array(result)
        np.testing.assert_array_equal(
            array,
            np.array([[1, 2], [3, 4], [5, 6], [7, 8]], dtype=np.uint8),
        )

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


if __name__ == "__main__":
    unittest.main()
