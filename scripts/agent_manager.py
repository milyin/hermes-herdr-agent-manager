#!/usr/bin/env python3
"""Run Codex or Hermes Agent in a pane owned by the agent manager."""
import argparse
from contextlib import contextmanager, ExitStack, nullcontext, suppress
from datetime import datetime, timezone
from fractions import Fraction
from functools import lru_cache
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

SKILL = 'hermes-herdr-pane-manager'
AGENTS = ('codex', 'hermes')
MAX_FRAME = 1024 * 1024
MAX_PROMPT = 32768
HERE = Path(__file__).resolve()


class Error(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def home():
    if override := os.environ.get('HERMES_HOME'):
        return Path(override).expanduser().absolute()
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'hermes'
    return Path.home() / '.hermes'


@lru_cache(maxsize=1)
def windows_io():
    # -I deliberately excludes the current directory and script directory.
    spec = importlib.util.spec_from_file_location('agent_manager_windows_io', HERE.with_name('windows_io.py'))
    if spec is None or spec.loader is None:
        raise Error('unsupported_platform', 'The Windows transport is missing from the skill install')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pane_script():
    directory = home() / 'skills' / SKILL
    script = directory / 'scripts/pane_manager.py'
    if not (directory / 'SKILL.md').is_file() or not script.is_file():
        raise Error('pane_manager_missing', f'Install {SKILL} in the active profile first')
    if not script.resolve().is_relative_to(directory.resolve()):
        raise Error('pane_manager_missing', 'Pane-manager entry point escapes the installed skill')
    return script.resolve()


def pane_call(operation, instance, workspace, *args, data=None, timeout: float=40):
    cmd = [sys.executable, '-I', '-B', str(pane_script()), operation, '--instance', instance,
           '--workspace', workspace, *args]
    try:
        proc = subprocess.run(cmd, input=data, encoding='utf-8', capture_output=True, timeout=timeout)
        out = json.loads(proc.stdout)
        if not isinstance(out, dict):
            raise ValueError
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise Error('pane_manager_unavailable', 'No verified pane-manager result; inspect state before retrying') from exc
    if proc.returncode or out.get('ok') is not True or not isinstance(out.get('result'), dict):
        failure = out.get('error') or {}
        details = {'pane_id': failure['pane_id']} if isinstance(failure.get('pane_id'), str) else {}
        raise Error(str(failure.get('code', 'pane_manager_rejected')), str(failure.get('message', 'Pane call failed')), **details)
    return out['result']


def guard(instance, workspace, pane, timeout: float=40, *, allow_locked=False):
    result = pane_call('lock-status', instance, workspace, '--pane', pane, timeout=timeout)
    if result.get('pane_id') != pane or type(result.get('user_locked')) is not bool:
        raise Error('lock_unknown', 'Cannot verify the exact pane and its lock')
    if result['user_locked'] and not allow_locked:
        raise Error('user_locked', 'The pane is under user control')


def private_dir(path):
    if os.name == 'nt':
        try:
            return windows_io().private_directory(path)
        except OSError as exc:
            raise Error('unsafe_state', 'The agent-manager state directory must be private and owned by the current user') from exc
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Error('unsafe_state', 'The agent-manager state directory must be private and owned by the current user')
    return path


def state_dir():
    return private_dir(home() / 'agent-manager')


def binding_key(instance, workspace, pane):
    return hashlib.sha256(json.dumps([instance, workspace, pane]).encode()).hexdigest()


def binding_file(instance, workspace, pane):
    return state_dir() / (binding_key(instance, workspace, pane) + '.json')


def endpoint(token):
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        raise Error('bad_binding', 'Invalid local receiver identity')
    if os.name == 'nt':
        return windows_io().pipe_name(hashlib.sha256(token.encode('ascii')).hexdigest())
    # Linux abstract sockets avoid the length limit of filesystem paths. Both ends check peer UID.
    return '\0hermes-agent-manager-' + token


def verify_peer(conn):
    import struct
    _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid():
        raise Error('peer_rejected', 'Receiver IPC is restricted to the same local UID')


@contextmanager
def listen_channel(token):
    if os.name == 'nt':
        with windows_io().Listener(endpoint(token)) as listener:
            yield listener.accept
    else:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(endpoint(token))
            server.listen(8)
            yield lambda: server.accept()[0]


@contextmanager
def connect_channel(token):
    if os.name == 'nt':
        with windows_io().client(endpoint(token)) as conn:
            yield conn
    else:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(3)
            conn.connect(endpoint(token))
            verify_peer(conn)
            yield conn


def read_channel(conn, seconds):
    if os.name == 'nt':
        return read_frame(io.BytesIO(conn.line(MAX_FRAME + 1, time.monotonic() + seconds)))
    conn.settimeout(seconds)
    with conn.makefile('rb') as stream:
        return read_frame(stream)


def send_channel(conn, data, seconds):
    if os.name == 'nt':
        conn.send(data, time.monotonic() + seconds)
    else:
        conn.settimeout(seconds)
        conn.sendall(data)


def frame(value):
    raw = json.dumps(value, allow_nan=False, ensure_ascii=True).encode() + b'\n'
    if len(raw) > MAX_FRAME:
        raise Error('frame_too_large', 'Result exceeds the local IPC limit')
    return raw


def unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('Duplicate JSON key')
        obj[key] = value
    return obj


def read_frame(stream):
    raw = stream.readline(MAX_FRAME + 1)
    if not raw or len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
        raise Error('no_result', 'Missing or oversized IPC frame')
    try:
        value = json.loads(raw,object_pairs_hook=unique_keys)
    except ValueError as exc:
        raise Error('invalid_response', 'Invalid IPC JSON') from exc
    if not isinstance(value, dict):
        raise Error('invalid_response', 'Expected an IPC object')
    return value


def validate_model(name, *, hermes=False):
    pattern = r'[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,191}' if hermes else r'[a-zA-Z0-9][a-zA-Z0-9._/-]{0,127}'
    if not isinstance(name, str) or not re.fullmatch(pattern, name):
        raise Error('invalid_model', 'Supply a valid model ID')
    return name


def text_input(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_PROMPT:
        raise Error('invalid_prompt', 'Supply a nonempty UTF-8 prompt of at most 32 KiB')
    return [{'type':'text','text':text,'text_elements':[]}]


def cwd_path(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise Error('invalid_cwd', 'Supply an existing absolute working directory')
    return str(path.resolve(strict=True))


def json_percent(value):
    return value.numerator if value.denominator == 1 else float(value)


def quota(response, *, exact=False):
    by_id = response.get('rateLimitsByLimitId')
    codex = by_id.get('codex') if isinstance(by_id, dict) else None
    rates = codex if isinstance(codex, dict) and codex else response.get('rateLimits')
    if not isinstance(rates, dict) or rates.get('limitId') not in (None,'codex'):
        raise Error('quota_unavailable', 'Codex did not report rate limits for this account')
    def window(key):
        val = rates.get(key)
        if val is None:
            return None
        try:
            used = val['usedPercent']
            mins = val.get('windowDurationMins')
            reset = val.get('resetsAt')
            if type(used) not in (int,float) or not 0 <= used < float('inf'):
                raise ValueError
            if mins is not None and (type(mins) is not int or not 0 < mins < 2**63):
                raise ValueError
            if reset is not None and type(reset) is not int:
                raise ValueError
            utc = None if reset is None else datetime.fromtimestamp(reset, timezone.utc).isoformat().replace('+00:00','Z')
        except (KeyError, TypeError, ValueError, OverflowError, OSError, AttributeError):
            return None
        remaining = Fraction(0) if used >= 100 else 100-Fraction(str(used))
        return {'remaining_percent':remaining if exact else json_percent(remaining), 'window_minutes':mins, 'reset_at':utc}
    return {'source':'codex_account_rate_limits', 'primary':window('primary'), 'secondary':window('secondary')}


def reset_seconds(window):
    return int(datetime.fromisoformat(window['reset_at'].replace('Z','+00:00')).timestamp())


class QuotaWriteGuard:
    def __init__(self, check, expires):
        self.check, self.expires = check, expires

    def __call__(self, deadline=None):
        until = self.check(deadline)
        self.seconds_left()
        return until

    def seconds_left(self):
        left = self.expires-time.time()
        if left <= 0:
            raise Error('quota_unavailable','Codex quota snapshot expired before the prompt write')
        return left


def agent_binary(name):
    # Do not load project-local executables, relative PATH entries, or batch shims.
    entries = [entry.strip('"') for entry in os.get_exec_path()]
    path = os.pathsep.join(entry for entry in entries if Path(entry).is_absolute())
    if os.name == 'nt':
        for entry in path.split(os.pathsep):
            if entry and (binary := Path(entry) / (name + '.exe')).is_file():
                return str(binary.resolve())
        return None
    return shutil.which(name, path=path) if path else None


class ChildRpc:
    label = 'Agent'
    kind = 'agent'
    version = {}

    def __init__(self, cwd, command, *, env=None):
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.write_poisoned = False
        self.replies = {}
        self.turns = {}
        self.sequence = 0
        self.dead = False
        self.windows_stdin = None
        self.io_stack = ExitStack()
        try:
            if os.name == 'nt':
                child_read, self.windows_stdin = self.io_stack.enter_context(windows_io().child_stdin())
                self.proc = subprocess.Popen(command, cwd=cwd, stdin=child_read, stdout=subprocess.PIPE, env=env)
                child_read.close()  # The subprocess owns its duplicate.
            else:
                self.proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
                assert self.proc.stdin is not None
                os.set_blocking(self.proc.stdin.fileno(), False)
            self.reader = threading.Thread(target=self.listen, daemon=True)
            self.reader.start()
        except BaseException:
            if hasattr(self, 'proc'):
                if self.proc.poll() is None:
                    self.proc.terminate()
                self.dispose()
            else:
                self.io_stack.close()
            raise

    def exited(self):
        return self.proc.poll() is not None

    def dispose(self):
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        self.io_stack.close()
        if hasattr(self, 'reader'):
            self.reader.join(timeout=1)
            if not self.reader.is_alive() and self.proc.stdout is not None:
                self.proc.stdout.close()

    def stop_unassigned(self):
        if not self.exited():
            self.proc.terminate()
        self.dispose()

    def write(self, msg, check=None, deadline=None):
        deadline = time.monotonic()+15 if deadline is None else deadline
        data = memoryview(frame({**self.version, **msg}))
        if not self.write_lock.acquire(timeout=max(0,deadline-time.monotonic())):
            raise Error('deadline_elapsed',f'Timed out before writing to {self.label}')
        started = False
        quota_deadline = None
        try:
            if self.write_poisoned:
                raise Error('outcome_unknown','A prior frame was partially written; do not send more input')
            try:
                win_pipe = getattr(self, 'windows_stdin', None)
                while data:
                    write_deadline = deadline if quota_deadline is None else min(deadline,quota_deadline)
                    remaining = write_deadline-time.monotonic()
                    if remaining <= 0:
                        code = 'outcome_unknown' if started else 'deadline_elapsed'
                        if not started and quota_deadline is not None and quota_deadline <= deadline:
                            code = 'quota_unavailable'
                        raise Error(code,f'Timed out writing to {self.label}; inspect the prior turn before retrying')
                    try:
                        if not started and check is not None:
                            request_deadline = check(deadline)
                            if request_deadline is not None:
                                deadline = min(deadline,request_deadline)
                            if time.monotonic() >= deadline:
                                raise Error('deadline_elapsed','Lock verification finished after the write deadline')
                        with (nullcontext() if started else self.write_fence(msg)):
                            # The approval fence can block after pane verification, including
                            # on Windows where an expired overlapped write could still complete.
                            if not started and time.monotonic() >= deadline:
                                raise Error('deadline_elapsed', 'The write deadline expired before the agent received input')
                            if not started and isinstance(check,QuotaWriteGuard):
                                quota_deadline = time.monotonic()+check.seconds_left()
                            write_deadline = deadline if quota_deadline is None else min(deadline,quota_deadline)
                            if win_pipe is not None:
                                # Only queue the first byte under the quota cutoff. If
                                # cancellation races completion, poison the stream and
                                # never send the rest of this frame or replay it.
                                first = not started and isinstance(check,QuotaWriteGuard) and quota_deadline is not None
                                if first and time.monotonic() >= write_deadline:
                                    if write_deadline == deadline:
                                        raise Error('deadline_elapsed','Transport deadline passed before the Windows pipe write')
                                    raise Error('quota_unavailable','Codex quota cutoff passed before the Windows pipe write')
                                started = True  # An overlapped write can have partially reached the child.
                                size = win_pipe.write(data[:1] if first else data,write_deadline)
                                if first and isinstance(check,QuotaWriteGuard) and (
                                        time.monotonic() >= write_deadline or time.time() >= check.expires):
                                    raise TimeoutError('First Windows prompt byte was not confirmed in time')
                            else:
                                assert self.proc.stdin is not None
                                size = os.write(self.proc.stdin.fileno(), data)
                    except BlockingIOError:
                        assert self.proc.stdin is not None
                        select.select([],[self.proc.stdin.fileno()],[],max(0,write_deadline-time.monotonic()))
                    else:
                        started = True
                        quota_deadline = None
                        if size <= 0:
                            raise Error('outcome_unknown', f'{self.label} stdin made no progress; do not replay')
                        data = data[size:]
            except TimeoutError as exc:
                raise Error('outcome_unknown', f'Timed out writing to {self.label}; inspect the prior turn before retrying') from exc
            except OSError as exc:
                raise Error(f'{self.kind}_disconnected', f'{self.label} disconnected; reconcile before any retry') from exc
        except BaseException:
            if started and data:
                self.write_poisoned = True
            raise
        finally:
            self.write_lock.release()
        return deadline

    @contextmanager
    def write_fence(self, msg):
        yield

    def listen(self):
        try:
            assert self.proc.stdout is not None
            while True:
                event = read_frame(self.proc.stdout)
                if self.version and event.get('jsonrpc') != '2.0':
                    raise Error('invalid_response', 'Expected a JSON-RPC 2.0 frame')
                if 'method' in event and 'id' in event:
                    self.server_request(event)
                    continue
                with self.condition:
                    if 'id' in event:
                        ident = event['id']
                        if type(ident) is int and ident in self.replies:
                            self.replies[ident] = event
                        else:
                            self.capture_reply(event)
                    else:
                        self.capture(event)
                    self.condition.notify_all()
        except (Error, OSError, ValueError):
            pass
        finally:
            with self.condition:
                self.dead = True
                self.condition.notify_all()

    def server_request(self, event):
        self.write({'id':event['id'],'error':{'code':-32000,'message':'No permission was granted by the operator'}})

    def capture_reply(self, event):
        pass

    def capture(self, event):
        raise NotImplementedError

    def wait(self, thread, turn, seconds):
        raise NotImplementedError

    def rpc(self, method, params, check=None, *, timeout=15):
        deadline = time.monotonic()+timeout
        with self.condition:
            if self.dead:
                raise Error(f'{self.kind}_disconnected', f'{self.label} is no longer connected; no work will be replayed')
            self.sequence += 1
            ident = self.sequence
            self.replies[ident] = None
        try:
            written_by = self.write({'id':ident,'method':method,'params':params},check,deadline)
            if method == 'account/rateLimits/read':
                # An unanswered quota read cannot submit work. Bound its reply
                # wait so it can return that definitive result to the caller.
                deadline = written_by
            with self.condition:
                self.condition.wait_for(lambda: self.replies[ident] is not None or self.dead, timeout=max(0,deadline-time.monotonic()))
                data = self.replies[ident]
            if data is None:
                raise Error('outcome_unknown', f'No verified {method} result; do not repeat the request blindly')
            if 'error' in data:
                failure = data['error']
                if 'result' in data or not isinstance(failure,dict) or type(failure.get('code')) is not int or not isinstance(failure.get('message'),str):
                    raise Error('invalid_response', f'Ambiguous error envelope from {method}')
                raise Error(f'{self.kind}_rejected', f'{self.label} refused {method}')
            if not isinstance(data.get('result'), dict):
                raise Error('invalid_response', f'Unexpected result from {method}')
            return data['result']
        finally:
            with self.condition:
                self.replies.pop(ident, None)


class Codex(ChildRpc):
    label = 'Codex'
    kind = 'codex'

    def __init__(self, cwd, check=None, *, launch_profile=None):
        binary = agent_binary('codex')
        if not binary:
            raise Error('codex_missing', 'Install and authenticate Codex before using this skill')
        args = [binary, '-c', 'features.fast_mode=false']
        if launch_profile:
            args.extend(['--profile', launch_profile])
        super().__init__(cwd, [*args, 'app-server', '--listen', 'stdio://'])
        try:
            self.rpc('initialize', {'clientInfo':{'name':'hermes_herdr_agent_manager','title':'Hermes Herdr Agent Manager','version':'0.1.0'}},check)
            self.write({'method':'initialized'},check)
        except BaseException:
            self.stop_unassigned()
            raise

    def capture(self, event):
        params = event.get('params')
        if not isinstance(params, dict):
            return
        thread = params.get('threadId')
        turn = params.get('turn') or {}
        key = turn.get('id') or params.get('turnId')
        if not isinstance(thread, str) or not isinstance(key, str):
            return
        method = event.get('method')
        if method not in ('turn/started','turn/completed','item/completed'):
            return
        if len(self.turns) >= 128 and (thread,key) not in self.turns:
            self.turns.pop(next(iter(self.turns)))
        row = self.turns.setdefault((thread,key), {})
        if method == 'item/completed':
            item = params.get('item') or {}
            if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary' and isinstance(item.get('text'),str):
                row['answer'] = item['text']
        elif method == 'turn/completed':
            row['status'] = turn.get('status')
            failure = turn.get('error')
            code = failure.get('codexErrorInfo') if isinstance(failure,dict) else None
            if code in ('modelNotFound','usageLimitExceeded','authenticationFailed','sandboxError','serverOverloaded'):
                row['error_code'] = code

    def state(self, thread, turn):
        with self.condition:
            return dict(self.turns.get((thread,turn), {}))

    def wait(self, thread, turn, seconds):
        with self.condition:
            self.condition.wait_for(lambda: self.turns.get((thread,turn),{}).get('status') is not None or self.dead, timeout=seconds)
            row = dict(self.turns.get((thread,turn),{}))
        status = row.get('status')
        if status is None:
            raise Error('outcome_unknown' if self.dead else 'turn_pending', f'Turn {turn} has no verified final result; use wait, not resend')
        if status != 'completed':
            detail = f" ({row['error_code']})" if row.get('error_code') else ''
            raise Error('turn_failed', f'Codex turn {turn} ended with status {status}{detail}; no automatic replay')
        if not row.get('answer'):
            raise Error('answer_unavailable', f'Turn {turn} completed without a final text item')
        return dict(thread_id=thread, turn_id=turn, answer=row['answer'])


class HermesAcp(ChildRpc):
    label = 'Hermes'
    kind = 'hermes'
    version = {'jsonrpc': '2.0'}

    def __init__(self, cwd, check, *, command=None):
        if command is None:
            binary = agent_binary('hermes')
            if binary is None:
                raise Error('hermes_missing', 'Put a native Hermes launcher with ACP support on the Herdr server PATH')
            command = [binary, 'acp']
        env = os.environ.copy()
        for name in ('HERMES_YOLO', 'HERMES_YOLO_MODE', 'HERMES_ACCEPT_HOOKS'):
            env.pop(name, None)
        self.session = None
        self.task = None
        self.blocked = False
        self.approval_note = 'denial was not confirmed'
        super().__init__(cwd, command, env=env)
        try:
            result = self.rpc('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                'clientInfo': {'name': 'hermes-herdr-agent-manager', 'version': '0.3.0'}}, check, timeout=25)
            if type(result.get('protocolVersion')) is not int or result['protocolVersion'] != 1:
                raise Error('hermes_incompatible', 'Hermes did not negotiate ACP version 1')
        except BaseException:
            self.stop_unassigned()
            raise

    def open_session(self, cwd, check):
        result = self.rpc('session/new', {'cwd': cwd, 'mcpServers': []}, check, timeout=35)
        ident = result.get('sessionId')
        if not isinstance(ident, str) or not ident or len(ident) > 256:
            raise Error('invalid_response', 'Hermes did not identify the new ACP session')
        self.session = ident
        models = result.get('models')
        current = models.get('currentModelId') if isinstance(models, dict) else None
        return ident, current if isinstance(current, str) else None

    @contextmanager
    def write_fence(self, msg):
        if msg.get('method') in ('session/new', 'session/prompt', 'session/set_model'):
            # Lock order is write_lock -> condition. Recheck after the potentially slow pane guard,
            # and serialize the first write with the approval latch. Replies bypass this fence.
            with self.condition:
                self.require_available()
                yield
        else:
            yield

    def server_request(self, event):
        # No operator approval or host capability is proxied. Latch before replying
        # so a subsequent caller cannot continue an approval-blocked session.
        with self.condition:
            self.blocked = True
            self.approval_note = 'denial was not confirmed'
            self.condition.notify_all()
        if event['method'] == 'session/request_permission':
            params = event.get('params')
            options = params.get('options') if isinstance(params, dict) and self.session and params.get('sessionId') == self.session else None
            outcome = {'outcome': 'cancelled'}
            if isinstance(options, list) and len(options) <= 128 and all(isinstance(option, dict) for option in options):
                ids = [option.get('optionId') for option in options]
                for option, ident in zip(options, ids):
                    if (option.get('kind') == 'reject_once' and isinstance(ident, str)
                            and 0 < len(ident) <= 256 and ids.count(ident) == 1):
                        outcome = {'outcome': 'selected', 'optionId': ident}
                        break
            self.write({'id': event['id'], 'result': {'outcome': outcome}})
            if outcome['outcome'] == 'selected':
                with self.condition:
                    self.approval_note = 'explicit rejection was sent; check settlement in Hermes'
        else:
            super().server_request(event)

    def approval_error(self):
        return Error('approval_required', f'Hermes requested operator input; no permission was granted ({self.approval_note})')

    def require_available(self, *, idle=False):
        with self.condition:
            if self.blocked:
                raise self.approval_error()
            if self.dead or (self.task and self.task['status'] == 'unknown'):
                raise Error('outcome_unknown', 'Hermes session outcome is not verified; inspect it in Hermes, do not replay')
            if idle and self.task and self.task['status'] is None:
                raise Error('turn_active', 'Wait for the current Hermes turn before sending another operation')

    def submit(self, prompt, check):
        with self.condition:
            self.require_available(idle=True)
            self.sequence += 1
            ident = self.sequence
            turn = uuid.uuid4().hex
            previous = self.task
            self.task = {'rpc_id': ident, 'turn': turn, 'status': None, 'text': '', 'bytes': 0}
        try:
            self.write({'id': ident, 'method': 'session/prompt', 'params': {
                'sessionId': self.session, 'prompt': [{'type': 'text', 'text': prompt}]}}, check)
        except Error as exc:
            with self.condition:
                if exc.code in ('outcome_unknown', 'hermes_disconnected'):
                    assert self.task is not None
                    self.task['status'] = 'unknown'
                else:
                    self.task = previous
                self.condition.notify_all()
            raise
        return turn

    def capture(self, event):
        row = self.task
        params = event.get('params')
        if (not row or row['status'] is not None or event.get('method') != 'session/update'
                or not isinstance(params, dict) or params.get('sessionId') != self.session):
            return
        update = params.get('update')
        if not isinstance(update, dict) or update.get('sessionUpdate') != 'agent_message_chunk':
            return
        content = update.get('content')
        text = content.get('text') if isinstance(content, dict) and content.get('type') == 'text' else None
        if isinstance(text, str) and row['bytes'] <= MAX_FRAME // 2:
            row['bytes'] += len(json.dumps(text, ensure_ascii=True))
            if row['bytes'] <= MAX_FRAME // 2:
                row['text'] += text

    def capture_reply(self, event):
        row = self.task
        if not row or type(event.get('id')) is not int or event['id'] != row['rpc_id'] or row['status'] is not None:
            return
        result = event.get('result')
        stop = result.get('stopReason') if isinstance(result, dict) and 'error' not in event else None
        if stop == 'end_turn':
            row['status'] = 'completed'
        elif stop in ('cancelled', 'max_tokens', 'max_turn_requests', 'refusal'):
            row['status'] = 'failed'
        else:
            # ACP errors may represent a lost gateway admission, not a clean rejection.
            row['status'] = 'unknown'

    def wait(self, thread, turn, seconds):
        with self.condition:
            row = self.task
            if not row or row['turn'] != turn or thread != self.session:
                raise Error('unknown_turn', 'Only the latest Hermes turn in this receiver can be read')
            self.condition.wait_for(lambda: row['status'] is not None or self.blocked or self.dead, timeout=seconds)
            if self.blocked:
                raise self.approval_error()
            if row['status'] == 'failed':
                raise Error('turn_failed', f'Hermes turn {turn} did not complete normally; no automatic replay')
            if row['status'] != 'completed':
                code = 'outcome_unknown' if self.dead or row['status'] == 'unknown' else 'turn_pending'
                raise Error(code, f'Hermes turn {turn} has no verified final result; do not resend')
            if not row['text'] or row['bytes'] > MAX_FRAME // 2:
                raise Error('answer_unavailable', 'Hermes finished without complete, bounded session output')
            # ACP v1 identifies the completion by request ID, but assistant chunks only
            # by session. Do not present session-wide output as this admission's answer.
            return dict(thread_id=thread, turn_id=turn, status='completed', session_output=row['text'])


class Receiver:
    agent = 'codex'

    def __init__(self, cwd):
        self.cwd = cwd
        self.codex = None
        self.thread = None
        self.turn = None
        self.model = None
        self.network = False
        self.uncertain = False
        self.quota_backend_failed = False
        self.mutation = threading.Lock()

    def backend(self, check):
        if not self.uncertain and self.thread is None and self.codex is not None and (
                self.quota_backend_failed or self.codex.dead is True or self.codex.write_poisoned is True):
            # No prompt or thread is attached to this broken client. Replace it
            # on a subsequent request, not while returning the rejection.
            check()
            self.codex.stop_unassigned()
            self.codex = None
            self.quota_backend_failed = False
        if self.codex is None:
            for attempt in range(2):
                try:
                    # Startup only: no thread or prompt has been sent by this client yet.
                    check()
                    self.codex = Codex(self.cwd,check)
                    break
                except Error as exc:
                    if exc.code not in ('codex_disconnected','outcome_unknown'):
                        raise
                    if attempt:
                        raise Error('codex_not_ready','Codex initialization failed before any prompt was sent') from exc
        return self.codex

    def quota_plan(self, check):
        try:
            snapshot = self.read_quota(check,exact=True)
        except Error as exc:
            # This was a read, so lack of a response cannot mean a prompt was sent.
            if exc.code not in ('quota_unavailable', 'outcome_unknown', 'invalid_response', 'codex_rejected', 'codex_disconnected','codex_not_ready'):
                raise
            snapshot = {'primary':None, 'secondary':None}
        windows, behind = [], []
        now = Fraction(str(time.time()))
        for name in ('primary','secondary'):
            window = snapshot[name]
            if window is None:
                continue
            remaining = window['remaining_percent']
            window['remaining_percent'] = json_percent(remaining)
            if window['window_minutes'] is None or window['reset_at'] is None:
                continue
            left = reset_seconds(window) - now
            if left <= 0:
                continue
            windows.append(name)
            duration = window['window_minutes'] * 60
            if remaining * duration < 100 * min(left, duration):
                behind.append(name)
        code = 'quota_unavailable' if not windows else 'quota_behind_pace' if behind else None
        return dict(snapshot, usable_windows=windows, behind_windows=behind, hold=code)

    def enforce_quota(self, packet, check):
        if packet.get('override_quota') is True:
            return check
        plan = self.quota_plan(check)
        if plan['hold'] == 'quota_unavailable':
            raise Error('quota_unavailable', 'No usable Codex quota window; no prompt was sent')
        if plan['hold'] == 'quota_behind_pace':
            raise Error('quota_behind_pace', 'Codex quota below the remaining time share; no prompt was sent')
        windows = plan['usable_windows']
        assert isinstance(windows,list) and windows
        expires = min(reset_seconds(plan[name]) for name in windows)

        return QuotaWriteGuard(check,expires)

    def read_quota(self, check, *, exact=False):
        backend = self.backend(check)
        assert backend is not None
        try:
            return quota(backend.rpc('account/rateLimits/read', {},check),exact=exact)
        except Error as exc:
            if self.thread is None and exc.code == 'codex_disconnected':
                self.quota_backend_failed = True
            raise

    def start(self, inputs, check):
        assert self.codex is not None
        result = self.codex.rpc('turn/start', {'threadId':self.thread,'input':inputs,'model':self.model,
            'cwd':self.cwd,'approvalPolicy':'never','sandboxPolicy':{'type':'workspaceWrite',
            'writableRoots':[self.cwd],'networkAccess':self.network,
            'excludeTmpdirEnvVar':True,'excludeSlashTmp':True}},check)
        turn = result.get('turn')
        ident = turn.get('id') if isinstance(turn,dict) else None
        if not isinstance(ident,str) or not ident:
            raise Error('outcome_unknown', 'Codex did not identify the started turn')
        self.turn = ident
        return dict(thread_id=self.thread, turn_id=self.turn, model=self.model, status='accepted')

    def thread_options(self):
        return {'cwd':self.cwd,'model':self.model,'approvalPolicy':'never',
            'sandbox':'workspace-write','config':{'sandbox_workspace_write.network_access':self.network,
            'sandbox_workspace_write.writable_roots':[self.cwd],
            'sandbox_workspace_write.exclude_tmpdir_env_var':True,'sandbox_workspace_write.exclude_slash_tmp':True}}

    def dispatch(self, packet, check=lambda _deadline=None: None):
        op = packet.get('op')
        if op == 'hello':
            receipt = {'receiver':True, 'cwd':self.cwd}
            return receipt if self.agent == 'codex' else dict(receipt, agent=self.agent)
        # Use distinct wire operations for clients requiring quota enforcement.
        # An older receiver rejects them instead of ignoring a new field.
        if op in ('run-admitted','steer-admitted','steer-bound-admitted'):
            op = op.removesuffix('-admitted')
        if op in ('run', 'quota', 'check-quota') and packet.get('agent', 'codex') != self.agent:
            raise Error('agent_mismatch', f'This pane is provisioned for {self.agent}; use a pane for the requested agent')
        if packet.get('override_quota') is True and (self.agent != 'codex' or op not in ('run','steer','steer-bound')
                                                      or packet.get('expected_turn') is not None):
            raise Error('invalid_quota_override', 'Quota override is only for a new Codex turn')
        if op == 'wait':
            with self.mutation:
                backend, thread, turn = self.active_backend(), self.thread, self.turn
            if backend is None or not turn or packet.get('turn_id') != turn:
                raise Error('unknown_turn', 'Only the latest confirmed turn in this receiver can be read')
            return backend.wait(thread, turn, packet['seconds'])
        if not self.mutation.acquire(blocking=False):
            raise Error('receiver_busy', 'Another operation is in progress; inspect before retrying')
        try:
            if op == 'steer-bound':
                expected, previous = packet.get('expected_thread'), packet.get('after_turn')
                if (not isinstance(expected,str) or not expected.strip() or len(expected)>256 or
                        (previous is not None and (not isinstance(previous,str) or not previous.strip() or len(previous)>256)) or
                        packet.get('expected_turn') is not None):
                    raise Error('invalid_binding','Bound steer requires a saved thread and is only for idle follow-up')
                if self.thread != expected:
                    raise Error('thread_changed','Expected thread does not match; no prompt was sent')
                if previous is not None and self.turn != previous:
                    raise Error('turn_changed','Previous turn does not match; no prompt was sent')
                op = 'steer'
            reply = self.apply(op, packet, check)
            backend = self.active_backend()
        except Error as exc:
            if op in ('run','steer') and exc.code in ('outcome_unknown','invalid_response','codex_disconnected','hermes_disconnected'):
                self.uncertain = True
            raise
        finally:
            self.mutation.release()
        if op == 'steer':
            assert backend is not None
            return backend.wait(reply['thread_id'],reply['turn_id'],packet['seconds'])
        return reply

    def active_backend(self) -> ChildRpc | None:
        return self.codex

    def apply(self, op, packet, check):
        if op == 'check-quota':
            return self.quota_plan(check)
        if op == 'quota':
            return self.read_quota(check)
        if op == 'recover':
            if self.thread is None or self.codex is None:
                raise Error('not_running', 'No recorded Codex thread is available for recovery')
            if not self.codex.exited():
                raise Error('codex_still_running', 'Will not interrupt a Codex process that has not exited')
            check()
            self.codex.dispose()
            check()
            replacement = Codex(self.cwd,check)
            self.uncertain = True
            try:
                output = replacement.rpc('thread/resume',dict(threadId=self.thread,**self.thread_options()),check)
                thread = output.get('thread')
                status = thread.get('status') if isinstance(thread,dict) else None
                if not isinstance(thread,dict) or not isinstance(status,dict) or thread.get('id') != self.thread or status.get('type') != 'idle':
                    raise Error('outcome_unknown', 'Could not verify the recorded thread is resumed and idle')
            except BaseException:
                # This new backend has not been allowed to submit any prompt.
                replacement.stop_unassigned()
                raise
            self.codex = replacement
            self.turn = None
            self.uncertain = False
            return dict(thread_id=self.thread,status='resumed')
        if self.uncertain:
            raise Error('outcome_unknown', 'A prior mutation could not be reconciled; no automatic replay')
        if op == 'run':
            if packet.get('cwd') != self.cwd:
                raise Error('cwd_mismatch', 'Working directory must match the owned pane provisioning directory')
            if self.thread is not None:
                raise Error('already_started', 'Use steer on the existing Codex thread')
            self.model = validate_model(packet['model'])
            inputs = text_input(packet['prompt'])
            self.network = packet.get('network') is True
            self.backend(check)
            assert self.codex is not None
            check_turn = self.enforce_quota(packet,check)
            result = self.codex.rpc('thread/start', self.thread_options(),check)
            thread = result.get('thread')
            self.thread = thread.get('id') if isinstance(thread,dict) else None
            if not isinstance(self.thread,str) or not self.thread:
                raise Error('outcome_unknown', 'Codex did not identify the new thread')
            print('Codex thread ready', flush=True)
            try:
                return self.start(inputs,check_turn)
            except Error as exc:
                if exc.code in ('quota_unavailable','deadline_elapsed'):
                    # These codes precede the first prompt byte. An empty
                    # native thread is not a confirmed first turn.
                    self.thread = None
                raise
        if self.codex is None or self.thread is None:
            raise Error('not_running', 'Run Codex in this pane first')
        if self.codex.dead:
            raise Error('codex_disconnected', 'Codex exited; recorded work will not be replayed')
        running = self.turn is not None and self.codex.state(self.thread,self.turn).get('status') is None
        if op == 'change-model':
            if running:
                raise Error('turn_active', 'Model may be changed only between turns')
            check()
            self.model = validate_model(packet['model'])
            return dict(model=self.model, applies='next_turn')
        if op == 'steer':
            inputs = text_input(packet['prompt'])
            expected = packet.get('expected_turn')
            if expected is None:
                if running:
                    raise Error('turn_active', 'Provide --turn-id to steer the exact active turn')
                return self.start(inputs,self.enforce_quota(packet,check))
            if not running or self.turn != expected:
                raise Error('turn_changed', 'Expected turn is not active; no follow-up was sent')
            r = self.codex.rpc('turn/steer', {'threadId':self.thread,'expectedTurnId':self.turn,'input':inputs},check)
            if r.get('turnId') != self.turn:
                raise Error('outcome_unknown', 'Steering acknowledgement did not match the active turn')
            return dict(thread_id=self.thread,turn_id=self.turn)
        raise Error('invalid_operation', 'Unknown receiver operation')


class HermesReceiver(Receiver):
    agent = 'hermes'

    def __init__(self, cwd):
        super().__init__(cwd)
        self.acp = None

    def active_backend(self) -> ChildRpc | None:
        return self.acp

    def apply(self, op, packet, check):
        if op in ('quota', 'check-quota', 'recover'):
            raise Error('unsupported_operation', f'{op} is not implemented for Hermes ACP; use the Hermes CLI or UI')
        if self.acp is not None:
            self.acp.require_available(idle=True)
        if self.uncertain:
            raise Error('outcome_unknown', 'A prior Hermes mutation could not be reconciled; no automatic replay')
        if op == 'steer' and packet.get('expected_turn') is not None:
            raise Error('unsupported_operation', 'Hermes ACP supports idle follow-ups, not exact active-turn steering')
        prompt = packet.get('prompt')
        if op in ('run', 'steer'):
            text_input(prompt)
            if prompt.lstrip().startswith('/'):
                raise Error('unsupported_prompt', 'Hermes ACP slash commands are not forwarded as prompts')
        if op == 'run':
            if packet.get('cwd') != self.cwd:
                raise Error('cwd_mismatch', 'Working directory must match the owned pane provisioning directory')
            if self.thread is not None:
                raise Error('already_started', 'Use steer to follow up in the existing Hermes session')
            if packet.get('network'):
                raise Error('unsupported_operation', '--network is Codex-only; Hermes uses its profile tool policy')
            requested = packet.get('model')
            if requested is not None:
                validate_model(requested, hermes=True)
            if self.acp is None:
                check()
                self.acp = HermesAcp(self.cwd, check)
            self.thread, self.model = self.acp.open_session(self.cwd, check)
            if requested is not None and requested != self.model:
                self.set_model(requested, check)
            print('Hermes ACP session ready', flush=True)
        elif self.acp is None or self.thread is None:
            raise Error('not_running', 'Run Hermes in this pane first')
        elif op == 'change-model':
            self.set_model(packet.get('model'), check)
            return dict(model=self.model, applies='next_turn')
        elif op != 'steer':
            raise Error('invalid_operation', 'Unknown receiver operation')
        assert self.acp is not None and isinstance(prompt, str)
        self.turn = self.acp.submit(prompt, check)
        return dict(thread_id=self.thread, turn_id=self.turn, model=self.model, status='submitted')

    def set_model(self, model, check):
        assert self.acp is not None
        model = validate_model(model, hermes=True)
        try:
            self.acp.rpc('session/set_model', {'sessionId':self.thread, 'modelId':model}, check)
        except Error as exc:
            if exc.code in ('outcome_unknown', 'invalid_response', 'hermes_disconnected'):
                self.uncertain = True
            self.acp.require_available()
            raise
        self.acp.require_available()
        self.model = model


@lru_cache(maxsize=1)
def interactive_module():
    try:
        spec = importlib.util.spec_from_file_location('agent_manager_interactive', HERE.with_name('interactive_codex.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (ImportError, OSError) as exc:
        raise Error('interactive_unavailable', 'Interactive Codex requires Python websockets 15') from exc
    return module


def serve(token, cwd, scope, agent='codex', *, model=None, worker=None, launch_profile=None):
    if worker is not None:
        receiver = interactive_module().build(sys.modules[__name__])(cwd, model, launch_profile, worker)
    else:
        receiver = HermesReceiver(cwd) if agent == 'hermes' else Receiver(cwd)
    slots = threading.BoundedSemaphore(8)
    def handle(conn):
        with conn:
            try:
                if os.name != 'nt':
                    verify_peer(conn)
                packet = read_channel(conn, 5)
                raw_deadline = packet.get('deadline')
                if not isinstance(raw_deadline,(float,int)) or isinstance(raw_deadline,bool):
                    raise Error('deadline_elapsed', 'Receiver request had no valid deadline')
                deadline = float(raw_deadline)
                if not time.monotonic() < deadline <= time.monotonic()+90:
                    raise Error('deadline_elapsed', 'Receiver request expired or had no valid deadline')
                def check(write_deadline=None):
                    until = deadline if write_deadline is None else min(deadline,write_deadline)
                    remaining = until-time.monotonic()
                    if remaining <= 0:
                        raise Error('deadline_elapsed', 'Receiver request expired')
                    pane = packet.get('pane')
                    read_only = packet['op'] in ('status', 'result') or (worker is not None and packet['op'] == 'wait')
                    if not isinstance(pane, str) or bound(scope, pane, timeout=min(remaining,40),
                                                         allow_locked=read_only) != token:
                        raise Error('bad_binding', 'This receiver is not bound to the requested pane')
                    if time.monotonic() >= until:
                        raise Error('deadline_elapsed', 'Receiver request expired during verification')
                    return until
                if packet.get('op') != 'hello':
                    check()
                if packet.get('op') in ('wait','steer','steer-bound','steer-admitted','steer-bound-admitted') and (type(packet.get('seconds')) is not int or not 1 <= packet['seconds'] <= 600):
                    raise Error('invalid_wait', 'Wait must be 1 through 600 seconds')
                if worker is None and packet.get('op') in ('status', 'result'):
                    raise Error('unsupported_operation', 'Exact managed-worker inspection requires an interactive receiver')
                result = {'ok':True,'result':receiver.dispatch(packet,check)}
            except Error as e:
                result = {'ok':False,'error':{'code':e.code,'message':str(e)}}
            except (ValueError, OSError, AttributeError, KeyError, TypeError):
                result = {'ok':False,'error':{'code':'protocol_error','message':'Request failed verification; do not blindly retry'}}
            try:
                send_channel(conn, frame(result), 5)
            except (OSError, Error):
                pass
            finally:
                slots.release()
    with listen_channel(token) as accept:
        if worker is not None:
            pane = os.environ.get('HERDR_PANE_ID')
            if not pane:
                raise Error('bad_binding', 'Interactive workers must start inside a Herdr pane')
            guard(*scope, pane, allow_locked=True)
            save_binding(scope, pane, token, agent)
        print('Hermes Herdr Agent Manager ready', flush=True)
        while True:
            conn = accept()
            if not slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=handle,args=(conn,),daemon=True).start()


def request(token, op, **fields):
    sent = False
    try:
        with connect_channel(token) as conn:
            deadline = time.monotonic()+(5 if op=='hello' else 65)
            wire = frame(dict(op=op,deadline=deadline,**fields))
            sent = True
            send_channel(conn, wire, 5)
            result = read_channel(conn, fields.get('seconds',0)+70)
    except (OSError, Error) as exc:
        raise Error('outcome_unknown' if sent else 'receiver_unavailable',
                    'No verified receiver reply; inspect state before retrying') from exc
    return receiver_result(result)


def receiver_result(result):
    if result.get('ok') is True and 'error' not in result and isinstance(result.get('result'),dict):
        return result['result']
    err = result.get('error')
    if result.get('ok') is False and 'result' not in result and isinstance(err,dict):
        code,message = err.get('code'),err.get('message')
        if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}',code) and isinstance(message,str):
            raise Error(code,message)
    raise Error('outcome_unknown','Malformed receiver receipt; inspect state before retrying')


def save_binding(scope, pane, token, agent):
    record = {'token':token,'scope':[*scope,pane],'agent':agent}
    directory = state_dir()
    target = directory / (binding_key(*scope,pane) + '.json')
    pending = directory / (token + '.tmp')
    created = False
    try:
        if os.name == 'nt':
            file = windows_io().create_state_file(pending)
        else:
            file = os.fdopen(os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8')
        created = True
        with file as stream:
            json.dump(record,stream)
        os.replace(pending,target)
    except OSError as exc:
        raise Error('partial_provision', f'Pane {pane} exists but its binding was not saved', pane_id=pane) from exc
    finally:
        if created:
            with suppress(OSError):
                pending.unlink(missing_ok=True)


def provision(scope: tuple[str, str], cwd, group, suffix, agent='codex', *,
              status=None, model=None, worker=None, launch_profile=None):
    token = uuid.uuid4().hex + uuid.uuid4().hex
    cwd = cwd_path(cwd)
    state_dir()
    launch = [sys.executable,'-I','-B',str(HERE),'_serve',token,cwd,*scope,str(home())]
    if worker is not None:
        module = interactive_module()
        module.preflight(sys.modules[__name__], cwd, model, launch_profile)
        import websockets
        package_root = str(Path(websockets.__file__).resolve().parents[1])
        launch = [sys.executable, '-I', '-B', '-c',
                  f'import runpy,sys;sys.path.append({package_root!r});'
                  f'runpy.run_path({str(HERE)!r},run_name="__main__")',
                  '_serve', token, cwd, *scope, str(home()), 'codex',
                  '--worker', worker, '--model', model]
        if launch_profile:
            launch.extend(['--launch-profile', launch_profile])
    elif agent != 'codex':
        launch.append(agent)
    label_args = ['--status', status] if status else ['--suffix', suffix]
    result = pane_call('create',*scope,'--cwd',cwd,'--group',group,*label_args,'--argv-stdin',
                       data=json.dumps(launch))
    pane = result.get('pane_id')
    if not isinstance(pane,str) or not pane:
        raise Error('outcome_unknown','Pane manager did not identify the provisioned pane')
    until = time.monotonic() + 10
    while True:
        try:
            hello = request(token,'hello')
            expected = {'receiver':True,'cwd':cwd}
            if worker is not None:
                expected.update(worker=worker, interactive=True)
            elif agent != 'codex':
                expected['agent'] = agent
            if hello != expected:
                raise Error('bad_binding','Receiver identity did not match')
            break
        except Error as exc:
            if exc.code != 'receiver_unavailable' or time.monotonic() >= until:
                raise Error('partial_provision', f'Pane {pane} exists but its receiver was not verified', pane_id=pane) from exc
            time.sleep(.1)
    if worker is not None:
        until = time.monotonic() + 20
        while time.monotonic() < until:
            try:
                if bound(scope, pane, allow_locked=True) != token:
                    raise Error('bad_binding', 'The interactive receiver did not bind the created pane')
                snapshot = request(token, 'status', pane=pane)
                if snapshot.get('thread_id') and snapshot.get('state') == 'idle':
                    return dict(snapshot, pane_id=pane, status='ready')
            except Error:
                pass
            time.sleep(.2)
        raise Error('partial_provision', 'Pane exists but its Codex TUI attachment was not verified', pane_id=pane)
    save_binding(scope, pane, token, agent)
    return {'pane_id':pane,'cwd':cwd,'agent':agent,'status':'ready'}


def verified_binding(scope: tuple[str, str], pane, timeout: float=40, *, agent=None, allow_locked=False):
    target = binding_file(*scope,pane)
    try:
        if os.name == 'nt':
            file = windows_io().read_state_file(target)
        else:
            file = os.fdopen(os.open(target, os.O_RDONLY | os.O_NOFOLLOW), 'r', encoding='utf-8')
        with file as stream:
            if os.name != 'nt':
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise Error('bad_binding','Agent-manager binding is not private')
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise Error('unowned_pane','Provision a pane using this skill; arbitrary shells and agents cannot be adopted') from exc
    if not isinstance(value,dict) or value.get('scope') != [*scope,pane] or not isinstance(value.get('token'),str):
        raise Error('bad_binding','Cannot verify this pane binding')
    # Before agent selection existed all receipts referred exclusively to Codex.
    owner = value.get('agent','codex')
    if owner not in AGENTS:
        raise Error('bad_binding','Cannot verify this pane agent')
    if agent is not None and owner != agent:
        raise Error('agent_mismatch', f'This pane was not provisioned for {agent}')
    guard(*scope,pane,timeout=timeout,allow_locked=allow_locked)
    return value['token'], owner


def bound(scope: tuple[str, str], pane, timeout: float=40, *, agent=None, allow_locked=False):
    return verified_binding(scope,pane,timeout,agent=agent,allow_locked=allow_locked)[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='op',required=True)
    for op in ('provision','run','steer','change-model','get-remaining-tokens','check-quota','wait','recover',
               'status','result','preflight','account-quota'):
        p = commands.add_parser(op,allow_abbrev=False)
        if op in ('preflight','account-quota'):
            p.add_argument('--launch-profile')
            if op == 'preflight':
                p.add_argument('--cwd', required=True)
                p.add_argument('--model', required=True)
                p.add_argument('--agent', required=True, choices=AGENTS)
            continue
        p.add_argument('--instance',required=True)
        p.add_argument('--workspace',required=True)
        if op != 'provision':
            p.add_argument('--pane',required=True)
        if op in ('provision','run'):
            p.add_argument('--cwd',required=True)
        if op in ('provision','run','get-remaining-tokens'):
            p.add_argument('--agent',choices=AGENTS,required=op != 'provision',default='codex')
        if op == 'check-quota':
            p.add_argument('--agent',choices=('codex',),default='codex')
        if op == 'provision':
            p.add_argument('--group',default='agents')
            p.add_argument('--suffix')
            p.add_argument('--status')
            p.add_argument('--worker', help='Provision a managed interactive Codex worker')
            p.add_argument('--launch-profile')
        if op in ('run','change-model','provision'):
            p.add_argument('--model',required=op == 'change-model',help='Required for Codex run; Hermes can use the profile model')
        if op == 'run':
            p.add_argument('--network',action='store_true',help='Only for explicitly authorized Codex sandbox networking')
        if op in ('run','steer'):
            p.add_argument('--override-quota',action='store_true',help='Skip local pacing for this new Codex turn only')
            prompt = p.add_mutually_exclusive_group(required=True)
            prompt.add_argument('--prompt')
            prompt.add_argument('--prompt-stdin',action='store_true')
            prompt.add_argument('--request-stdin', action='store_true', help='Read JSON prompt and run parameters')
        if op in ('steer','wait'):
            p.add_argument('--seconds',type=int,choices=range(1,601),metavar='1-600',default=120)
        if op in ('steer','wait'):
            p.add_argument('--turn-id',required=(op=='wait'))
        if op=='steer':
            p.add_argument('--thread-id',help='Require this native thread for an idle follow-up')
            p.add_argument('--after-turn-id',help='Also require this last turn; use with --thread-id')
            p.add_argument('--return-admission', action='store_true')
        if op == 'result':
            p.add_argument('--turn-id', required=True)
    internal=commands.add_parser('_serve',allow_abbrev=False)
    internal.add_argument('token')
    internal.add_argument('cwd')
    internal.add_argument('instance')
    internal.add_argument('workspace')
    internal.add_argument('profile_home')
    internal.add_argument('agent',nargs='?',default='codex',choices=AGENTS)
    internal.add_argument('--worker')
    internal.add_argument('--model')
    internal.add_argument('--launch-profile')
    a=parser.parse_args()
    if a.op == 'run' and a.agent == 'codex' and a.model is None:
        parser.error('run --agent codex requires --model')
    if (a.op == 'run' and a.agent != 'codex' and a.override_quota or
            a.op == 'steer' and a.turn_id is not None and a.override_quota):
        parser.error('--override-quota is only for a new Codex turn')
    if a.op == 'steer' and ((a.after_turn_id is not None and a.thread_id is None) or
                            (a.thread_id is not None and a.turn_id is not None)):
        parser.error('--after-turn-id requires --thread-id; --turn-id cannot be used with --thread-id')
    try:
        if os.name != 'nt' and not sys.platform.startswith('linux'):
            raise Error('unsupported_platform','The agent-manager receiver requires Windows or Linux')
        if a.op == '_serve':
            os.environ['HERMES_HOME'] = a.profile_home
            pane_script()
            serve(a.token,cwd_path(a.cwd),(a.instance,a.workspace),a.agent, model=a.model,
                  worker=a.worker, launch_profile=a.launch_profile)
            return 0
        scope=(getattr(a, 'instance', None),getattr(a, 'workspace', None))
        if a.op == 'preflight':
            if a.agent != 'codex':
                raise Error('interactive_unavailable', 'This backend has no managed interactive receiver')
            result=interactive_module().preflight(sys.modules[__name__],a.cwd,a.model,a.launch_profile)
        elif a.op == 'account-quota':
            module=interactive_module()
            module.validate_profile(sys.modules[__name__],a.launch_profile)
            probe=Codex(str(Path.home()), launch_profile=a.launch_profile)
            try:
                result=module.read_quota(sys.modules[__name__], probe)
            finally:
                probe.stop_unassigned()
        elif a.op == 'provision':
            suffix = a.suffix if a.suffix is not None else {'codex':'Codex','hermes':'Hermes'}[a.agent]
            if a.worker and (a.agent != 'codex' or not a.model):
                raise Error('invalid_argument', 'A managed interactive worker requires Codex and a model')
            if not a.worker and (a.launch_profile or a.model):
                raise Error('invalid_argument', 'Use --worker with a launch profile or model')
            result=provision(scope,a.cwd,a.group,suffix,a.agent,status=a.status,model=a.model,
                             worker=a.worker,launch_profile=a.launch_profile)
        else:
            token, owner=verified_binding(scope,a.pane,agent=getattr(a,'agent',None),
                                          allow_locked=a.op in ('status','result'))
            fields={'pane':a.pane}
            if a.op in ('run','get-remaining-tokens'):
                fields['agent']=a.agent
            if a.op in ('run','steer'):
                if a.request_stdin:
                    body = read_frame(sys.stdin.buffer)
                    if not isinstance(body.get('parameters',{}),dict):
                        raise Error('invalid_parameters','Run parameters must be an object')
                    fields.update(prompt=body.get('prompt'),parameters=body.get('parameters',{}),
                                  account=body.get('account'))
                else:
                    fields['prompt']=sys.stdin.buffer.read(MAX_PROMPT+1).decode() if a.prompt_stdin else a.prompt
                text_input(fields['prompt'])
                if owner=='codex':
                    fields['override_quota']=a.override_quota
                elif a.override_quota:
                    raise Error('invalid_quota_override','Quota override is only for a new Codex turn')
            if a.op == 'change-model':
                fields['model']=validate_model(a.model, hermes=True)
            if a.op == 'run' and (a.agent == 'codex' or a.model is not None):
                fields['model']=validate_model(a.model, hermes=a.agent == 'hermes')
            if a.op == 'run':
                fields.update(cwd=cwd_path(a.cwd),network=a.network)
            if a.op in ('steer','wait'):
                fields['seconds']=a.seconds
            if a.op in ('wait','result'):
                fields['turn_id']=a.turn_id
            if a.op == 'steer':
                fields['expected_turn']=a.turn_id
                fields['return_admission']=a.return_admission
            op='quota' if a.op=='get-remaining-tokens' else a.op
            if a.op=='steer' and a.thread_id is not None:
                fields.update(expected_thread=a.thread_id,after_turn=a.after_turn_id)
                op='steer-bound'
            if owner=='codex' and (a.op=='run' or (a.op=='steer' and a.turn_id is None)):
                op+='-admitted'
            result=request(token,op,**fields)
        print(json.dumps(dict(ok=True,result=result)))
        return 0
    except (Error, OSError, ValueError) as e:
        failure = dict(code=e.code,message=str(e),**e.details) if isinstance(e,Error) else dict(code='local_error',message='Local file, state, or input failed verification')
        print(json.dumps(dict(ok=False,error=failure)))
        return 1


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    sys.exit(main())
