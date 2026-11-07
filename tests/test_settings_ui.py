from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dev_auto.adapters import Adapter
from dev_auto.adapters import Result
from dev_auto.cli import main, parser, status, show_status
from dev_auto.demo import fixture
from dev_auto.orchestrator import Orchestrator
from dev_auto.settings import (DEFAULTS, apply_overrides, cli_arguments, load_settings,
                               save_settings, session_settings, validate_settings)
from dev_auto.storage import ProjectLock, SafetyError
from dev_auto.ui import TerminalUI


CATALOG = [{"model": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "efforts": ["low", "medium", "high", "xhigh", "max"]}]


class SettingsUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dev-auto-ui-test-")
        self.folder = Path(self.temp.name)
        self.store, self.inventory = fixture(self.folder)
        (self.store.project / "scenario.json").write_text('{"mode":"normal","capture_options":true}')

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, **kwargs):
        return Orchestrator(self.store, inventory=self.inventory, quota_reader=lambda: {"limits": {}},
                            announce=lambda *_: None, **kwargs)

    def test_choices_and_task_without_directory_question(self):
        prompts, output = [], []
        answers = iter(["2", "opus", "high", "1", "xhigh", "implemente o login"])
        def read(text):
            prompts.append(text)
            return next(answers)
        ui = TerminalUI(read=read, write=output.append, color=False)
        ui.header(self.store.project)
        selected = ui.configure(deepcopy(DEFAULTS), catalog=CATALOG)
        task = ui.task()
        self.assertEqual(selected["primary"], "codex")
        self.assertEqual(selected["agents"]["claude"], {"model": "opus", "effort": "high"})
        self.assertEqual(selected["agents"]["codex"], {"model": "gpt-6.1-sol", "effort": "xhigh"})
        self.assertEqual(task, "implemente o login")
        self.assertFalse(any("pasta" in p.lower() for p in prompts))
        self.assertIn("1º  Codex", "\n".join(output))

    def test_enter_preserves_explicit_model_and_effort(self):
        answers = iter([""] * 5)
        ui = TerminalUI(read=lambda _: next(answers), write=lambda _: None, color=False)
        self.assertEqual(ui.configure(deepcopy(DEFAULTS), catalog=CATALOG), DEFAULTS)

    def test_model_and_effort_flags_are_explicit_for_each_cli(self):
        for agent in ["claude", "codex"]:
            adapter = Adapter(agent, self.inventory[agent]["path"], self.store.path)
            adapter.configure({"model": "example-model", "effort": "high"})
            command = adapter.command()
            self.assertEqual(command[command.index("--model") + 1], "example-model")
            if agent == "claude":
                self.assertEqual(command[command.index("--effort") + 1], "high")
            else:
                self.assertIn('model_reasoning_effort="high"', command)

    def test_default_options_do_not_pass_fake_values(self):
        for agent in ["claude", "codex"]:
            adapter = Adapter(agent, self.inventory[agent]["path"], self.store.path)
            adapter.configure({"model": None, "effort": None})
            command = adapter.command()
            self.assertNotIn("--model", command)
            self.assertNotIn("--effort", command)
            self.assertFalse(any("model_reasoning_effort=" in part for part in command))

    def test_settings_saved_separately_per_project(self):
        selected = deepcopy(DEFAULTS)
        selected["primary"] = "codex"
        save_settings(self.store, selected)
        self.assertEqual(load_settings(self.store), selected)
        self.assertEqual((self.store.path / "preferences.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.store.project / ".codex/config.toml").exists())
        self.assertFalse((self.store.project / ".claude/settings.json").exists())

    def test_parser_and_background_roundtrip(self):
        selected = deepcopy(DEFAULTS)
        selected["primary"] = "codex"
        selected["agents"]["claude"] = {"model": "claude-sonnet-5-5", "effort": "high"}
        parsed = parser().parse_args([*cli_arguments(selected), "tarefa"])
        self.assertEqual(apply_overrides(DEFAULTS, parsed), selected)

    def test_invalid_settings_fail_without_starting_agents(self):
        for bad in ["ultra", "not-an-effort"]:
            data = deepcopy(DEFAULTS)
            data["agents"]["codex"]["effort"] = bad
            with self.assertRaises(SafetyError):
                validate_settings(data)
        data = deepcopy(DEFAULTS)
        data["agents"]["claude"]["model"] = "--dangerous"
        with self.assertRaises(SafetyError):
            validate_settings(data)

    def test_codex_can_be_primary_and_receives_its_configuration(self):
        selected = deepcopy(DEFAULTS)
        selected["primary"] = "codex"
        selected["agents"]["codex"]["effort"] = "high"
        state = self.engine().run("codex primeiro", settings=selected)
        self.assertEqual(json.loads((self.store.project / "calls.json").read_text()), ["codex"])
        self.assertEqual(json.loads((self.store.project / "codex-options.json").read_text()), {"model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual(state["settings"], selected)
        view = status(self.store)
        self.assertEqual(view["primary_agent"], "codex")
        self.assertEqual(view["secondary_agent"], "claude")
        self.assertEqual(view["settings"]["agents"]["codex"]["effort"], "high")

    def test_claude_receives_selected_model_and_effort(self):
        selected = deepcopy(DEFAULTS)
        selected["agents"]["claude"] = {"model": "opus", "effort": "xhigh"}
        self.engine().run("claude primeiro", settings=selected)
        self.assertEqual(json.loads((self.store.project / "claude-options.json").read_text()), {"model": "opus", "effort": "xhigh"})

    def test_resume_preserves_selected_models_and_order(self):
        selected = deepcopy(DEFAULTS)
        selected["primary"] = "codex"
        selected["agents"]["codex"]["effort"] = "high"
        self.store.request("pause")
        self.engine().run("objetivo salvo", settings=selected)
        state = self.engine().run()
        self.assertEqual(state["settings"], selected)
        self.assertEqual(state["last_agent"], "codex")
        self.assertEqual(state["objective"], "objetivo salvo")

    def test_legacy_sessions_keep_defaults_until_configured(self):
        self.assertEqual(session_settings({})["agents"]["claude"], {"model": None, "effort": None})

    def test_codex_primary_falls_back_then_reclaims_after_renewal(self):
        selected = deepcopy(DEFAULTS)
        selected["primary"] = "codex"
        calls = []
        class Fake:
            def __init__(self, name, *args):
                self.name = name
            def run(self, project, context, lock, usage, tick, **kwargs):
                calls.append(self.name)
                if len(calls) == 1:
                    usage.reject()
                    return Result("limited")
                complete = len(calls) == 3
                if self.name == "codex":
                    self_outer.assertIn("Etapa do Claude concluída", context)
                return Result("ok", {"done": complete, "blocked": False, "summary": "continuar", "decisions": [],
                    "completed": ["Etapa do Claude concluída"] if self.name == "claude" else ["Finalizado"],
                    "pending": [] if complete else ["finalizar"], "next_steps": [], "problems": [], "tests": ["PASS"]})
        self_outer = self
        engine = self.engine(adapter_factory=Fake, poll=0)
        def quota():
            if len(calls) >= 2:
                return {"limits": {"rateLimits": {"primary": {"usedPercent": 1}}}}
            return {"limits": {}}
        engine.quota_reader = quota
        state = engine.run("Ciclo inverso", settings=selected)
        self.assertEqual(calls, ["codex", "claude", "codex"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["last_handoff"]["to"], "codex")

    def test_whole_cli_uses_cwd_and_interactive_settings(self):
        answers = iter(["2", "sonnet", "medium", "1", "high", "minha tarefa"])
        prompts = []
        def read(text):
            prompts.append(text)
            return next(answers)
        def run(engine, objective, settings=None):
            self.assertEqual(engine.store.project, self.store.project)
            self.assertEqual(objective, "minha tarefa")
            self.assertEqual(settings["primary"], "codex")
            self.assertEqual(settings["agents"]["codex"]["effort"], "high")
            return {"status": "completed"}
        with patch("dev_auto.cli.Path.cwd", return_value=self.store.project), \
             patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=read), \
             patch("dev_auto.ui.model_catalog", return_value=CATALOG), \
             patch("dev_auto.cli.Orchestrator.run", autospec=True, side_effect=run), \
             patch.dict(os.environ, {"DEV_AUTO_HOME": str(self.store.home)}), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main([]), 0)
        self.assertFalse(any("pasta" in p.lower() for p in prompts))
        self.assertEqual(load_settings(self.store)["primary"], "codex")

    def test_ui_cancel_starts_nothing(self):
        ui = TerminalUI(read=lambda _: "/sair", write=lambda _: None, color=False)
        self.assertIsNone(ui.task())

    def test_background_keeps_selected_order_model_and_effort(self):
        env = {**os.environ, "DEV_AUTO_HOME": str(self.store.home),
               "PATH": str(self.folder / "bin") + os.pathsep + os.environ["PATH"]}
        command = [sys.executable, "-m", "dev_auto", "--background", "--first", "codex", "--codex-effort", "high", "tarefa em background"]
        done = subprocess.run(command, cwd=self.store.project, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        import time
        until = time.monotonic() + 15
        while time.monotonic() < until:
            state = self.store.load()
            if state and state["status"] == "completed":
                break
            time.sleep(.05)
        self.assertEqual(self.store.load()["settings"]["primary"], "codex")
        self.assertEqual(self.store.load()["status"], "completed")
        self.assertEqual(json.loads((self.store.project / "codex-options.json").read_text())["effort"], "high")
