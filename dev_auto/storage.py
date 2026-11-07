from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid
import pwd


class SafetyError(RuntimeError):
    pass


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic(path: Path, value):
    private_dir(path.parent)
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def redact(value):
    """Defense in depth. Raw subprocess output/credentials are never logged."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"token|cookie|password|secret|api.?key|authorization", k, re.I)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?:sk-[\w-]{12,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)", "[REDACTED]", value)
        value = re.sub(r"(?im)((?:authorization|cookie|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*)([^\n,;}]+)", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return value


def git(project, *args, check=True):
    result = subprocess.run(["git", "-C", str(project), *args], capture_output=True, timeout=60,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if check and result.returncode:
        raise SafetyError("Verificação Git falhou; nenhum agente foi iniciado.")
    return result.stdout


def find_git_root(path):
    """Return a worktree root, or None for an ordinary folder. Broken Git fails closed."""
    path = Path(path).resolve()
    markers = [parent / ".git" for parent in (path, *path.parents)]
    try:
        process = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                                 capture_output=True, timeout=15,
                                 env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    except FileNotFoundError:
        if any(marker.exists() or marker.is_symlink() for marker in markers):
            raise SafetyError("Este projeto tem metadados Git, mas o executável Git não está disponível.")
        return None
    if process.returncode == 0:
        return Path(os.fsdecode(process.stdout).strip()).resolve()
    if b"not a git repository" in process.stderr and not any(marker.exists() or marker.is_symlink() for marker in markers):
        return None
    raise SafetyError("A verificação do repositório Git falhou. Metadados preservados para inspeção.")


def project_root(path):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise SafetyError("O projeto precisa ser um diretório.")
    return find_git_root(path) or path


def state_home_path(home=None):
    return Path(home or os.environ.get("DEVSWITCH_HOME") or os.environ.get("DEV_AUTO_HOME")
                or Path.home() / ".local/state/dev-auto").expanduser().resolve()


def scope_lock_root():
    # Independent of both state overrides and HOME; all invocations by this user agree.
    # Computing a lock path must not write/chmod the account home during config
    # queries or the Homebrew sandbox test. Create it only when taking a lock.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/devswitch-locks"


def scope_path(path):
    identity = Path(path).resolve().stat()
    # Filesystem identity also covers case aliases on normal macOS volumes.
    key = f"{identity.st_dev}:{identity.st_ino}"
    return private_dir(scope_lock_root()) / (hashlib.sha256(key.encode()).hexdigest() + ".lock")


class ProjectLock:
    def __init__(self, path, scope=None):
        self.path, self.fd = Path(path), None
        self.scope = Path(scope).resolve() if scope is not None else None
        self.fds = []

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if self.scope is not None:
                # Shared ancestors + exclusive root prevent parent/child writers;
                # sibling folders remain independent. Guardians inherit every fd.
                for parent in reversed(self.scope.parents):
                    fd = os.open(scope_path(parent), os.O_RDWR | os.O_CREAT, 0o600)
                    self.fds.append(fd)
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fd = os.open(scope_path(self.scope), os.O_RDWR | os.O_CREAT, 0o600)
                self.fds.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            self.fds.append(self.fd)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.__exit__()
            raise SafetyError("Já existe um controlador ou agente ativo neste projeto ou em uma pasta sobreposta.")
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        # Do not unlock explicitly: a surviving guardian must retain all locks.
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []
        self.fd = None

    @staticmethod
    def busy(path, scope=None):
        try:
            with ProjectLock(path, scope):
                return False
        except SafetyError:
            return True


class Store:
    def __init__(self, project, home=None):
        self.project = project_root(project)
        self.has_git = find_git_root(self.project) is not None
        self.home = private_dir(state_home_path(home))
        if self.project.is_relative_to(self.home.resolve()):
            raise SafetyError("O diretório de estado não pode conter a pasta do projeto; use um diretório separado ou uma subpasta para o estado.")
        self.key = hashlib.sha256(str(self.project).encode()).hexdigest()[:24]
        self.path = private_dir(self.home / "projects" / self.key)
        common = git(self.project, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip() if self.has_git else None
        # Repository-wide, independent of DEV_AUTO_HOME; shared by all worktrees.
        # Never chmod the existing Git directory or delete the lock inode.
        self.lock_path = Path(common).resolve() / "dev-auto.lock" if common else scope_lock_root() / ("project-" + self.key + ".lock")
        self.state_path = self.path / "state.json"
        self.requests = private_dir(self.path / "requests")
        atomic(self.path / "workspace.json", {"project": str(self.project), "opened_at": time.time()})

    def lock(self):
        return ProjectLock(self.lock_path, self.project)

    def busy(self):
        return ProjectLock.busy(self.lock_path, self.project)

    def folder_files(self):
        """No Git dependency: inventory source files, without following directory links."""
        excluded = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
                    ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt",
                    ".ssh", ".aws", ".codex", ".claude", ".Trash"}
        patterns = []
        ignore = self.project / ".devswitchignore"
        if ignore.is_file() and not ignore.is_symlink():
            patterns = [line.strip().rstrip("/") for line in ignore.read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]

        def skip(path):
            if path.resolve().is_relative_to(self.home.resolve()):
                return True
            rel = path.relative_to(self.project).as_posix()
            return path.name in excluded or any(fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(path.name, pat) for pat in patterns)

        def failed(_):
            raise SafetyError("Não foi possível ler todos os arquivos para o backup; nenhum novo agente foi iniciado.")

        files = []
        for root, directories, names in os.walk(self.project, followlinks=False, onerror=failed):
            parent = Path(root)
            for name in list(directories):
                path = parent / name
                if skip(path):
                    directories.remove(name)
                elif path.is_symlink():
                    directories.remove(name)
                    files.append(path.relative_to(self.project).as_posix())
            for name in names:
                path = parent / name
                if not skip(path):
                    files.append(path.relative_to(self.project).as_posix())
        return files

    def load(self):
        state = read_json(self.state_path)
        if state is not None and (state.get("schema") != 1 or state.get("project") != str(self.project)):
            raise SafetyError("Estado incompatível; preservado para inspeção.")
        return state

    def save(self, state):
        state["updated_at"] = time.time()
        atomic(self.state_path, state)

    def event(self, event, **fields):
        record = {"at": time.time(), "event": event, **redact(fields)}
        fd = os.open(self.path / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def request(self, action, agent=None):
        atomic(self.requests / f"{time.time_ns()}-{uuid.uuid4().hex}.json", {"action": action, "agent": agent})

    def pop_requests(self):
        items = []
        for p in sorted(self.requests.glob("*.json")):
            items.append(read_json(p))
            p.unlink()
        return items

    def checkpoint(self, label):
        """Read-only Git inventory + content-addressed copies; never stash/commit/reset."""
        if self.has_git and git(self.project, "ls-files", "-u"):
            raise SafetyError("Há conflitos Git não resolvidos. Sessão pausada para preservar o trabalho.")
        paths = (git(self.project, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
                 if self.has_git else [os.fsencode(name) for name in self.folder_files()])
        blobs = private_dir(self.path / "blobs")
        manifest = {}
        for raw in set(paths) - {b""}:
            name = os.fsdecode(raw)
            p = self.project / name
            if p.resolve().is_relative_to(self.home.resolve()):
                continue
            # Reject a symlink ancestor that could copy data outside this project.
            if not p.parent.resolve().is_relative_to(self.project):
                raise SafetyError("Arquivo atravessa link externo; verifique o projeto.")
            try:
                info = p.lstat()
            except FileNotFoundError:
                manifest[name] = {"deleted": True}
                continue
            if stat.S_ISLNK(info.st_mode):
                manifest[name] = {"symlink": os.readlink(p)}
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                temp = blobs / (uuid.uuid4().hex + ".tmp")
                with p.open("rb") as source, temp.open("xb") as target:
                    temp.chmod(0o600)
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
                sha = digest.hexdigest()
                if (blobs / sha).exists():
                    temp.unlink()
                else:
                    os.replace(temp, blobs / sha)
                # A changing file means another writer; do not start a new agent.
                after = p.stat()
                if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise SafetyError("Arquivo alterado durante backup; tente novamente quando estiver estável.")
                manifest[name] = {"sha256": sha, "mode": stat.S_IMODE(info.st_mode), "size": info.st_size}
            elif stat.S_ISDIR(info.st_mode):
                raise SafetyError("Submódulos precisam de sessão própria; não iniciar no repositório pai.")
            else:
                raise SafetyError("Tipo de arquivo não suportado no checkpoint.")
        cid = str(time.time_ns()) + "-" + label
        folder = private_dir(self.path / "checkpoints" / cid)
        atomic(folder / "manifest.json", manifest)
        git_info = {
            "head": git(self.project, "rev-parse", "HEAD", check=False).decode().strip(),
            "branch": git(self.project, "symbolic-ref", "--short", "HEAD", check=False).decode().strip(),
            "status": git(self.project, "status", "--porcelain=v1", "-z").decode(errors="replace"),
        } if self.has_git else None
        atomic(folder / ("git.json" if self.has_git else "workspace.json"), git_info or {"mode": "folder", "project": str(self.project)})
        index_path = git(self.project, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip() if self.has_git else None
        if index_path and Path(index_path).is_file():
            shutil.copyfile(index_path, folder / "index.backup")
            (folder / "index.backup").chmod(0o600)
        return {"id": cid, "files": manifest, "git": {k: git_info[k] for k in ("head", "branch")} if git_info else None,
                "fingerprint": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}


def changed(before, after):
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
