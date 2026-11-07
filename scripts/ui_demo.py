"""Interactive UI against simulated provider executables. No quotas are consumed."""
import os
from pathlib import Path
import tempfile
import sys

from dev_auto.demo import fixture

folder = Path(tempfile.mkdtemp(prefix="dev-auto-ui-demo-"))
store, _ = fixture(folder)
(store.project / "scenario.json").write_text('{"mode":"normal","capture_options":true}')
env = {**os.environ, "DEV_AUTO_HOME": str(store.home),
       "PATH": str(folder / "bin") + os.pathsep + os.environ["PATH"]}
print("DEMONSTRAÇÃO SIMULADA — só a interface/controlador são reais; nenhum provedor será chamado.", flush=True)
print(f"Artefatos: {folder}", flush=True)
os.chdir(store.project)
os.execvpe("devswitch", ["devswitch", *sys.argv[1:]], env)
