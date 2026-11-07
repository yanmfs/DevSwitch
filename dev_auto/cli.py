from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .adapters import CodexAccount, check_interfaces, detect
from .orchestrator import Orchestrator
from .storage import ProjectLock, SafetyError, Store, atomic, private_dir, read_json, redact
from .settings import (AGENTS, EFFORTS, apply_overrides, cli_arguments, has_overrides,
                       load_settings, save_settings, session_settings)
from .ui import TerminalUI, Cancelled
from . import __version__
from .workspaces import is_launcher_directory, recent_workspaces


def parser():
    p = argparse.ArgumentParser(prog="devswitch", description="Claude Code ⇄ Codex: escolha a ordem, os modelos e o effort por projeto.")
    p.add_argument("words", nargs="*", help='Tarefa ou config, status, pause, resume, switch claude|codex|auto, logs, doctor, demo')
    p.add_argument("--version", action="version", version="devswitch " + __version__)
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="Cores do terminal")
    p.add_argument("--no-color", dest="color", action="store_const", const="never", help="Saída sem códigos de cor")
    p.add_argument("-C", "--project", help="Pasta do projeto; padrão: pasta atual (Git opcional)")
    p.add_argument("--background", action="store_true", help="Executa sem depender da janela do terminal")
    p.add_argument("--json", action="store_true", help="Saída estruturada de status/doctor")
    p.add_argument("--refresh", action="store_true", help="Consulta a cota real do Codex em status/doctor")
    p.add_argument("--step-timeout", type=int, default=1200, help="Timeout seguro de cada etapa em segundos")
    p.add_argument("--lines", type=int, default=30, help="Quantidade de eventos de logs")
    p.add_argument("--configure", action="store_true", help="Abre as escolhas de agentes, modelos e effort antes de iniciar/retomar")
    p.add_argument("--first", choices=AGENTS, help="Agente principal; o outro assume quando necessário")
    for name in AGENTS:
        p.add_argument(f"--{name}-model", help=f"Modelo de {name}, ou default para o padrão do CLI")
        p.add_argument(f"--{name}-effort", choices=(*EFFORTS, "default"), help=f"Effort de {name}")
    return p


def doctor(refresh=False):
    info = check_interfaces(detect())
    if refresh and info["codex"]["subscription_login"]:
        try:
            info["codex"]["account"] = CodexAccount(info["codex"]["path"]).read()
        except SafetyError as e:
            info["codex"]["quota_error"] = str(e)
    return info


def status(store, refresh=False):
    state = store.load()
    if not state:
        return {"state": "no_session", "project": str(store.project)}
    busy = store.busy()
    settings = session_settings(state)
    first = settings["primary"]
    result = {
        "state": state["status"] if busy or state["status"] not in {"running", "waiting_quota", "ready", "starting"} else "interrupted",
        "project": str(store.project), "active_agent": state["active_agent"] if busy else None,
        "primary_agent": first, "secondary_agent": "codex" if first == "claude" else "claude",
        "controller_or_guardian_running": busy, "task": state["objective"], "step": state["step"],
        "last_handoff": state["last_handoff"], "availability": state["availability"],
        "context": str(store.path / "HANDOFF.md"), "preferred": state.get("preferred") or first,
        "settings": settings, "runtime": state.get("runtime", {}),
        "project_mode": "git" if store.has_git else "folder",
    }
    if refresh:
        executable = __import__("shutil").which("codex")
        if executable:
            try:
                from .usage import Availability
                usage = Availability()
                usage.codex_snapshot(CodexAccount(executable).read()["limits"])
                result["availability"] = {**result["availability"], "codex": usage.dump()}
            except SafetyError as e:
                result["quota_note"] = str(e)
    return result


def show_status(data):
    TerminalUI().status(data)


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        ui = TerminalUI(color=None if args.color == "auto" else args.color == "always")
        words = args.words
        command = words[0] if words else None
        if command in {"help", "/help", "commands"}:
            ui.help()
            return 0
        if command == "doctor":
            data = doctor(args.refresh)
            print(json.dumps(data, ensure_ascii=False, indent=2)) if args.json else ui.doctor(data)
            return 0 if all(v["path"] and v["subscription_login"] and v.get("interfaces_ok") for v in data.values()) else 2
        if command == "demo":
            from .demo import run_demo
            run_demo()
            return 0
        project, objective = args.project or str(Path.cwd()), " ".join(words)
        if command is None and not sys.stdin.isatty():
            raise SafetyError('Informe a tarefa: devswitch "descrição da tarefa"')
        if args.project is None and is_launcher_directory(project):
            if sys.stdin.isatty() and not args.json:
                selected = ui.workspace(Path(project), recent_workspaces())
                if selected is None:
                    return 0
                project = str(selected)
            elif command not in {"status", "logs", "config", "pause", "switch", "resume"}:
                raise SafetyError('Informe uma pasta de projeto com -C, por exemplo: devswitch -C ~/Projetos/meu-projeto "tarefa"')
        store = Store(project)
        if command == "status":
            data = status(store, args.refresh)
            print(json.dumps(data, ensure_ascii=False, indent=2)) if args.json else ui.status(data)
            return 0
        if command == "config":
            settings = apply_overrides(load_settings(store), args)
            if has_overrides(args):
                save_settings(store, settings)
            elif not args.json:
                if not sys.stdin.isatty():
                    raise SafetyError("Use config --json para consultar ou informe --first/--claude-model/--codex-model para configurar sem interação.")
                ui.header(store.project)
                settings = ui.configure(settings)
                save_settings(store, settings)
            print(json.dumps(settings, ensure_ascii=False, indent=2)) if args.json else print("Escolhas salvas para as próximas tarefas deste projeto.")
            return 0
        if command == "logs":
            events = store.path / "events.jsonl"
            if events.exists():
                from collections import deque
                with events.open() as f:
                    lines = list(deque(f, maxlen=max(1, args.lines)))
                if args.json:
                    print("".join(lines), end="")
                else:
                    ui.logs([json.loads(line) for line in lines])
            else:
                print("Nenhum evento registrado.")
            return 0
        if command in {"pause", "switch"}:
            if not store.load():
                raise SafetyError("Não há sessão neste projeto.")
            if command == "switch" and (len(words) != 2 or words[1] not in {"claude", "codex", "auto"}):
                raise SafetyError("Use switch claude, switch codex ou switch auto.")
            store.request(command, words[1] if command == "switch" else None)
            print("Solicitação persistida. Será aplicada na próxima etapa segura." if store.busy()
                  else "Solicitação persistida. Use devswitch resume para aplicá-la.")
            return 0
        state = store.load()
        if command is None:
            ui.header(store.project)
            if state and state["status"] != "completed":
                if store.busy():
                    ui.status(status(store))
                    return 0
                ui.write("Há uma tarefa salva: " + state["objective"])
                action = ui.choose("Sua tarefa anterior", [("resume", "Retomar com o contexto salvo"),
                                 ("configure_resume", "Retomar escolhendo agentes, modelos e effort"),
                                 ("status", "Ver estado"), ("exit", "Sair")], "resume")
                if action == "status":
                    ui.status(status(store))
                    return 0
                if action == "exit":
                    return 0
                if action == "configure_resume":
                    args.configure = True
                else:
                    ui.summary(session_settings(state))
                command = "resume"
        if command == "resume":
            objective = None
            if not state:
                raise SafetyError("Não há sessão neste projeto.")
            settings = apply_overrides(session_settings(state), args) if has_overrides(args) or args.configure else None
            if args.configure:
                if not sys.stdin.isatty():
                    raise SafetyError("--configure precisa de terminal interativo; use as flags de modelo/effort em scripts.")
                ui.header(store.project)
                settings = ui.configure(settings)
            if settings:
                save_settings(store, settings)
        else:
            if state and state["status"] != "completed":
                raise SafetyError("Há uma tarefa pendente. Use resume; o objetivo existente foi preservado.")
            settings = apply_overrides(load_settings(store), args)
            if command is None or args.configure:
                if not sys.stdin.isatty():
                    raise SafetyError("--configure precisa de terminal interativo; use as flags de modelo/effort em scripts.")
                if command is not None:
                    ui.header(store.project)
                settings = ui.configure(settings)
            if command is None:
                objective = ui.task(settings, status_callback=lambda: ui.status(status(store)),
                                    save_callback=lambda chosen: save_settings(store, chosen))
                if objective is None:
                    return 0
            if not objective:
                raise SafetyError("A tarefa não pode estar vazia.")
            save_settings(store, settings)
        if args.step_timeout < 5:
            raise SafetyError("O timeout mínimo é 5 segundos.")
        if args.background:
            # Reserve launch intent so two background invocations cannot queue two tasks.
            with store.lock():
                launch = store.path / "launch.json"
                prior = read_json(launch, {})
                if time.time() - prior.get("at", 0) < 10:
                    raise SafetyError("Inicialização em andamento; consulte status em alguns segundos.")
                atomic(launch, {"at": time.time()})
            cmd = [sys.executable, "-m", "dev_auto", "-C", str(store.project), "--step-timeout", str(args.step_timeout),
                   *(cli_arguments(settings) if settings is not None else []), "resume" if command == "resume" else objective]
            # Structured messages only; CLI stdout never contains raw tool output.
            output = store.path / "controller.log"
            with output.open("a") as f:
                child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=f,
                                         start_new_session=True, close_fds=True)
            print(f"Controlador iniciado (PID {child.pid}). Use devswitch status e devswitch logs.")
            return 0
        def live_input():
            for entry in ui.poll_live():
                ui.live_command(entry, store, lambda: ui.status(status(store)))
        if sys.stdin.isatty() and not args.background:
            ui.line("EM EXECUÇÃO  /status consultar · /pause salvar e pausar · /switch transferir", "dim")
        ui.start_dashboard()
        try:
            result = Orchestrator(store, step_timeout=args.step_timeout, announce=ui.event,
                                  terminal_input=live_input, on_update=ui.dashboard).run(objective, settings=settings)
        finally:
            ui.stop_dashboard()
        return 0 if result["status"] == "completed" else 2
    except (Cancelled, KeyboardInterrupt):
        print("\ndevswitch: Sessão cancelada; progresso existente preservado.", file=sys.stderr)
        return 130
    except (SafetyError, FileNotFoundError, NotADirectoryError, json.JSONDecodeError) as e:
        print("devswitch: " + redact(str(e)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
