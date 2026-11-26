"""A single Codex protocol connection shared by the pane and admissions.

The TUI owns initialization and approvals. The receiver serializes turn starts
on that connection so an automated request cannot steer an operator's turn.
"""
import hashlib
import json
import os
from queue import Full, Queue
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import suppress

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import unix_serve


def build(core):
    Error, Codex, ChildRpc, Receiver = core.Error, core.Codex, core.ChildRpc, core.Receiver

    class Bridge(Codex):
        def __init__(self, cwd, model, launch_profile, thread_id=None):
            self.outbox = Queue(maxsize=2048)
            self.write_requests = threading.RLock()
            self.attached = threading.Event()
            self.ready = threading.Event()
            self.viewer_started = threading.Event()
            self.viewer_pid = None
            self.viewer = None
            self.thread_id = thread_id
            self.materialized = thread_id is not None
            self.active_turn = None
            self.finished = set()
            self.forwarded = {}
            self.forward_seq = 0
            self.requests = {}
            self.uncertain_turn = False
            self.pending_start = None
            self.viewer_process = None
            self.latest = None
            self.model = model
            self.workdir = cwd
            self.launch_profile = launch_profile
            self.runtime = tempfile.TemporaryDirectory(prefix='herdr-codex-')
            self.sock = Path(self.runtime.name) / 'viewer.sock'
            binary = core.agent_binary('codex')
            launch = [binary]
            if launch_profile:
                launch += ['--profile', launch_profile]
            self.launch = launch
            try:
                ChildRpc.__init__(self, cwd, [*launch, 'app-server', '--listen', 'stdio://'])
                self.server = unix_serve(self.connection, str(self.sock), max_size=core.MAX_FRAME,
                                         ping_interval=10, ping_timeout=10)
                self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.server_thread.start()
                viewer_argv = [*launch, '--model', model, '--remote', 'unix://' + str(self.sock)]
                if thread_id:
                    viewer_argv.extend(['resume', thread_id])
                self.viewer_process = subprocess.Popen(viewer_argv, cwd=cwd)
                self.viewer_started.set()
            except BaseException:
                self.stop()
                raise

        def stop(self):
            viewer = self.viewer_process
            if viewer and viewer.poll() is None:
                with suppress(ProcessLookupError):
                    viewer.terminate()
            if hasattr(self, 'server'):
                self.server.shutdown()
            if hasattr(self, 'proc'):
                self.stop_unassigned()
            self.runtime.cleanup()

        def emit(self, message):
            viewer = self.viewer
            if viewer is not None:
                try:
                    self.outbox.put_nowait(message)
                except Full:
                    self.attached.clear()
                    viewer.close(1011, 'Viewer could not keep up')

        def connection(self, ws):
            self.viewer_started.wait(timeout=3)
            pid, uid, _gid = struct.unpack('3i', ws.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            with self.condition:
                if (self.viewer is not None or self.viewer_process is None or uid != os.getuid()
                        or not is_descendant(pid, self.viewer_process.pid)):
                    ws.close(1008, 'Worker pane only allows its own viewer')
                    return
                self.viewer = ws
                self.viewer_pid = pid
            def write_events():
                try:
                    while (message := self.outbox.get()) is not None:
                        ws.send(json.dumps(message))
                except (OSError, ConnectionClosed):
                    pass
            writer = threading.Thread(target=write_events, daemon=True)
            writer.start()
            try:
                for raw in ws:
                    event = json.loads(raw)
                    if isinstance(event, dict):
                        self.forward(event)
            except (ValueError, OSError, ConnectionClosed, Error):
                pass
            finally:
                self.attached.clear()
                with self.condition:
                    self.viewer = None
                    self.viewer_pid = None
                with suppress(Exception):
                    self.outbox.put_nowait(None)
                writer.join(timeout=2)

        def refuse(self, event, message):
            if 'id' in event:
                self.emit({'id': event['id'], 'error': {'code': -32000, 'message': message}})

        def forward(self, event):
            method = event.get('method')
            params = event.get('params') or {}
            if method in ('thread/start', 'thread/resume') and self.thread_id:
                if method != 'thread/resume' or params.get('threadId') != self.thread_id:
                    return self.refuse(event, 'This managed pane is bound to one worker thread')
            if method in ('thread/fork', 'thread/unsubscribe', 'thread/archive', 'thread/unarchive',
                          'thread/queue/start', 'thread/queue/add', 'thread/queue/replace',
                          'thread/rewind', 'thread/rollback', 'thread/revert', 'review/start'):
                return self.refuse(event, 'This operation changes a managed worker binding')
            if method == 'thread/start':
                event = dict(event, params=dict(params, cwd=self.workdir, model=self.model,
                                               ephemeral=False, historyMode='paginated'))
            if method and method.startswith('turn/') and params.get('threadId') != self.thread_id:
                return self.refuse(event, 'The turn must target the managed worker thread')
            if method == 'turn/start':
                # The same lock covers read/submit for automation and the
                # operator's native admission. The async event reader keeps
                # delivering approvals and completion events during that wait.
                with self.write_requests:
                    if self.uncertain_turn:
                        return self.refuse(event, 'A preceding turn admission requires inspection')
                    try:
                        result = self.rpc(method, params)
                    except Error as exc:
                        if exc.code in ('outcome_unknown', 'codex_disconnected', 'invalid_response'):
                            self.uncertain_turn = True
                        return self.refuse(event, str(exc))
                    self.observe_start(result)
                    self.emit({'id': event['id'], 'result': result})
                return
            if method and 'id' in event:
                with self.condition:
                    self.forward_seq += 1
                    ident = f'tui:{self.forward_seq}'
                    self.forwarded[ident] = (event['id'], method)
                self.write(dict(event, id=ident))
            else:
                self.write(event)

        def observe_start(self, result):
            turn_id = (result.get('turn') or {}).get('id')
            if not isinstance(turn_id, str) or not turn_id:
                self.uncertain_turn = True
                raise Error('outcome_unknown', 'Codex did not identify the turn')
            with self.condition:
                if turn_id not in self.finished:
                    self.active_turn = turn_id
            return turn_id

        def capture_reply(self, event):
            original = self.forwarded.pop(event['id'], None)
            if original:
                original_id, method = original
                if method in ('thread/start', 'thread/resume'):
                    thread = (event.get('result') or {}).get('thread') or {}
                    thread_id = thread.get('id')
                    if isinstance(thread_id, str) and (self.thread_id in (None, thread_id)):
                        self.thread_id = thread_id
                        self.attached.set()
                        self.ready.set()
                self.emit(dict(event, id=original_id))

        def server_request(self, event):
            params = event.get('params') or {}
            if params.get('threadId') == self.thread_id:
                with self.condition:
                    self.requests[event['id']] = (event['method'], time.time(), params.get('turnId'))
            # The actual Codex TUI decides and replies over this same connection.
            self.emit(event)

        def capture(self, event):
            Codex.capture(self, event)
            params = event.get('params') or {}
            if params.get('threadId') == self.thread_id:
                method = event.get('method')
                turn_id = (params.get('turn') or {}).get('id')
                if method == 'turn/started' and turn_id not in self.finished:
                    self.active_turn = turn_id
                elif method == 'turn/completed' and turn_id:
                    self.finished.add(turn_id)
                    if len(self.finished) > 128:
                        self.finished = set(list(self.finished)[-64:])
                    if self.active_turn == turn_id:
                        self.active_turn = None
                elif method == 'serverRequest/resolved':
                    self.requests.pop(params.get('requestId'), None)
            self.emit(event)

        def live(self):
            viewer = self.viewer_process
            pid = self.viewer_pid
            if viewer is None or viewer.poll() is not None or self.dead or not self.attached.is_set() or pid is None:
                return 'unknown'
            try:
                state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except (OSError, IndexError):
                return 'unknown'
            return 'suspended' if state in ('T', 't') else 'idle'

        def read_thread(self):
            thread = self.rpc('thread/read', {'threadId': self.thread_id, 'includeTurns': False}).get('thread')
            if not isinstance(thread, dict) or thread.get('id') != self.thread_id:
                raise Error('outcome_unknown', 'Could not verify the bound Codex thread')
            return thread

        def metadata(self, turn, parameters):
            reply = self.rpc('thread/attachment/add', {'threadId': self.thread_id, 'attachmentType': 'herdr-managed-run',
                             'identityKey': turn, 'payload': {'parameters': parameters}})
            if (reply.get('attachment') or {}).get('identityKey') != turn:
                raise Error('outcome_unknown', 'Codex did not confirm the exact turn metadata')

        def page(self, method, **params):
            cursor = None
            seen = set()
            while True:
                page = self.rpc(method, dict(threadId=self.thread_id, cursor=cursor, limit=50, **params))
                yield from page.get('data', [])
                cursor = page.get('nextCursor')
                if not cursor:
                    break
                if cursor in seen:
                    raise Error('invalid_response', 'Codex repeated a history cursor')
                seen.add(cursor)

        def before_admission(self, worker):
            if not self.materialized:
                self.rpc('thread/name/set', {'threadId': self.thread_id, 'name': worker})
                self.materialized = True
            turn = next(self.page('thread/turns/list'), None)
            return turn.get('id') if turn else None

        def reconcile(self):
            pending = self.pending_start
            if not self.uncertain_turn or not pending or self.live() == 'unknown':
                return
            with self.write_requests:
                if self.pending_start is not pending:
                    return
                if pending.get('turn') is None:
                    turn = next(self.page('thread/turns/list'), None)
                    if not turn or turn.get('id') == pending['before']:
                        return
                    pending['turn'] = turn.get('id')
                if not pending['turn']:
                    return
                self.latest = (pending['turn'], pending['parameters'])
                self.metadata(*self.latest)
                self.pending_start = None
                self.uncertain_turn = False

        def result(self, turn):
            match = next((item for item in self.page('thread/turns/list') if item.get('id') == turn), {})
            cached = self.state(self.thread_id, turn)
            state = cached.get('status') or match.get('status')
            if state in (None, 'inProgress'):
                return dict(thread_id=self.thread_id, turn_id=turn, state='working' if match else 'unknown')
            if state == 'completed':
                answer = cached.get('answer')
                if answer is None:
                    items = (entry['item'] for entry in self.page('thread/items/list', turnId=turn)
                             if entry.get('turnId') == turn)
                    texts = [item['text'] for item in items if item.get('type') == 'agentMessage'
                             and item.get('phase') != 'commentary' and isinstance(item.get('text'), str)]
                    answer = texts[-1] if texts else ''
            elif state in ('failed', 'interrupted'):
                answer = f'Codex turn {state}.'
            else:
                return dict(thread_id=self.thread_id, turn_id=turn, state='unknown')
            parameters = {}
            if self.latest and self.latest[0] == turn:
                parameters = self.latest[1]
            else:
                record = next((row for row in self.page('thread/attachment/list')
                               if row.get('identityKey') == turn and row.get('attachmentType') == 'herdr-managed-run'), None)
                if record:
                    parameters = record['payload']['parameters']
            completed = match.get('completedAt')
            return dict(thread_id=self.thread_id, turn_id=turn, state='done', outcome=state,
                        parameters=parameters, message=answer,
                        completed_at=datetime.fromtimestamp(completed, timezone.utc).isoformat()
                        if type(completed) is int else None)

        def status(self):
            live = self.live()
            if live == 'idle' and self.uncertain_turn and self.pending_start:
                self.reconcile()
            current = self.latest[0] if self.latest else None
            row = dict(thread_id=self.thread_id, turn_id=current, state=live, active=False)
            if live == 'unknown' or not self.thread_id:
                row['recoverable'] = bool(self.thread_id and self.materialized and (
                    self.dead or (self.viewer_process and self.viewer_process.poll() is not None)))
                return row
            native = self.read_thread().get('status') or {}
            flags = native.get('activeFlags') or []
            active = native.get('type') == 'active' or self.active_turn is not None
            with self.condition:
                requests = list(self.requests.items())
            if self.uncertain_turn:
                row.update(state='unknown', active=active)
            elif live == 'suspended' or flags or requests:
                row.update(state='suspended' if live == 'suspended' else 'blocked', active=active)
                current = self.active_turn or current
                if current and active:
                    row['turn_id'] = current
                    request, (reason, observed, request_turn) = requests[0] if requests else (
                        live if not flags else flags[0], ('operator input required' if flags else 'pane process suspended', time.time(), current))
                    if request_turn and request_turn != current:
                        return row
                    key = (current, str(request), row['state'])
                    if getattr(self, 'last_attention', None) != key:
                        self.last_attention = key
                        self.attention_time = observed
                        self.attention_id = uuid.uuid4().hex[:8]
                    row['attention'] = dict(attention_id=self.attention_id, state=row['state'], reason=reason,
                                            observed_at=datetime.fromtimestamp(self.attention_time, timezone.utc).isoformat())
            elif active:
                self.last_attention = None
                row.update(state='working', active=True)
            elif native.get('type') == 'idle':
                self.last_attention = None
                if current:
                    result = self.result(current)
                    row['state'] = result['state']
                    if row['state'] == 'done':
                        ended = getattr(self, 'completed_at', {})
                        row['completed_at'] = result['completed_at'] or ended.setdefault(current, datetime.now(timezone.utc).isoformat())
                        self.completed_at = {current: row['completed_at']}
            else:
                row['state'] = 'unknown'
            return row

    class InteractiveReceiver(Receiver):
        def __init__(self, cwd, model, launch_profile, worker):
            super().__init__(cwd)
            self.worker = worker
            self.bridge = Bridge(cwd, model, launch_profile)
            self.model = model
            self.launch_profile = launch_profile

        def dispatch(self, packet, check=lambda _deadline=None: None):
            bridge = self.bridge
            op = packet.get('op')
            if op == 'hello':
                return dict(receiver=True, cwd=self.cwd, worker=self.worker, interactive=True)
            if op == 'status':
                return dict(bridge.status(), working_directory=self.cwd, worker=self.worker, agent='codex')
            if op == 'recover':
                with self.mutation, bridge.write_requests:
                    if self.bridge is not bridge or not bridge.status().get('recoverable'):
                        raise Error('worker_unavailable', 'The bound receiver cannot safely restart the TUI')
                    check()
                    bridge.stop()
                    replacement = Bridge(self.cwd, self.model, self.launch_profile, bridge.thread_id)
                    replacement.latest = bridge.latest
                    replacement.pending_start = bridge.pending_start
                    replacement.uncertain_turn = bridge.uncertain_turn
                    self.bridge = replacement
                    if not replacement.ready.wait(20):
                        raise Error('outcome_unknown', 'Codex has not reattached to the exact worker thread')
                    return dict(thread_id=replacement.thread_id, status='resumed')
            if op == 'result':
                return bridge.result(packet['turn_id'])
            if op == 'wait':
                until = time.monotonic() + packet['seconds']
                with bridge.condition:
                    bridge.condition.wait_for(lambda: bridge.state(bridge.thread_id, packet['turn_id']).get('status')
                                              or bridge.dead, timeout=max(0, until - time.monotonic()))
                return bridge.result(packet['turn_id'])
            if op in ('quota', 'check-quota'):
                return self.quota_plan(check) if op == 'check-quota' else read_quota(core, bridge)
            if op not in ('run-admitted', 'steer-bound-admitted'):
                raise Error('invalid_operation', 'Use run or a bound idle steer on the interactive worker')
            if packet.get('override_quota') is True:
                raise Error('invalid_quota_override', 'Managed workers enforce account quota pacing')
            if op == 'run-admitted' and (packet.get('cwd') != self.cwd or packet.get('model') != self.model):
                raise Error('binding_changed', 'Worker working directory or model changed')
            if op == 'steer-bound-admitted' and packet.get('expected_thread') != bridge.thread_id:
                raise Error('binding_changed', 'Worker thread changed')
            inputs = core.text_input(packet['prompt'])
            with bridge.write_requests:
                if bridge.uncertain_turn:
                    raise Error('outcome_unknown', 'Resolve the preceding admission before starting another turn')
                if bridge.live() != 'idle' or not bridge.thread_id:
                    raise Error('worker_unavailable', 'The Codex TUI is detached or suspended')
                if bridge.active_turn or bridge.read_thread().get('status', {}).get('type') != 'idle':
                    raise Error('turn_active', 'An operator or automated turn is already active')
                previous = bridge.latest
                if op == 'run-admitted' and previous:
                    raise Error('already_started', 'Use bound steer on this existing worker')
                parameters = packet.get('parameters', {}) if not previous else previous[1]
                plan = self.quota_plan(check)
                expected = packet.get('account')
                if not expected or plan.get('account') != expected:
                    raise Error('quota_account_changed', 'The selected account could not be verified before submission')
                if plan.get('ordinary_usage_allowed') is False or plan['hold']:
                    raise Error(plan['hold'] or 'quota_unavailable', 'The bound Codex account is holding new work')
                quota_check = core.QuotaWriteGuard(check, min(core.reset_seconds(plan[key]) for key in plan['usable_windows']))
                attempt = dict(before=bridge.before_admission(self.worker), parameters=parameters,
                               input_id=uuid.uuid4().hex, turn=None)
                bridge.pending_start = attempt
                try:
                    response = bridge.rpc('turn/start', dict(threadId=bridge.thread_id, input=inputs,
                                                           model=self.model, clientUserMessageId=attempt['input_id']),
                                          quota_check)
                    turn = bridge.observe_start(response)
                except Error as exc:
                    if exc.code in ('outcome_unknown', 'codex_disconnected', 'invalid_response'):
                        bridge.uncertain_turn = True
                    else:
                        bridge.pending_start = None
                    raise
                attempt['turn'] = turn
                bridge.latest = (turn, parameters)
                try:
                    bridge.metadata(turn, parameters)
                except Error as exc:
                    bridge.uncertain_turn = True
                    raise Error('outcome_unknown', 'Turn admitted, but Codex did not confirm its run metadata') from exc
                bridge.pending_start = None
                return dict(thread_id=bridge.thread_id, turn_id=turn, model=self.model, status='accepted')

        def read_quota(self, check, *, exact=False):
            return read_quota(core, self.bridge, check, exact=exact)

    return InteractiveReceiver


def validate_profile(core, launch_profile):
    if launch_profile:
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', launch_profile) or launch_profile in ('.', '..'):
            raise core.Error('invalid_launch_profile', 'Invalid Codex profile name')
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        if not (home / f'{launch_profile}.config.toml').is_file():
            raise core.Error('invalid_launch_profile', 'The Codex launch profile does not exist')


def preflight(core, cwd, model, launch_profile):
    core.cwd_path(cwd)
    core.validate_model(model)
    if not core.agent_binary('codex') or not shutil.which('codex'):
        raise core.Error('codex_missing', 'Install Codex for the Herdr server user')
    if os.name != 'posix' or not Path('/proc/self/stat').is_file():
        raise core.Error('unsupported_platform', 'Interactive worker panes require Linux and Unix sockets')
    validate_profile(core, launch_profile)
    probe = core.Codex(cwd, launch_profile=launch_profile)
    try:
        cursor = None
        seen = set()
        while True:
            report = probe.rpc('model/list', {'includeHidden': True, 'cursor': cursor, 'limit': 100})
            if any(model in (row.get('id'), row.get('model')) for row in report.get('data', [])):
                break
            cursor = report.get('nextCursor')
            if not cursor or cursor in seen:
                config = probe.rpc('config/read', {'includeLayers': False, 'cwd': cwd}).get('config') or {}
                if config.get('model') != model:
                    raise core.Error('model_unavailable', 'Codex did not identify the selected model')
                break
            seen.add(cursor)
    finally:
        probe.stop_unassigned()
    return dict(ready=True, agent='codex', model=model)


def is_descendant(pid, parent):
    while pid > 1:
        if pid == parent:
            return True
        try:
            pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return False
    return False


def read_quota(core, backend, check=None, *, exact=False):
    limits = backend.rpc('account/rateLimits/read', {}, check)
    result = core.quota(limits, exact=exact)
    account = limits.get('accountId')
    if not account:
        identity = backend.rpc('account/read', {'refreshToken': False}, check)
        current = identity.get('account') or {}
        account = identity.get('userId') or (current.get('email') if current.get('type') == 'chatgpt' else None)
    if not isinstance(account, str) or not account:
        raise core.Error('quota_unavailable', 'Codex did not identify the account for these limits')
    return dict(result, account=hashlib.sha256(account.encode()).hexdigest()[:16],
                ordinary_usage_allowed=limits.get('ordinaryUsageAllowed'),
                observed_at=datetime.now(timezone.utc).isoformat())
