"""Idempotent stdlib-only installation into this project's venv and ~/.local/bin."""
import os
from pathlib import Path
import subprocess
import sys
import venv

root = Path(__file__).resolve().parent
os.umask(0o077)
environment = root / ".venv"
if not (environment / "bin/python").exists():
    venv.EnvBuilder(with_pip=False).create(environment)
python = environment / "bin/python"
purelib = Path(subprocess.check_output([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True).strip())
(purelib / "dev_auto_local.pth").write_text(str(root) + "\n")
bin_dir = Path.home() / ".local/bin"
bin_dir.mkdir(exist_ok=True, parents=True)
marker = "# managed by auto-ai-switch/install.py"
launchers = [bin_dir / name for name in ("devswitch", "dev-auto")]
for launcher in launchers:
    if launcher.exists() and marker not in launcher.read_text():
        sys.exit(f"Comando {launcher.name} já existe; preservado. Escolha outro caminho antes de instalar.")
for launcher in launchers:
    launcher.write_text(f"#!{python}\n{marker}\nfrom dev_auto.cli import main\nraise SystemExit(main())\n")
    launcher.chmod(0o755)
print(f"Instalado: {launchers[0]}\nCompatibilidade: {launchers[1]}\nPython isolado: {python}\nDependências externas: nenhuma")
