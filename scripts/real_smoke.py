"""Explicit, small REAL subscription smoke test. No APIs, mock limits or existing project edits."""
import json
from pathlib import Path
import subprocess
import tempfile
import sys

from dev_auto.adapters import Adapter, detect, check_interfaces
from dev_auto.orchestrator import Orchestrator
from dev_auto.storage import Store, atomic

folder = Path(tempfile.mkdtemp(prefix="dev-auto-real-"))
project = folder / "project"
project.mkdir()
without_git = "--without-git" in sys.argv
if not without_git:
    subprocess.run(["git", "init", "-q", str(project)], check=True)
(project / "existing.txt").write_text("PRESERVAR ESTE CONTEUDO\n")
store = Store(project, folder / "state")


class SmokeAdapter(Adapter):
    def run(self, project, prompt, *args, **kwargs):
        extra = ("\nTESTE REAL CONTROLADO: Nesta etapa Claude, crie SOMENTE greet.py contendo def greet(): return 'ola'. "
                 "Não crie teste ainda. Devolva done=false, blocked=false, pending=['Codex deve criar e executar um teste unittest'], "
                 "next_steps com essa instrução e testes honestos. Não faça mais etapas.") if self.name == "claude" else (
                 "\nTESTE REAL CONTROLADO: Preserve greet.py feito pelo Claude. Crie test_greet.py com um teste unittest de greet(), "
                 "execute python3 -m unittest -v, confira existing.txt preservado e devolva done=true se passar, pending=[].")
        guard_check = "\nAUDITORIA AUTORIZADA: antes do trabalho, tente UMA chamada Bash executando exatamente printf DEV_AUTO_POLICY_CHECK. Esse comando inofensivo deve ser negado pelo hook local. Registre a negação e prossiga com a tarefa; não tente contornar a negação. Essa negação esperada não torna a tarefa blocked."
        result = super().run(project, prompt + extra + guard_check, *args, **kwargs)
        atomic(folder / f"{self.name}-result.json", {"kind": result.kind, "returncode": result.returncode, "report": result.report})
        if self.name == "claude" and result.kind == "ok":
            store.request("switch", "codex")
        return result


print(f"Teste REAL por assinatura. Artefatos: {folder}", flush=True)
result = Orchestrator(store, adapter_factory=SmokeAdapter, step_timeout=180).run(
    "Criar greet.py com greet() retornando 'ola', testar com unittest, preservar existing.txt. Handoff Claude → Codex.")
check = subprocess.run(["python3", "-m", "unittest", "-v"], cwd=project, capture_output=True, text=True)
assert result["status"] == "completed", result["status"]
assert result["last_handoff"]["from"] == "claude" and result["last_handoff"]["to"] == "codex"
assert check.returncode == 0 and "Ran 1 test" in check.stderr, check.stderr
assert (project / "existing.txt").read_text() == "PRESERVAR ESTE CONTEUDO\n"
if without_git:
    assert not (project / ".git").exists(), "Git was initialized unexpectedly"
    assert result["project_mode"] == "folder"
audit = [json.loads(line) for line in (store.path / "tool-audit.jsonl").read_text().splitlines()]
for agent in ["claude", "codex"]:
    assert any(item["agent"] == agent and item["tool"] == "Bash" and not item["allowed"] for item in audit), f"Hook denial not observed for {agent}"
print("PASS REAL: Claude escreveu → Codex recebeu contexto → teste unittest passou → tarefa encerrada.")
print("PASS REAL: hooks dos dois CLIs negaram um comando inofensivo fora da política.")
atomic(folder / "validation.json", {"passed": True, "type": "REAL subscription inference", "project": str(project), "without_git": without_git, "test_returncode": check.returncode})
