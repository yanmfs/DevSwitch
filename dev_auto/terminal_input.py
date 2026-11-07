"""Small terminal editor and slash palette, sharing one footer with quota meters."""
import codecs
import os
import select
import shutil
import sys
import termios
import tty

COMMANDS = [
    ("/help", "Ver comandos e atalhos"), ("/status", "Ver sessão, agentes e cotas"),
    ("/config", "Escolher ordem, modelos e effort"), ("/models", "Listar modelos disponíveis"),
    ("/first ", "Escolher quem começa: claude ou codex"),
    ("/model ", "Escolher modelo: agente e ID"), ("/effort ", "Escolher esforço: agente e nível"),
    ("/pause", "Salvar e pausar na próxima etapa segura"),
    ("/switch ", "Transferir para claude, codex ou auto"),
    ("/clear", "Limpar a tela"), ("/exit", "Sair ou pausar a execução"), ("/sair", "Sair ou pausar a execução"),
]
LIVE = {"/help", "/status", "/pause", "/switch", "/clear", "/exit", "/sair"}


def suggestions(text, live=False):
    if not text.startswith("/") or "\n" in text:
        return []
    if text.startswith(("/first ", "/switch ")):
        command = text.split(" ", 1)[0]
        agents = ("claude", "codex", "auto") if command == "/switch" else ("claude", "codex")
        return [(command + " " + agent, "Usar " + agent) for agent in agents if (command + " " + agent).startswith(text)]
    if text.startswith(("/model ", "/effort ")) and not live:
        command, rest = text.split(" ", 1)
        if " " not in rest:
            return [(command + " " + agent + " ", "Configurar " + agent) for agent in ("claude", "codex") if agent.startswith(rest)]
        agent, value = rest.split(" ", 1)
        if command == "/effort":
            values = ("low", "medium", "high", "xhigh", "max", "default")
        elif agent == "claude":
            from .models import CLAUDE_VERSIONS, CLAUDE_ALIASES
            values = [key for key, _ in (*CLAUDE_VERSIONS, *CLAUDE_ALIASES)] + ["default"]
        else:
            return []  # Custom and Codex IDs remain ordinary editable text.
        return [(f"{command} {agent} {item}", item) for item in values if item.startswith(value)]
    return [(value, detail) for value, detail in COMMANDS
            if (not live or value.strip() in LIVE) and value.startswith(text)]


class InputDock:
    def __init__(self, ui, live=False):
        self.ui, self.live = ui, live
        self.fd = sys.stdin.fileno()
        self.old = None
        self.layout = None
        self.text, self.cursor, self.selected = "", 0, 0
        self.escape = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pasting = False
        self.state = None
        self.history = []
        self.history_index = 0
        self.closed = False

    def open(self):
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd, when=termios.TCSANOW)
        sys.stdout.write("\033[?2004h")
        self.ui.input_dock = self
        self.render()
        return self

    def insert(self, value):
        self.text = self.text[:self.cursor] + value + self.text[self.cursor:]
        self.cursor += len(value)
        self.selected = 0

    def key(self, char):
        if self.escape:
            self.escape += char
            if self.escape == "\033[":
                return
            if len(self.escape) == 2 or not (char.isalpha() or char == "~"):
                if len(self.escape) > 24 or len(self.escape) == 2:
                    self.escape = ""
                return
            sequence, self.escape = self.escape, ""
            matches = suggestions(self.text, self.live)
            if sequence in {"\033[200~", "\033[201~"}:
                self.pasting = sequence == "\033[200~"
            elif sequence in {"\033[A", "\033[B"}:
                direction = -1 if sequence.endswith("A") else 1
                if matches:
                    self.selected = (self.selected + direction) % len(matches)
                elif self.history:
                    self.history_index = max(0, min(len(self.history), self.history_index + direction))
                    self.text = self.history[self.history_index] if self.history_index < len(self.history) else ""
                    self.cursor = len(self.text)
            elif sequence == "\033[C":
                self.cursor = min(len(self.text), self.cursor + 1)
            elif sequence == "\033[D":
                self.cursor = max(0, self.cursor - 1)
            elif sequence in {"\033[H", "\033[1~", "\033[7~"}:
                self.cursor = 0
            elif sequence in {"\033[F", "\033[4~", "\033[8~"}:
                self.cursor = len(self.text)
            elif sequence == "\033[3~":
                self.text = self.text[:self.cursor] + self.text[self.cursor+1:]
            return
        if char == "\033":
            self.escape = char
        elif char in {"\n", "\r"}:
            if self.pasting:
                self.insert(" ")
                return
            matches = suggestions(self.text, self.live)
            if matches and self.text != matches[self.selected % len(matches)][0]:
                self.text = matches[self.selected % len(matches)][0]
                self.cursor = len(self.text)
                if self.text.endswith(" "):
                    return
            value = self.text.strip()
            if value:
                self.history.append(value)
            self.history_index = len(self.history)
            self.text, self.cursor, self.selected = "", 0, 0
            return value
        elif char == "\t":
            matches = suggestions(self.text, self.live)
            if matches:
                self.text = matches[self.selected % len(matches)][0]
                self.cursor = len(self.text)
        elif char in {"\x7f", "\x08"}:
            if self.cursor:
                self.text = self.text[:self.cursor-1] + self.text[self.cursor:]
                self.cursor -= 1
            self.selected = 0
        elif char == "\x01":
            self.cursor = 0
        elif char == "\x05":
            self.cursor = len(self.text)
        elif char == "\x15":
            self.text, self.cursor = self.text[self.cursor:], 0
        elif char == "\x0b":
            self.text = self.text[:self.cursor]
        elif char == "\x17":
            prefix = self.text[:self.cursor].rstrip()
            start = prefix.rfind(" ") + 1
            self.text = self.text[:start] + self.text[self.cursor:]
            self.cursor = start
        elif char in {"\x03", "\x04"} and not self.text:
            if self.live:
                return "/pause"
            from .ui import Cancelled
            raise Cancelled("Inicialização cancelada; nenhum agente foi iniciado.")
        elif char.isprintable():
            self.insert(char)

    def read_key(self):
        block = os.read(self.fd, 1)  # Never swallow typeahead belonging to the next prompt.
        if not block:
            from .ui import Cancelled
            raise Cancelled("Terminal fechado; progresso preservado.")
        for char in self.decoder.decode(block):
            result = self.key(char)
            if result is not None:
                return result

    def readline(self):
        while True:
            if select.select([self.fd], [], [], .2)[0]:
                value = self.read_key()
                self.render()
                if value is not None:
                    return value
            else:
                self.render()

    def poll(self):
        lines = []
        changed = False
        while select.select([self.fd], [], [], 0)[0]:
            value = self.read_key()
            changed = True
            if value is not None:
                lines.append(value)
        if changed:
            self.render()
        return lines

    def before_output(self):
        if self.layout:
            sys.stdout.write(f"\033[{self.layout[2]};1H\033[2K")

    def render(self):
        if self.closed:
            return
        from .ui import cells, fit
        size = shutil.get_terminal_size((80, 24))
        quota = []
        if self.state:
            from .dashboard import quota_rows
            quota = quota_rows(self.state, bar_width=16 if size.columns >= 80 else 8)
            if size.lines < 30:
                quota = [quota[0], quota[1], quota[4]]
        matches = suggestions(self.text, self.live)
        slots = min(4, max(1, size.lines - len(quota) - 9))
        self.selected %= max(1, len(matches))
        start = max(0, self.selected - slots + 1)
        rows = [(row, "green") for row in quota]
        for index in range(slots):
            pos = start + index
            if pos < len(matches):
                value, detail = matches[pos]
                mark = "› " if pos == self.selected else "  "
                rows.append((mark + value.strip() + "  ·  " + detail, "bright" if pos == self.selected else "muted"))
            else:
                rows.append(("" if matches or index else "Digite / para ver os comandos", "dim"))
        rows += [("╭" + "─" * max(0, size.columns - 5), "dim")]
        # A horizontal viewport keeps the insertion cursor visible for long tasks.
        prefix = "  você › "
        visible_start = 0
        available = max(8, size.columns - cells(prefix) - 3)
        while cells(self.text[visible_start:self.cursor]) > available:
            visible_start += 1
        shown = self.text[visible_start:]
        input_row = len(rows)
        rows.append((prefix + fit(shown, available), "bright"))
        rows.append(("╰─ enter enviar · ↑↓ escolher · tab completar · ctrl+c interromper", "dim"))
        height = len(rows)
        bottom = max(1, size.lines - height)
        layout = (size.columns, size.lines, bottom, height)
        if self.layout != layout:
            self.restore_region()
            sys.stdout.write("\n" * height + f"\033[1;{bottom}r\033[{bottom};1H")
            self.layout = layout
        output = []
        for index, (row, style) in enumerate(rows, bottom + 1):
            output.append(f"\033[{index};1H\033[2K" + self.ui.tint(fit(row, size.columns - 1), style))
        cursor_column = cells(prefix + self.text[visible_start:self.cursor]) + 1
        output.append(f"\033[{bottom + input_row + 1};{min(size.columns - 1, cursor_column)}H")
        sys.stdout.write("".join(output))
        sys.stdout.flush()

    def restore_region(self):
        if self.layout:
            sys.stdout.write(f"\033[r\033[{self.layout[2]+1};1H\033[J")
            self.layout = None

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.restore_region()
        sys.stdout.write("\033[?2004l")
        sys.stdout.flush()
        if self.old is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.old)
        if self.ui.input_dock is self:
            self.ui.input_dock = None
