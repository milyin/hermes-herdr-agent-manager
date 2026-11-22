"""Opt-in native smoke test. Only pane-manager CLI controls Herdr panes."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Live(unittest.TestCase):
    def fixture(self, *, agent):
        default_home = (Path(os.environ['LOCALAPPDATA'])/'hermes' if os.name == 'nt'
                        else Path.home()/'.hermes')
        parent = Path(os.environ.get('HERMES_HOME') or default_home)
        skill = parent/'skills/hermes-herdr-pane-manager'
        self.assertTrue((skill/'scripts/pane_manager.py').is_file())
        runtime = tempfile.TemporaryDirectory(prefix='am-', dir=os.environ.get('TMPDIR'))
        self.addCleanup(runtime.cleanup)
        base = Path(runtime.name)
        temporary_home = base/'profile'
        (temporary_home/'skills').mkdir(parents=True)
        shutil.copytree(skill, temporary_home/'skills'/skill.name,
                        ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
        # The operator supplies a disposable server; this suite never launches Herdr directly.
        env = dict(os.environ)
        # Hermes ACP uses a configured profile for the live test; an empty home cannot answer.
        env['HERMES_HOME'] = str(parent if agent == 'hermes' else temporary_home)
        instance = env['AGENT_MANAGER_TEST_INSTANCE']
        self.assertTrue(instance.startswith('agent-manager-smoke-'))
        workspace = base/'empty'
        workspace.mkdir()
        subprocess.run(['git','init','-q',str(workspace)],check=True,env=env)

        def call(script, args, timeout=35):
            r = subprocess.run([sys.executable,'-I','-B',str(script),*args],env=env,capture_output=True,encoding='utf-8',timeout=timeout)
            try:
                return json.loads(r.stdout)
            except ValueError:
                self.fail(f'{r.returncode}: {r.stderr[:1000]} {r.stdout[:500]}')
        pane_script = skill/'scripts/pane_manager.py'
        manager = ROOT/'scripts/agent_manager.py'
        ready = {}
        for _ in range(35):
            ready = call(pane_script,['workspaces','--instance',instance])
            if ready.get('ok'):
                break
            time.sleep(.1)
        else:
            self.fail(f'Isolated Herdr unavailable: {ready}')
        created = call(pane_script,['workspace-create','--instance',instance,'--label',f'agent-manager-smoke-{agent}','--cwd',str(workspace)])
        self.assertTrue(created.get('ok'),created)
        where = ['--instance',instance,'--workspace',created['result']['workspace_id']]
        self.addCleanup(call, pane_script, ['close',*where,'--pane',created['result']['root_pane_id']])
        provisioned = call(manager,['provision',*where,'--cwd',str(workspace),'--agent',agent])
        self.assertTrue(provisioned.get('ok'),provisioned)
        where += ['--pane',provisioned['result']['pane_id']]
        self.addCleanup(call, pane_script, ['close',*where])
        self.assertEqual(provisioned['result']['agent'],agent)
        return call, manager, where, workspace, temporary_home

    @unittest.skipUnless(os.getenv('AGENT_MANAGER_HERMES_LIVE') == '1','requires real local Herdr and configured Hermes ACP')
    def test_native_hermes_lifecycle(self):
        call, manager, where, workspace, _ = self.fixture(agent='hermes')
        run = call(manager,['run',*where,'--cwd',str(workspace),'--agent','hermes',
                            '--prompt','Remember the word JUNIPER. Reply with exactly AM_HERMES. Do not use tools.'],80)
        self.assertTrue(run.get('ok'),run)
        self.assertEqual(run['result']['status'],'submitted')
        wait = call(manager,['wait',*where,'--turn-id',run['result']['turn_id'],'--seconds','40'],85)
        self.assertTrue(wait.get('ok'),wait)
        self.assertEqual(wait['result']['status'],'completed')
        self.assertIn('AM_HERMES',wait['result']['session_output'])
        if model := run['result']['model']:
            selected = call(manager,['change-model',*where,'--model',model])
            self.assertTrue(selected.get('ok'),selected)
            self.assertEqual(selected['result']['model'],model)
        follow = call(manager,['steer',*where,'--prompt','Repeat the word I asked you to remember. Do not use tools.',
                               '--seconds','40'],85)
        self.assertTrue(follow.get('ok'),follow)
        self.assertEqual(follow['result']['thread_id'],run['result']['thread_id'])
        self.assertNotEqual(follow['result']['turn_id'],run['result']['turn_id'])
        self.assertIn('JUNIPER',follow['result']['session_output'])

    @unittest.skipUnless(os.getenv('AGENT_MANAGER_LIVE') == '1','requires real local Herdr and authenticated Codex')
    def test_native_agent_lifecycle(self):
        call, manager, where, workspace, temporary_home = self.fixture(agent='codex')
        model = os.environ['AGENT_MANAGER_TEST_MODEL']
        run = call(manager,['run',*where,'--cwd',str(workspace),'--agent','codex','--model',model,'--prompt','Reply with exactly AM_READY. Do not use tools.'],65)
        self.assertTrue(run.get('ok'),run)
        wait = call(manager,['wait',*where,'--turn-id',run['result']['turn_id'],'--seconds','30'],75)
        self.assertTrue(wait.get('ok'),wait)
        self.assertIn('AM_READY',wait['result']['answer'])
        choice = call(manager,['change-model',*where,'--model',model]); self.assertTrue(choice.get('ok'),choice)
        follow = call(manager,['steer',*where,'--prompt','Reply with exactly AM_FOLLOW. Do not use tools.','--seconds','30'],75)
        self.assertTrue(follow.get('ok'),follow)
        self.assertEqual(follow['result']['thread_id'],run['result']['thread_id'])
        self.assertIn('AM_FOLLOW',follow['result']['answer'])
        rates = call(manager,['get-remaining-tokens',*where,'--agent','codex'])
        self.assertTrue(rates.get('ok'),rates)
        self.assertIn('remaining_percent',rates['result']['primary'])
        if os.name == 'nt':
            # Don't discover or fault-inject into arbitrary Windows processes.
            recovered = call(manager,['recover',*where],65)
            self.assertEqual(recovered.get('error', {}).get('code'), 'codex_still_running', recovered)
            return
        receipt, = (temporary_home/'agent-manager').glob('*.json')
        key = json.loads(receipt.read_text())['token'].encode()
        matches = []
        for candidate in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                command = candidate.read_bytes().split(b'\x00')
            except OSError:
                continue
            if command[1:6] == [b'-I', b'-B', os.fsencode(manager), b'_serve', key]:
                matches.append(candidate.parent)
        receiver, = matches
        self.assertEqual(receiver.stat().st_uid, os.getuid())
        children = {p for row in (receiver/'task').glob('*/children') for p in row.read_text().split()}
        codex, = [int(pid) for pid in children if b'app-server\x00' in (Path('/proc')/pid/'cmdline').read_bytes()]
        # Pin the PID before validating and fault-injecting into this test's own child.
        fd = os.pidfd_open(codex)
        try:
            proc = Path('/proc')/str(codex)
            self.assertIn(b'stdio://\x00',(proc/'cmdline').read_bytes())
            self.assertIn(f'PPid:\t{receiver.name}',(proc/'status').read_text().splitlines())
            signal.pidfd_send_signal(fd,signal.SIGTERM)
        finally:
            os.close(fd)
        deadline = time.monotonic()+4
        while time.monotonic() < deadline and (Path('/proc')/str(codex)/'cmdline').exists():
            if not (Path('/proc')/str(codex)/'cmdline').read_bytes():
                break
            time.sleep(.05)
        recovered = call(manager,['recover',*where],65)
        self.assertTrue(recovered.get('ok'),recovered)
        self.assertEqual(recovered['result']['thread_id'],run['result']['thread_id'])
