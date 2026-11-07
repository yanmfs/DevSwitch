import copy
import errno
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch
import fcntl

from dev_auto.branding import PORTRAIT
from dev_auto.cli import main
from dev_auto.demo import fixture
from dev_auto.settings import DEFAULTS, load_settings
from dev_auto.storage import Store
from dev_auto.ui import TerminalUI, cells, clean

ROOT = Path(__file__).resolve().parent.parent


class TerminalProcess:
    """Actual controlling TTY: exercises termios, arrow keys, stdin polling and signals."""
    def __init__(self, project, env, args=(), columns=100):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(project)
            os.execve(sys.executable, [sys.executable, '-m', 'dev_auto', *args], env)
        self.pid, self.fd, self.data, self.status = pid, fd, b'', None
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 48, columns, 0, 0))

    @property
    def text(self):
        return self.data.decode('utf-8', errors='replace')

    def pump(self, wait=.05):
        if select.select([self.fd], [], [], wait)[0]:
            try:
                data = os.read(self.fd, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                return False
            self.data += data
            return bool(data)
        return True

    def until(self, marker, timeout=12):
        deadline = time.monotonic() + timeout
        while marker not in self.text and time.monotonic() < deadline:
            self.pump()
        if marker not in self.text:
            raise AssertionError('TTY marker missing: ' + marker + '\n' + clean(self.text[-2000:]))
        return self.text

    def send(self, value):
        os.write(self.fd, value.encode())

    def finish(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            result, status = os.waitpid(self.pid, os.WNOHANG)
            if result:
                self.status = os.waitstatus_to_exitcode(status)
                self.pid = None
                for _ in range(5):
                    if not self.pump(.01):
                        break
                return self.status
        raise AssertionError('TTY did not exit:\n' + clean(self.text[-2000:]))

    def close(self):
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
                os.waitpid(self.pid, 0)
            except ProcessLookupError:
                pass
            self.pid = None
        os.close(self.fd)


class DevSwitchConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='devswitch-console-')
        self.folder = Path(self.temp.name)
        self.store, self.inventory = fixture(self.folder)
        (self.store.project / 'scenario.json').write_text('{"mode":"normal","capture_options":true}')
        self.env = {**os.environ, 'DEVSWITCH_HOME': str(self.store.home), 'DEV_AUTO_HOME': str(self.store.home),
                    'PATH': str(self.folder / 'bin') + os.pathsep + os.environ['PATH'],
                    'TERM': 'xterm-256color', 'PYTHONUNBUFFERED': '1'}

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(['devswitch', *args], cwd=self.store.project, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def test_brand_name_version_and_help_without_git(self):
        version = self.run_cli('--version')
        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout.strip(), 'devswitch 1.3.0')
        help_result = subprocess.run(['devswitch', 'help'], cwd=self.folder, env=self.env, capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('/switch claude|codex|auto', help_result.stdout)
        self.assertIn('devswitch doctor', help_result.stdout)
        self.assertNotIn('dev-auto', help_result.stdout)

    def test_portrait_is_bundled_text_and_has_no_runtime_image_dependency(self):
        self.assertGreater(len(PORTRAIT), 10)
        self.assertTrue(any(any(0x2800 <= ord(c) <= 0x28ff for c in line) for line in PORTRAIT))
        output = []
        TerminalUI(write=output.append, color=False, width=100).header(self.store.project)
        self.assertIn('YAN SUZANO', '\n'.join(output))
        self.assertTrue(any(PORTRAIT[4] in line for line in output))

    def test_layout_fits_small_large_and_unicode_paths(self):
        for width in (32, 40, 64, 78, 80, 100, 128):
            with self.subTest(width=width):
                output = []
                ui = TerminalUI(write=output.append, read=lambda _: '', color=False, width=width)
                ui.header('/projetos/项目/ação-com-nome-muito-longo/' * 2)
                ui.summary(DEFAULTS)
                ui.help()
                ui.choose('modelo com um título comprido demais para caber na janela', [('x', 'nome de modelo muito comprido')])
                self.assertTrue(all(cells(line) <= width for line in output), [(cells(line), line) for line in output if cells(line) > width])
                self.assertNotIn('\x1b', '\n'.join(output))

    def test_no_color_respects_environment(self):
        with patch.dict(os.environ, {'NO_COLOR': '1', 'TERM': 'xterm-256color'}), patch('sys.stdout.isatty', return_value=True):
            ui = TerminalUI()
            self.assertFalse(ui.color)
        with patch.dict(os.environ, {'TERM': 'dumb'}), patch('sys.stdout.isatty', return_value=True):
            self.assertFalse(TerminalUI().color)

    def test_slash_commands_update_preferences_without_starting_agent(self):
        answers = iter(['/first codex', '/model claude opus', '/effort codex high',
                        '/effort codex invalid', '/unknown', '/help', '/models', '/status', 'minha tarefa'])
        output, saves, checks = [], [], []
        ui = TerminalUI(read=lambda _: next(answers), write=output.append, color=False)
        chosen = copy.deepcopy(DEFAULTS)
        with patch('dev_auto.ui.model_catalog', return_value=[]):
            task = ui.task(chosen, status_callback=lambda: checks.append(True),
                           save_callback=lambda value: saves.append(copy.deepcopy(value)))
        self.assertEqual(task, 'minha tarefa')
        self.assertEqual(chosen['primary'], 'codex')
        self.assertEqual(chosen['agents']['claude']['model'], 'opus')
        self.assertEqual(chosen['agents']['codex']['effort'], 'high')
        self.assertEqual(len(saves), 3)
        self.assertEqual(checks, [True])
        self.assertIn('Comando desconhecido', '\n'.join(output))
        self.assertIn('Effort inválido', '\n'.join(output))
        self.assertFalse((self.store.project / 'calls.json').exists())

    def test_live_commands_enqueue_only_safe_control_requests(self):
        output, checks = [], []
        ui = TerminalUI(write=output.append, color=False)
        for entry in ['/status', '/switch codex', '/pause', '/model claude opus', '/switch bad']:
            ui.live_command(entry, self.store, lambda: checks.append(True))
        requests = self.store.pop_requests()
        self.assertEqual([item['action'] for item in requests], ['switch', 'pause'])
        self.assertEqual(requests[0]['agent'], 'codex')
        self.assertEqual(checks, [True])
        self.assertIn('resume --configure', '\n'.join(output))

    def test_json_outputs_are_machine_readable_even_with_color_always(self):
        config = self.run_cli('--color', 'always', 'config', '--json')
        self.assertEqual(json.loads(config.stdout), DEFAULTS)
        status = self.run_cli('--color', 'always', 'status', '--json')
        self.assertEqual(json.loads(status.stdout)['state'], 'no_session')
        self.assertNotIn('\x1b', config.stdout + status.stdout)
        doctor = self.run_cli('doctor', '--json')
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertTrue(json.loads(doctor.stdout)['claude']['subscription_login'])

    def test_json_logs_and_pretty_logs_are_consistent(self):
        self.store.event('task_completed')
        raw, pretty = self.run_cli('logs', '--json'), self.run_cli('logs')
        self.assertEqual(json.loads(raw.stdout)['event'], 'task_completed')
        self.assertIn('task_completed', pretty.stdout)
        self.assertIn('HISTÓRICO', pretty.stdout)

    def test_renamed_state_variable_preserves_legacy_support_and_lock(self):
        legacy, renamed = self.folder / 'legacy', self.folder / 'renamed'
        with patch.dict(os.environ, {'DEV_AUTO_HOME': str(legacy), 'DEVSWITCH_HOME': ''}):
            first = Store(self.store.project)
        with patch.dict(os.environ, {'DEV_AUTO_HOME': str(legacy), 'DEVSWITCH_HOME': str(renamed)}):
            second = Store(self.store.project)
        self.assertEqual(first.home, legacy.resolve())
        self.assertEqual(second.home, renamed.resolve())
        self.assertEqual(first.lock_path, second.lock_path)

    def test_installer_is_idempotent_and_preserves_foreign_command(self):
        home = self.folder / 'install-home'
        home.mkdir()
        env = {**self.env, 'HOME': str(home)}
        for _ in range(2):
            result = subprocess.run([sys.executable, str(ROOT / 'install.py')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('devswitch', 'dev-auto'):
            command = home / '.local/bin' / name
            self.assertTrue(command.stat().st_mode & 0o111)
            output = subprocess.check_output([str(command), '--version'], env=env, text=True)
            self.assertEqual(output.strip(), 'devswitch 1.3.0')
        (home / '.local/bin/devswitch').write_text('FOREIGN COMMAND')
        result = subprocess.run([sys.executable, str(ROOT / 'install.py')], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((home / '.local/bin/devswitch').read_text(), 'FOREIGN COMMAND')

    def test_terminal_raw_arrows_typeahead_and_slash_configuration(self):
        terminal = TerminalProcess(self.store.project, self.env, ['--color', 'always'])
        try:
            terminal.until('01 / ORDEM')
            terminal.send('\x1b[B\n')
            terminal.until('✓ Codex primeiro')
            terminal.send('\n\n\n\n/first claude\n/model claude opus\n/effort codex high\n/status\n/exit\n')
            self.assertEqual(terminal.finish(), 0, clean(terminal.text[-1000:]))
            config = load_settings(self.store)
            self.assertEqual(config['primary'], 'claude')
            self.assertEqual(config['agents']['claude']['model'], 'opus')
            self.assertEqual(config['agents']['codex']['effort'], 'high')
            self.assertNotIn('Traceback', terminal.text)
            self.assertFalse((self.store.project / 'calls.json').exists())
        finally:
            terminal.close()

    def test_terminal_ctrl_c_restores_terminal_and_starts_nothing(self):
        terminal = TerminalProcess(self.store.project, self.env, ['--color', 'always'])
        try:
            terminal.until('01 / ORDEM')
            terminal.send('\x03')
            self.assertEqual(terminal.finish(), 130)
            self.assertIn('cancelada', terminal.text)
            self.assertNotIn('Traceback', terminal.text)
            self.assertFalse((self.store.project / 'calls.json').exists())
        finally:
            terminal.close()

    def test_terminal_plain_mode_has_no_ansi(self):
        terminal = TerminalProcess(self.store.project, self.env, ['--no-color'], columns=40)
        try:
            terminal.until('Escolha [1]')
            terminal.send('\n\n\n\n\n/exit\n')
            self.assertEqual(terminal.finish(), 0)
            self.assertNotIn('\x1b', terminal.text)
            self.assertNotIn('Traceback', terminal.text)
        finally:
            terminal.close()

    def test_terminal_live_pause_switch_and_resume_preserve_work(self):
        (self.store.project / 'scenario.json').write_text('{"mode":"slow","capture_options":true}')
        terminal = TerminalProcess(self.store.project, self.env, ['--no-color', 'minha tarefa'])
        try:
            terminal.until('Etapa 1:')
            terminal.send('/switch codex\n/pause\n')
            self.assertEqual(terminal.finish(), 2, clean(terminal.text[-1500:]))
            state = self.store.load()
            self.assertEqual(state['status'], 'paused')
            self.assertEqual(state['preferred'], 'codex')
            self.assertEqual((self.store.project / 'partial.txt').read_text(), 'progresso antes da pausa segura')
            self.assertEqual(json.loads((self.store.project / 'calls.json').read_text()), ['claude'])
            result = self.run_cli('resume')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.store.load()['status'], 'completed')
            self.assertEqual(json.loads((self.store.project / 'calls.json').read_text()), ['claude', 'codex'])
            self.assertEqual(self.store.load()['objective'], 'minha tarefa')
        finally:
            terminal.close()


if __name__ == '__main__':
    unittest.main()
