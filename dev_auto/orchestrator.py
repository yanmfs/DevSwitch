from __future__ import annotations

import signal
import time
import uuid

from .adapters import Adapter, CodexAccount, check_interfaces, detect
from .handoff import export, merge, prompt
from .storage import ProjectLock, SafetyError, atomic, changed, redact
from .usage import Availability
from .settings import DEFAULTS, session_settings, validate_settings
from .monitor import QuotaMonitor


class Orchestrator:
    def __init__(self, store, adapter_factory=Adapter, inventory=None, quota_reader=None,
                 poll=60, step_timeout=1200, announce=print, terminal_input=None, on_update=None):
        self.store = store
        self.adapter_factory = adapter_factory
        self.inventory = inventory
        self.quota_reader = quota_reader
        self.poll, self.step_timeout, self.announce = poll, step_timeout, announce
        self.interrupt = False
        self.pause_requested = False
        self.preferred = None
        self.next_quota = 0
        self.terminal_input = terminal_input
        self.on_update = on_update
        self.monitor = None

    def notify(self):
        if self.on_update:
            self.on_update({**self.state, "availability": {name: value.dump() for name, value in self.usage.items()}})

    def poll_terminal(self):
        if self.terminal_input:
            self.terminal_input()

    def persist(self):
        self.state["availability"] = {name: value.dump() for name, value in self.usage.items()}
        self.state["preferred"] = self.preferred
        self.store.save(self.state)
        export(self.store, self.state)
        self.notify()

    def requests(self):
        for req in self.store.pop_requests():
            if req["action"] == "pause":
                self.pause_requested = True
            if req["action"] == "switch":
                self.preferred = req["agent"] if req["agent"] in {"claude", "codex"} else None
                self.store.event("switch_requested", agent=self.preferred or "auto")

    def heartbeat(self):
        self.poll_terminal()
        self.requests()
        self.refresh_quota(background=True)
        self.state["heartbeat_at"] = time.time()
        self.persist()
        return self.interrupt

    def refresh_quota(self, background=False):
        if self.monitor:
            result = self.monitor.take()
            if result:
                started, data, failed = result
                if failed:
                    self.store.event("quota_query_unavailable", agent="codex")
                elif started >= self.usage["codex"].observed_at:
                    try:
                        self.usage["codex"].codex_snapshot(data["limits"])
                    except (KeyError, TypeError, ValueError, AttributeError):
                        self.store.event("quota_query_unavailable", agent="codex")
        if time.monotonic() < self.next_quota:
            return
        self.next_quota = time.monotonic() + self.poll
        if self.usage["codex"].status in {"missing", "auth_required"}:
            return
        if background and self.monitor:
            self.monitor.request()
            return
        if self.monitor and self.monitor.thread is not None:
            return
        try:
            data = self.quota_reader() if self.quota_reader else CodexAccount(self.found["codex"]["path"]).read()
            self.usage["codex"].codex_snapshot(data["limits"])
        except (SafetyError, OSError, ValueError):
            # A failed monitoring query is not proof that model access is exhausted.
            self.store.event("quota_query_unavailable", agent="codex")

    def run(self, objective=None, settings=None):
        with self.store.lock() as lock:
            self.state = self.store.load()
            if objective is not None:
                if self.state and self.state["status"] != "completed":
                    raise SafetyError("Há uma tarefa pendente. Use resume; o objetivo existente foi preservado.")
                if self.state:
                    atomic(self.store.path / "history" / (self.state["id"] + ".json"), self.state)
                self.state = {
                    "schema": 1, "id": str(uuid.uuid4()), "project": str(self.store.project),
                    "objective": redact(objective), "status": "starting", "active_agent": None,
                    "last_agent": None, "last_handoff": None, "step": 0, "errors": 0, "stagnant": 0,
                    "created_at": time.time(), "availability": {}, "preferred": None,
                    "settings": validate_settings(settings or DEFAULTS), "runtime": {},
                    "handoff": {"summary": "Tarefa ainda não iniciada.", "modified_files": [],
                                "decisions": [], "completed": [], "pending": [redact(objective)],
                                "next_steps": ["Inspecionar o projeto e implementar a primeira etapa."],
                                "problems": [], "tests": [], "checkpoint": None, "last_agent": None},
                }
                self.store.event("task_created", session=self.state["id"])
            elif not self.state:
                raise SafetyError("Não há sessão neste projeto.")
            elif self.state["status"] == "completed":
                self.announce("Esta tarefa já está concluída.")
                return self.state
            elif settings is not None:
                self.state["settings"] = validate_settings(settings)
                self.state["preferred"] = None
                self.store.event("session_settings_updated")
            self.state["settings"] = session_settings(self.state)
            self.state["project_mode"] = "git" if self.store.has_git else "folder"
            self.state.setdefault("runtime", {})
            primary = self.state["settings"]["primary"]
            secondary = "codex" if primary == "claude" else "claude"
            self.usage = {name: Availability(**self.state["availability"].get(name, {})) for name in ("claude", "codex")}
            self.preferred = self.state.get("preferred")
            self.found = self.inventory if self.inventory is not None else check_interfaces(detect())
            for name, item in self.found.items():
                if not item["path"]:
                    self.usage[name].status = "missing"
                elif not item.get("interfaces_ok", False):
                    raise SafetyError(f"CLI {name} incompatível. Execute devswitch doctor.")
                elif not item["subscription_login"]:
                    self.usage[name].status = "auth_required"
                elif self.usage[name].status in {"auth_required", "missing"}:
                    self.usage[name] = Availability()
            # Acquiring the lock establishes that the previous guardian has stopped.
            recovered = self.store.checkpoint("recovery" if objective is None else "baseline")
            if self.state.get("active_agent") or self.state["status"] == "running":
                self.store.event("interruption_recovered")
                old = (self.state.get("before_step") or {}).get("files", {})
                merge(self.state, None, changed(old, recovered["files"]), recovered["id"], self.state.get("active_agent"))
            self.state["active_agent"] = None
            self.state["status"] = "ready"
            self.state["controller_pid"] = __import__("os").getpid()
            self.persist()
            self.monitor = QuotaMonitor(lambda: self.quota_reader() if self.quota_reader else
                                        CodexAccount(self.found["codex"]["path"]).read(stop_event=self.monitor.cancel))
            old_signals = {}
            if __import__("threading").current_thread() is __import__("threading").main_thread():
                for sig in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
                    old_signals[sig] = signal.signal(sig, lambda *_: setattr(self, "interrupt", True))
            try:
                while True:
                    self.poll_terminal()
                    self.requests()
                    if self.pause_requested or self.interrupt:
                        self.state["status"] = "paused"
                        break
                    self.refresh_quota()
                    order = [self.preferred, "codex" if self.preferred == "claude" else "claude"] if self.preferred else [primary, secondary]
                    agent = next((name for name in order if self.usage[name].eligible()), None)
                    if agent is None:
                        if all(u.status in {"auth_required", "missing"} for u in self.usage.values()):
                            self.state["status"] = "needs_login"
                            self.announce("Autenticação por assinatura necessária. Execute devswitch doctor.")
                            break
                        self.state["status"] = "waiting_quota"
                        self.state["active_agent"] = None
                        if not getattr(self, "waiting_announced", False):
                            self.store.event("waiting_for_quota")
                            self.announce("Ambos indisponíveis. Aguardando renovação; nenhum agente está escrevendo.")
                            self.waiting_announced = True
                        self.persist()
                        # Poll without keeping the UI/control channel blocked for a minute.
                        until = time.monotonic() + min(self.poll, 30)
                        while time.monotonic() < until and not self.interrupt:
                            self.poll_terminal()
                            self.requests()
                            self.refresh_quota(background=True)
                            self.notify()
                            if self.pause_requested:
                                break
                            time.sleep(0.2)
                        continue
                    self.waiting_announced = False
                    before = self.store.checkpoint("before")
                    previous_completed = set(self.state["handoff"]["completed"])
                    self.state["before_step"] = before
                    self.state["step"] += 1
                    if self.state["last_agent"] and self.state["last_agent"] != agent:
                        transfer = {"at": time.time(), "from": self.state["last_agent"], "to": agent,
                                    "checkpoint": before["id"]}
                        self.state["last_handoff"] = transfer
                        self.store.event("handoff", **transfer)
                    self.state["active_agent"] = agent
                    self.state["status"] = "running"
                    self.persist()
                    config = self.state["settings"]["agents"][agent]
                    self.store.event("step_started", agent=agent, step=self.state["step"], model=config["model"], effort=config["effort"])
                    self.announce(f"Etapa {self.state['step']}: {agent} · {config['model'] or 'padrão do CLI'} · effort {config['effort'] or 'padrão do modelo'}")
                    adapter = self.adapter_factory(agent, self.found[agent]["path"], self.store.path)
                    if hasattr(adapter, "configure"):
                        adapter.configure(config)
                    def tick():
                        self.state["runtime"][agent] = getattr(adapter, "runtime", {})
                        return self.heartbeat()
                    result = adapter.run(self.store.project, prompt(self.state), tuple(lock.fds),
                                         self.usage[agent], tick, timeout=self.step_timeout)
                    self.state["runtime"][agent] = getattr(adapter, "runtime", {})
                    after = self.store.checkpoint("after")
                    files = changed(before["files"], after["files"])
                    merge(self.state, result.report, files, after["id"], agent)
                    self.state["last_agent"] = agent
                    self.state["active_agent"] = None
                    self.state["before_step"] = None
                    self.store.event("step_finished", agent=agent, step=self.state["step"],
                                     outcome=result.kind, changed_files=len(files), returncode=result.returncode)
                    deleted = [f for f in before["files"] if not before["files"][f].get("deleted") and (f not in after["files"] or after["files"][f].get("deleted"))]
                    if before["git"] != after["git"] or deleted:
                        self.state["status"] = "needs_attention"
                        self.state["handoff"]["problems"].append("Verificação de integridade detectou mudança de HEAD/branch ou remoção de arquivo. Backup preservado; requer inspeção humana.")
                        self.store.event("integrity_change_detected", removed_files=len(deleted))
                        break
                    if result.kind == "ok":
                        # Do not erase a near-limit event with a successful end of turn.
                        if self.usage[agent].status not in {"near_limit", "limited"} or self.usage[agent].retry_at <= time.time():
                            self.usage[agent].success()
                        self.state["errors"] = 0
                        new_completed = set(result.report["completed"]) - previous_completed
                        self.state["stagnant"] = self.state["stagnant"] + 1 if not files and not new_completed else 0
                        self.announce(redact(result.report["summary"]))
                        if result.report["blocked"]:
                            self.state["status"] = "needs_attention"
                            break
                        if result.report["done"] and not result.report["pending"] and result.report["tests"]:
                            self.state["status"] = "completed"
                            self.store.event("task_completed")
                            break
                        if self.state["stagnant"] >= 4:
                            self.state["status"] = "needs_attention"
                            self.store.event("no_progress_detected")
                            break
                    elif result.kind == "limited":
                        if self.usage[agent].eligible():
                            self.usage[agent].reject()
                        self.announce(f"{agent}: limite informado pelo CLI; progresso preservado.")
                    elif result.kind == "auth_required":
                        self.usage[agent].status = "auth_required"
                    elif result.kind in {"interrupted", "timeout", "protocol_error"}:
                        self.state["status"] = "paused"
                        break
                    else:
                        self.state["errors"] += 1
                        self.usage[agent].retry_at = time.time() + 60
                        if self.state["errors"] >= 3:
                            self.state["status"] = "needs_attention"
                            break
                    self.state["status"] = "ready"
                    self.persist()
            except (OSError, SafetyError, ValueError):
                # Keep active_agent if a guardian may survive; status checks use the lock.
                self.state["status"] = "needs_attention"
                self.store.event("safety_stop")
                self.persist()
                raise
            finally:
                self.monitor.close()
                self.persist()
                for sig, handler in old_signals.items():
                    signal.signal(sig, handler)
            self.announce(f"Estado: {self.state['status']} | Contexto: {self.store.path / 'HANDOFF.md'}")
            return self.state
