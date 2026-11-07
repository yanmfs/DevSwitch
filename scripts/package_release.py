"""Create a deterministic source asset from the reviewed Git commit, never the home directory."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib

root = Path(__file__).resolve().parent.parent
version = tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
if subprocess.check_output(['git','status','--porcelain'],cwd=root):
    sys.exit('Commit the reviewed public files before building a release.')
names = subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
for name in filter(None,names):
    if name.startswith(('.venv/','.release/','docs/validation/','docs/codex-schema/')) or name == 'docs/VALIDATION.md':
        sys.exit('Local-only data found in the release file list: '+name)
destination = root/'dist'
destination.mkdir(exist_ok=True)
archive = subprocess.check_output(['git','archive','--format=tar',f'--prefix=devswitch-{version}/','HEAD'],cwd=root)
payload = gzip.compress(archive,mtime=0)
asset = destination/f'devswitch-{version}.tar.gz'
asset.write_bytes(payload)
manifest = {'version':version,'file':asset.name,'sha256':hashlib.sha256(payload).hexdigest(),
            'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()}
(destination/'release.json').write_text(json.dumps(manifest,indent=2)+'\n')
(destination/'SHA256SUMS').write_text(f"{manifest['sha256']}  {asset.name}\n")
print(json.dumps(manifest,indent=2))
