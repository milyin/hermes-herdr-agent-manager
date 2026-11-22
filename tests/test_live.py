"""Opt-in native smoke test. Only pane-manager CLI controls Herdr panes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.getenv('AGENT_MANAGER_LIVE') == '1','requires real local Herdr and authenticated Codex')
class Live(unittest.TestCase):
    def test_native_agent_lifecycle(self):
        parent = Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')
        skill = parent/'skills/hermes-herdr-pane-manager'
        self.assertTrue((skill/'scripts/pane_manager.py').is_file())
        runtime = tempfile.TemporaryDirectory(prefix='am-')
        self.addCleanup(runtime.cleanup)
        base = Path(runtime.name)
        temporary_home = base/'profile'
        (temporary_home/'skills').mkdir(parents=True)
        (temporary_home/'skills'/skill.name).symlink_to(skill.resolve(), target_is_directory=True)
        # The operator supplies a disposable server; this suite never launches Herdr directly.
        env = dict(os.environ)
        env['HERMES_HOME'] = str(temporary_home)
        instance = env['AGENT_MANAGER_TEST_INSTANCE']
        self.assertTrue(instance.startswith('agent-manager-smoke-'))
        workspace = base/'empty'
        workspace.mkdir()
        subprocess.run(['git','init','-q',str(workspace)],check=True,env=env)

        def call(script, args, timeout=35):
            r = subprocess.run([sys.executable,'-I',str(script),*args],env=env,capture_output=True,text=True,timeout=timeout)
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
        created = call(pane_script,['workspace-create','--instance',instance,'--label','agent-manager-smoke','--cwd',str(workspace)])
        self.assertTrue(created.get('ok'),created)
        where = ['--instance',instance,'--workspace',created['result']['workspace_id']]
        provisioned = call(manager,['provision',*where,'--cwd',str(workspace)])
        self.assertTrue(provisioned.get('ok'),provisioned)
        where += ['--pane',provisioned['result']['pane_id']]
        self.addCleanup(call, pane_script, ['close',*where])
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
        receipt, = (temporary_home/'agent-manager').glob('*.json')
        key = json.loads(receipt.read_text())['token'].encode()
        matches = []
        for candidate in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                command = candidate.read_bytes().split(b'\x00')
            except OSError:
                continue
            if command[1:5] == [b'-I', os.fsencode(manager), b'_serve', key]:
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
