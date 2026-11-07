#!/usr/bin/env python3
"""Bundled SIMULATED CLI for the offline demo/tests. Never contacts a provider."""
import json
import os
from pathlib import Path
import sys
import time

name = Path(sys.argv[0]).name
args = sys.argv[1:]
if "--version" in args:
    print("SIMULATED 1.0")
    sys.exit(0)
if "--help" in args:
    print("--output-format --json-schema --permission-prompts --restricted --json --output-schema --ignore-user-config --sandbox --dangerously-bypass-hook-trust --skip-git-repo-check")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "DO-NOT-LOG", "accessToken": "DO-NOT-LOG"}))
    sys.exit(0)
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
    sys.exit(0)
if args[:2] == ["debug", "models"]:
    print(json.dumps({"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list",
                                  "supported_reasoning_levels": [{"effort": x} for x in ["low", "medium", "high", "xhigh", "max"]]}]}))
    sys.exit(0)
if args and args[0] == "app-server":
    for line in sys.stdin:
        event = json.loads(line)
        if "id" not in event:
            continue
        value = {}
        if event["method"] == "account/read":
            value = {"account": {"type": "chatgpt", "planType": "SIMULATED"}}
        if event["method"] == "account/rateLimits/read":
            value = {"rateLimits": {"primary": {"usedPercent": 0}}}
        print(json.dumps({"id": event["id"], "result": value}), flush=True)
    sys.exit(0)

prompt = sys.stdin.read()
root = Path.cwd()
scenario = json.loads((root / "scenario.json").read_text())
if scenario.get("capture_options"):
    selected = {"model": None, "effort": None}
    if "--model" in args:
        selected["model"] = args[args.index("--model") + 1]
    if "--effort" in args:
        selected["effort"] = args[args.index("--effort") + 1]
    for value in args:
        if value.startswith("model_reasoning_effort="):
            selected["effort"] = json.loads(value.split("=", 1)[1])
    (root / f"{name}-options.json").write_text(json.dumps(selected))
    (root / f"{name}-arguments.json").write_text(json.dumps(args))
calls = root / "calls.json"
history = json.loads(calls.read_text()) if calls.exists() else []
history.append(name)
calls.write_text(json.dumps(history))

def emit(value):
    print(json.dumps(value), flush=True)

def report(done=False):
    return {"done": done, "blocked": False, "summary": f"SIMULADO: etapa {name}",
            "decisions": ["Preservar o arquivo existente"], "completed": [f"etapa {len(history)} por {name}"],
            "pending": [] if done else ["finalizar"], "next_steps": [] if done else ["validar arquivo"],
            "problems": [], "tests": ["SIMULADO: conteúdo preservado, PASS"]}

if scenario.get("mode") == "sleep":
    if scenario.get("detached_child"):
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        (root / "detached.pid").write_text(str(child.pid))
    (root / "agent.pid").write_text(str(os.getpid()))
    (root / "partial.txt").write_text("progresso antes da interrupção")
    while True:
        time.sleep(0.1)

if scenario.get("mode") == "invalid":
    emit({"type": "turn.completed", "usage": {}})
    sys.exit(0)

if scenario.get("mode") == "slow":
    (root / "agent.pid").write_text(str(os.getpid()))
    (root / "partial.txt").write_text("progresso antes da pausa segura")
    time.sleep(3)

if len(history) == 1 and name == "claude" and scenario.get("mode") == "cycle":
    (root / "progress.txt").write_text("Claude: primeira implementação\n")
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "utilization": 1.0, "resetsAt": time.time() + 0.1}})
    sys.exit(1)

if scenario.get("mode") == "cycle":
    if name == "codex":
        # Simulate the renewal passing, including the production five-second margin.
        time.sleep(4.2)
    assert "progress.txt" in prompt, "handoff did not include file"
    assert (root / "progress.txt").read_text().startswith("Claude:")
    with (root / "progress.txt").open("a") as f:
        f.write(name + ": continuou preservando o anterior\n")
    value = report(done=len(history) >= 3)
else:
    value = report(done=scenario.get("mode") != "slow" or len(history) >= 2)
    (root / "result.txt").write_text("ok")

if name == "claude":
    emit({"type": "result", "is_error": False, "structured_output": value})
else:
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(value)}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
