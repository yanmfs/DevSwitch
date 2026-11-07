"""Official PreToolUse hook for both CLIs. No input or credentials are logged."""
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time


def shell_allowed(command):
    # Conservative command grammar: no eval, substitutions, redirects or background jobs.
    if re.search(r"[`$<>\n\r]|(?<!&)&(?!&)", command):
        return False
    if re.search(r"(?i)(?:\.env(?:\W|$)|credentials|auth\.json|keychain|\.aws|\.ssh|\.codex|\.claude|dev_auto|dev-auto|devswitch)", command):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    chunks, current = [], []
    for token in tokens:
        if token in {"&&", "||", ";", "|"}:
            if not current:
                return False
            chunks.append(current)
            current = []
        else:
            current.append(token)
    if current:
        chunks.append(current)
    for part in chunks:
        exe, *args = part
        base = Path(exe).name
        if exe.startswith("/") and exe not in {"/usr/bin/git", "/bin/ls", "/bin/cat", "/usr/bin/wc"}:
            return False
        if base in {"ls", "pwd", "rg", "cat", "head", "tail", "wc", "file", "diff"}:
            continue
        if base == "sed" and len(args) >= 2 and args[0] == "-n" and re.fullmatch(r"\d+(?:,\d+|,\$)?p", args[1]):
            continue
        if base == "find" and args and args[0] == "." and not any(a in {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"} for a in args):
            continue
        if base == "git" and args and args[0] in {"status", "diff", "log", "show", "ls-files", "rev-parse"}:
            if any(x.startswith(("--output", "--ext-diff", "--textconv")) for x in args):
                return False
            continue
        if base == "cd" and len(args) == 1 and not args[0].startswith(("/", "~", "..")):
            continue
        if base in {"python", "python3"} and len(args) >= 2 and args[:2] in [["-m", "unittest"], ["-m", "pytest"], ["-m", "compileall"]]:
            continue
        if base in {"pytest", "ruff", "mypy"} and not exe.startswith(("/", "..")):
            continue
        if base in {"npm", "pnpm", "yarn"} and args:
            if args[0] in {"test", "build", "lint", "typecheck"} or (len(args) > 1 and args[0] == "run" and args[1] in {"test", "build", "lint", "check", "typecheck"}):
                continue
        if base in {"cargo", "go", "swift"} and args and args[0] in {"test", "build", "check", "vet"}:
            continue
        if base == "make" and args and all(a in {"test", "check", "build", "lint"} for a in args):
            continue
        if base == "node" and args and args[0] == "--check":
            continue
        return False
    return bool(chunks)


def decision(event):
    name, args = event.get("tool_name", ""), event.get("tool_input", {})
    if not isinstance(args, dict):
        return False
    if name in {"Bash", "exec_command", "shell_command", "shell"}:
        return shell_allowed(str(args.get("command", args.get("cmd", ""))))
    if name in {"Agent", "Task", "spawn_agent", "send_input", "resume_agent", "WebFetch", "WebSearch"} or name.startswith("mcp__"):
        return False
    raw = json.dumps(args)
    if re.search(r"(?i)(?:\.env(?:\W|$)|credentials|auth\.json|keychain|\.aws|\.ssh|\.codex|\.claude|auto-ai-switch|\.local/state/dev-auto)", raw):
        return False
    if name == "apply_patch":
        patch = args.get("command", args.get("patch", ""))
        if "*** Delete File:" in patch or "*** Move to:" in patch:
            return False
    # Reject direct file access outside the project, including symlink escapes.
    root = Path(os.environ.get("DEV_AUTO_PROJECT", event.get("cwd", "."))).resolve()
    for key in ("file_path", "path"):
        path = args.get(key)
        if isinstance(path, str):
            target = (root / path).resolve()
            if not target.is_relative_to(root) or ".git" in target.relative_to(root).parts:
                return False
    return True


def main():
    try:
        event = json.load(sys.stdin)
        allowed = decision(event)
    except Exception:
        event = {}
        allowed = False
    audit = os.environ.get("DEV_AUTO_TOOL_AUDIT")
    if audit:
        try:
            safe_names = {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "apply_patch", "Agent", "Task", "spawn_agent"}
            record = {"at": time.time(), "agent": os.environ.get("DEV_AUTO_AGENT") if os.environ.get("DEV_AUTO_AGENT") in {"claude", "codex"} else "unknown",
                      "tool": event.get("tool_name") if event.get("tool_name") in safe_names else "other", "allowed": allowed}
            fd = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass
    if not allowed:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "devswitch: operação fora da política autônoma. Shell aceita comandos simples (rg, cat, ls, sed -n, git status/diff, testes), sem loops, redirecionamentos ou substituições. Para leitura, use uma dessas formas permitidas. Se a ação em si for destrutiva, externa ou exigir permissão, não tente contornar: registre blocked=true e solicite ação humana."
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
