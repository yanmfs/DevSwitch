"""Project-scoped launch choices; never modifies either agent's global config."""
from __future__ import annotations

from copy import deepcopy
import json
import re
import shutil
import subprocess

from .storage import SafetyError, atomic, read_json

AGENTS = ("claude", "codex")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULTS = {
    "primary": "claude",
    "agents": {
        "claude": {"model": "sonnet", "effort": "medium"},
        "codex": {"model": "gpt-6.1-sol", "effort": "medium"},
    },
}


def validate_settings(value):
    if not isinstance(value, dict) or value.get("primary") not in AGENTS:
        raise SafetyError("Configuração de agentes inválida; arquivo preservado.")
    result = {"primary": value["primary"], "agents": {}}
    for agent in AGENTS:
        item = value.get("agents", {}).get(agent)
        if not isinstance(item, dict):
            raise SafetyError(f"Configuração de {agent} ausente.")
        model, effort = item.get("model"), item.get("effort")
        if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/\[\]-]{0,139}", model)):
            raise SafetyError(f"ID de modelo inválido para {agent}.")
        if effort is not None and effort not in EFFORTS:
            raise SafetyError(f"Effort inválido para {agent}: use {', '.join(EFFORTS)}.")
        result["agents"][agent] = {"model": model, "effort": effort}
    return result


def load_settings(store):
    return validate_settings(read_json(store.path / "preferences.json", deepcopy(DEFAULTS)))


def save_settings(store, settings):
    atomic(store.path / "preferences.json", validate_settings(settings))


def session_settings(state):
    if state.get("settings"):
        return validate_settings(state["settings"])
    # Old sessions keep the CLI defaults they were created with until reconfigured.
    return {"primary": "claude", "agents": {a: {"model": None, "effort": None} for a in AGENTS}}


def apply_overrides(settings, args):
    result = deepcopy(settings)
    if args.first:
        result["primary"] = args.first
    for agent in AGENTS:
        for key in ("model", "effort"):
            value = getattr(args, f"{agent}_{key}")
            if value is not None:
                result["agents"][agent][key] = None if value == "default" else value
    return validate_settings(result)


def has_overrides(args):
    return bool(args.first or any(getattr(args, f"{a}_{k}") is not None for a in AGENTS for k in ("model", "effort")))


def cli_arguments(settings):
    args = ["--first", settings["primary"]]
    for name in AGENTS:
        for key in ("model", "effort"):
            args += [f"--{name}-{key}", settings["agents"][name][key] or "default"]
    return args


def model_catalog():
    """Read the installed Codex catalog without inference, auth reads or network calls."""
    executable = shutil.which("codex")
    if not executable:
        return []
    try:
        process = subprocess.run([executable, "debug", "models", "--bundled"],
                                 capture_output=True, text=True, timeout=5)
        if process.returncode:
            return []
        data = json.loads(process.stdout)
        models = data.get("models", []) if isinstance(data, dict) else data
        result = []
        for item in models:
            if item.get("visibility") != "list" or not item.get("slug"):
                continue
            result.append({"model": item["slug"], "label": item.get("display_name") or item["slug"],
                           "efforts": [x["effort"] for x in item.get("supported_reasoning_levels", []) if x.get("effort") in EFFORTS]})
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired, TypeError, KeyError):
        return []
