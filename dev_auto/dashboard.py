"""Terminal footer; every meter is a provider observation, never an estimate."""
import math
import shutil
import sys
import time

from .models import model_label

LABELS = {"claude": "Claude Code", "codex": "Codex"}
STATUS = {"unknown": "a verificar", "available": "disponível", "limited": "limite atingido",
          "near_limit": "perto do limite", "missing": "CLI ausente", "auth_required": "login necessário"}


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02}s"
    return f"{seconds}s"


def quota_rows(state, now=None, bar_width=16):
    now = time.time() if now is None else now
    rows = [f"COTAS / {state['status']} · etapa {state.get('step', 0)} · decorrido {duration(now - state.get('created_at', now))}"]
    settings = state.get("settings", {}).get("agents", {})
    for name in ("claude", "codex"):
        usage = state.get("availability", {}).get(name, {})
        percent = usage.get("used_percent")
        known = type(percent) in (int, float) and math.isfinite(percent) and 0 <= percent <= 100
        if known:
            filled = round(percent * bar_width / 100)
            meter = "█" * filled + "░" * (bar_width - filled)
            label = f"{percent:.1f}% usado"
        else:
            meter, label = "·" * bar_width, "% não informado"
        role = "EM EXECUÇÃO" if state.get("active_agent") == name else STATUS.get(usage.get("status"), "a verificar")
        rows.append(f"{LABELS[name]:11} [{meter}] {label} · {role}")
        cfg = settings.get(name, {})
        detail = f"  {model_label(name, cfg.get('model'))} · effort {cfg.get('effort') or 'padrão'}"
        retry = usage.get("retry_at") or 0
        if usage.get("status") in {"limited", "near_limit"}:
            detail += " · " + (f"nova tentativa em {duration(retry - now)}" if retry > now else "nova tentativa na próxima etapa segura")
        elif usage.get("resets_at") and usage["resets_at"] > now:
            detail += f" · renovação em {duration(usage['resets_at'] - now)}"
        rows.append(detail)
        observed = usage.get("quota_observed_at") or (usage.get("observed_at") if known else None)
        rows.append("  Última leitura de cota há " + duration(now - observed) if observed else "  Sem leitura de percentual; disponibilidade conforme eventos do CLI")
    rows.append("/status detalhes · /pause salvar · /switch claude|codex|auto")
    return rows


class LiveDashboard:
    def __init__(self, ui):
        self.ui = ui
        self.active = ui.native_input and sys.stdout.isatty()
        self.animated = self.active and ui.color
        self.layout = None
        self.height = 0
        self.last_tick = 0
        self.last_plain = None
        self.last_state = None
        self.editor = None
        if ui._raw_available():
            from .terminal_input import InputDock
            self.editor = InputDock(ui, live=True)
            self.editor.open()

    def update(self, state, force=False):
        if not self.active:
            return
        self.last_state = state
        if self.editor:
            self.editor.state = state
        tick = time.monotonic()
        if not force and tick - self.last_tick < 1:
            return
        self.last_tick = tick
        if self.editor:
            self.editor.render()
            return
        size = shutil.get_terminal_size((80, 24))
        rows = quota_rows(state, bar_width=16 if size.columns >= 80 else 8)
        if not self.animated or size.lines < len(rows) + 8 or size.columns < 64:
            self.restore()
            signature = (state["status"], state.get("active_agent"), tuple(
                (k, v.get("status"), v.get("used_percent")) for k, v in state.get("availability", {}).items()))
            if signature != self.last_plain:
                self.last_plain = signature
                for row in (rows[0], rows[1], rows[4]):
                    self.ui.line(row, "green")
            return
        bottom = size.lines - len(rows)
        if self.layout != (size.columns, size.lines):
            self.restore()
            sys.stdout.write("\n" * len(rows) + f"\033[1;{bottom}r\033[{bottom};1H")
            self.layout = (size.columns, size.lines)
            self.height = len(rows)
        # Logs and canonical keyboard echo stay in the upper scroll region.
        # Save/restore the cursor so repainting never discards a typed command.
        from .ui import fit
        output = ["\033[s"]
        for index, row in enumerate(rows, bottom + 1):
            output.append(f"\033[{index};1H\033[2K" + self.ui.tint(fit("  " + row, size.columns - 1), "bright" if index == bottom + 1 else "green"))
        output.append("\033[u")
        sys.stdout.write("".join(output))
        sys.stdout.flush()

    def restore(self):
        if self.layout:
            columns, lines = self.layout
            sys.stdout.write("\033[r" + f"\033[{lines - self.height + 1};1H\033[J")
            sys.stdout.flush()
            self.layout = None

    def close(self):
        if self.editor:
            self.editor.close()
            self.editor = None
        self.restore()
        if self.active and self.last_state:
            for row in quota_rows(self.last_state):
                self.ui.line(row, "muted")
        self.active = False
