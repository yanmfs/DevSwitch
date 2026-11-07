"""A repeatable complete simulated handoff using the production subprocess adapters."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .orchestrator import Orchestrator
from .storage import Store


def fixture(folder, use_git=True):
    folder = Path(folder)
    project = folder / "project"
    project.mkdir()
    if use_git:
        subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "existing.txt").write_text("ALTERAÇÃO PREEXISTENTE: preservar\n")
    (project / "scenario.json").write_text(json.dumps({"mode": "cycle"}))
    binary = folder / "bin"
    binary.mkdir()
    source = Path(__file__).resolve().parent / "assets/simulated_cli.py"
    inventory = {}
    for name in ["claude", "codex"]:
        executable = binary / name
        executable.write_text(f"#!{sys.executable}\n" + source.read_text())
        executable.chmod(0o700)
        inventory[name] = {"path": str(executable), "version": "SIMULATED", "subscription_login": True, "interfaces_ok": True}
    return Store(project, folder / "state"), inventory


def run_demo():
    folder = Path(tempfile.mkdtemp(prefix="dev-auto-demo-"))
    store, inventory = fixture(folder)
    print("DEMONSTRAÇÃO SIMULADA — nenhum provedor ou cota será utilizado.")
    result = Orchestrator(store, inventory=inventory, quota_reader=lambda: {"limits": {}}, poll=0.1).run(
        "SIMULADO: preservar existente, implementar e validar em três etapas")
    history = json.loads((store.project / "calls.json").read_text())
    assert history == ["claude", "codex", "claude"], history
    assert (store.project / "existing.txt").read_text() == "ALTERAÇÃO PREEXISTENTE: preservar\n"
    assert result["status"] == "completed"
    print("PASS: esgotamento Claude → Codex → renovação Claude → concluído.")
    print("PASS: código anterior, handoff e estado persistente preservados.")
    print(f"Artefatos da demonstração: {folder}")
