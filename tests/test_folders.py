import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from dev_auto.adapters import Adapter, Result
from dev_auto.cli import status
from dev_auto.demo import fixture
from dev_auto.orchestrator import Orchestrator
from dev_auto.settings import DEFAULTS
from dev_auto.storage import ProjectLock, SafetyError, Store, find_git_root


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='devswitch-folder-test-')
        self.folder = Path(self.temp.name)
        self.store, self.inventory = fixture(self.folder, use_git=False)
        (self.store.project / 'scenario.json').write_text('{"mode":"normal","capture_options":true}')
        (self.store.project / 'schema.sql').write_text('CREATE TABLE users(id INTEGER);\n')
        self.env = {**os.environ, 'DEVSWITCH_HOME': str(self.store.home), 'DEV_AUTO_HOME': str(self.store.home),
                    'PATH': str(self.folder / 'bin') + os.pathsep + os.environ['PATH']}

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, **kwargs):
        return Orchestrator(self.store, inventory=self.inventory, quota_reader=lambda: {'limits': {}},
                            announce=lambda *_: None, poll=.1, **kwargs)

    def test_plain_folder_remains_unchanged_and_git_optional(self):
        self.assertFalse(self.store.has_git)
        self.assertEqual(self.store.project, self.store.project.resolve())
        before = sorted(p.name for p in self.store.project.iterdir())
        self.store.checkpoint('test')
        self.assertEqual(sorted(p.name for p in self.store.project.iterdir()), before)
        self.assertFalse((self.store.project / '.git').exists())

    def test_snapshot_copies_existing_sql_content_and_modes(self):
        checkpoint = self.store.checkpoint('baseline')
        entry = checkpoint['files']['schema.sql']
        self.assertEqual((self.store.path / 'blobs' / entry['sha256']).read_text(), 'CREATE TABLE users(id INTEGER);\n')
        self.assertIsNone(checkpoint['git'])
        self.assertTrue((self.store.path / 'checkpoints' / checkpoint['id'] / 'workspace.json').exists())
        self.assertEqual(entry['mode'], (self.store.project / 'schema.sql').stat().st_mode & 0o777)

    def test_codex_receives_official_skip_flag_only_for_plain_folder(self):
        adapter = Adapter('codex', self.inventory['codex']['path'], self.store.path)
        self.assertIn('--skip-git-repo-check', adapter.command(self.store.project))
        repository = self.folder / 'repo'
        repository.mkdir()
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        self.assertNotIn('--skip-git-repo-check', adapter.command(repository))
        self.assertIn('--sandbox', adapter.command(self.store.project))
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', adapter.command(self.store.project))

    def test_full_claude_task_works_without_git(self):
        state = self.engine().run('trabalhar no SQL existente')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['project_mode'], 'folder')
        self.assertEqual((self.store.project / 'schema.sql').read_text(), 'CREATE TABLE users(id INTEGER);\n')
        self.assertFalse((self.store.project / '.git').exists())
        self.assertEqual(json.loads((self.store.project / 'calls.json').read_text()), ['claude'])

    def test_full_codex_task_works_without_git(self):
        selected = copy.deepcopy(DEFAULTS)
        selected['primary'] = 'codex'
        state = self.engine().run('implementar SQL', settings=selected)
        self.assertEqual(state['last_agent'], 'codex')
        args = json.loads((self.store.project / 'codex-arguments.json').read_text())
        self.assertIn('--skip-git-repo-check', args)
        self.assertFalse((self.store.project / '.git').exists())

    def test_handoff_and_renewal_cycle_without_git(self):
        (self.store.project / 'scenario.json').write_text('{"mode":"cycle"}')
        state = self.engine().run('continuar sem perder contexto')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(json.loads((self.store.project / 'calls.json').read_text()), ['claude', 'codex', 'claude'])
        self.assertIn('progress.txt', state['handoff']['modified_files'])
        self.assertIn('codex: continuou', (self.store.project / 'progress.txt').read_text())
        self.assertFalse((self.store.project / '.git').exists())

    def test_parent_and_child_folders_never_lock_simultaneously(self):
        child = self.store.project / 'sql'
        child.mkdir()
        nested = Store(child, self.store.home)
        for first, second in [(self.store, nested), (nested, self.store)]:
            with first.lock():
                self.assertTrue(second.busy())
                with self.assertRaises(SafetyError):
                    with second.lock():
                        self.fail('overlapping projects acquired locks')

    def test_sibling_projects_can_lock_independently(self):
        sibling = self.folder / 'other'
        sibling.mkdir()
        other = Store(sibling, self.store.home)
        with self.store.lock(), other.lock():
            self.assertTrue(self.store.busy())
            self.assertTrue(other.busy())

    def test_parent_folder_and_nested_git_repository_are_exclusive(self):
        child = self.store.project / 'repo'
        child.mkdir()
        subprocess.run(['git', 'init', '-q', str(child)], check=True)
        nested = Store(child, self.store.home)
        self.assertTrue(nested.has_git)
        for first, second in [(self.store, nested), (nested, self.store)]:
            with first.lock():
                self.assertTrue(second.busy())

    def test_different_state_homes_and_symlink_alias_share_lock(self):
        alias = self.folder / 'alias'
        alias.symlink_to(self.store.project, target_is_directory=True)
        other = Store(alias, self.folder / 'other-state')
        self.assertEqual(other.project, self.store.project)
        self.assertEqual(other.lock_path, self.store.lock_path)
        with self.store.lock():
            self.assertTrue(other.busy())

    def test_internal_state_and_dependencies_not_recursively_backed_up(self):
        for name in ['node_modules', '.venv', '__pycache__']:
            folder = self.store.project / name
            folder.mkdir()
            (folder / 'ignored.txt').write_text('cache')
        self.store = Store(self.store.project, self.store.project / '.state')
        self.store.event('test')
        checkpoint = self.store.checkpoint('test')
        self.assertIn('schema.sql', checkpoint['files'])
        self.assertFalse(any(name.startswith(('.state/', 'node_modules/', '.venv/', '__pycache__/')) for name in checkpoint['files']))

    def test_custom_ignore_rules_are_applied_without_git(self):
        (self.store.project / '.devswitchignore').write_text('*.dump\ncache/\n# comment\n')
        (self.store.project / 'large.dump').write_text('ignored')
        (self.store.project / 'cache').mkdir()
        (self.store.project / 'cache' / 'temp').write_text('ignored')
        files = self.store.checkpoint('test')['files']
        self.assertNotIn('large.dump', files)
        self.assertNotIn('cache/temp', files)
        self.assertIn('schema.sql', files)

    def test_no_git_executable_is_supported_for_ordinary_folder(self):
        with patch('dev_auto.storage.subprocess.run', side_effect=FileNotFoundError):
            store = Store(self.store.project, self.store.home)
            self.assertFalse(store.has_git)
            self.assertIn('schema.sql', store.checkpoint('test')['files'])

    def test_broken_git_metadata_is_not_silently_ignored(self):
        (self.store.project / '.git').mkdir()
        with self.assertRaises(SafetyError):
            Store(self.store.project, self.store.home)

    def test_cli_accepts_folder_with_spaces_and_persists_state(self):
        space = self.folder / 'sql tem'
        space.mkdir()
        (space / 'scenario.json').write_text('{"mode":"normal","capture_options":true}')
        result = subprocess.run(['devswitch', '--first', 'codex', 'minha tarefa'], cwd=space,
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        view = subprocess.run(['devswitch', 'status', '--json'], cwd=space,
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(json.loads(view.stdout)['project_mode'], 'folder')
        self.assertEqual(json.loads(view.stdout)['state'], 'completed')
        self.assertFalse((space / '.git').exists())

    def test_folder_locks_survive_controller_sigkill_until_writer_stops(self):
        (self.store.project / 'scenario.json').write_text('{"mode":"sleep"}')
        process = subprocess.Popen(['devswitch', 'tarefa'], cwd=self.store.project, env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        parent = Store(self.folder, self.folder / 'parent-state')
        try:
            deadline = time.monotonic() + 10
            while not (self.store.project / 'agent.pid').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.store.project / 'agent.pid').exists())
            agent = int((self.store.project / 'agent.pid').read_text())
            self.assertTrue(parent.busy())
            process.kill()
            process.wait()
            # All ancestor lock fds are retained by the guardian until cleanup.
            self.assertTrue(parent.busy())
            deadline = time.monotonic() + 10
            while parent.busy() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertFalse(parent.busy())
            with self.assertRaises(ProcessLookupError):
                os.kill(agent, 0)
            self.assertEqual((self.store.project / 'partial.txt').read_text(), 'progresso antes da interrupção')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    unittest.main()
