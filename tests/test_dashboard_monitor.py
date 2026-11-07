import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from dev_auto.dashboard import quota_rows
from dev_auto.demo import fixture
from dev_auto.monitor import QuotaMonitor
from dev_auto.orchestrator import Orchestrator
from dev_auto.settings import DEFAULTS
from dev_auto.usage import Availability
from test_devswitch_console import TerminalProcess


class DashboardMonitorTests(unittest.TestCase):
    def state(self):
        return {'status':'running','active_agent':'codex','step':2,'created_at':100,
                'settings':copy.deepcopy(DEFAULTS),'availability':{
                    'claude':Availability(status='limited',retry_at=180,observed_at=100).dump(),
                    'codex':Availability(status='available',used_percent=42,observed_at=110).dump()}}

    def test_unknown_is_not_zero_and_real_percentage_is_used(self):
        text='\n'.join(quota_rows(self.state(),now=120))
        self.assertIn('% não informado',text)
        self.assertIn('42.0% usado',text)
        self.assertIn('nova tentativa em 1m 00s',text)
        self.assertIn('EM EXECUÇÃO',text)
        self.assertIn('Última leitura de cota há 10s',text)

    def test_successful_retry_does_not_relabel_stale_quota_as_current(self):
        usage=Availability(status='limited',used_percent=100,resets_at=120,quota_observed_at=100)
        usage.success(now=130)
        self.assertEqual(usage.status,'available')
        self.assertIsNone(usage.used_percent)
        self.assertIsNone(usage.resets_at)
        self.assertEqual(usage.quota_observed_at,0)

    def test_successful_step_preserves_actual_quota_sample_time(self):
        usage=Availability()
        usage.codex_snapshot({'rateLimits':{'primary':{'usedPercent':42}}})
        stamp=usage.quota_observed_at
        usage.success(now=stamp+10)
        state=self.state()
        state['availability']['codex']=usage.dump()
        self.assertIn('Última leitura de cota há 20s',quota_rows(state,now=stamp+20)[6])

    def test_deadline_never_claims_renewal_is_confirmed(self):
        rows=quota_rows(self.state(),now=200)
        self.assertIn('limite atingido',rows[1])
        self.assertIn('próxima etapa segura',rows[2])

    def test_invalid_percent_never_appears_as_a_measurement(self):
        for percent in (float('nan'),float('inf'),-2,101,True):
            state=self.state()
            state['availability']['codex']['used_percent']=percent
            self.assertIn('% não informado',quota_rows(state,now=120)[4])

    def test_reader_is_async_and_only_one_request_runs(self):
        gate=threading.Event()
        calls=[]
        def reader():
            calls.append(True)
            gate.wait(2)
            return {'limits':{}}
        monitor=QuotaMonitor(reader)
        try:
            started=time.monotonic()
            monitor.request()
            monitor.request()
            self.assertLess(time.monotonic()-started,.2)
            self.assertIsNone(monitor.take())
            gate.set()
            monitor.thread.join(2)
            self.assertEqual(monitor.take()[1],{'limits':{}})
            self.assertEqual(len(calls),1)
        finally:
            gate.set()
            monitor.close()

    def test_close_signals_cancellation(self):
        monitor=QuotaMonitor(lambda: monitor.cancel.wait(2))
        monitor.request()
        monitor.close()
        self.assertFalse(monitor.thread.is_alive())

    def test_error_text_is_never_retained(self):
        def reader():
            raise RuntimeError('secret-token-do-not-keep')
        monitor=QuotaMonitor(reader)
        monitor.request()
        monitor.thread.join(2)
        result=monitor.take()
        self.assertEqual(result[1:],(None,True))
        self.assertNotIn('secret',str(result))
        monitor.close()

    def test_stale_query_cannot_override_new_limit_error(self):
        with tempfile.TemporaryDirectory() as folder:
            store,inventory=fixture(folder)
            engine=Orchestrator(store,inventory=inventory)
            engine.usage={'codex':Availability()}
            engine.monitor=QuotaMonitor(lambda:{'limits':{'rateLimits':{'primary':{'usedPercent':10}}}})
            try:
                engine.monitor.request()
                engine.monitor.thread.join(2)
                engine.usage['codex'].reject()
                engine.next_quota=time.monotonic()+60
                engine.refresh_quota(background=True)
                self.assertEqual(engine.usage['codex'].status,'limited')
            finally:
                engine.monitor.close()

    def test_quota_refreshes_during_an_active_step(self):
        with tempfile.TemporaryDirectory() as folder:
            store,inventory=fixture(folder)
            (store.project/'scenario.json').write_text('{"mode":"slow"}')
            calls=[]
            frames=[]
            def reader():
                calls.append(time.monotonic())
                return {'limits':{'rateLimits':{'primary':{'usedPercent':10 if len(calls)==1 else 42}}}}
            result=Orchestrator(store,inventory=inventory,quota_reader=reader,poll=.1,
                                announce=lambda _:None,on_update=lambda s:frames.append(copy.deepcopy(s))).run('teste monitor')
            self.assertEqual(result['status'],'completed')
            self.assertTrue(any(s['active_agent']=='claude' and s['availability']['codex']['used_percent']==42 for s in frames))
            self.assertGreater(len(calls),1)

    def test_offline_demo_works_without_the_test_source_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)
            source=Path(__file__).resolve().parent.parent/'dev_auto'
            shutil.copytree(source,target/'dev_auto',ignore=shutil.ignore_patterns('__pycache__'))
            result=subprocess.run([sys.executable,'-m','dev_auto','demo'],cwd=target,
                                  env={**os.environ,'PYTHONPATH':str(target)},capture_output=True,text=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('PASS: esgotamento Claude',result.stdout)
            self.assertFalse((target/'tests').exists())

    def test_colored_terminal_footer_and_live_pause_restore_screen(self):
        with tempfile.TemporaryDirectory() as folder:
            store,inventory=fixture(folder)
            (store.project/'scenario.json').write_text('{"mode":"slow"}')
            env={**os.environ,'DEVSWITCH_HOME':str(store.home),'DEV_AUTO_HOME':str(store.home),
                 'PATH':str(Path(folder)/'bin')+os.pathsep+os.environ['PATH'],
                 'TERM':'xterm-256color','PYTHONUNBUFFERED':'1'}
            terminal=TerminalProcess(store.project,env,['--color','always','tarefa monitor'])
            try:
                terminal.until('COTAS / running')
                terminal.send('/status\n/pause\n')
                self.assertEqual(terminal.finish(),2,terminal.text[-1000:])
                self.assertIn('% não informado',terminal.text)
                self.assertIn('0.0% usado',terminal.text)  # fake app-server, explicitly simulated
                self.assertIn('\x1b[r',terminal.text)
                self.assertNotIn('Traceback',terminal.text)
                self.assertEqual(store.load()['status'],'paused')
                self.assertTrue((store.project/'partial.txt').exists())
            finally:
                terminal.close()


if __name__=='__main__':
    unittest.main()
