"""Unit tests for analysis.py - the bottleneck finder (SRS appendix VI.3).

Each pattern is tested with recorded samples built to show exactly that pattern,
plus the boundaries (thresholds, minimum share, minimum stretch length)."""
import unittest

from _setup import samples

import analysis


def keys(findings):
    return [f["key"] for f in findings]


class StatesTest(unittest.TestCase):
    """_states(): which patterns hold at one (windowed) moment."""

    def w(self, **kw):
        base = {"cpu_util": None, "cpu_core_max": None, "gpu_util": None,
                "disk_util": None, "board_util": None, "ram_util": None}
        base.update(kw)
        return base

    def test_one_core_maxed_on_many_cores_is_serial(self):
        # 16 cores, one at 100 %: whole CPU ~6 % = 1 core of work
        self.assertIn("serial", analysis._states(self.w(cpu_util=6.5, cpu_core_max=100), False, 16))

    def test_serial_needs_more_than_two_cores(self):
        self.assertNotIn("serial", analysis._states(self.w(cpu_util=50, cpu_core_max=100), False, 2))

    def test_serial_not_reported_while_gpu_is_maxed(self):
        # the busy core is the thread feeding the GPU, not serial code
        found = analysis._states(self.w(cpu_util=6.5, cpu_core_max=100, gpu_util=99), True, 16)
        self.assertNotIn("serial", found)
        self.assertIn("gpu_bound", found)

    def test_many_busy_cores_is_cpu_bound_not_serial(self):
        found = analysis._states(self.w(cpu_util=97, cpu_core_max=100), False, 16)
        self.assertIn("cpu_bound", found)
        self.assertNotIn("serial", found)

    def test_cpu_bound_threshold_is_85(self):
        self.assertIn("cpu_bound", analysis._states(self.w(cpu_util=85), False, 8))
        self.assertNotIn("cpu_bound", analysis._states(self.w(cpu_util=84.9), False, 8))

    def test_gpu_starved_only_when_the_run_uses_the_gpu(self):
        moment = self.w(cpu_util=90, cpu_core_max=100, gpu_util=10)
        self.assertIn("gpu_starved", analysis._states(moment, True, 8))
        self.assertNotIn("gpu_starved", analysis._states(moment, False, 8))

    def test_pcie_disk_ram_thresholds(self):
        self.assertIn("pcie", analysis._states(self.w(board_util=60), False, 8))
        self.assertNotIn("pcie", analysis._states(self.w(board_util=59), False, 8))
        self.assertIn("disk", analysis._states(self.w(disk_util=90), False, 8))
        self.assertNotIn("disk", analysis._states(self.w(disk_util=89), False, 8))
        self.assertIn("ram", analysis._states(self.w(ram_util=92), False, 8))
        self.assertNotIn("ram", analysis._states(self.w(ram_util=91), False, 8))

    def test_nothing_busy_is_idle(self):
        self.assertIn("idle", analysis._states(self.w(cpu_util=3, cpu_core_max=10, gpu_util=1, disk_util=0), False, 8))

    def test_missing_readings_never_count_as_a_pattern(self):
        self.assertEqual(analysis._states(self.w(), True, 8), set())


class WindowedTest(unittest.TestCase):
    def test_each_sample_is_the_average_of_the_last_second(self):
        rows = samples(41, dt=0.05, cpu_util=lambda i: 100 if i % 2 else 0)
        win = analysis._windowed(rows)
        self.assertEqual(len(win), len(rows))
        self.assertAlmostEqual(win[-1]["cpu_util"], 50, delta=3)
        self.assertEqual(win[0]["cpu_util"], 0)  # first sample: only itself

    def test_non_numbers_are_ignored(self):
        rows = [{"t": 0.0, "gpu_util": None}, {"t": 0.1, "gpu_util": 80}, {"t": 0.2, "gpu_util": "n/a"}]
        self.assertEqual(analysis._windowed(rows)[-1]["gpu_util"], 80)


class AnalyzeTest(unittest.TestCase):
    def test_serial_program_is_found_with_share_and_span(self):
        rows = samples(200, cpu_util=6.5, cpu_core_max=100, cpu_cores=[100] + [0] * 15)  # 10 s
        found = analysis.analyze(rows)
        self.assertEqual(keys(found)[0], "serial")
        serial = found[0]
        self.assertTrue(serial["problem"])
        self.assertGreater(serial["share"], 0.9)
        self.assertTrue(serial["spans"])
        self.assertGreaterEqual(serial["spans"][0][1] - serial["spans"][0][0], analysis.MIN_SPAN_S)

    def test_core_count_comes_from_the_samples(self):
        # same readings, but only 2 cores recorded: not "serial"
        rows = samples(200, cpu_util=50, cpu_core_max=100, cpu_cores=[100, 0])
        self.assertNotIn("serial", keys(analysis.analyze(rows)))

    def test_gpu_starved_needs_gpu_use(self):
        # GPU busy for a moment (>= 30 %) then idle while the CPU works
        rows = samples(200, cpu_util=95, cpu_core_max=100, cpu_cores=[100] * 8,
                       gpu_util=lambda i: 60 if i < 5 else 5)
        self.assertIn("gpu_starved", keys(analysis.analyze(rows)))
        # GPU never used: its idleness says nothing about the program
        rows = samples(200, cpu_util=95, cpu_core_max=100, cpu_cores=[100] * 8, gpu_util=2, gpu_vram=1.0)
        self.assertNotIn("gpu_starved", keys(analysis.analyze(rows)))

    def test_vram_growth_counts_as_gpu_use(self):
        rows = samples(200, cpu_util=95, cpu_core_max=100, cpu_cores=[100] * 8, gpu_util=5,
                       gpu_vram=lambda i: 1.0 if i < 10 else 1.5)
        self.assertIn("gpu_starved", keys(analysis.analyze(rows)))

    def test_short_pattern_below_min_share_is_not_reported(self):
        # disk saturated for 5 % of the run only
        rows = samples(200, disk_util=lambda i: 100 if i < 10 else 50, cpu_util=50, cpu_core_max=60)
        self.assertNotIn("disk", keys(analysis.analyze(rows)))

    def test_problems_come_before_good_findings(self):
        rows = samples(200, cpu_util=95, cpu_core_max=100, cpu_cores=[100] * 8, ram_util=95)
        found = analysis.analyze(rows)
        self.assertEqual(keys(found), ["ram", "cpu_bound"])
        self.assertTrue(found[0]["problem"])
        self.assertFalse(found[1]["problem"])

    def test_too_few_or_zero_length_runs_give_nothing(self):
        self.assertEqual(analysis.analyze(samples(3, cpu_util=100)), [])
        self.assertEqual(analysis.analyze([{"t": 1.0, "cpu_util": 100}] * 10), [])
        self.assertEqual(analysis.analyze([{"cpu_util": 100}] * 10), [])  # no times

    def test_every_finding_has_the_documented_fields(self):
        rows = samples(200, cpu_util=2, cpu_core_max=5, gpu_util=0, disk_util=0)
        for f in analysis.analyze(rows):
            self.assertEqual(set(f), {"key", "title", "problem", "advice", "share", "seconds", "spans"})
            self.assertEqual(f["title"], analysis.PATTERNS[f["key"]][0])
            self.assertLessEqual(f["share"], 1.0)


class ReportTest(unittest.TestCase):
    def test_no_findings(self):
        self.assertEqual(analysis.report([]), ["Bottlenecks: none found - no single pattern held for 10% of the run."])

    def test_lines_show_mark_share_title_and_advice(self):
        f = {"key": "serial", "title": "T", "problem": True, "advice": "A", "share": 0.5, "seconds": 5,
             "spans": [(0.0, 5.0)]}
        lines = analysis.report([f])
        self.assertEqual(lines[1], "   !  50%  T  [0-5 s]")
        self.assertEqual(lines[2].strip(), "-> A")

    def test_spans_text_longest_first_and_limited(self):
        spans = [(0, 1), (2, 10), (11, 13), (20, 21)]
        self.assertEqual(analysis.spans_text(spans, limit=2), "2-10 s, 11-13 s (+2 more)")
        self.assertEqual(analysis.spans_text([]), "")


if __name__ == "__main__":
    unittest.main()
