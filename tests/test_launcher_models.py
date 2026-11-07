from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dev_auto.cli import main
from dev_auto.demo import fixture
from dev_auto.models import CLAUDE_VERSIONS
from dev_auto.settings import DEFAULTS, load_settings
from dev_auto.storage import Store
from dev_auto.ui import TerminalUI
from dev_auto.workspaces import is_launcher_directory, recent_workspaces
from test_devswitch_console import TerminalProcess

CATALOG = [{'model': 'gpt-6.1-sol', 'label': 'GPT-6.1 Sol', 'efforts': ['low','medium','high','xhigh','max']}]


class LauncherModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='devswitch-launcher-')
        self.folder = Path(self.temp.name)
        self.store, self.inventory = fixture(self.folder, use_git=False)
        (self.store.project / 'scenario.json').write_text('{"mode":"normal","capture_options":true}')
        self.launcher_home = self.folder / 'home'
        self.launcher_home.mkdir()
        (self.launcher_home / 'private-file.txt').write_text('never include in a project')
        self.env = {**os.environ, 'HOME': str(self.launcher_home),
                    'DEVSWITCH_HOME': str(self.store.home), 'DEV_AUTO_HOME': str(self.store.home),
                    'PATH': str(self.folder / 'bin') + os.pathsep + os.environ['PATH'],
                    'TERM': 'xterm-256color', 'PYTHONUNBUFFERED': '1'}

    def tearDown(self):
        self.temp.cleanup()

    def test_home_and_ancestors_open_launcher_regular_folders_do_not(self):
        with patch('dev_auto.workspaces.Path.home', return_value=self.launcher_home):
            self.assertTrue(is_launcher_directory(self.launcher_home))
            self.assertTrue(is_launcher_directory(self.folder))
            self.assertTrue(is_launcher_directory(Path('/')))
            self.assertFalse(is_launcher_directory(self.store.project))

    def test_recent_projects_use_metadata_without_starting_or_scanning_work(self):
        self.assertEqual(recent_workspaces(self.store.home), [self.store.project])
        self.assertFalse((self.store.path / 'checkpoints').exists())
        self.assertFalse((self.store.project / 'calls.json').exists())
        self.assertFalse((self.store.project / '.git').exists())

    def test_old_sessions_are_discovered_without_new_workspace_metadata(self):
        (self.store.path / 'workspace.json').unlink()
        (self.store.path / 'state.json').write_text(json.dumps({'project':str(self.store.project),'updated_at':123}))
        self.assertEqual(recent_workspaces(self.store.home), [self.store.project])

    def test_deleted_corrupt_and_home_entries_do_not_break_recents(self):
        root = self.store.home / 'projects'
        for key, value in [('invalid','not json'), ('list','[1,2]'), ('missing',json.dumps({'project':str(self.folder/'absent')}))]:
            target = root / key
            target.mkdir()
            (target / 'workspace.json').write_text(value)
        target = root/'home-entry'
        target.mkdir()
        (target/'workspace.json').write_text(json.dumps({'project':str(self.launcher_home)}))
        with patch('dev_auto.workspaces.Path.home', return_value=self.launcher_home):
            self.assertEqual(recent_workspaces(self.store.home), [self.store.project])

    def test_picker_handles_invalid_and_quoted_paths_with_spaces(self):
        project = self.folder/'outro projeto'
        project.mkdir()
        answers = iter(['browse','/definitely-not-a-real-project',f'"{project}"'])
        output = []
        ui = TerminalUI(read=lambda _:next(answers),write=output.append,color=False)
        self.assertEqual(ui.workspace(self.launcher_home,[]),project.resolve())
        self.assertIn('Pasta não encontrada','\n'.join(output))

    def test_picker_exit_starts_nothing(self):
        ui=TerminalUI(read=lambda _:'exit',write=lambda _:None,color=False)
        self.assertIsNone(ui.workspace(self.launcher_home,[]))
        self.assertFalse((self.store.project/'calls.json').exists())

    def test_claude_menu_shows_numbers_and_returns_exact_pinned_id(self):
        answers=iter(['claude','claude-sonnet-5-5','high','gpt-6.1-sol','medium'])
        output=[]
        ui=TerminalUI(read=lambda _:next(answers),write=output.append,color=False)
        selected=ui.configure(deepcopy(DEFAULTS),catalog=CATALOG)
        self.assertEqual(selected['agents']['claude'],{'model':'claude-sonnet-5-5','effort':'high'})
        text='\n'.join(output)
        for name in ['Sonnet 5.5','Opus 5.5','Haiku 5.5','Fable 5.1','automático (alias)']:
            self.assertIn(name,text)

    def test_existing_aliases_are_never_silently_changed(self):
        answers=iter(['']*5)
        ui=TerminalUI(read=lambda _:next(answers),write=lambda _:None,color=False)
        self.assertEqual(ui.configure(deepcopy(DEFAULTS),catalog=CATALOG),DEFAULTS)

    def test_models_command_prints_official_ids(self):
        output=[]
        ui=TerminalUI(write=output.append,color=False)
        with patch('dev_auto.ui.model_catalog',return_value=CATALOG):
            ui.models()
        for value,_ in CLAUDE_VERSIONS:
            self.assertIn(value,'\n'.join(output))

    def test_broad_folder_noninteractive_task_requires_explicit_target(self):
        with patch.dict(os.environ,self.env), patch('dev_auto.cli.Path.cwd',return_value=self.launcher_home), \
             patch('sys.stdin.isatty',return_value=False), patch('sys.stderr',new_callable=io.StringIO) as error:
            self.assertEqual(main(['minha tarefa']),2)
            self.assertIn('-C',error.getvalue())
        self.assertFalse((self.launcher_home/'calls.json').exists())

    def test_home_json_query_never_opens_an_interactive_picker(self):
        with patch.dict(os.environ,self.env), patch('dev_auto.cli.Path.cwd',return_value=self.launcher_home), \
             patch('sys.stdin.isatty',return_value=True), patch('dev_auto.ui.TerminalUI.workspace') as picker, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(main(['status','--json']),0)
        self.assertFalse(picker.called)
        self.assertEqual(json.loads(output.getvalue())['state'],'no_session')

    def test_real_terminal_home_launcher_pins_sonnet_and_runs_only_project(self):
        terminal=TerminalProcess(self.launcher_home,self.env,['--no-color'])
        try:
            terminal.until('PROJETOS RECENTES')
            terminal.send('1\n')
            terminal.until('01 / ORDEM')
            terminal.send('claude\nclaude-sonnet-5-5\nhigh\ngpt-6.1-sol\nmedium\nimplemente uma função\n')
            self.assertEqual(terminal.finish(),0,terminal.text[-1200:])
            config=load_settings(self.store)
            self.assertEqual(config['agents']['claude']['model'],'claude-sonnet-5-5')
            selected=json.loads((self.store.project/'claude-options.json').read_text())
            self.assertEqual(selected,{'model':'claude-sonnet-5-5','effort':'high'})
            self.assertFalse((self.launcher_home/'calls.json').exists())
            self.assertEqual((self.launcher_home/'private-file.txt').read_text(),'never include in a project')
            self.assertFalse((self.store.project/'.git').exists())
        finally:
            terminal.close()


if __name__=='__main__':
    unittest.main()
