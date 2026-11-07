import fcntl
import json
import os
from pathlib import Path
import struct
import tempfile
import termios
import unittest

from dev_auto.demo import fixture
from dev_auto.terminal_input import suggestions
from test_devswitch_console import TerminalProcess


class InputDockTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='devswitch-dock-')
        self.folder=Path(self.temp.name)
        self.store,_=fixture(self.folder)
        (self.store.project/'scenario.json').write_text('{"mode":"normal"}')
        self.env={**os.environ,'DEVSWITCH_HOME':str(self.store.home),'DEV_AUTO_HOME':str(self.store.home),
                  'PATH':str(self.folder/'bin')+os.pathsep+os.environ['PATH'],
                  'TERM':'xterm-256color','PYTHONUNBUFFERED':'1'}

    def tearDown(self):
        self.temp.cleanup()

    def task_terminal(self):
        terminal=TerminalProcess(self.store.project,self.env,['--color','always'])
        terminal.until('01 / ORDEM')
        terminal.send('\n'*5)
        terminal.until('Digite / para ver os comandos')
        return terminal

    def test_slash_filters_and_live_commands_are_contextual(self):
        self.assertGreaterEqual(len(suggestions('/')),10)
        self.assertEqual(suggestions('tarefa'),[])
        self.assertEqual(suggestions('/sta'),[('/status','Ver sessão, agentes e cotas')])
        self.assertNotIn('/config',[x.strip() for x,_ in suggestions('/',live=True)])
        self.assertEqual([x for x,_ in suggestions('/switch c')],['/switch claude','/switch codex'])
        self.assertEqual([x for x,_ in suggestions('/effort claude x')],['/effort claude xhigh'])

    def test_slash_palette_tab_and_unicode_editing_in_real_terminal(self):
        terminal=self.task_terminal()
        try:
            terminal.send('/')
            terminal.until('Ver comandos e atalhos')
            terminal.send('sta\t\n')
            terminal.until('Nenhuma tarefa iniciada')
            terminal.send('açãx\x7fo\x01teste \x05\n')
            self.assertEqual(terminal.finish(),0,terminal.text[-1000:])
            self.assertEqual(self.store.load()['objective'],'teste ação')
            self.assertIn('\x1b[?2004l',terminal.text)
            self.assertNotIn('Traceback',terminal.text)
        finally:
            terminal.close()

    def test_arrows_choose_status_without_starting_an_agent(self):
        terminal=self.task_terminal()
        try:
            terminal.send('/\x1b[B\n')
            terminal.until('Nenhuma tarefa iniciada')
            terminal.send('/exit\n')
            self.assertEqual(terminal.finish(),0)
            self.assertFalse((self.store.project/'calls.json').exists())
        finally:
            terminal.close()

    def test_bracketed_paste_does_not_execute_pasted_newlines(self):
        terminal=self.task_terminal()
        try:
            terminal.send('\x1b[200~implemente ação\ne testes\x1b[201~')
            terminal.until('implemente ação e testes')
            self.assertFalse((self.store.project/'calls.json').exists())
            terminal.send('\n')
            self.assertEqual(terminal.finish(),0)
            self.assertEqual(self.store.load()['objective'],'implemente ação e testes')
        finally:
            terminal.close()

    def test_resize_and_ctrl_c_restore_the_input_terminal(self):
        terminal=self.task_terminal()
        try:
            fcntl.ioctl(terminal.fd,termios.TIOCSWINSZ,struct.pack('HHHH',24,60,0,0))
            terminal.send('/sta')
            terminal.pump(.3)
            terminal.send('\x03')
            self.assertEqual(terminal.finish(),130)
            self.assertIn('\x1b[r',terminal.text)
            self.assertIn('\x1b[?2004l',terminal.text)
            self.assertNotIn('Traceback',terminal.text)
        finally:
            terminal.close()

    def test_live_input_survives_meter_updates_and_can_complete_pause(self):
        (self.store.project/'scenario.json').write_text('{"mode":"slow"}')
        terminal=TerminalProcess(self.store.project,self.env,['--color','always','teste do rodapé'])
        try:
            terminal.until('COTAS / running')
            terminal.send('/pa')
            terminal.until('Salvar e pausar na próxima etapa segura')
            for _ in range(6):
                terminal.pump(.2)
            terminal.send('\t\n')
            self.assertEqual(terminal.finish(),2,terminal.text[-1000:])
            self.assertEqual(self.store.load()['status'],'paused')
            self.assertEqual(json.loads((self.store.project/'calls.json').read_text()),['claude'])
            self.assertTrue((self.store.project/'partial.txt').exists())
        finally:
            terminal.close()


if __name__=='__main__':
    unittest.main()
