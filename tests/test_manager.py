import importlib.util
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def api():
    script = ROOT / 'scripts/agent_manager.py'
    if not script.exists():
        raise AssertionError('The agent-manager CLI is not implemented')
    spec = importlib.util.spec_from_file_location('agent_manager', script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Contract(unittest.TestCase):
    def test_missing_pane_manager_fails_closed(self):
        mod = api()
        with tempfile.TemporaryDirectory() as home, patch.dict('os.environ', {'HERMES_HOME':home}):
            with self.assertRaises(mod.Error) as caught:
                mod.pane_script()
        self.assertEqual(caught.exception.code, 'pane_manager_missing')

    def test_quota_distinguishes_windows_and_uses_utc(self):
        result = api().quota({'rateLimits':{'primary':{'usedPercent':25,'windowDurationMins':60,'resetsAt':1730947200},
                                           'secondary':{'usedPercent':80,'windowDurationMins':10080,'resetsAt':1731552000}}})
        self.assertEqual(result['primary'], {'remaining_percent':75,'window_minutes':60,'reset_at':'2024-11-07T02:40:00Z'})
        self.assertEqual(result['secondary']['remaining_percent'],20)
        self.assertIsNone(api().quota({'rateLimits':{}})['primary'])

    def test_private_binding_changes_with_scope(self):
        mod = api()
        self.assertNotEqual(mod.binding_key('first','w1','w1:p1'), mod.binding_key('second','w1','w1:p1'))
        self.assertNotEqual(mod.binding_key('first','w1','w1:p1'), mod.binding_key('first','w2','w1:p1'))

    def test_validation_refuses_bad_model(self):
        mod = api()
        with self.assertRaises(mod.Error):
            mod.validate_model('--dangerously-bypass-approvals-and-sandbox')

    def test_guard_refuses_zoomed_or_ambiguous(self):
        mod = api()
        for state in (True,None):
            with patch.object(mod,'pane_call',return_value={'pane_id':'p','user_locked':state}):
                with self.assertRaises(mod.Error):
                    mod.guard('s','w','p')

    def test_steer_correlates_exact_active_turn_without_submitting_another(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        receiver.thread, receiver.turn = 'owned-thread', 'active-turn'
        class Backend:
            dead = False
            calls = []

            def state(self, thread, turn):
                return {}

            def rpc(self, method, params, check):
                check()
                self.calls.append((method, params))
                return {'turnId':'active-turn'}

            def wait(self, thread, turn, seconds):
                return dict(thread_id=thread, turn_id=turn, answer='matching answer')

        receiver.codex = backend = Backend()
        result = receiver.dispatch({'op':'steer','prompt':'literal follow-up','seconds':5,'expected_turn':'active-turn'})
        self.assertEqual(result['answer'], 'matching answer')
        self.assertEqual(backend.calls, [('turn/steer',{'threadId':'owned-thread','expectedTurnId':'active-turn',
            'input':[{'type':'text','text':'literal follow-up','text_elements':[]}]})])

    def test_failure_exposes_known_native_error_code_without_raw_text(self):
        mod = api()
        fake = mod.Codex.__new__(mod.Codex)
        fake.condition = threading.Condition()
        fake.dead = False
        fake.turns = {}
        fake.capture({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'one','status':'failed',
            'error':{'codexErrorInfo':'modelNotFound','message':'private diagnostic payload'}}}})
        with self.assertRaises(mod.Error) as caught:
            fake.wait('t', 'one', 1)
        self.assertEqual(caught.exception.code, 'turn_failed')
        self.assertIn('modelNotFound',str(caught.exception))
        self.assertNotIn('private diagnostic payload',str(caught.exception))

    def test_recover_resumes_exact_thread_without_replaying_prompt(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        receiver.thread, receiver.turn = 'previous-thread', 'prior-turn'
        receiver.model = 'known-model'
        class OldBackend:
            dead = True

            def exited(self):
                return True

            def dispose(self):
                pass

        class NewBackend:
            def __init__(self):
                self.calls = []

            def rpc(self, method, params, check):
                check()
                self.calls.append((method,params))
                return {'thread':{'id':'previous-thread','status':{'type':'idle'}},'model':'known-model'}

        receiver.codex = OldBackend()
        new = NewBackend()
        with patch.object(mod,'Codex',return_value=new):
            out = receiver.dispatch({'op':'recover'})
        self.assertEqual(out, {'thread_id':'previous-thread','status':'resumed'})
        self.assertEqual([method for method,_ in new.calls], ['thread/resume'])
        self.assertEqual(new.calls[0][1]['threadId'],'previous-thread')
        self.assertIsNone(receiver.turn)

    def test_malformed_turn_receipt_latches_uncertainty(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        receiver.thread,receiver.model = 't','model'
        class Backend:
            dead = False

            def rpc(self, method, params, check):
                check()
                if method != 'turn/start':
                    raise AssertionError(method)
                return {'turn':None}

        receiver.codex = Backend()
        for _ in range(2):
            with self.assertRaises(mod.Error) as caught:
                receiver.dispatch({'op':'steer','prompt':'hello','seconds':1})
            self.assertEqual(caught.exception.code,'outcome_unknown')
        self.assertTrue(receiver.uncertain)

    def test_last_lock_check_holds_write_lock_and_writes_nothing_on_rejection(self):
        mod = api()
        codex = mod.Codex.__new__(mod.Codex)
        codex.write_lock = threading.Lock()
        codex.write_poisoned = False
        r, w = os.pipe()
        with os.fdopen(r,'rb') as reader, os.fdopen(w,'wb') as writer:
            codex.proc = type('Process',(),{'stdin':writer})()
            def locked(_deadline):
                self.assertTrue(codex.write_lock.locked())
                raise mod.Error('user_locked','locked')
            with self.assertRaises(mod.Error) as caught:
                codex.write({'method':'turn/start'},locked)
            self.assertEqual(caught.exception.code,'user_locked')
            os.set_blocking(reader.fileno(),False)
            with self.assertRaises(BlockingIOError):
                os.read(reader.fileno(),1)

    def test_malformed_rpc_error_is_not_a_definitive_rejection(self):
        mod = api()
        codex = mod.Codex.__new__(mod.Codex)
        codex.condition, codex.replies, codex.sequence, codex.dead = threading.Condition(), {}, 0, False
        codex.write = lambda message, *_: codex.replies.update({message['id']:{'id':message['id'],'error':None}})
        with self.assertRaises(mod.Error) as caught:
            codex.rpc('turn/start', {})
        self.assertEqual(caught.exception.code, 'invalid_response')

    def test_unknown_receiver_result_never_returns_a_definitive_error(self):
        mod = api()
        for invalid in ({'ok':False,'error':True}, {'ok':False,'error':{'code':'codex_rejected'}},
                        {'ok':True,'result':{},'error':{'code':'codex_rejected','message':'untrusted'}}):
            with self.assertRaises(mod.Error) as caught:
                mod.receiver_result(invalid)
            self.assertEqual(caught.exception.code,'outcome_unknown')

    def test_duplicate_json_fields_are_not_silently_overwritten(self):
        mod = api()
        wire = b'{"id":1,"result":{"accepted":true},"result":null}\n'
        with self.assertRaises(mod.Error) as caught:
            mod.read_frame(io.BytesIO(wire))
        self.assertEqual(caught.exception.code,'invalid_response')
