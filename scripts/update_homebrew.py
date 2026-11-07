"""Generate the tap formula from the checksum of the exact release asset."""
import argparse
import json
from pathlib import Path
import re

parser=argparse.ArgumentParser()
parser.add_argument('manifest',type=Path)
parser.add_argument('tap',type=Path)
parser.add_argument('--source-url',help='Override for a local pre-publication build test')
args=parser.parse_args()
data=json.loads(args.manifest.read_text())
version,checksum=data['version'],data['sha256']
if not re.fullmatch(r'\d+\.\d+\.\d+',version) or not re.fullmatch(r'[a-f0-9]{64}',checksum):
    raise SystemExit('Invalid release version/checksum')
url=args.source_url or f'https://github.com/yanmfs/devswitch/releases/download/v{version}/devswitch-{version}.tar.gz'
if '"' in url or '\n' in url:
    raise SystemExit('Invalid source URL')
formula='''# Generated from the reviewed release asset by scripts/update_homebrew.py.
class Devswitch < Formula
  include Language::Python::Virtualenv

  desc "Switch safely between Claude Code and Codex with persistent context"
  homepage "https://github.com/yanmfs/devswitch"
  url "@URL@"
  version "@VERSION@"
  sha256 "@SHA256@"
  license "MIT"

  depends_on :macos
  depends_on "python@3.14"

  resource "setuptools" do
    url "https://files.pythonhosted.org/packages/ef/55/6dc6d6bb953bdfc52519a0059592c52b6e3b8ad51cf8256dffceb55b0610/setuptools-84.2.1-py3-none-any.whl"
    sha256 "ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2"
  end

  def install
    venv = virtualenv_create(libexec, "python3.14")
    venv.pip_install resource("setuptools"), build_isolation: false
    venv.pip_install_and_link buildpath, build_isolation: false
  end

  def caveats
    <<~EOS
      Install Claude Code and Codex CLI separately and sign in with your subscriptions.
      Run `devswitch doctor` to check both integrations, then `devswitch` in your project.
      No API keys or additional paid APIs are configured by DevSwitch.
    EOS
  end

  test do
    assert_match "devswitch @VERSION@", shell_output("#{bin}/devswitch --version")
    system bin/"devswitch", "-C", testpath, "--first", "codex", "--claude-effort", "high", "config"
    settings = JSON.parse(shell_output("#{bin}/devswitch -C #{testpath} config --json"))
    assert_equal "codex", settings.fetch("primary")
    assert_equal "high", settings.fetch("agents").fetch("claude").fetch("effort")
  end
end
'''
formula=formula.replace('@URL@',url).replace('@VERSION@',version).replace('@SHA256@',checksum)
target=args.tap/'Formula/devswitch.rb'
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(formula)
print(target)
