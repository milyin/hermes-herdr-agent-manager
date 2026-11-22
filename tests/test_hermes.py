"""Cross-platform ACP wire contract tests; no Hermes credentials or Herdr needed."""
from pathlib import Path
from contextlib import contextmanager
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_manager import api

FAKE = r'''
import json, sys
from pathlib import Path


def send(**data):
    print(json.dumps(dict(jsonrpc='2.0', **data)), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    assert request['jsonrpc'] == '2.0'
    ident, method, data = (request[k] for k in ('id', 'method', 'params'))
    if method == 'initialize':
        assert data['clientCapabilities'] == {}
        result = {'protocolVersion': 1}
    elif method == 'session/new':
        assert data['mcpServers'] == []
        result = {'sessionId': 'owned-session', 'models': {'currentModelId': 'test:default'}}
    elif method == 'session/set_model':
        assert data == {'sessionId': 'owned-session', 'modelId': 'test:other'}
        Path('model-write-observed').touch()
        result = {}
    elif method == 'session/prompt':
        assert data['sessionId'] == 'owned-session'
        prompt, = data['prompt']
        if prompt['text'] == 'LOSE':
            break
        if prompt['text'] in ('ASK', 'NO_REJECT'):
            options = [{'kind': 'allow_once', 'optionId': 'yes'}]
            expected = {'outcome': 'cancelled'}
            if prompt['text'] == 'ASK':
                options.append({'kind': 'reject_once', 'optionId': 'no'})
                expected = {'outcome': 'selected', 'optionId': 'no'}
            send(id='permission', method='session/request_permission', params={
                'sessionId': 'owned-session', 'options': options})
            assert json.loads(sys.stdin.readline()) == dict(jsonrpc='2.0', id='permission',
                result={'outcome': expected})
        for session, kind, text in [('wrong-session', 'agent_message_chunk', 'wrong'),
                                    ('owned-session', 'agent_thought_chunk', 'private reasoning'),
                                    ('owned-session', 'agent_message_chunk', prompt['text'])]:
            send(method='session/update', params={'sessionId': session, 'update': {
                'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}})
        if prompt['text'] == 'MIXED':
            send(method='session/update', params={'sessionId': 'owned-session', 'update': {
                'sessionUpdate': 'agent_message_chunk', 'messageId': 'another-admission',
                'content': {'type': 'text', 'text': 'peer output'}}})
        result = {'stopReason': 'end_turn'}
    else:
        raise AssertionError(method)
    send(id=ident, result=result)
'''


class HermesContract(unittest.TestCase):
    def setUp(self):
        self.mod = api()
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.cwd = temp.name
        script = Path(self.cwd) / 'fake_acp.py'
        script.write_text(FAKE, encoding='utf-8')
        self.launcher = self.mod.HermesAcp
        factory = lambda cwd, check: self.launcher(cwd, check, command=[sys.executable, '-I', '-B', str(script)])
        backend = patch.object(self.mod, 'HermesAcp', side_effect=factory)
        backend.start()
        self.addCleanup(backend.stop)
        self.receiver = self.mod.HermesReceiver(self.cwd)
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        if self.receiver.acp is not None:
            self.receiver.acp.stop_unassigned()

    def send(self, op, **fields):
        return self.receiver.dispatch(dict(op=op, **fields))

    def run_turn(self, prompt):
        return self.send('run', agent='hermes', cwd=self.cwd, prompt=prompt)

    def wait_for(self, turn):
        return self.send('wait', turn_id=turn['turn_id'], seconds=3)

    def test_run_then_follow_up_in_same_session(self):
        accepted = self.run_turn('Unicode \u2603\nsecond line')
        self.assertEqual(accepted['status'], 'submitted')
        self.assertEqual(accepted['model'], 'test:default')
        self.assertEqual(self.wait_for(accepted)['session_output'], 'Unicode \u2603\nsecond line')
        self.assertEqual(self.send('change-model', model='test:other')['model'], 'test:other')
        follow = self.send('steer', prompt='follow-up', seconds=3)
        self.assertEqual(follow['thread_id'], accepted['thread_id'])
        self.assertNotEqual(follow['turn_id'], accepted['turn_id'])
        self.assertEqual(follow['session_output'], 'follow-up')

    def test_does_not_report_unattributed_session_output_as_turn_answer(self):
        result = self.wait_for(self.run_turn('MIXED'))
        self.assertNotIn('answer', result)
        self.assertEqual(result['session_output'], 'MIXEDpeer output')

    def test_codex_model_required_before_pane_lookup(self):
        result = subprocess.run([sys.executable, '-I', '-B', str(self.mod.HERE), 'run',
                                 '--agent', 'codex', '--instance', 'fixture', '--workspace', 'w1',
                                 '--pane', 'w1:p1', '--cwd', self.cwd, '--prompt', 'test'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--model', result.stderr)

    def test_deny_server_permission_and_refuse_more_prompts(self):
        accepted = self.run_turn('ASK')
        with self.assertRaises(self.mod.Error) as caught:
            self.wait_for(accepted)
        self.assertEqual(caught.exception.code, 'approval_required')
        backend = self.receiver.acp
        with backend.condition:
            self.assertTrue(backend.condition.wait_for(lambda: backend.task['status'] == 'completed', timeout=3),
                            'fixture did not verify the explicit denial response')
        with self.assertRaises(self.mod.Error) as caught:
            self.send('steer', prompt='must not send', seconds=1)
        self.assertEqual(caught.exception.code, 'approval_required')

    def test_no_reject_option_is_reported_as_unconfirmed(self):
        turn = self.run_turn('NO_REJECT')
        with self.assertRaises(self.mod.Error) as caught:
            self.wait_for(turn)
        self.assertEqual(caught.exception.code, 'approval_required')
        self.assertIn('denial was not confirmed', str(caught.exception))

    def test_approval_latched_during_pane_check_blocks_model_write(self):
        self.wait_for(self.run_turn('first'))
        worker = self.receiver.acp
        def check(_deadline=None):
            # The latch can change while the pane-manager call is in progress.
            worker.blocked = True
        with self.assertRaises(self.mod.Error) as caught:
            self.receiver.dispatch(dict(op='change-model', model='test:other'), check)
        self.assertEqual(caught.exception.code, 'approval_required')
        self.assertEqual(self.receiver.model, 'test:default')
        self.assertFalse((Path(self.cwd)/'model-write-observed').exists())

    def test_expired_deadline_during_fence_never_writes_model_request(self):
        self.wait_for(self.run_turn('first'))
        worker = self.receiver.acp
        fence = worker.write_fence
        @contextmanager
        def contended(msg):
            # Simulate the production fence becoming available after the verified deadline.
            time.sleep(.3)
            with fence(msg):
                yield
        def check(_deadline=None):
            return time.monotonic() + .15
        with patch.object(worker, 'write_fence', contended):
            with self.assertRaises(self.mod.Error) as caught:
                self.receiver.dispatch(dict(op='change-model', model='test:other'), check)
        self.assertEqual(caught.exception.code, 'deadline_elapsed')
        self.assertFalse((Path(self.cwd)/'model-write-observed').exists())

    def test_disconnected_prompt_cannot_be_replayed(self):
        turn = self.run_turn('LOSE')
        with self.assertRaises(self.mod.Error) as caught:
            self.wait_for(turn)
        self.assertEqual(caught.exception.code, 'outcome_unknown')
        with self.assertRaises(self.mod.Error) as caught:
            self.send('steer', prompt='must not send', seconds=1)
        self.assertEqual(caught.exception.code, 'outcome_unknown')

    def test_blocks_wrong_agent_slash_and_codex_network_flag_before_launch(self):
        for payload, code in [(dict(agent='codex', prompt='hi'), 'agent_mismatch'),
                              (dict(agent='hermes', prompt='  /approve'), 'unsupported_prompt'),
                              (dict(agent='hermes', prompt='hi', network=True), 'unsupported_operation')]:
            with self.assertRaises(self.mod.Error) as caught:
                self.send('run', cwd=self.cwd, **payload)
            self.assertEqual(caught.exception.code, code)
            self.assertIsNone(self.receiver.acp)

    def test_exact_turn_steering_does_not_become_a_followup(self):
        first = self.run_turn('first')
        self.wait_for(first)
        with self.assertRaises(self.mod.Error) as caught:
            self.send('steer', prompt='must not send', expected_turn=first['turn_id'], seconds=1)
        self.assertEqual(caught.exception.code, 'unsupported_operation')
        self.assertEqual(self.wait_for(first)['session_output'], 'first')
