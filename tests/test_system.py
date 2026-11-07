import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from dev_auto.adapters import Adapter, Result, check_interfaces, cli_env, detect, vet_codex_hook_sources
from dev_auto.cli import status
from dev_auto.demo import fixture
from dev_auto.handoff import prompt, validate
from dev_auto.orchestrator import Orchestrator
from dev_auto.storage import ProjectLock, SafetyError, Store, atomic, read_json, redact
from dev_auto.tool_guard import decision, shell_allowed
from dev_auto.usage import Availability, choose


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dev-auto-test-")
        self.folder = Path(self.temp.name)
        self.store, self.inventory = fixture(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, **kwargs):
        return Orchestrator(self.store, inventory=self.inventory, quota_reader=lambda: {"limits": {}},
                            poll=0.1, announce=lambda *_: None, **kwargs)

    def test_full_cycle_real_processes_mock_providers(self):
        state = self.engine().run("Implementar sem apagar trabalho anterior")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(json.loads((self.store.project / "calls.json").read_text()), ["claude", "codex", "claude"])
        self.assertEqual(len(state["handoff"]["completed"]), 2)
        self.assertIn("progress.txt", state["handoff"]["modified_files"])
        self.assertEqual((self.store.project / "existing.txt").read_text(), "ALTERAÇÃO PREEXISTENTE: preservar\n")
        self.assertIn("codex: continuou", (self.store.project / "progress.txt").read_text())
        self.assertEqual(Store(self.store.project, self.store.home).load()["status"], "completed")
        self.assertEqual(read_json(self.store.path / "handoff.json")["objective"], state["objective"])
        self.assertTrue((self.store.path / "HANDOFF.md").exists())
        events = [json.loads(x)["event"] for x in (self.store.path / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events.count("handoff"), 2)

    def test_cli_detection_and_auth_fields_whitelisted(self):
        with patch.dict(os.environ, {"PATH": str(self.folder / "bin") + os.pathsep + os.environ["PATH"]}):
            info = check_interfaces(detect())
        self.assertTrue(all(i["interfaces_ok"] and i["subscription_login"] for i in info.values()))
        self.assertNotIn("DO-NOT-LOG", json.dumps(info))

    def test_persistent_atomic_state_permissions(self):
        self.store.save({"schema": 1, "project": str(self.store.project), "status": "paused"})
        self.assertEqual(self.store.load()["status"], "paused")
        self.assertEqual(self.store.state_path.stat().st_mode & 0o777, 0o600)

    def test_lock_prevents_second_controller(self):
        with ProjectLock(self.store.lock_path):
            with self.assertRaises(SafetyError):
                with ProjectLock(self.store.lock_path):
                    self.fail("second lock acquired")
            code = 'from dev_auto.storage import ProjectLock; import sys;\nwith ProjectLock(sys.argv[1]): pass'
            child = subprocess.run([sys.executable, "-c", code, str(self.store.lock_path)], capture_output=True)
            self.assertNotEqual(child.returncode, 0)

    def test_subdirectory_uses_same_project_lock(self):
        sub = self.store.project / "src"
        sub.mkdir()
        other = Store(sub, self.store.home)
        self.assertEqual(other.lock_path, self.store.lock_path)
        self.assertEqual(other.path, self.store.path)

    def test_custom_state_homes_share_repository_lock(self):
        other = Store(self.store.project, self.folder / "other-state-home")
        self.assertEqual(other.lock_path, self.store.lock_path)
        mode = self.store.lock_path.parent.stat().st_mode
        with ProjectLock(self.store.lock_path):
            with self.assertRaises(SafetyError):
                with ProjectLock(other.lock_path):
                    self.fail("different state home bypassed repository lock")
        self.assertEqual(self.store.lock_path.parent.stat().st_mode, mode)

    def test_backup_contains_existing_dirty_and_staged_data(self):
        subprocess.run(["git", "-C", str(self.store.project), "add", "existing.txt"], check=True)
        (self.store.project / "existing.txt").write_text("dirty after stage")
        checkpoint = self.store.checkpoint("test")
        blob = checkpoint["files"]["existing.txt"]["sha256"]
        self.assertEqual((self.store.path / "blobs" / blob).read_text(), "dirty after stage")
        self.assertTrue((self.store.path / "checkpoints" / checkpoint["id"] / "index.backup").exists())
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.store.project), "show", ":existing.txt"]).decode(), "ALTERAÇÃO PREEXISTENTE: preservar\n")

    def test_threshold_does_not_use_context_tokens(self):
        usage = Availability()
        usage.claude_event({"type": "result", "usage": {"input_tokens": 90000}})
        self.assertIsNone(usage.used_percent)
        self.assertEqual(usage.claude_event({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "utilization": .91, "resetsAt": time.time() + 1000}}), "near_limit")
        self.assertAlmostEqual(usage.used_percent, 91)
        self.assertFalse(usage.eligible())

    def test_fallback_and_renewal_are_not_assumed_success(self):
        usage = Availability()
        usage.reject(now=100)
        self.assertIsNone(usage.resets_at)
        self.assertFalse(usage.eligible(101))
        self.assertTrue(usage.eligible(1001))
        self.assertEqual(usage.status, "limited")
        usage.success(now=1001)
        self.assertEqual(usage.status, "available")

    def test_codex_two_windows_and_unknown_on_missing(self):
        usage = Availability()
        usage.codex_snapshot({"rateLimits": {"primary": {"usedPercent": 5}, "secondary": {"usedPercent": 100, "resetsAt": time.time() + 10000}}})
        self.assertFalse(usage.eligible())
        self.assertEqual(usage.used_percent, 100)
        usage.codex_snapshot({"rateLimits": {"primary": {"usedPercent": 4}}})
        self.assertTrue(usage.eligible())

    def test_both_exhausted_selects_neither(self):
        items = {name: Availability() for name in ["claude", "codex"]}
        for value in items.values():
            value.reject()
        self.assertIsNone(choose(items))

    def test_pause_safe_and_resume_preserves_objective(self):
        self.store.request("pause")
        first = self.engine().run("persistir objetivo")
        self.assertEqual(first["status"], "paused")
        self.assertFalse((self.store.project / "calls.json").exists())
        final = self.engine().run()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["objective"], "persistir objetivo")

    def test_manual_switch_is_persistent_and_safe(self):
        (self.store.project / "scenario.json").write_text('{"mode":"normal"}')
        self.store.request("switch", "codex")
        state = self.engine().run("teste troca")
        self.assertEqual(state["last_agent"], "codex")
        self.assertEqual(state["preferred"], "codex")

    def test_new_task_cannot_overwrite_pending_objective(self):
        self.store.request("pause")
        self.engine().run("original")
        with self.assertRaises(SafetyError):
            self.engine().run("substituição")
        self.assertEqual(self.store.load()["objective"], "original")

    def test_completed_task_does_not_repeat(self):
        (self.store.project / "scenario.json").write_text('{"mode":"normal"}')
        self.engine().run("objetivo")
        calls = (self.store.project / "calls.json").read_text()
        self.engine().run()
        self.assertEqual((self.store.project / "calls.json").read_text(), calls)

    def test_interrupted_state_recovery(self):
        self.store.request("pause")
        state = self.engine().run("recover")
        state["status"], state["active_agent"] = "running", "claude"
        state["before_step"] = self.store.checkpoint("crash-before")
        self.store.save(state)
        (self.store.project / "survives.txt").write_text("partial work")
        (self.store.project / "scenario.json").write_text('{"mode":"normal"}')
        final = self.engine().run()
        self.assertEqual(final["status"], "completed")
        self.assertIn("survives.txt", final["handoff"]["modified_files"])
        self.assertEqual((self.store.project / "survives.txt").read_text(), "partial work")

    def test_invalid_protocol_does_not_mark_task_complete(self):
        (self.store.project / "scenario.json").write_text('{"mode":"invalid"}')
        with ProjectLock(self.store.lock_path) as lock:
            result = Adapter("codex", self.inventory["codex"]["path"], self.store.path).run(
                self.store.project, "test", lock.fd, Availability(), lambda: False, timeout=5)
        self.assertEqual(result.kind, "invalid_report")

    def test_timeout_stops_agent_before_lock_release(self):
        (self.store.project / "scenario.json").write_text('{"mode":"sleep","detached_child":true}')
        with ProjectLock(self.store.lock_path) as lock:
            result = Adapter("claude", self.inventory["claude"]["path"], self.store.path).run(
                self.store.project, "test", lock.fd, Availability(), lambda: False, timeout=.3)
        self.assertEqual(result.kind, "timeout")
        pid = int((self.store.project / "agent.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(ProjectLock.busy(self.store.lock_path))
        if sys.platform == "darwin":
            from dev_auto.guardian import identity
            self.assertIsNone(identity(int((self.store.project / "detached.pid").read_text())))

    def test_both_exhausted_waits_and_resumes_after_renewal(self):
        seen, calls = [], []
        engine = None
        class Fake:
            def __init__(self, name, *args):
                self.name = name
            def run(self, project, prompt, lock, usage, heartbeat, **kwargs):
                calls.append(self.name)
                if len(calls) <= 2:
                    usage.reject()
                    return Result("limited")
                report = {"done": True, "blocked": False, "summary": "ok", "decisions": [], "completed": ["feito"], "pending": [], "next_steps": [], "problems": [], "tests": ["PASS"]}
                return Result("ok", report)
        def quota():
            seen.append(engine.state["status"])
            if engine.state["status"] == "waiting_quota":
                engine.usage["claude"].success()
            return {"limits": {}}
        engine = Orchestrator(self.store, adapter_factory=Fake, inventory=self.inventory,
                              quota_reader=quota, poll=.01, announce=lambda *_: None)
        state = engine.run("esperar cotas")
        self.assertEqual(state["status"], "completed")
        self.assertIn("waiting_quota", seen)
        self.assertEqual(calls, ["claude", "codex", "claude"])

    def test_sigkill_controller_guardian_stops_writer(self):
        (self.store.project / "scenario.json").write_text('{"mode":"sleep"}')
        code = '''from dev_auto.storage import Store, ProjectLock
from dev_auto.adapters import Adapter
from dev_auto.usage import Availability
import sys
s=Store(sys.argv[1],sys.argv[2])
with ProjectLock(s.lock_path) as lock:
 Adapter("claude",sys.argv[3],s.path).run(s.project,"test",lock.fd,Availability(),lambda:False,timeout=60)
'''
        p = subprocess.Popen([sys.executable, "-c", code, str(self.store.project), str(self.store.home), self.inventory["claude"]["path"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not (self.store.project / "agent.pid").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.store.project / "agent.pid").exists())
            pid = int((self.store.project / "agent.pid").read_text())
            self.assertTrue(ProjectLock.busy(self.store.lock_path))
            p.kill()
            p.wait()
            deadline = time.monotonic() + 10
            while ProjectLock.busy(self.store.lock_path) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(ProjectLock.busy(self.store.lock_path))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertEqual((self.store.project / "partial.txt").read_text(), "progresso antes da interrupção")
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()

    def test_credential_environment_removed(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sensitive", "ANTHROPIC_API_KEY": "sensitive", "CLAUDECODE": "1"}):
            clean = cli_env()
        self.assertNotIn("OPENAI_API_KEY", clean)
        self.assertNotIn("ANTHROPIC_API_KEY", clean)
        self.assertNotIn("CLAUDECODE", clean)

    def test_redaction_and_no_raw_tool_logs(self):
        text = redact('access_token=private-value\nBearer something-private\nsk-1234567890123456789\neyJabc.abc.signature')
        for secret in ["private-value", "something-private", "sk-123", "eyJabc"]:
            self.assertNotIn(secret, text)

    def test_tool_guard_blocks_destructive_and_escape_commands(self):
        for command in ["rm -rf src", "git reset --hard", "git clean -fd", "git push", "sudo test", "python3 -c 'print(1)'", "cat ~/.codex/auth.json", "echo x > file", "npm test &", "npm test; rm foo", "$(rm file)", "curl example.com", "git diff --output stolen"]:
            self.assertFalse(shell_allowed(command), command)
        for command in ["git status --short", "git diff --stat", "rg 'hello' src", "python3 -m unittest discover -s tests", "npm test", "npm run build", "git status && git diff --stat", "sed -n '1,200p' greet.py", "find . -name AGENTS.md"]:
            self.assertTrue(shell_allowed(command), command)
        self.assertFalse(decision({"tool_name": "apply_patch", "tool_input": {"command": "*** Delete File: app.py"}}))
        self.assertFalse(decision({"tool_name": "Agent", "tool_input": {}}))

    def test_corrupt_state_stops_without_overwrite(self):
        self.store.state_path.write_text("{bad json")
        with self.assertRaises(json.JSONDecodeError):
            self.engine().run("do work")
        self.assertEqual(self.store.state_path.read_text(), "{bad json")

    def test_background_command_status_logs_and_completion(self):
        (self.store.project / "scenario.json").write_text('{"mode":"normal"}')
        env = {**os.environ, "DEV_AUTO_HOME": str(self.store.home),
               "PATH": str(self.folder / "bin") + os.pathsep + os.environ["PATH"]}
        result = subprocess.run([sys.executable, "-m", "dev_auto", "-C", str(self.store.project),
                                 "--background", "uma tarefa simulada"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Controlador iniciado", result.stdout)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.store.load()
            if state and state["status"] == "completed":
                break
            time.sleep(.05)
        self.assertEqual(self.store.load()["status"], "completed")
        view = subprocess.run([sys.executable, "-m", "dev_auto", "-C", str(self.store.project), "status", "--json"], capture_output=True, text=True, env=env)
        self.assertEqual(json.loads(view.stdout)["state"], "completed")
        logs = subprocess.run([sys.executable, "-m", "dev_auto", "-C", str(self.store.project), "logs"], capture_output=True, text=True, env=env)
        self.assertIn("task_completed", logs.stdout)

    def test_non_git_project_is_supported_without_initialization(self):
        folder = self.folder / "not-git"
        folder.mkdir()
        store = Store(folder, self.store.home)
        self.assertEqual(store.project, folder.resolve())
        self.assertFalse(store.has_git)
        self.assertFalse((folder / ".git").exists())

    def test_external_codex_hooks_refused_without_modification(self):
        folder = self.store.project / ".codex"
        folder.mkdir()
        content = '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"external-tool"}]}]}}'
        (folder / "hooks.json").write_text(content)
        with self.assertRaises(SafetyError):
            vet_codex_hook_sources(self.store.project)
        self.assertEqual((folder / "hooks.json").read_text(), content)


if __name__ == "__main__":
    unittest.main()
