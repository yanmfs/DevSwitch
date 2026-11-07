"""Green phosphor terminal console. Standard library only; plain-output fallback."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import os
import re
import select
import shlex
import shutil
import sys
import textwrap
import unicodedata

# Load terminal support before displaying a prompt. A Ctrl+C during a lazy
# import can be consumed by an importlib weakref callback instead of cancelling.
try:
    import termios
    import tty
except ImportError:
    termios = tty = None

from .branding import OWNER, PORTRAIT, TAGLINE, WORDMARK
from .settings import AGENTS, EFFORTS, model_catalog, validate_settings
from .storage import SafetyError, redact
from .models import CLAUDE_VERSIONS, CLAUDE_ALIASES, model_label

LABELS = {"claude": "Claude Code", "codex": "Codex"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
COLORS = {"green": "38;2;148;231;151", "bright": "1;38;2;186;255;181",
          "dim": "38;2;68;112;77", "muted": "38;2;126;157;133",
          "warning": "38;2;232;192;112", "error": "38;2;239;138;127"}
COMMANDS = ["/help", "/status", "/config", "/models", "/first", "/model", "/effort",
            "/pause", "/switch", "/clear", "/exit", "/sair"]


class Cancelled(SafetyError):
    pass


def cells(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
               for c in ANSI.sub("", str(value)))


def clean(value):
    value = ANSI.sub("", str(redact(value)))
    return "".join(c for c in value if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf"}).replace("\t", "  ")


def fit(value, width):
    value = clean(value).replace("\n", " ")
    if cells(value) <= width:
        return value
    result = ""
    for c in value:
        if cells(result + c) > max(0, width - 1):
            break
        result += c
    return result + ("…" if width else "")


def wrap_cells(value, width):
    """Wrap by terminal cells, including combining marks and wide Unicode paths."""
    rows = []
    for paragraph in clean(value).split("\n"):
        current = ""
        for token in re.findall(r"\s+|\S+", paragraph):
            if cells(current + token) <= width:
                current += token
                continue
            if current.strip():
                rows.append(current.rstrip())
            current = "" if token.isspace() else token
            while cells(current) > width:
                chunk = ""
                count = 0
                for char in current:
                    if cells(chunk + char) > width:
                        break
                    chunk += char
                    count += 1
                rows.append(chunk)
                current = current[count:]
        rows.append(current.rstrip())
    return rows


class TerminalUI:
    def __init__(self, read=None, write=None, color=None, width=None):
        self.read = read or input
        self._output = write or print
        self.write = self._write
        self.native_input = read is None and write is None
        terminal = sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ
        self.color = terminal if color is None else color
        self.width = max(32, min(width or shutil.get_terminal_size((100, 24)).columns, 128))
        self.project = None
        self.live_buffer = ""
        self.live_eof = False
        self.live_dashboard = None
        self.input_dock = None

    def _write(self, value=""):
        dock = self.input_dock
        if dock:
            dock.before_output()
        self._output(value)
        if dock:
            dock.render()

    def ask_task(self):
        if not self._raw_available():
            return self.ask("\n  você › ")
        from .terminal_input import InputDock
        editor = InputDock(self)
        try:
            editor.open()
            return editor.readline()
        finally:
            editor.close()

    def start_dashboard(self):
        from .dashboard import LiveDashboard
        self.live_dashboard = LiveDashboard(self)

    def dashboard(self, state):
        if self.live_dashboard:
            self.live_dashboard.update(state)

    def stop_dashboard(self):
        if self.live_dashboard:
            self.live_dashboard.close()
            self.live_dashboard = None

    def tint(self, value, code="green"):
        code = COLORS.get(code, code)
        return f"\033[{code}m{value}\033[0m" if self.color else value

    def line(self, value="", style=None):
        for text in wrap_cells(value, self.width - 4):
            self.write("  " + (self.tint(text, style) if style else text))

    def rule(self, title=""):
        title = (" " + fit(clean(title).upper(), self.width - 10) + " ") if title else ""
        self.write(self.tint("  ──" + title + "─" * max(0, self.width - 6 - cells(title)), "dim"))

    def box(self, title, rows, style="green"):
        inside = self.width - 6
        self.write(self.tint("  ╭─ " + fit(title, inside - 3) + " " + "─" * max(0, inside - cells(fit(title, inside - 3)) - 3) + "╮", style))
        for row in rows:
            parts = wrap_cells(row, inside - 2)
            for part in parts:
                value = fit(part, inside - 2)
                self.write(self.tint("  │", "dim") + " " + value + " " * max(0, inside - 1 - cells(value)) + self.tint("│", "dim"))
        self.write(self.tint("  ╰" + "─" * inside + "╯", "dim"))

    def header(self, project, hint=None):
        self.project = project
        self.write("")
        self.rule("devswitch / personal coding system")
        if self.width >= 78:
            right = ["> INICIAR SISTEMA? S / N: S", "", "IDENTIDADE // " + OWNER, "", *WORDMARK,
                     "", TAGLINE, "", "MÓDULOS CARREGADOS", "CLAUDE CODE  ⇄  CODEX",
                     "CONTEXTO PERSISTENTE · CONTROLE EXCLUSIVO", "", "LOCAL  /  MACOS  /  TERMINAL", "",
                     "> while (curious) { keep_building(); }"]
            left_width = max(cells(row) for row in PORTRAIT)
            for index in range(max(len(PORTRAIT), len(right))):
                portrait = PORTRAIT[index] if index < len(PORTRAIT) else ""
                label = right[index] if index < len(right) else ""
                shade = "bright" if index in {2, 4, 5, 6} else "green" if index % 2 else "muted"
                self.write("  " + self.tint(portrait, "green" if index % 2 else "bright")
                           + " " * (left_width - cells(portrait) + 3)
                           + self.tint(fit(label, self.width - left_width - 7), shade))
        else:
            if self.width >= 44:
                for row in WORDMARK:
                    self.line(row, "bright")
            else:
                self.line("DEVSWITCH", "bright")
            self.line(OWNER + " // " + TAGLINE, "green")
            self.line("Claude Code ⇄ Codex", "muted")
        self.rule()
        self.line("PROJETO  " + str(project), "muted")
        self.line(hint or "/help comandos   /config agentes   /status sessão", "dim")
        self.write("")

    def ask(self, message):
        try:
            prompt = self.tint(message, "bright")
            if self.native_input and self.color and "readline" in sys.modules:
                prompt = ANSI.sub(lambda match: "\001" + match[0] + "\002", prompt)
            return self.read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled("Inicialização cancelada; nenhum agente foi iniciado.")

    def _raw_available(self):
        if not (termios and tty and self.native_input and self.color and self.width >= 64 and sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        try:
            termios.tcgetattr(sys.stdin.fileno())
            return True
        except (ImportError, OSError, ValueError, AttributeError):
            return False

    def _draw_choices(self, choices, index):
        for i, (_, label) in enumerate(choices):
            prefix = "›" if i == index else " "
            mark = " [enter]" if i == index else ""
            self.write("  " + self.tint(f"{prefix} {i + 1:>2}  " + fit(label, self.width - 16 - len(mark)) + mark,
                                       "bright" if i == index else "muted"))
        hint = "↑/↓ mover · enter escolher · número ou nome" if self._raw_available() else "número ou nome · enter mantém a seleção"
        self.line(hint, "dim")

    def _raw_choose(self, choices, index):
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        typed = ""
        try:
            tty.setcbreak(fd, when=termios.TCSANOW)
            while True:
                key = os.read(fd, 1)
                if key in {b"\x03", b"\x04"} or not key:
                    raise Cancelled("Inicialização cancelada; nenhum agente foi iniciado.")
                if key == b"\x1b":
                    sequence = b""
                    for _ in range(2):
                        if select.select([fd], [], [], .06)[0]:
                            sequence += os.read(fd, 1)
                    if sequence in {b"[A", b"[B"}:
                        index = (index + (-1 if sequence == b"[A" else 1)) % len(choices)
                        typed = ""
                        self.write(f"\033[{len(choices) + 2}A\033[J")
                        self._draw_choices(choices, index)
                    continue
                if key in {b"\n", b"\r"}:
                    answer = typed.strip()
                    answer = {"/help": "help", "/sair": "exit", "/exit": "exit"}.get(answer, answer)
                    if not answer:
                        chosen = index
                    elif answer.isdecimal() and 1 <= int(answer) <= len(choices):
                        chosen = int(answer) - 1
                    else:
                        chosen = next((i for i, (name, _) in enumerate(choices) if name == answer), None)
                    if chosen is not None:
                        self.line("✓ " + choices[chosen][1], "green")
                        return choices[chosen][0]
                    typed = ""
                    self.line("Escolha uma das opções acima.", "warning")
                elif key in {b"\x7f", b"\x08"}:
                    typed = typed[:-1]
                elif key.isascii() and key.decode().isprintable():
                    typed += key.decode()
        except KeyboardInterrupt:
            raise Cancelled("Inicialização cancelada; nenhum agente foi iniciado.")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def choose(self, title, choices, default=None):
        if not choices:
            raise SafetyError("Nenhuma opção disponível.")
        self.rule(title)
        index = next((i for i, (key, _) in enumerate(choices) if key == default), 0)
        self._draw_choices(choices, index)
        if self._raw_available():
            return self._raw_choose(choices, index)
        while True:
            answer = self.ask(f"  Escolha [{index + 1}] › ")
            answer = {"/help": "help", "/sair": "exit", "/exit": "exit"}.get(answer, answer)
            if not answer:
                return choices[index][0]
            if answer.isdecimal() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1][0]
            if any(answer == key for key, _ in choices):
                return answer
            self.line("Digite o número ou o nome de uma das opções.", "warning")

    def configure(self, settings, catalog=None):
        result = deepcopy(settings)
        result["primary"] = self.choose("01 / ordem de execução", [
            ("claude", "Claude Code primeiro  →  Codex depois"),
            ("codex", "Codex primeiro  →  Claude Code depois")], result["primary"])
        catalog = model_catalog() if catalog is None else catalog
        for agent in AGENTS:
            self.write("")
            previous = result["agents"][agent]
            if agent == "claude":
                choices = [*CLAUDE_VERSIONS, *CLAUDE_ALIASES]
            else:
                choices = [(m["model"], m["label"]) for m in catalog]
                choices = choices or [(previous["model"] or "gpt-6.1-sol", previous["model"] or "gpt-6.1-sol")]
                choices.sort(key=lambda item: item[0] != previous["model"])
            if previous["model"] and previous["model"] not in {key for key, _ in choices}:
                choices.insert(0, (previous["model"], previous["model"]))
            choices += [("default", "Padrão do CLI"), ("custom", "Informar outro ID de modelo")]
            model = self.choose(f"{'02' if agent == 'claude' else '04'} / {LABELS[agent]} / modelo", choices, previous["model"] or "default")
            if model == "custom":
                model = self.ask("  ID do modelo › ")
                if not model:
                    raise SafetyError("Informe um ID de modelo para continuar.")
            previous["model"] = None if model == "default" else model
            allowed = EFFORTS
            info = next((m for m in catalog if m["model"] == model), None) if agent == "codex" else None
            if info and info["efforts"]:
                allowed = tuple(info["efforts"])
            if agent == "claude" and model == "haiku":
                allowed = ()
            effort = self.choose(f"{'03' if agent == 'claude' else '05'} / {LABELS[agent]} / effort",
                                 [(x, x) for x in allowed] + [("default", "Padrão do modelo")], previous["effort"] or "default")
            previous["effort"] = None if effort == "default" else effort
        result = validate_settings(result)
        self.summary(result)
        return result

    def summary(self, settings):
        first = settings["primary"]
        second = "codex" if first == "claude" else "claude"
        rows = []
        for number, name in enumerate((first, second), 1):
            cfg = settings["agents"][name]
            rows.append(f"{number}º  {LABELS[name]} · {model_label(name, cfg['model'])} · effort {cfg['effort'] or 'padrão do modelo'}")
        rows.append("Handoff automático · progresso e contexto preservados")
        self.write("")
        self.box("SESSÃO CONFIGURADA", rows)
        self.write("")

    def help(self, live=False):
        rows = [("/help", "Ver os comandos"), ("/status", "Estado, agentes, modelos e consumo"),
                ("/clear", "Limpar a tela"), ("/pause", "Pausar na próxima etapa segura"),
                ("/switch claude|codex|auto", "Pedir uma transferência segura")]
        if not live:
            rows = [("/config", "Escolher a ordem, os modelos e o effort"),
                    ("/first claude|codex", "Escolher quem começa"), ("/model claude claude-sonnet-5-5", "Fixar a versão de um agente (aliases também aceitos)"),
                    ("/effort codex high", "Alterar o effort de um agente"), ("/models", "Ver modelos do catálogo local"), *rows]
        rows += [("/exit  ou  /sair", "Pausar e sair" if live else "Sair sem iniciar a tarefa")]
        self.rule("comandos da sessão")
        for command, meaning in rows:
            self.line(command, "bright")
            self.line("  " + meaning, "muted")
        self.rule("no terminal")
        for row in ["devswitch                 abrir este console", "devswitch config          preferências do projeto",
                    "devswitch status          consultar a sessão", "devswitch resume          retomar com contexto",
                    "devswitch pause           solicitar pausa segura", "devswitch logs            histórico de operações",
                    "devswitch doctor          verificar os dois CLIs", "devswitch demo            ciclo simulado, sem cota"]:
            self.line(row, "green")
        self.line('Digite uma tarefa para iniciar, por exemplo: "implemente o login e valide os testes".', "muted")
        self.write("")

    def clear(self):
        if self.color and self.native_input:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        if self.project is not None:
            self.header(self.project)

    def models(self):
        self.rule("modelos / claude code")
        for value, label in CLAUDE_VERSIONS:
            self.line(label + "  /  " + value, "green")
        self.line("Aliases automáticos: sonnet · opus · haiku · fable", "muted")
        self.line("Versão fixa preserva o número escolhido; aliases acompanham o CLI.", "dim")
        self.rule("modelos / codex")
        catalog = model_catalog()
        for item in catalog:
            self.line(item["model"] + "  /  " + ", ".join(item["efforts"]), "green")
        if not catalog:
            self.line("Catálogo local indisponível. Use /model codex <id>.", "warning")

    def workspace(self, current, recent):
        from .workspaces import is_launcher_directory
        self.header("nenhum projeto selecionado", hint="Escolha um projeto · /help ajuda · /sair fechar")
        self.line("Console aberto em " + str(current), "muted")
        self.line("Escolha a pasta de trabalho para começar.", "bright")
        choices = [(str(path), path.name + "  /  " + str(path)) for path in recent]
        choices += [("browse", "Escolher outra pasta"), ("help", "Ver os comandos"), ("exit", "Sair")]
        while True:
            selection = self.choose("projetos recentes / abrir projeto", choices, choices[0][0])
            if selection == "exit":
                return None
            if selection == "help":
                self.help()
                continue
            if selection != "browse":
                return Path(selection)
            while True:
                raw = self.ask("  Pasta do projeto (/sair para voltar) › ")
                if raw in {"/sair", "/exit", "/quit"}:
                    break
                if not raw:
                    continue
                candidate = Path(raw).expanduser()
                if not candidate.is_dir():
                    try:
                        parts = shlex.split(raw)
                        if len(parts) == 1:
                            candidate = Path(parts[0]).expanduser()
                    except ValueError:
                        pass
                if not candidate.is_dir():
                    self.line("Pasta não encontrada. Cole o caminho de uma pasta existente.", "warning")
                    continue
                candidate = candidate.resolve()
                if is_launcher_directory(candidate):
                    self.line("Escolha a pasta específica do projeto. Para uma tarefa deliberadamente nesta raiz, use devswitch -C <pasta>.", "warning")
                    continue
                return candidate

    def task(self, settings=None, status_callback=None, save_callback=None):
        if self.native_input and sys.stdin.isatty() and self.color:
            try:
                import readline
                readline.set_completer(lambda text, state: ([item for item in COMMANDS if item.startswith(text)] + [None])[state])
                readline.set_completer_delims(" \t\n")
                readline.parse_and_bind("tab: complete")
            except ImportError:
                pass
        self.line("PRONTO. O que vamos construir?", "bright")
        self.line("Escreva a tarefa · /help comandos · /config ajustar · /sair fechar", "muted")
        while True:
            text = self.ask_task()
            if text in {"/sair", "/exit", "/quit"}:
                self.line("Sessão encerrada. Keep building.", "dim")
                return None
            if not text:
                continue
            if not text.startswith("/"):
                return text
            try:
                parts = shlex.split(text)
                command, args = parts[0], parts[1:]
                if command in {"/help", "/?"}:
                    self.help()
                elif command == "/clear":
                    self.clear()
                elif command == "/models":
                    self.models()
                elif command == "/status":
                    if status_callback:
                        status_callback()
                    elif settings:
                        self.summary(settings)
                    else:
                        self.line("Nenhuma tarefa iniciada.", "muted")
                elif command in {"/config", "/first", "/model", "/effort"}:
                    if settings is None:
                        raise SafetyError("Abra devswitch para configurar a sessão.")
                    updated = self.configure(settings) if command == "/config" else deepcopy(settings)
                    if command == "/first":
                        if len(args) != 1 or args[0] not in AGENTS:
                            raise SafetyError("Use /first claude ou /first codex.")
                        updated["primary"] = args[0]
                    if command in {"/model", "/effort"}:
                        if len(args) != 2 or args[0] not in AGENTS:
                            raise SafetyError(f"Use {command} claude|codex <valor>.")
                        updated["agents"][args[0]][command[1:]] = None if args[1] == "default" else args[1]
                    updated = validate_settings(updated)
                    settings.clear()
                    settings.update(updated)
                    if save_callback:
                        save_callback(settings)
                    if command != "/config":
                        self.summary(settings)
                elif command in {"/pause", "/switch"}:
                    self.line("A tarefa ainda não começou. Use /first para escolher quem inicia.", "warning")
                else:
                    self.line(f"Comando desconhecido: {fit(command, 35)}. Use /help.", "warning")
            except Cancelled:
                raise
            except (SafetyError, ValueError) as error:
                self.line(str(error), "warning")

    def status(self, data):
        self.write("")
        self.rule("estado da sessão")
        self.line("Estado: " + data["state"], "bright")
        self.line("Projeto: " + data["project"], "muted")
        if data.get("project_mode"):
            self.line("Proteção: " + ("Git + snapshots" if data["project_mode"] == "git" else "snapshots de arquivos · sem Git"), "dim")
        if "task" not in data:
            self.line("Nenhuma tarefa iniciada neste projeto.", "muted")
            return
        self.line(f"Ativo: {data['active_agent'] or 'nenhum'} · Etapa {data['step']}", "green")
        self.summary(data["settings"])
        self.line("Tarefa: " + data["task"], "bright")
        for name, usage in data["availability"].items():
            percent = usage.get("used_percent")
            meter = "não informado pelo CLI"
            if isinstance(percent, (int, float)):
                count = round(max(0, min(100, percent)) / 5)
                meter = "█" * count + "░" * (20 - count) + f"  {percent:.1f}%"
            self.line(f"{LABELS.get(name, name)} / {usage['status']} / {meter}", "green")
            if usage.get("resets_at"):
                self.line("  Renovação: " + datetime.fromtimestamp(usage["resets_at"]).astimezone().strftime("%d/%m %H:%M:%S %Z"), "muted")
            observed = data.get("runtime", {}).get(name, {})
            if observed.get("reported_model"):
                self.line("  Modelo informado pelo CLI: " + observed["reported_model"], "muted")
        transfer = data.get("last_handoff")
        self.line("Última transferência: " + (f"{transfer['from']} → {transfer['to']}" if transfer else "nenhuma"), "muted")
        self.line("Contexto: " + data["context"], "dim")
        self.write("")

    def doctor(self, info):
        self.rule("diagnóstico / integrações")
        for name, item in info.items():
            ready = item.get("path") and item.get("subscription_login") and item.get("interfaces_ok")
            self.line(("✓ " if ready else "! ") + LABELS[name], "bright" if ready else "warning")
            self.line("  " + str(item.get("version") or "não instalado"), "muted")
            self.line("  Login por assinatura: " + ("confirmado" if item.get("subscription_login") else "não confirmado"), "green")
            self.line("  Interface: " + ("compatível" if item.get("interfaces_ok") else "verificar instalação"), "muted")
            if item.get("quota_error"):
                self.line("  " + item["quota_error"], "warning")
        self.line("devswitch doctor --json --refresh  /  detalhes e consulta de cotas", "dim")

    def event(self, message):
        self.line(f"{datetime.now():%H:%M:%S}  ›  {message}", "green")

    def logs(self, records):
        self.rule("histórico / checkpoints e transferências")
        for item in records:
            stamp = datetime.fromtimestamp(item["at"]).astimezone().strftime("%d/%m %H:%M:%S")
            detail = " · ".join(str(item[k]) for k in ("agent", "model", "effort", "outcome") if item.get(k) is not None)
            self.line(f"{stamp}  {item['event']}" + ("  /  " + detail if detail else ""), "green")
        self.write("")

    def poll_live(self):
        if self.input_dock and self.input_dock.live:
            return self.input_dock.poll()
        if self.live_eof or not self.native_input or not sys.stdin.isatty():
            return []
        try:
            fd = sys.stdin.fileno()
            if not select.select([fd], [], [], 0)[0]:
                return []
            data = os.read(fd, 4096)
            if not data:
                self.live_eof = True
                return []
            self.live_buffer += data.decode("utf-8", errors="replace")
            lines = self.live_buffer.split("\n")
            self.live_buffer = lines.pop()
            return [line.strip() for line in lines if line.strip()]
        except (OSError, ValueError):
            self.live_eof = True
            return []

    def live_command(self, command, store, status_callback):
        try:
            parts = shlex.split(command)
            if not parts:
                return
            name, args = parts[0], parts[1:]
            if name in {"/help", "/?"}:
                self.help(live=True)
            elif name == "/status":
                status_callback()
            elif name == "/clear":
                self.clear()
            elif name in {"/pause", "/sair", "/exit", "/quit"}:
                store.request("pause")
                self.event("Pausa solicitada. A etapa atual será concluída e o contexto será salvo.")
            elif name == "/switch" and len(args) == 1 and args[0] in {*AGENTS, "auto"}:
                store.request("switch", args[0])
                self.event("Transferência solicitada para " + args[0] + ". Será aplicada na próxima etapa segura.")
            elif name in {"/config", "/first", "/model", "/effort"}:
                self.line("Use /pause e depois devswitch resume --configure para ajustar esta tarefa.", "warning")
            else:
                self.line("Durante a execução, use /help, /status, /pause ou /switch claude|codex|auto.", "warning")
        except ValueError:
            self.line("Comando inválido. Use /help.", "warning")
