"""Full regression and retest, entirely simulated providers. Saves both reports."""
from pathlib import Path
import re
import subprocess
import sys
from dev_auto.storage import atomic

root = Path(__file__).resolve().parent.parent
folder = root / 'docs/validation'
summaries = []
for index in (1, 2):
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                            cwd=root, capture_output=True, text=True)
    output = result.stdout + result.stderr
    atomic(folder / f'devswitch-regression-{index}.txt', output)
    count = re.search(r'Ran (\d+) tests?', output)
    summary = {'pass': index, 'tests': int(count[1]) if count else None, 'passed': result.returncode == 0}
    summaries.append(summary)
    print(f"Rodada {index}: {summary['tests']} testes, {'PASS' if summary['passed'] else 'FAIL'}", flush=True)
    if result.returncode:
        print(output)
        atomic(folder / 'devswitch-regression.json', summaries)
        sys.exit(result.returncode)
atomic(folder / 'devswitch-regression.json', summaries)
atomic(folder / 'simulated-tests.txt', output)
print('Regressão e reteste concluídos. Nenhum provedor foi chamado.', flush=True)
