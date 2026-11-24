import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

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

    def test_fractional_quota_allows_exact_pacing_equality(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        now = 1730947200
        with patch.object(receiver,'codex') as backend, patch.object(mod.time,'time',return_value=now):
            backend.rpc.return_value = {'rateLimits':{'primary':{'usedPercent':90.4,'windowDurationMins':300,'resetsAt':now+1728}}}
            plan = receiver.dispatch({'op':'check-quota'})
            self.assertIsNone(plan['hold'])
            self.assertEqual(plan['primary']['remaining_percent'],9.6)
            backend.rpc.return_value = {'rateLimits':{'primary':{'usedPercent':1e-15,'windowDurationMins':300,'resetsAt':now+18000}}}
            self.assertEqual(receiver.dispatch({'op':'check-quota'})['hold'],'quota_behind_pace')

    def test_quota_ignores_an_out_of_range_duration(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        now = 1730947200
        with patch.object(receiver,'codex') as backend, patch.object(mod.time,'time',return_value=now):
            backend.rpc.return_value = {'rateLimits':{
                'primary':{'usedPercent':12.5,'windowDurationMins':10**320,'resetsAt':now+60},
                'secondary':{'usedPercent':50,'windowDurationMins':300,'resetsAt':now+60}}}
            plan = receiver.dispatch({'op':'check-quota'})
            self.assertEqual(plan['usable_windows'],['secondary'])
            self.assertIsNone(plan['hold'])

    def test_run_rejects_behind_pace_before_creating_a_thread(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        packet = dict(op='run', cwd='/workspace', model='model', prompt='literal prompt')
        now = 1730947200
        weekly = dict(usedPercent=90, windowDurationMins=10080, resetsAt=now+2*86400)
        with patch.object(receiver, 'codex') as backend, patch.object(mod, 'time') as clock:
            clock.time.return_value = now
            backend.rpc.return_value = {'rateLimits':{'primary':weekly, 'secondary':None}}
            with self.assertRaises(mod.Error) as caught:
                receiver.dispatch(packet)
            self.assertEqual(caught.exception.code, 'quota_behind_pace')
            self.assertEqual([c.args[0] for c in backend.rpc.call_args_list], ['account/rateLimits/read'])
            self.assertIsNone(receiver.thread)
            self.assertFalse(receiver.uncertain)

    def test_quota_plan_uses_all_and_only_usable_windows(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        now = 1730947200
        primary = dict(usedPercent=50, windowDurationMins=10080, resetsAt=now+2*86400)
        secondary = dict(usedPercent=60, windowDurationMins=300, resetsAt=now+2*3600)
        with patch.object(receiver, 'codex') as backend, patch.object(mod.time, 'time', return_value=now):
            rates: dict[str,dict[str,object]] = {'primary':dict(primary),'secondary':dict(secondary)}
            backend.rpc.return_value = {'rateLimits':rates}
            packet = {'op':'check-quota'}
            plan = receiver.dispatch(packet)
            self.assertEqual(plan['usable_windows'], ['primary','secondary'])
            self.assertIsNone(plan['hold'])  # Equality in the five-hour window passes.
            rates['secondary']['usedPercent'] = 61
            self.assertEqual(receiver.dispatch(packet)['behind_windows'], ['secondary'])
            rates['secondary'] = {'usedPercent':'malformed'}
            plan = receiver.dispatch(packet)
            self.assertEqual(plan['usable_windows'], ['primary'])
            self.assertIsNone(plan['hold'])
            rates['primary']['resetsAt'] = now-1
            self.assertEqual(receiver.dispatch(packet)['hold'], 'quota_unavailable')
            backend.rpc.side_effect = mod.Error('outcome_unknown','No quota response')
            self.assertEqual(receiver.dispatch(packet)['hold'], 'quota_unavailable')
            self.assertIsNone(receiver.thread)
            self.assertFalse(receiver.uncertain)

    def test_idle_quota_override_only_applies_to_one_new_turn(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        receiver.thread, receiver.turn, receiver.model = 'owned', 'previous', 'model'
        packet = dict(op='steer-bound-admitted', expected_thread='owned', prompt='next', seconds=1)
        with patch.object(receiver, 'codex') as backend:
            backend.dead = False
            backend.state.return_value = {'status':'completed'}
            backend.rpc.return_value = {}
            for extra in ({}, {'override_quota':'yes'}):
                with self.assertRaises(mod.Error) as caught:
                    receiver.dispatch(dict(packet, **extra))
                self.assertEqual(caught.exception.code, 'quota_unavailable')
            backend.rpc.reset_mock()
            backend.rpc.return_value = {'turn':{'id':'new'}}
            backend.wait.return_value = {'turn_id':'new'}
            self.assertEqual(receiver.dispatch(dict(packet, override_quota=True))['turn_id'], 'new')
            self.assertEqual([c.args[0] for c in backend.rpc.call_args_list], ['turn/start'])
            backend.rpc.return_value = {}
            with self.assertRaises(mod.Error) as caught:
                receiver.dispatch(packet)
            self.assertEqual(caught.exception.code, 'quota_unavailable')
            self.assertEqual(receiver.turn,'new')

    def test_cli_uses_quota_aware_operations_without_fallback(self):
        mod = api()
        scope = ['--instance','fixture','--workspace','w','--pane','p']
        commands = [(['run','--cwd',str(ROOT),'--agent','codex','--model','model','--override-quota'], 'run-admitted', True),
                    (['steer','--thread-id','thread','--after-turn-id','last'], 'steer-bound-admitted', False),
                    (['steer'], 'steer-admitted', False), (['steer','--turn-id','active'], 'steer', False)]
        with patch.object(mod, 'pane_script'), patch.object(mod, 'verified_binding', return_value=('key','codex')), patch('sys.stdout',new_callable=io.StringIO):
            for args, wire_op, override in commands:
                with (patch.object(mod.sys,'argv',['agent_manager.py',*args,*scope,'--prompt','hello']),
                      patch.object(mod,'request',side_effect=mod.Error('invalid_operation','old receiver')) as request):
                    self.assertEqual(mod.main(),1)
                    request.assert_called_once()
                    self.assertEqual(request.call_args.args,('key',wire_op))
                    self.assertIs(request.call_args.kwargs['override_quota'],override)

    def test_private_binding_changes_with_scope(self):
        mod = api()
        self.assertNotEqual(mod.binding_key('first','w1','w1:p1'), mod.binding_key('second','w1','w1:p1'))
        self.assertNotEqual(mod.binding_key('first','w1','w1:p1'), mod.binding_key('first','w2','w1:p1'))

    def test_hermes_idle_steer_uses_legacy_wire_operation(self):
        mod = api()
        scope = ['--instance','fixture','--workspace','w','--pane','p']
        with (tempfile.TemporaryDirectory() as home, patch.dict(os.environ,{'HERMES_HOME':home}),
              patch.object(mod,'pane_script'), patch.object(mod,'guard'),
              patch('sys.stdout',new_callable=io.StringIO) as output):
            path = mod.binding_file('fixture','w','p')
            if os.name == 'nt':
                stream = mod.windows_io().create_state_file(path)
            else:
                stream = os.fdopen(os.open(path,os.O_CREAT | os.O_EXCL | os.O_WRONLY,0o600),'w',encoding='utf-8')
            with stream:
                json.dump(dict(token='key',agent='hermes',scope=['fixture','w','p']),stream)
            for args, expected in (([], 'steer'), (['--thread-id','owned'], 'steer-bound')):
                with (patch.object(mod.sys,'argv',['agent_manager.py','steer',*scope,'--prompt','hello',*args]),
                      patch.object(mod,'request',return_value={}) as send):
                    self.assertEqual(mod.main(),0,output.getvalue())
                    self.assertEqual(send.call_args.args,('key',expected))
            with (patch.object(mod.sys,'argv',['agent_manager.py','steer',*scope,'--prompt','hello','--override-quota']),
                  patch.object(mod,'request') as send):
                self.assertEqual(mod.main(),1)
                send.assert_not_called()

    def test_binding_cleanup_failure_preserves_partial_provision(self):
        mod = api()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            cwd = str(Path(temporary).resolve())
            with (patch.dict(os.environ, {'HERMES_HOME':temporary}),
                  patch.object(mod, 'pane_call', return_value={'pane_id':'w1:p2'}),
                  patch.object(mod, 'request', return_value={'receiver': True, 'cwd':cwd}),
                  patch.object(mod.os, 'replace', side_effect=PermissionError('binding is busy')),
                  patch.object(Path, 'unlink', side_effect=PermissionError('cleanup is busy'))):
                with self.assertRaises(mod.Error) as caught:
                    mod.provision(('instance','w1'), cwd, 'agents', 'Codex')
            self.assertEqual(caught.exception.code, 'partial_provision')
            self.assertIn('w1:p2', str(caught.exception))

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

    def test_bound_steer_checks_saved_thread_and_idle_turn_before_submission(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        receiver.thread, receiver.turn = 'owned-thread', 'finished-turn'
        with patch.object(receiver, 'apply') as apply, patch.object(receiver, 'active_backend') as backend:
            backend.return_value.wait.return_value = {'turn_id':'new-turn'}
            packet = dict(op='steer-bound', prompt='test', seconds=1, expected_thread='owned-thread', after_turn='finished-turn')
            for changes, code in (({'expected_thread':'wrong'},'thread_changed'), ({'after_turn':'wrong'},'turn_changed'),
                                  ({'expected_thread':None},'invalid_binding')):
                with self.assertRaises(mod.Error) as caught:
                    receiver.dispatch(dict(packet, **changes))
                self.assertEqual(caught.exception.code, code)
                apply.assert_not_called()
            apply.return_value = {'thread_id':'owned-thread','turn_id':'new-turn'}
            self.assertEqual(receiver.dispatch(packet)['turn_id'],'new-turn')
            self.assertEqual(apply.call_args.args[0],'steer')
            backend.return_value.wait.assert_called_once_with('owned-thread','new-turn',1)

    def test_bound_steer_cannot_turn_into_active_steering(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        receiver.thread, receiver.turn = 'owned-thread', 'active-turn'
        with patch.object(receiver, 'codex') as backend:
            backend.dead = False
            backend.state.return_value = {}
            with self.assertRaises(mod.Error) as caught:
                receiver.dispatch(dict(op='steer-bound', prompt='do not send', seconds=1,
                                       expected_thread='owned-thread', after_turn='active-turn'))
            self.assertEqual(caught.exception.code, 'turn_active')
            backend.rpc.assert_not_called()

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
                if method == 'account/rateLimits/read':
                    return {'rateLimits':{'primary':{'usedPercent':0,'windowDurationMins':300,'resetsAt':int(time.time())+60}}}
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
            writer.close()
            self.assertEqual(reader.read(), b'')

    def test_malformed_rpc_error_is_not_a_definitive_rejection(self):
        mod = api()
        codex = mod.Codex.__new__(mod.Codex)
        codex.condition, codex.replies, codex.sequence, codex.dead = threading.Condition(), {}, 0, False
        def malformed_reply(message, _check, deadline):
            codex.replies[message['id']] = {'id':message['id'],'error':None}
            return deadline
        codex.write = malformed_reply
        with self.assertRaises(mod.Error) as caught:
            codex.rpc('turn/start', {})
        self.assertEqual(caught.exception.code, 'invalid_response')

    def test_quota_rpc_reply_wait_honors_request_deadline(self):
        mod = api()
        codex = mod.Codex.__new__(mod.Codex)
        codex.condition, codex.replies, codex.sequence, codex.dead = threading.Condition(), {}, 0, False
        codex.write_lock = threading.Lock()
        codex.write_poisoned = False
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd,'rb'), os.fdopen(write_fd,'wb') as writer:
            codex.proc = type('Process',(),{'stdin':writer})()
            with (patch.object(mod.time,'monotonic',return_value=100),
                  patch.object(codex.condition,'wait_for',return_value=False) as wait):
                with self.assertRaises(mod.Error) as caught:
                    codex.rpc('account/rateLimits/read',{},lambda _until:107)
                self.assertEqual(caught.exception.code,'outcome_unknown')
                self.assertEqual(wait.call_args.kwargs['timeout'],7)
                with self.assertRaises(mod.Error):
                    codex.rpc('turn/steer',{},lambda _until:107)
                self.assertEqual(wait.call_args.kwargs['timeout'],15)

    def test_disconnected_initial_quota_read_does_not_strand_receiver(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        packet = dict(op='run', cwd='/workspace', model='model', prompt='hello')
        broken = Mock(dead=False,write_poisoned=False)
        broken.rpc.side_effect = mod.Error('codex_disconnected','read channel closed')
        receiver.codex = broken
        with self.assertRaises(mod.Error) as caught:
            receiver.dispatch(packet)
        self.assertEqual(caught.exception.code,'quota_unavailable')
        self.assertFalse(receiver.uncertain)
        fresh = Mock(dead=False,write_poisoned=False)
        fresh.rpc.side_effect = ({'thread':{'id':'owned'}},{'turn':{'id':'first'}})
        timeout = mod.Error('outcome_unknown','initialization timed out')
        with patch.object(mod,'Codex',side_effect=(timeout,timeout,fresh)):
            with self.assertRaises(mod.Error) as caught:
                receiver.dispatch(dict(packet,override_quota=True))
            self.assertEqual(caught.exception.code,'codex_not_ready')
            self.assertFalse(receiver.uncertain)
            self.assertEqual(receiver.dispatch(dict(packet,override_quota=True))['turn_id'],'first')
        broken.stop_unassigned.assert_called_once()

    def test_quota_expiring_after_thread_creation_rejects_before_prompt_write(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        packet = dict(op='run',cwd='/workspace',model='model',prompt='hello')
        now = 1730947200
        writes = []
        with patch.object(receiver,'codex') as backend, patch.object(mod.time,'time',return_value=now) as wall:
            def rpc(method, _params, verify):
                verify()
                writes.append(method)
                if method=='account/rateLimits/read':
                    return {'rateLimits':{'primary':{'usedPercent':45,'windowDurationMins':300,'resetsAt':now+1}}}
                if method=='thread/start':
                    wall.return_value=now+1
                    return {'thread':{'id':'unsubmitted'}}
                return {'turn':{'id':'unexpected'}}
            backend.rpc.side_effect = rpc
            with self.assertRaises(mod.Error) as caught:
                receiver.dispatch(packet)
            self.assertEqual(caught.exception.code,'quota_unavailable')
            self.assertEqual(writes,['account/rateLimits/read','thread/start'])
            self.assertIsNone(receiver.thread)
            self.assertFalse(receiver.uncertain)

    def test_unknown_receiver_result_never_returns_a_definitive_error(self):
        mod = api()
        for invalid in ({'ok':False,'error':True}, {'ok':False,'error':{'code':'codex_rejected'}},
                        {'ok':True,'result':{},'error':{'code':'codex_rejected','message':'untrusted'}}):
            with self.assertRaises(mod.Error) as caught:
                mod.receiver_result(invalid)
            self.assertEqual(caught.exception.code,'outcome_unknown')

    def test_initial_turn_prewrite_deadline_does_not_bind_empty_thread(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        packet = dict(op='run',cwd='/workspace',model='model',prompt='hello',override_quota=True)
        with patch.object(receiver,'codex') as backend:
            backend.rpc.side_effect = ({'thread':{'id':'empty'}},mod.Error('deadline_elapsed','Before writing'))
            with self.assertRaises(mod.Error) as caught:
                receiver.dispatch(packet)
            self.assertEqual(caught.exception.code,'deadline_elapsed')
            self.assertIsNone(receiver.thread)
            self.assertFalse(receiver.uncertain)

    def test_quota_expiry_at_real_write_fence_puts_no_bytes_on_pipe(self):
        from contextlib import contextmanager
        mod = api()
        receiver = mod.Receiver('/workspace')
        codex = mod.Codex.__new__(mod.Codex)
        codex.write_lock = threading.Lock()
        codex.write_poisoned = False
        now = 1730947200
        plan = dict(hold=None,usable_windows=['primary'],primary={'reset_at':'2024-11-07T02:40:01Z'})
        read_fd, write_fd = os.pipe()
        with (os.fdopen(read_fd,'rb') as reader, os.fdopen(write_fd,'wb') as writer,
              patch.object(mod.time,'time',return_value=now) as wall, patch.object(receiver,'quota_plan',return_value=plan)):
            codex.proc = type('Process',(),{'stdin':writer})()
            @contextmanager
            def write_fence(_message):
                wall.return_value = now+1
                yield
            codex.write_fence = write_fence
            check = receiver.enforce_quota({},lambda _deadline:None)
            with self.assertRaises(mod.Error) as caught:
                codex.write({'method':'turn/start'},check)
            self.assertEqual(caught.exception.code,'quota_unavailable')
            writer.close()
            self.assertEqual(reader.read(),b'')

    def test_quota_expiry_finishes_an_already_started_prompt_frame(self):
        mod = api()
        receiver = mod.Receiver('/workspace')
        codex = mod.Codex.__new__(mod.Codex)
        codex.write_lock, codex.write_poisoned = threading.Lock(), False
        now = 1730947200
        plan = dict(hold=None,usable_windows=['primary'],primary={'reset_at':'2024-11-07T02:40:01Z'})
        real_write = os.write
        read_fd, write_fd = os.pipe()
        with (os.fdopen(read_fd,'rb') as reader, os.fdopen(write_fd,'wb') as writer,
              patch.object(mod.time,'time',return_value=now) as wall,
              patch.object(mod.time,'monotonic',return_value=100) as monotonic,
              patch.object(receiver,'quota_plan',return_value=plan)):
            codex.proc = type('Process',(),{'stdin':writer})()
            def short_write(fd, data):
                if monotonic.return_value == 100:
                    monotonic.return_value = 102
                    wall.return_value = now+2
                    return real_write(fd,data[:1])
                return real_write(fd,data)
            message = {'method':'turn/start'}
            with patch.object(mod.os,'write',side_effect=short_write):
                codex.write(message,receiver.enforce_quota({},lambda _deadline:None))
            writer.close()
            self.assertEqual(reader.read(),mod.frame({**codex.version,**message}))
            self.assertFalse(codex.write_poisoned)

    def test_duplicate_json_fields_are_not_silently_overwritten(self):
        mod = api()
        wire = b'{"id":1,"result":{"accepted":true},"result":null}\n'
        with self.assertRaises(mod.Error) as caught:
            mod.read_frame(io.BytesIO(wire))
        self.assertEqual(caught.exception.code,'invalid_response')


@unittest.skipUnless(os.name == 'nt', 'Windows transport')
class WindowsTransport(unittest.TestCase):
    def test_receiver_round_trip_in_isolated_python(self):
        mod = api()
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            dependency = root/'skills'/mod.SKILL
            (dependency/'scripts').mkdir(parents=True)
            (dependency/'SKILL.md').touch()
            (dependency/'scripts/pane_manager.py').touch()
            proc = subprocess.Popen([sys.executable, '-I', str(mod.HERE), '_serve', token,
                                     str(root), 'isolated', 'w1', str(root)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                until = time.monotonic() + 5
                while proc.poll() is None and time.monotonic() < until:
                    try:
                        result = mod.request(token, 'hello')
                        break
                    except mod.Error as exc:
                        self.assertEqual(exc.code, 'receiver_unavailable')
                        time.sleep(.05)
                else:
                    self.fail('Windows receiver did not become ready')
                self.assertEqual(result, {'receiver': True, 'cwd': str(root.resolve())})
            finally:
                if proc.poll() is None:
                    proc.terminate()
                proc.communicate(timeout=5)

    def test_owner_only_state_directory_is_reopenable(self):
        mod = api()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            with patch.dict(os.environ, {'HERMES_HOME':temporary}):
                state = mod.state_dir()
                self.assertEqual(mod.state_dir(), state)
                self.assertTrue(state.is_dir())

    def test_binding_is_private_even_without_acl_inheritance(self):
        mod = api()
        win = mod.windows_io()
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary).resolve()
            with win.only_user() as flags:  # Directory ACE does not descend to files.
                self.assertTrue(win.create_directory(str(root/'agent-manager'), win.c.byref(flags)))
            cwd = str(root)
            scope = ('instance', 'w1')
            with (patch.dict(os.environ, {'HERMES_HOME':cwd}),
                  patch.object(mod, 'pane_call', return_value={'pane_id':'w1:p2'}),
                  patch.object(mod, 'request', return_value={'receiver': True, 'cwd':cwd}),
                  patch.object(mod, 'guard')):
                self.assertEqual(mod.provision(scope, cwd, 'agents', 'Codex')['status'], 'ready')
                self.assertRegex(mod.bound(scope, 'w1:p2'), r'^[0-9a-f]{64}$')

    def test_owned_child_stdin_deadline_transport(self):
        mod = api()
        win = mod.windows_io()
        with win.child_stdin() as (read_end, writer):
            with subprocess.Popen([sys.executable, '-I', '-c', 'import sys; print(sys.stdin.readline().strip())'],
                                  stdin=read_end, stdout=subprocess.PIPE) as proc:
                self.assertEqual(writer.write(b'READY\n', time.monotonic() + 3), 6)
                self.assertEqual(proc.communicate(timeout=5)[0].strip(), b'READY')

    def test_unread_stdin_cancels_a_write_past_its_deadline(self):
        win = api().windows_io()
        with win.child_stdin() as (_, writer):
            # Keep the read handle open without consuming data, above the pipe buffer size.
            with self.assertRaises(TimeoutError):
                writer.write(b'x' * (2 * 1024 * 1024), time.monotonic() + .1)
