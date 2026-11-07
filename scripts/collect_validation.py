"""Preserve only known, non-secret smoke-test artifacts and local mock-test output."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from dev_auto.storage import atomic, private_dir

root = Path(__file__).resolve().parent.parent
target = private_dir(root / "docs/validation")
sources = [arg for arg in sys.argv[1:] if arg != '--artifacts-only']
if sources:
    smoke = Path(sources[0]).resolve()
    assert json.loads((smoke / "validation.json").read_text())["passed"]
    real = private_dir(target / "real-smoke")
    for name in ["validation.json", "claude-result.json", "codex-result.json"]:
        shutil.copyfile(smoke / name, real / name)
    state_dir = next((smoke / "state/projects").iterdir())
    state = json.loads((state_dir / "state.json").read_text())
    atomic(real / "runtime.json", {"settings": state.get("settings"), "runtime": state.get("runtime")})
    for name in ["HANDOFF.md", "handoff.json", "events.jsonl", "tool-audit.jsonl"]:
        shutil.copyfile(state_dir / name, real / name)
    example = private_dir(real / "project")
    for name in ["greet.py", "test_greet.py", "existing.txt"]:
        shutil.copyfile(smoke / "project" / name, example / name)
    check = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=example, capture_output=True, text=True)
    atomic(real / "unittest.txt", check.stdout + check.stderr)
    assert check.returncode == 0

if '--artifacts-only' in sys.argv:
    print('Evidências reais preservadas; suíte não reexecutada.')
    sys.exit(0)

tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                       cwd=root, capture_output=True, text=True)
atomic(target / "simulated-tests.txt", tests.stdout + tests.stderr)
print(tests.stdout + tests.stderr)
sys.exit(tests.returncode)
