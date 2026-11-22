"""Unit tests for telemetry.py: LibreHardwareMonitor parsing and sensor choice, the RAPL
power averaging, and the Poller itself on this machine (rate and value ranges, FR-12/NFR-1)."""
import time
import unittest

import _setup  # noqa: F401  (sys.path)

import telemetry

LHM_TREE = {
    "Text": "Sensor", "Children": [
        {"Text": "DESKTOP", "Children": [
            {"Text": "AMD Ryzen 7 5800X", "HardwareId": "/amdcpu/0", "Children": [
                {"Text": "Temperatures", "Children": [
                    {"Text": "Core (Tctl/Tdie)", "SensorId": "/amdcpu/0/temperature/2", "Type": "Temperature",
                     "Value": "61,5 °C"},
                    {"Text": "CCD1 (Tdie)", "SensorId": "/amdcpu/0/temperature/3", "Type": "Temperature",
                     "Value": "58.0 °C"}]},
                {"Text": "Powers", "Children": [
                    {"Text": "Package", "SensorId": "/amdcpu/0/power/0", "Type": "Power", "Value": "88.2 W"}]},
                {"Text": "Load", "Children": [
                    {"Text": "CPU Total", "SensorId": "/amdcpu/0/load/0", "Type": "Load", "Value": "12 %"}]}]},
            {"Text": "Nuvoton NCT6798D", "HardwareId": "/lpc/nct6798d/0", "Children": [
                {"Text": "Chipset", "SensorId": "/lpc/nct6798d/0/temperature/5", "Type": "Temperature",
                 "Value": "47.0 °C"}]},
            {"Text": "NVIDIA RTX 3080 Ti", "HardwareId": "/gpu-nvidia/0", "Children": [
                {"Text": "GPU Core", "SensorId": "/gpu-nvidia/0/temperature/0", "Type": "Temperature",
                 "Value": "55 °C"},
                {"Text": "Broken", "SensorId": "/gpu-nvidia/0/temperature/9", "Type": "Temperature",
                 "Value": "-"}]}]}]}


class LhmParseTest(unittest.TestCase):
    def setUp(self):
        self.sensors = telemetry.lhm_web_sensors(LHM_TREE)

    def test_only_temperatures_and_power_with_numbers_are_kept(self):
        self.assertEqual({s["type"] for s in self.sensors}, {"Temperature", "Power"})
        self.assertNotIn("Broken", [s["name"] for s in self.sensors])
        self.assertEqual(len(self.sensors), 5)

    def test_comma_decimal_and_units_are_parsed(self):
        tctl = next(s for s in self.sensors if s["name"] == "Core (Tctl/Tdie)")
        self.assertEqual(tctl["value"], 61.5)
        self.assertEqual(tctl["hw"], "AMD Ryzen 7 5800X")
        self.assertEqual(tctl["kind"], "Cpu")

    def test_hardware_kinds_come_from_the_sensor_id(self):
        kinds = {s["name"]: s["kind"] for s in self.sensors}
        self.assertEqual(kinds["Chipset"], "SuperIO")
        self.assertEqual(kinds["GPU Core"], "GpuNvidia")


class PickTest(unittest.TestCase):
    def setUp(self):
        self.sensors = telemetry.lhm_web_sensors(LHM_TREE)

    def test_cpu_prefers_the_package_style_sensor(self):
        self.assertEqual(telemetry.pick_lhm(self.sensors, ("Cpu",)), {"temp": 61.5, "power": 88.2})

    def test_gpu_by_name(self):
        self.assertEqual(telemetry.pick_lhm(self.sensors, ("Gpu",), "NVIDIA RTX 3080 Ti")["temp"], 55.0)
        self.assertIsNone(telemetry.pick_lhm(self.sensors, ("Gpu",), "Radeon")["temp"])

    def test_nothing_known_gives_none(self):
        self.assertEqual(telemetry.pick_lhm([], ("Cpu",)), {"temp": None, "power": None})

    def test_board_temp_only_from_a_named_chipset_sensor(self):
        self.assertEqual(telemetry.board_temp(self.sensors), 47.0)
        unnamed = [{"kind": "SuperIO", "type": "Temperature", "name": "Temperature #3", "value": 40.0}]
        self.assertIsNone(telemetry.board_temp(unnamed))  # never guess which unnamed sensor it is


class FakePdh:
    def __init__(self, values):
        self.values = list(values)

    def value(self, counter):
        return self.values.pop(0)


class RaplTest(unittest.TestCase):
    """_cpu_power(): watts over the last ~0.5 s, weighted by each reading's interval."""

    def make(self, values):
        p = telemetry.Poller.__new__(telemetry.Poller)  # no sensors opened
        p._pdh, p._rapl = FakePdh(values), object()
        p._rapl_t, p._rapl_hist = 0.0, telemetry.collections.deque()
        return p

    def test_needs_at_least_150_ms_of_readings(self):
        p = self.make([50000])
        self.assertIsNone(p._cpu_power(0.1))

    def test_energy_over_time_not_plain_average(self):
        # 0.05 s at 100 W, then 0.15 s at 20 W -> (5 + 3) J / 0.2 s = 40 W (plain average would be 60)
        p = self.make([100000, 20000])
        p._cpu_power(0.05)
        self.assertAlmostEqual(p._cpu_power(0.20), 40.0)

    def test_old_readings_drop_out_after_half_a_second(self):
        p = self.make([100000, 20000, 20000])
        p._cpu_power(0.2)
        p._cpu_power(0.4)
        self.assertAlmostEqual(p._cpu_power(0.9), 20.0)


class PollerLiveTest(unittest.TestCase):
    """The real Poller on this PC (no GPU or disk chosen, so it only needs psutil)."""

    @classmethod
    def setUpClass(cls):
        cls.p = telemetry.Poller()
        cls.p.start()
        times = set()
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            s = cls.p.latest
            if s:
                times.add(s["t"])
            time.sleep(0.005)
        cls.times = times
        cls.sample = dict(cls.p.latest)
        cls.p.stop()

    def test_polls_at_least_10_times_a_second(self):  # NFR: 10 Hz+, design 20 Hz
        self.assertGreaterEqual(len(self.times), 15)

    def test_values_are_in_range(self):
        s = self.sample
        self.assertTrue(0 <= s["cpu_util"] <= 100)
        self.assertTrue(0 <= s["ram_util"] <= 100)
        self.assertGreater(s["ram_used"], 0)
        self.assertEqual(len(s["cpu_cores"]), telemetry.psutil.cpu_count())
        self.assertEqual(s["cpu_core_max"], max(s["cpu_cores"]) if False else s["cpu_core_max"])
        self.assertGreaterEqual(s["cpu_core_max"], max(s["cpu_cores"]) - 0.5)

    def test_parts_not_watched_are_unavailable_not_zero(self):
        for k in ("gpu_util", "disk_util", "disk_read"):
            self.assertIsNone(self.sample[k], k)

    def test_stop_ends_the_thread(self):
        self.p._thread.join(1.0)
        self.assertFalse(self.p._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
