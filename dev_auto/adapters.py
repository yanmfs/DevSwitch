from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import subprocess
import sys
import time
import tomllib

from .handoff import SCHEMA, validate
from .storage import SafetyError, atomic, find_git_root


def cli_env():
    env = os.environ.copy()
    # Never accidentally select metered API credentials or a third-party provider.
    for key in list(env):
        if (key.startswith(("ANTHROPIC_", "OPENAI_", "CODEX_API_", "CLAUDE_CODE_USE_"))
                or key in {"CLAUDECODE", "CLAUDE_CODE_OAUTH_TOKEN", "API_TIMEOUT_MS"}):
            env.pop(key, None)
    return env


def detect():
    found = {}
    for name in ("claude", "codex"):
        executable = shutil.which(name)
        item = {"path": executable, "version": None, "subscription_login": False}
        if executable:
            try:
                version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30, env=cli_env())
                item["version"] = version.stdout.strip().splitlines()[0] if version.stdout.strip() else "unknown"
                if name == "claude":
                    auth = subprocess.run([executable, "auth", "status"], capture_output=True, text=True, timeout=30, env=cli_env())
                    data = json.loads(auth.stdout)
                    item["subscription_login"] = bool(data.get("loggedIn") and data.get("authMethod") == "claude.ai")
                    item["auth_method"] = data.get("authMethod", "unknown")
                else:
                    auth = subprocess.run([executable, "login", "status"], capture_output=True, text=True, timeout=30, env=cli_env())
                    item["subscription_login"] = auth.returncode == 0 and "ChatGPT" in (auth.stdout + auth.stderr)
                    item["auth_method"] = "chatgpt" if item["subscription_login"] else "unverified"
            except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
                item["error"] = "local_check_failed"
        found[name] = item
    return found


def check_interfaces(found):
    required = {
        "claude": (["--help"], ["--output-format", "--json-schema", "--permission-prompts", "--restricted"]),
        "codex": (["exec", "--help"], ["--json", "--output-schema", "--ignore-user-config", "--sandbox", "--dangerously-bypass-hook-trust", "--skip-git-repo-check"]),
    }
    for name, (args, flags) in required.items():
        if not found[name]["path"]:
            continue
        result = subprocess.run([found[name]["path"], *args], capture_output=True, text=True, timeout=30, env=cli_env())
        missing = [flag for flag in flags if flag not in result.stdout]
        found[name]["interfaces_ok"] = result.returncode == 0 and not missing
        found[name]["missing_flags"] = missing
    return found


class CodexAccount:
    """Documented JSON-RPC over local stdio; never reads an auth file or token."""
    def __init__(self, executable):
        self.executable = executable

    def read(self, stop_event=None):
        p = subprocess.Popen([self.executable, "app-server", "--listen", "stdio://",
                              "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=cli_env(), start_new_session=True)
        buffer = b""
        seq = 0
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)

        def rpc(method, params=None):
            nonlocal buffer, seq
            seq += 1
            message = {"id": seq, "method": method}
            if params is not None:
                message["params"] = params
            p.stdin.write((json.dumps(message) + "\n").encode())
            p.stdin.flush()
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    raise SafetyError("Monitoramento de cotas encerrado.")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("id") == seq:
                        if "error" in event:
                            raise SafetyError("Consulta oficial de cotas indisponível; usar fallback por erros do CLI.")
                        return event["result"]
                if sel.select(0.2):
                    block = os.read(p.stdout.fileno(), 65536)
                    if not block:
                        break
                    buffer += block
            raise SafetyError("Consulta de cotas expirou; percentual permanece desconhecido.")

        try:
            from . import __version__
            rpc("initialize", {"clientInfo": {"name": "dev_auto", "title": "DevSwitch", "version": __version__}})
            p.stdin.write(b'{"method":"initialized"}\n')
            p.stdin.flush()
            account = rpc("account/read", {"refreshToken": False}).get("account") or {}
            if account.get("type") != "chatgpt":
                raise SafetyError("Codex precisa estar autenticado com ChatGPT; API paga não será usada.")
            # Explicit whitelist prevents email/account identifiers being persisted.
            raw = rpc("account/rateLimits/read")
            buckets = raw.get("rateLimitsByLimitId") or {}
            bucket = buckets.get("codex") or raw.get("rateLimits") or {}
            safe = {k: bucket[k] for k in ("limitId", "primary", "secondary", "rateLimitReachedType", "spendControlReached") if k in bucket}
            limits = {"rateLimits": safe}
            if isinstance(raw.get("ordinaryUsageAllowed"), bool):
                limits["ordinaryUsageAllowed"] = raw["ordinaryUsageAllowed"]
            return {"auth_method": "chatgpt", "plan": account.get("planType"), "limits": limits}
        finally:
            sel.close()
            p.stdin.close()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            p.stdout.close()


@dataclass
class Result:
    kind: str
    report: dict | None = None
    returncode: int = 0


class Adapter:
    def __init__(self, name, executable, folder):
        self.name, self.executable, self.folder = name, executable, Path(folder)
        self.model, self.effort = None, None
        self.runtime = {}
        self.schema = self.folder / "report-schema.json"
        atomic(self.schema, SCHEMA)

    def configure(self, options):
        self.model, self.effort = options["model"], options["effort"]
        self.runtime = {"configured_model": self.model, "configured_effort": self.effort,
                        "reported_model": None, "reported_effort": None}

    def command(self, project=None):
        guard = shlex.join([sys.executable, str(Path(__file__).with_name("tool_guard.py"))])
        hooks = [{"matcher": ".*", "hooks": [{"type": "command", "command": guard, "timeout": 5}]}]
        selected = ["--model", self.model] if self.model else []
        if self.name == "claude":
            if self.effort:
                selected += ["--effort", self.effort]
            return [self.executable, "-p", "--output-format", "stream-json", "--verbose",
                    *selected,
                    "--json-schema", json.dumps(SCHEMA), "--max-turns", "16",
                    "--restricted", "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--settings", json.dumps({"hooks": {"PreToolUse": hooks}}),
                    "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                    "--tools", "Read,Edit,Write,Glob,Grep,Bash",
                    "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash",
                    "--disallowedTools", "Bash(rm *),Bash(git reset *),Bash(git clean *),Bash(git checkout *),Bash(git restore *),Bash(git stash *),Bash(git push *),Bash(sudo *)"]
        if self.effort:
            selected += ["-c", "model_reasoning_effort=" + json.dumps(self.effort)]
        if project is not None and find_git_root(project) is None:
            selected += ["--skip-git-repo-check"]
        return [self.executable, "-a", "never", "exec", *selected, "--ignore-user-config", "--dangerously-bypass-hook-trust",
                "--sandbox", "workspace-write", "--json", "--color", "never",
                "--output-schema", str(self.schema), "-c", 'forced_login_method="chatgpt"',
                "-c", 'model_provider="openai"', "-c", "features.multi_agent=false",
                "-c", "sandbox_workspace_write.network_access=false", "-c", "features.hooks=true",
                "-c", "hooks.PreToolUse=" + _toml(hooks), "-"]

    def run(self, project, prompt, lock_fd, usage, heartbeat, timeout=1200):
        if self.name == "codex":
            vet_codex_hook_sources(Path(project))
        lock_fds = (lock_fd,) if isinstance(lock_fd, int) else tuple(lock_fd)
        life_read, life_write = os.pipe()
        wrapper = [sys.executable, "-m", "dev_auto.guardian", ",".join(map(str, lock_fds)), str(life_read), *self.command(project)]
        p = subprocess.Popen(wrapper, cwd=project, env={**cli_env(), "DEV_AUTO_PROJECT": str(project),
                             "DEV_AUTO_AGENT": self.name,
                             "DEV_AUTO_TOOL_AUDIT": str(self.folder / "tool-audit.jsonl")}, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             pass_fds=(*lock_fds, life_read), start_new_session=True)
        os.close(life_read)
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)
        buffer = b""
        report, kind = None, None
        saw_success = False
        deadline = time.monotonic() + timeout
        next_heartbeat = 0

        def accept(line):
            nonlocal report, kind, saw_success
            try:
                event = json.loads(line)
            except ValueError:
                return
            if self.name == "claude":
                if event.get("type") == "system" and event.get("subtype") == "init":
                    model = event.get("model")
                    if isinstance(model, str) and len(model) < 140:
                        self.runtime["reported_model"] = model
                    effort = event.get("effort") or event.get("effortLevel")
                    if effort in {"low", "medium", "high", "xhigh", "max"}:
                        self.runtime["reported_effort"] = effort
                quota = usage.claude_event(event)
                if quota == "rejected":
                    kind = "limited"
                if event.get("type") == "result":
                    value = event.get("structured_output")
                    if validate(value):
                        report = value
                    if event.get("is_error"):
                        error_text = json.dumps(event.get("errors", [])) + str(event.get("result", ""))
                        if _rate_error(error_text):
                            usage.reject(source="claude_result_error")
                            kind = "limited"
                        elif "authentication" in error_text.lower() or "not logged in" in error_text.lower():
                            kind = "auth_required"
                    else:
                        saw_success = True
            else:
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        try:
                            value = json.loads(item.get("text", ""))
                            if validate(value):
                                report = value
                        except ValueError:
                            pass
                if event.get("type") == "turn.completed":
                    saw_success = True
                if event.get("type") in {"error", "turn.failed"}:
                    error = event.get("error") or event.get("message", "")
                    error_text = json.dumps(error)
                    if _rate_error(error_text):
                        usage.reject(source="codex_exec_error")
                        kind = "limited"
                    elif any(x in error_text.lower() for x in ["unauthorized", "authentication", "not logged in"]):
                        kind = "auth_required"

        try:
            # Prompts go over stdin, never command line or operation logs.
            try:
                p.stdin.write(prompt.encode())
            except BrokenPipeError:
                pass
            finally:
                p.stdin.close()
            while True:
                if time.monotonic() >= next_heartbeat:
                    if heartbeat():
                        kind = "interrupted"
                        break
                    next_heartbeat = time.monotonic() + 1
                if time.monotonic() >= deadline:
                    kind = "timeout"
                    break
                if kind == "limited":
                    break
                ready = sel.select(0.2)
                if ready:
                    block = os.read(p.stdout.fileno(), 65536)
                    if not block:
                        break
                    buffer += block
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        accept(line)
                    if len(buffer) > 8 * 1024 * 1024:
                        # Fail closed on an invalid or unbounded protocol line.
                        kind = "protocol_error"
                        break
                elif p.poll() is not None:
                    break
            if buffer:
                accept(buffer)
        finally:
            os.close(life_write)
            # The guardian stops the entire agent group before releasing its lock.
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                raise SafetyError("Supervisor ainda retém o lock. Nenhum segundo agente será iniciado.")
            sel.close()
            p.stdout.close()
        if kind:
            return Result(kind, report, p.returncode)
        if p.returncode == 0 and report is not None and saw_success:
            return Result("ok", report)
        return Result("invalid_report" if saw_success else "error", report, p.returncode)


def _rate_error(text):
    # Only inspect provider error envelopes, never arbitrary tool/user output.
    return any(x in text.lower() for x in ["rate_limit", "rate limit", "usage limit", "limit reached", "out of extra usage"])


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(k + "=" + _toml(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_toml(v) for v in value) + "]"
    return json.dumps(value)


def vet_codex_hook_sources(project):
    """The one-invocation trust flag may run only the hook shipped by this application.

    Codex merges hooks across layers; a config override cannot safely erase them.
    Reject additional sources rather than silently trusting them or changing settings.
    """
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    directories = {codex_home, *(parent / ".codex" for parent in [project, *project.parents])}
    for directory in directories:
        hooks = directory / "hooks.json"
        if hooks.exists():
            try:
                if json.loads(hooks.read_text()).get("hooks"):
                    raise SafetyError("Há hooks adicionais do Codex. Integração pausada para não autorizar scripts externos automaticamente.")
            except ValueError:
                raise SafetyError("Arquivo de hooks do Codex inválido; preservado para inspeção.")
        config = directory / "config.toml"
        if directory != codex_home and config.exists():
            with config.open("rb") as f:
                parsed = tomllib.load(f)
            if parsed.get("hooks") or parsed.get("plugins") or parsed.get("mcp_servers"):
                raise SafetyError("Configuração local contém hooks/plugins/MCP. Integração pausada para não executar código externo sem revisão.")
