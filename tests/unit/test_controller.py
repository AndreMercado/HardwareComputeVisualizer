"""Unit tests for the controller logic in main.py (class App and its helpers).

App is the window's controller: it validates a START request (UC-5..7, UC-13), launches the
workload, classifies how a run ended (UC-9, UC-13, UC-7 alt b), applies the safety rules
(UC-12 telemetry lost, UC-14 thermal abort), routes the agent's events to the log, and saves /
finds recorded runs. These methods are tested on a stand-in object holding only the state each
method reads, so no window is opened; widgets are mocks."""
import collections
import json
import os
import queue
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import _setup  # noqa: F401  (must come before main: data folder)

import main

App = main.App


def fake_app(**state):
    """A stand-in for App with the fields the start/stop/tick code reads (widgets are mocks)."""
    ns = types.SimpleNamespace(
        running=False, selected=None, selected_value=None, remote=None, replay=None,
        hw={"gpu_info": [], "cpu_temp": True, "disks": [], "threads": 8}, local_hw={"machine": "PC"},
        gpu_index=0, disk_index=0, options={p: f"{p} name" for p in main.PARTS},
        test_var=mock.Mock(get=mock.Mock(return_value="2d")),
        program_var=mock.Mock(get=mock.Mock(return_value="")),
        goal_var=mock.Mock(get=mock.Mock(return_value="Render a scene")), goal_ids={"Render a scene": "render"},
        _lhm_probed=time.monotonic(), events=queue.Queue(), sample_times=collections.deque(),
        btn_start=mock.Mock(), btn_stop=mock.Mock(), proc=None, poller=None,
        _fail=mock.Mock(), _log=mock.Mock(), _set_status=mock.Mock(), _sync_controls=mock.Mock(),
        _show_target=mock.Mock(), _set_summary=mock.Mock(), _show_summary=mock.Mock(),
        _clear_readout=mock.Mock(), _remember_program=mock.Mock(), stop_test=mock.Mock(),
        _update_readout=mock.Mock(), _check_thermal=mock.Mock(), _machine_name=mock.Mock(return_value="PC"),
        _link_profile=mock.Mock(), _read_proc=mock.Mock(),
    )
    ns.__dict__.update(state)
    return ns


def bind(ns, *names):
    """Use App's real methods on the stand-in."""
    for n in names:
        setattr(ns, n, types.MethodType(getattr(App, n), ns))
    return ns


class StartValidationTest(unittest.TestCase):
    """start_test(): requests that must be refused with a message, nothing started (UC-13)."""

    def start(self, test, part, **state):
        app = fake_app(selected=part, selected_value=f"{part} name", **state)
        app.test_var.get.return_value = test
        with mock.patch.object(main.subprocess, "Popen") as popen, \
                mock.patch.object(main.telemetry, "Poller") as poller:
            App.start_test(app)
        return app, popen

    def refused(self, app, popen, text):
        app._fail.assert_called_once()
        self.assertIn(text, app._fail.call_args[0][0])
        popen.assert_not_called()
        self.assertFalse(app.running)

    def test_no_part_selected(self):
        self.refused(*self.start("2d", None), "Select a component first")

    def test_test_that_cannot_target_the_part(self):
        self.refused(*self.start("3d", "cpu"), "can't target CPU. It works on: GPU")

    def test_ai_benchmark_is_local_only(self):
        self.refused(*self.start("ai", "ram", remote=mock.Mock()), "runs on this PC only")

    def test_board_needs_an_nvidia_gpu(self):
        self.refused(*self.start("bus", "board"), "PCIe link of an NVIDIA GPU")

    def test_ml_on_gpu_needs_nvidia(self):
        hw = {"gpu_info": [{"kind": "pdh", "index": 0}], "cpu_temp": True, "disks": []}
        self.refused(*self.start("ml", "gpu", hw=hw), "needs an NVIDIA GPU")

    def test_your_code_needs_a_command(self):
        self.refused(*self.start("code", "ram"), "Type the command that starts your program")

    def test_ollama_not_running(self):
        with mock.patch.object(main.agent, "ollama_models", return_value=None):
            self.refused(*self.start("ai", "ram"), "Ollama isn't running")

    def test_no_tool_calling_model(self):
        with mock.patch.object(main.agent, "ollama_models", return_value=["x:1b"]), \
                mock.patch.object(main.agent, "pick_model", return_value=None):
            self.refused(*self.start("ai", "ram"), "No supported model is installed")

    def test_already_running_does_nothing(self):
        app, popen = self.start("2d", "ram", running=True)
        app._fail.assert_not_called()
        popen.assert_not_called()

    def test_no_cpu_temperature_asks_first_and_cancel_starts_nothing(self):  # UC-14 alt a
        hw = {"gpu_info": [], "cpu_temp": False, "disks": []}
        with mock.patch.object(main, "messagebox") as box:
            box.askokcancel.return_value = False
            app, popen = self.start("2d", "cpu", hw=hw)
        self.assertEqual(box.askokcancel.call_args[0][0], "No CPU temperature")
        popen.assert_not_called()
        app._fail.assert_not_called()


class StartLaunchTest(unittest.TestCase):
    def test_2d_on_ram_launches_the_workload_and_poller(self):
        app = fake_app(selected="ram", selected_value="16 GB")
        app.test_var.get.return_value = "2d"
        proc = mock.Mock(stdout=[])
        with mock.patch.object(main.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(main.telemetry, "Poller") as poller:
            App.start_test(app)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:], [os.path.join(main.HERE, "workloads.py"), "ram", str(main.TEST_SECONDS)])
        poller.assert_called_once()
        poller.return_value.start.assert_called_once()
        self.assertTrue(app.running)
        self.assertEqual(app.involved, ("ram", "cpu"))  # target first, then the parts that work with it
        app.btn_start.config.assert_called_with(state="disabled")
        app.btn_stop.config.assert_called_with(state="normal")

    def test_your_code_passes_the_project_python_and_folder(self):
        app = fake_app(selected="cpu", selected_value="CPU", _code_plan=mock.Mock(return_value=("py.exe", "C:\\proj", "note")))
        app.test_var.get.return_value = "code"
        app.program_var.get.return_value = "main.py"
        with mock.patch.object(main.subprocess, "Popen", return_value=mock.Mock(stdout=[])) as popen, \
                mock.patch.object(main.telemetry, "Poller"):
            App.start_test(app)
        self.assertEqual(popen.call_args[0][0][-1], "main.py")
        env = popen.call_args[1]["env"]
        self.assertEqual((env["HCV_PYTHON"], env["HCV_CWD"]), ("py.exe", "C:\\proj"))
        app._remember_program.assert_called_once_with("main.py")


class EndOfRunTest(unittest.TestCase):
    """_tick_running(): how a run that ended by itself is classified."""

    def tick(self, poll=0, elapsed=5.0, **state):
        app = fake_app(running=True, test_key="2d", target="ram", is_ai=False, is_code=False, duration=30,
                       t_start=time.monotonic() - elapsed, wl_error=None, wl_got_data=True, code_exit=None,
                       ai_steps=0, ai_done_text=None, ai_stop_text=None, _input_hint=False,
                       proc=mock.Mock(poll=mock.Mock(return_value=poll), returncode=poll))
        app.__dict__.update(state)
        bind(app, "_drain_events", "_wl_fresh")
        App._tick_running(app, time.monotonic())
        return app

    def test_workload_finished_normally(self):  # UC-9
        self.tick().stop_test.assert_called_once_with("completed")

    def test_workload_that_never_reported_failed_to_start(self):  # UC-13
        app = self.tick(poll=1, wl_got_data=False)
        app.stop_test.assert_called_once_with("failed", summary=False)
        self.assertIn("failed to start: workload exited with code 1", app._fail.call_args[0][0])

    def test_error_after_data_is_a_failed_run(self):
        self.tick(wl_error="GL lost").stop_test.assert_called_once_with("failed — GL lost")

    def test_your_code_reports_how_the_program_exited(self):
        app = self.tick(is_code=True, test_key="code", code_exit="program exited with code 3")
        app.stop_test.assert_called_once_with("program exited with code 3")

    def test_agent_that_called_finish_completed(self):
        self.tick(is_ai=True, test_key="ai", ai_done_text="done").stop_test.assert_called_once_with("completed")

    def test_agent_that_quit_without_finish_is_not_completed(self):
        app = self.tick(is_ai=True, test_key="ai", ai_stop_text="step limit reached (30 steps)")
        self.assertIn("agent stopped without finishing (step limit", app.stop_test.call_args[0][0])

    def test_agent_time_limit(self):  # UC-7 alt b
        app = self.tick(poll=None, elapsed=31, is_ai=True, test_key="ai")
        app.stop_test.assert_called_once_with("time limit reached")

    def test_agent_that_failed_before_any_step(self):
        app = self.tick(is_ai=True, test_key="ai", wl_error="Ollama stopped responding")
        app.stop_test.assert_called_once_with("failed", summary=False)
        app._fail.assert_called_once()

    def test_running_frame_records_a_sample_and_checks_safety(self):
        poller = mock.Mock(latest={"t": 1.0, "cpu_util": 40.0, "ram_util": 50.0, "gpu_util": None})
        app = self.tick(poll=None, poller=poller, last_t=None, wl={"gbps": 20.0}, wl_time=time.monotonic(),
                        samples={}, record=[], peak_gbps=0.0)
        app.stop_test.assert_not_called()
        self.assertEqual(len(app.record), 1)
        row = app.record[0]
        self.assertEqual((row["cpu_util"], row["gbps"]), (40.0, 20.0))
        self.assertNotIn("gpu_util", row)  # unavailable readings are not recorded as numbers
        self.assertEqual(app.peak_gbps, 20.0)
        app._update_readout.assert_called_once()
        app._check_thermal.assert_called_once()

    def test_same_sample_twice_is_recorded_once(self):
        poller = mock.Mock(latest={"t": 1.0, "cpu_util": 40.0})
        app = self.tick(poll=None, poller=poller, last_t=1.0, wl={}, wl_time=0.0, samples={}, record=[],
                        peak_gbps=0.0)
        self.assertEqual(app.record, [])


class SafetyTest(unittest.TestCase):
    def test_cpu_over_the_limit_aborts(self):  # UC-14
        app = fake_app(involved=("cpu", "ram"))
        with mock.patch.object(main, "messagebox") as box:
            App._check_thermal(app, {"cpu_temp": 96.0})
        app.stop_test.assert_called_once_with("aborted — thermal limit (CPU 96 °C)")
        self.assertEqual(box.showwarning.call_args[0][0], "Thermal limit")

    def test_gpu_limit_is_the_lower_of_90_and_the_vendor_slowdown(self):
        app = fake_app(involved=("gpu",))
        with mock.patch.object(main, "messagebox"):
            App._check_thermal(app, {"gpu_temp": 86.0, "gpu_slowdown": 85})
        app.stop_test.assert_called_once()
        app = fake_app(involved=("gpu",))
        with mock.patch.object(main, "messagebox"):
            App._check_thermal(app, {"gpu_temp": 86.0, "gpu_slowdown": None})
        app.stop_test.assert_not_called()

    def test_parts_not_in_the_test_are_not_checked(self):
        app = fake_app(involved=("ram",))
        App._check_thermal(app, {"cpu_temp": 99.0})
        app.stop_test.assert_not_called()

    def readout_app(self, **state):
        app = fake_app(involved=("cpu",), target="cpu", lost_since=None, msg_unavail=set(),
                       stat_val=collections.defaultdict(mock.Mock), _src=lambda k, s: s, _set_src=mock.Mock(),
                       _show_missing=mock.Mock())
        app.__dict__.update(state)
        return app

    def test_telemetry_lost_for_a_second_stops_the_test(self):  # UC-12
        app = self.readout_app()
        App._update_readout(app, {"t": 1.0}, False)
        app.stop_test.assert_not_called()  # the first moment only starts the clock
        app.lost_since -= 1.5
        App._update_readout(app, {"t": 2.0}, False)
        app.stop_test.assert_called_once_with("stopped — CPU telemetry lost")

    def test_one_reading_back_resets_the_clock(self):
        app = self.readout_app(lost_since=time.monotonic() - 5)
        App._update_readout(app, {"t": 1.0, "cpu_util": 10.0}, False)
        self.assertIsNone(app.lost_since)
        app.stop_test.assert_not_called()

    def test_optional_sensors_alone_never_count_as_lost(self):
        app = self.readout_app()
        App._update_readout(app, {"cpu_util": 5.0, "cpu_core_max": 9.0, "cpu_freq": 3000.0}, False)
        self.assertIsNone(app.lost_since)
        self.assertIn("cpu_temp", app.msg_unavail)  # shown as "unavailable", not hidden

    def test_replay_never_stops_for_lost_telemetry(self):
        app = self.readout_app(replay={"part": "cpu"}, lost_since=time.monotonic() - 5)
        App._update_readout(app, {"t": 1.0}, False)
        app.stop_test.assert_not_called()


class StopTest(unittest.TestCase):
    def stop_app(self, **state):
        app = fake_app(running=True, t_start=time.monotonic() - 3, poller=mock.Mock(), proc=mock.Mock(),
                       _kill_tree=mock.Mock(return_value=False), _drain_events=mock.Mock())
        app.__dict__.update(state)
        return app

    def test_stop_ends_the_run_and_shows_the_summary(self):  # UC-8
        app = self.stop_app()
        poller = app.poller
        App.stop_test(app, "stopped by user")
        self.assertFalse(app.running)
        poller.stop.assert_called_once()
        app._show_summary.assert_called_once()
        self.assertEqual(app._show_summary.call_args[0][0], "stopped by user")
        app.btn_start.config.assert_called_with(state="normal")
        app.btn_stop.config.assert_called_with(state="disabled")
        app._clear_readout.assert_called_once()

    def test_force_stop_is_reported(self):  # FR-10
        app = self.stop_app(_kill_tree=mock.Mock(return_value=True))
        App.stop_test(app, "stopped by user")
        self.assertEqual(app._show_summary.call_args[0][0], "stopped by user (force-stopped)")

    def test_replay_end_has_no_summary(self):
        app = self.stop_app(replay={"part": "cpu"})
        App.stop_test(app, "finished")
        app._show_summary.assert_not_called()
        self.assertIsNone(app.replay)

    def test_stop_when_idle_does_nothing(self):
        app = self.stop_app(running=False)
        App.stop_test(app, "x")
        app._kill_tree.assert_not_called()


class EventsTest(unittest.TestCase):
    def test_agent_events_are_counted_and_logged(self):
        app = fake_app(ai_steps=0, ai_blocked=0, ai_done_text=None, ai_stop_text=None, code_exit=None,
                       _input_hint=False)
        for d in ({"ev": "tool", "text": "run_python(...)"}, {"ev": "blocked", "text": "network"},
                  {"ev": "tool", "text": "finish"}, {"ev": "done", "text": "ok"}):
            app.events.put(d)
        App._drain_events(app)
        self.assertEqual((app.ai_steps, app.ai_blocked, app.ai_done_text), (2, 1, "ok"))
        self.assertEqual(app._log.call_count, 4)

    def test_input_hint_is_shown_once(self):
        app = fake_app(_input_hint=False, code_exit=None)
        for _ in range(3):
            app.events.put({"ev": "out", "text": "EOFError: EOF when reading a line"})
        App._drain_events(app)
        hints = [c for c in app._log.call_args_list if "input()" in c[0][0]]
        self.assertEqual(len(hints), 1)

    def test_workload_output_lines_are_sorted(self):
        app = fake_app(wl={}, wl_error=None, wl_got_data=False, wl_time=0.0)
        proc = mock.Mock(stdout=['{"gbps": 30.5}\n', 'garbage\n', '{"ev": "out", "text": "hi"}\n',
                                 '{"error": "boom"}\n'])
        App._read_proc(app, proc)
        self.assertEqual(app.wl, {"gbps": 30.5})
        self.assertTrue(app.wl_got_data)
        self.assertEqual(app.wl_error, "boom")
        self.assertEqual(app.events.get_nowait(), {"ev": "out", "text": "hi"})


class RecordedRunsTest(unittest.TestCase):
    def setUp(self):
        self.runs = tempfile.mkdtemp(prefix="hcv-runs-")
        self.addCleanup(shutil.rmtree, self.runs, True)
        patcher = mock.patch.object(main, "RUNS_DIR", self.runs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, **state):
        app = fake_app(test_key="2d", target="ram", target_name="16 GB", program=None, script="ram",
                       involved=("ram", "cpu"), t_start=time.monotonic() - 30,
                       record=[{"t": 0.0, "gbps": 30.0}, {"t": 0.05, "gbps": 32.0}],
                       samples={"gbps": [30.0, 32.0]})
        app.__dict__.update(state)
        return bind(app, "_save_run", "_previous_run")

    def test_saved_run_has_samples_averages_and_findings(self):
        name = self.app()._save_run("completed", 30.0, "summary text", [{"key": "idle"}])
        self.assertTrue(name.endswith("-2d-ram.json"))
        with open(os.path.join(self.runs, name), encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual(run["averages"], {"gbps": 31.0})
        self.assertEqual(run["result"], "completed")
        self.assertEqual(len(run["samples"]), 2)
        self.assertEqual(run["findings"], [{"key": "idle"}])
        self.assertEqual(run["involved"], ["ram", "cpu"])

    def test_nothing_recorded_saves_nothing(self):
        self.assertIsNone(self.app(record=[])._save_run("failed", 0.0, "", []))
        self.assertEqual(os.listdir(self.runs), [])

    def write(self, name, **run):
        base = {"part_name": "16 GB", "machine": "PC", "program": None, "averages": {"gbps": 1.0}}
        base.update(run)
        with open(os.path.join(self.runs, name), "w", encoding="utf-8") as f:
            json.dump(base, f)

    def test_previous_run_is_the_newest_comparable_one(self):
        self.write("run-20261101-100000-2d-ram.json", averages={"gbps": 10.0})
        self.write("run-20261102-100000-2d-ram.json", averages={"gbps": 20.0})
        self.write("run-20261103-100000-2d-ram.json", machine="other")      # another machine
        self.write("run-20261104-100000-2d-ram.json", part_name="8 GB")     # other RAM
        self.write("run-20261105-100000-2d-ram.json", averages={})          # nothing to compare
        self.write("run-20261106-100000-3d-gpu.json")                       # other test
        with open(os.path.join(self.runs, "run-20261107-100000-2d-ram.json"), "w") as f:
            f.write("{broken")
        self.assertEqual(self.app()._previous_run()["averages"], {"gbps": 20.0})

    def test_no_previous_run(self):
        self.assertIsNone(self.app()._previous_run())


class DialogsTest(unittest.TestCase):
    """While an AI or the editor drives the app, pop-ups go to the log instead of blocking."""

    def test_pop_ups_go_to_the_log_under_ai_control(self):
        d = main._Dialogs()
        d.app = types.SimpleNamespace(control=object(), editor_busy=False, _log=mock.Mock())
        with mock.patch.object(main, "_messagebox") as real:
            d.showerror("Can't start test", "Select a component first")
            d.confirm = True
            self.assertTrue(d.askokcancel("No CPU temperature", "Continue?"))
        real.showerror.assert_not_called()
        real.askokcancel.assert_not_called()
        self.assertEqual([n["kind"] for n in d.notes], ["error", "question"])

    def test_real_pop_ups_otherwise(self):
        d = main._Dialogs()
        d.app = types.SimpleNamespace(control=None, editor_busy=False)
        with mock.patch.object(main, "_messagebox") as real:
            d.showwarning("Thermal limit", "CPU reached 96 °C")
        real.showwarning.assert_called_once_with("Thermal limit", "CPU reached 96 °C")


class ReplayAndTablesTest(unittest.TestCase):
    def test_replay_gives_the_sample_for_the_playback_time(self):
        rows = [{"t": 0.0, "v": 0}, {"t": 0.5, "v": 1}, {"t": 1.0, "v": 2}]
        with mock.patch.object(main.time, "monotonic", return_value=100.0):
            r = main.Replay(rows)
            r.start()
        for now, want in ((100.0, 0), (100.6, 1), (101.2, 2), (150.0, 2)):
            with mock.patch.object(main.time, "monotonic", return_value=now):
                self.assertEqual(r.latest["v"], want)

    def test_color_helpers(self):
        self.assertEqual(main.mix("#000000", "#ffffff", 0.5), "#7f7f7f")
        self.assertEqual(main.mix("#102030", "#ffffff", -1), "#102030")  # clamped
        self.assertEqual(main.lighten("#000000", 1.0), "#ffffff")

    def test_every_test_targets_real_parts_and_observed_parts_exist(self):
        for key, t in main.TESTS.items():
            self.assertLessEqual(set(t["targets"]), set(main.PARTS), key)
            for script in t["targets"].values():
                self.assertLessEqual(set(main.OBSERVED[script]), set(main.PARTS), script)

    def test_every_stat_belongs_to_a_part_or_the_workload(self):
        for key, _, _, part, _ in main.STATS:
            self.assertIn(part, set(main.PARTS) | {"wl"}, key)
        self.assertLessEqual(main.OPTIONAL, {s[0] for s in main.STATS})


if __name__ == "__main__":
    unittest.main()
