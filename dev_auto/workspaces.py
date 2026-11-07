"""Read-only project discovery for the launcher; never starts an agent."""
from pathlib import Path

from .storage import read_json, state_home_path


def is_launcher_directory(path):
    path = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    return path == home or path in home.parents


def recent_workspaces(home=None, limit=8):
    root = state_home_path(home) / "projects"
    if not root.is_dir():
        return []
    found = {}
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        try:
            info = read_json(folder / "workspace.json") or read_json(folder / "state.json", {})
            if not isinstance(info, dict):
                continue
            raw = info.get("project")
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                continue
            path = Path(raw).resolve()
            if not path.is_dir() or is_launcher_directory(path):
                continue
            stamp = info.get("opened_at", info.get("updated_at", 0))
            stamp = stamp if isinstance(stamp, (int, float)) else 0
            found[path] = max(found.get(path, 0), stamp)
        except (OSError, ValueError, TypeError):
            continue
    return [path for path, _ in sorted(found.items(), key=lambda item: item[1], reverse=True)[:limit]]
