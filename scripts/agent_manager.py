#!/usr/bin/env python3
"""Run a Codex app-server in a pane owned by the agent manager."""
import argparse
from contextlib import contextmanager, ExitStack, suppress
from datetime import datetime, timezone
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
MAX_FRAME = 1024 * 1024
MAX_PROMPT = 32768
HERE = Path(__file__).resolve()


class Error(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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
        raise Error(str(failure.get('code', 'pane_manager_rejected')), str(failure.get('message', 'Pane call failed')))
    return out['result']


def guard(instance, workspace, pane, timeout: float=40):
    result = pane_call('lock-status', instance, workspace, '--pane', pane, timeout=timeout)
    if result.get('pane_id') != pane or type(result.get('user_locked')) is not bool:
        raise Error('lock_unknown', 'Cannot verify the exact pane and its lock')
    if result['user_locked']:
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


def validate_model(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/-]{0,127}', name):
        raise Error('invalid_model', 'Supply an explicit Codex model ID')
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


def quota(response):
    rates = (response.get('rateLimitsByLimitId') or {}).get('codex') or response.get('rateLimits')
    if not isinstance(rates, dict):
        raise Error('quota_unavailable', 'Codex did not report rate limits for this account')
    def window(key):
        val = rates.get(key)
        if val is None:
            return None
        try:
            used = val['usedPercent']
            mins = val.get('windowDurationMins')
            reset = val.get('resetsAt')
            if type(used) not in (int,float) or not 0 <= used <= 100:
                raise ValueError
            if mins is not None and (type(mins) is not int or mins <= 0):
                raise ValueError
            if reset is not None and type(reset) is not int:
                raise ValueError
            utc = None if reset is None else datetime.fromtimestamp(reset, timezone.utc).isoformat().replace('+00:00','Z')
        except (KeyError, TypeError, ValueError, OverflowError, OSError, AttributeError) as exc:
            raise Error('quota_unavailable', 'Codex returned an invalid rate window') from exc
        return {'remaining_percent':100-used, 'window_minutes':mins, 'reset_at':utc}
    return {'source':'codex_account_rate_limits', 'primary':window('primary'), 'secondary':window('secondary')}


def codex_binary():
    if os.name == 'nt':
        # Do not load a project-local executable or invoke npm's batch shim.
        for entry in os.get_exec_path():
            directory = Path(entry.strip('"'))
            if directory.is_absolute() and (directory/'codex.exe').is_file():
                return str(directory/'codex.exe')
        return None
    return shutil.which('codex')


class Codex:
    def __init__(self, cwd, check=None):
        binary = codex_binary()
        if not binary:
            raise Error('codex_missing', 'Install and authenticate Codex before using this skill')
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
            command = [str(Path(binary).resolve()), '-c', 'features.fast_mode=false',
                       'app-server', '--listen', 'stdio://']
            if os.name == 'nt':
                child_read, self.windows_stdin = self.io_stack.enter_context(windows_io().child_stdin())
                self.proc = subprocess.Popen(command, cwd=cwd, stdin=child_read, stdout=subprocess.PIPE)
                child_read.close()  # The subprocess owns its duplicate.
            else:
                self.proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                assert self.proc.stdin is not None
                os.set_blocking(self.proc.stdin.fileno(), False)
            threading.Thread(target=self.listen, daemon=True).start()
            self.rpc('initialize', {'clientInfo':{'name':'hermes_herdr_agent_manager','title':'Hermes Herdr Agent Manager','version':'0.1.0'}},check)
            self.write({'method':'initialized'},check)
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

    def stop_unassigned(self):
        if not self.exited():
            self.proc.terminate()
        self.dispose()

    def write(self, msg, check=None, deadline=None):
        deadline = time.monotonic()+15 if deadline is None else deadline
        data = memoryview(frame(msg))
        if not self.write_lock.acquire(timeout=max(0,deadline-time.monotonic())):
            raise Error('deadline_elapsed','Timed out before writing to Codex')
        started = False
        try:
            if self.write_poisoned:
                raise Error('outcome_unknown','A prior frame was partially written; do not send more input')
            try:
                win_pipe = getattr(self, 'windows_stdin', None)
                while data:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        code = 'outcome_unknown' if started else 'deadline_elapsed'
                        raise Error(code,'Timed out writing to Codex; inspect the prior turn before retrying')
                    try:
                        if not started and check is not None:
                            request_deadline = check(deadline)
                            if request_deadline is not None:
                                deadline = min(deadline,request_deadline)
                            if time.monotonic() >= deadline:
                                raise Error('deadline_elapsed','Lock verification finished after the write deadline')
                        if win_pipe is not None:
                            started = True  # An overlapped write can have partially reached Codex.
                            size = win_pipe.write(data, deadline)
                        else:
                            assert self.proc.stdin is not None
                            size = os.write(self.proc.stdin.fileno(), data)
                    except BlockingIOError:
                        assert self.proc.stdin is not None
                        select.select([],[self.proc.stdin.fileno()],[],max(0,deadline-time.monotonic()))
                    else:
                        started = True
                        if size <= 0:
                            raise Error('outcome_unknown', 'Codex stdin made no progress; do not replay')
                        data = data[size:]
            except TimeoutError as exc:
                raise Error('outcome_unknown', 'Timed out writing to Codex; inspect the prior turn before retrying') from exc
            except OSError as exc:
                raise Error('codex_disconnected', 'Codex disconnected; reconcile before any retry') from exc
        except BaseException:
            if started and data:
                self.write_poisoned = True
            raise
        finally:
            self.write_lock.release()

    def listen(self):
        try:
            assert self.proc.stdout is not None
            while True:
                event = read_frame(self.proc.stdout)
                if 'method' in event and 'id' in event:
                    # Never approve server-initiated requests: human approval is not proxied here.
                    self.write({'id':event['id'],'error':{'code':-32000,'message':'No permission was granted by the operator'}})
                    continue
                with self.condition:
                    if 'id' in event:
                        ident = event['id']
                        if type(ident) is int and ident in self.replies:
                            self.replies[ident] = event
                    else:
                        self.capture(event)
                    self.condition.notify_all()
        except (Error, OSError, ValueError):
            pass
        finally:
            with self.condition:
                self.dead = True
                self.condition.notify_all()

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

    def rpc(self, method, params, check=None):
        deadline = time.monotonic()+15
        with self.condition:
            if self.dead:
                raise Error('codex_disconnected', 'Codex is no longer connected; no work will be replayed')
            self.sequence += 1
            ident = self.sequence
            self.replies[ident] = None
        try:
            self.write({'id':ident,'method':method,'params':params},check,deadline)
            with self.condition:
                self.condition.wait_for(lambda: self.replies[ident] is not None or self.dead, timeout=max(0,deadline-time.monotonic()))
                data = self.replies[ident]
            if data is None:
                raise Error('outcome_unknown', f'No verified {method} result; do not repeat the request blindly')
            if 'error' in data:
                failure = data['error']
                if 'result' in data or not isinstance(failure,dict) or type(failure.get('code')) is not int or not isinstance(failure.get('message'),str):
                    raise Error('invalid_response', f'Ambiguous error envelope from {method}')
                raise Error('codex_rejected', f'Codex refused {method}')
            if not isinstance(data.get('result'), dict):
                raise Error('invalid_response', f'Unexpected result from {method}')
            return data['result']
        finally:
            with self.condition:
                self.replies.pop(ident, None)

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


class Receiver:
    def __init__(self, cwd):
        self.cwd = cwd
        self.codex = None
        self.thread = None
        self.turn = None
        self.model = None
        self.network = False
        self.uncertain = False
        self.mutation = threading.Lock()

    def backend(self, check):
        if self.codex is None:
            for attempt in range(2):
                try:
                    # Startup only: no thread or prompt has been sent by this client yet.
                    check()
                    self.codex = Codex(self.cwd,check)
                    break
                except Error as exc:
                    if attempt or exc.code not in ('codex_disconnected','outcome_unknown'):
                        raise
        return self.codex

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
            return {'receiver':True, 'cwd':self.cwd}
        if op == 'wait':
            with self.mutation:
                backend, thread, turn = self.codex, self.thread, self.turn
            if backend is None or not turn or packet.get('turn_id') != turn:
                raise Error('unknown_turn', 'Only the latest confirmed turn in this receiver can be read')
            return backend.wait(thread, turn, packet['seconds'])
        if not self.mutation.acquire(blocking=False):
            raise Error('receiver_busy', 'Another operation is in progress; inspect before retrying')
        try:
            reply = self.apply(op, packet, check)
            backend = self.codex
        except Error as exc:
            if op in ('run','steer') and exc.code in ('outcome_unknown','invalid_response','codex_disconnected'):
                self.uncertain = True
            raise
        finally:
            self.mutation.release()
        if op == 'steer':
            assert backend is not None
            return backend.wait(reply['thread_id'],reply['turn_id'],packet['seconds'])
        return reply

    def apply(self, op, packet, check):
        if op == 'quota':
            backend = self.backend(check)
            assert backend is not None
            return quota(backend.rpc('account/rateLimits/read', {},check))
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
            result = self.codex.rpc('thread/start', self.thread_options(),check)
            thread = result.get('thread')
            self.thread = thread.get('id') if isinstance(thread,dict) else None
            if not isinstance(self.thread,str) or not self.thread:
                raise Error('outcome_unknown', 'Codex did not identify the new thread')
            print('Codex thread ready', flush=True)
            return self.start(inputs,check)
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
                return self.start(inputs,check)
            if not running or self.turn != expected:
                raise Error('turn_changed', 'Expected turn is not active; no follow-up was sent')
            r = self.codex.rpc('turn/steer', {'threadId':self.thread,'expectedTurnId':self.turn,'input':inputs},check)
            if r.get('turnId') != self.turn:
                raise Error('outcome_unknown', 'Steering acknowledgement did not match the active turn')
            return dict(thread_id=self.thread,turn_id=self.turn)
        raise Error('invalid_operation', 'Unknown receiver operation')


def serve(token, cwd, scope):
    receiver = Receiver(cwd)
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
                    if not isinstance(pane, str) or bound(scope, pane, timeout=min(remaining,40)) != token:
                        raise Error('bad_binding', 'This receiver is not bound to the requested pane')
                    if time.monotonic() >= until:
                        raise Error('deadline_elapsed', 'Receiver request expired during verification')
                    return until
                if packet.get('op') != 'hello':
                    check()
                if packet.get('op') in ('wait','steer') and (type(packet.get('seconds')) is not int or not 1 <= packet['seconds'] <= 600):
                    raise Error('invalid_wait', 'Wait must be 1 through 600 seconds')
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


def provision(scope: tuple[str, str], cwd, group, suffix):
    token = uuid.uuid4().hex + uuid.uuid4().hex
    cwd = cwd_path(cwd)
    cwd_state = state_dir()
    result = pane_call('create',*scope,'--cwd',cwd,'--group',group,'--suffix',suffix,'--argv-stdin',
                       data=json.dumps([sys.executable,'-I','-B',str(HERE),'_serve',token,cwd,*scope,str(home())]))
    pane = result.get('pane_id')
    if not isinstance(pane,str) or not pane:
        raise Error('outcome_unknown','Pane manager did not identify the provisioned pane')
    until = time.monotonic() + 10
    while True:
        try:
            hello = request(token,'hello')
            if hello != {'receiver':True,'cwd':cwd}:
                raise Error('bad_binding','Receiver identity did not match')
            break
        except Error as exc:
            if exc.code != 'receiver_unavailable' or time.monotonic() >= until:
                raise Error('partial_provision', f'Pane {pane} exists; receiver was not verified. Do not create a duplicate blindly') from exc
            time.sleep(.1)
    record = {'token':token,'scope':[*scope,pane]}
    target = cwd_state / (binding_key(*scope,pane) + '.json')
    pending = cwd_state / (token + '.tmp')
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
        raise Error('partial_provision', f'Pane {pane} exists; binding was not saved. Do not create a duplicate blindly') from exc
    finally:
        if created:
            with suppress(OSError):
                pending.unlink(missing_ok=True)
    return {'pane_id':pane,'cwd':cwd,'status':'ready'}


def bound(scope: tuple[str, str], pane, timeout: float=40):
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
    guard(*scope,pane,timeout=timeout)
    return value['token']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='op',required=True)
    for op in ('provision','run','steer','change-model','get-remaining-tokens','wait','recover'):
        p = commands.add_parser(op,allow_abbrev=False)
        p.add_argument('--instance',required=True)
        p.add_argument('--workspace',required=True)
        if op != 'provision':
            p.add_argument('--pane',required=True)
        if op in ('provision','run'):
            p.add_argument('--cwd',required=True)
        if op in ('run','get-remaining-tokens'):
            p.add_argument('--agent',required=True,choices=('codex',))
        if op == 'provision':
            p.add_argument('--group',default='agents')
            p.add_argument('--suffix',default='Codex')
        if op in ('run','change-model'):
            p.add_argument('--model',required=True)
        if op == 'run':
            p.add_argument('--network',action='store_true',help='Only for explicitly authorized Codex sandbox networking')
        if op in ('run','steer'):
            prompt = p.add_mutually_exclusive_group(required=True)
            prompt.add_argument('--prompt')
            prompt.add_argument('--prompt-stdin',action='store_true')
        if op in ('steer','wait'):
            p.add_argument('--seconds',type=int,choices=range(1,601),metavar='1-600',default=120)
        if op in ('steer','wait'):
            p.add_argument('--turn-id',required=(op=='wait'))
    internal=commands.add_parser('_serve',allow_abbrev=False)
    internal.add_argument('token')
    internal.add_argument('cwd')
    internal.add_argument('instance')
    internal.add_argument('workspace')
    internal.add_argument('profile_home')
    a=parser.parse_args()
    try:
        if os.name != 'nt' and not sys.platform.startswith('linux'):
            raise Error('unsupported_platform','The agent-manager receiver requires Windows or Linux')
        if a.op == '_serve':
            os.environ['HERMES_HOME'] = a.profile_home
            pane_script()
            serve(a.token,cwd_path(a.cwd),(a.instance,a.workspace))
            return 0
        pane_script()
        scope=(a.instance,a.workspace)
        if a.op == 'provision':
            result=provision(scope,a.cwd,a.group,a.suffix)
        else:
            token=bound(scope,a.pane)
            fields={'pane':a.pane}
            if a.op in ('run','steer'):
                fields['prompt']=sys.stdin.buffer.read(MAX_PROMPT+1).decode() if a.prompt_stdin else a.prompt
                text_input(fields['prompt'])
            if a.op in ('run','change-model'):
                fields['model']=validate_model(a.model)
            if a.op == 'run':
                fields.update(cwd=cwd_path(a.cwd),network=a.network)
            if a.op in ('steer','wait'):
                fields['seconds']=a.seconds
            if a.op == 'wait':
                fields['turn_id']=a.turn_id
            if a.op == 'steer':
                fields['expected_turn']=a.turn_id
            result=request(token,'quota' if a.op=='get-remaining-tokens' else a.op,**fields)
        print(json.dumps(dict(ok=True,result=result)))
        return 0
    except (Error, OSError, ValueError) as e:
        failure = dict(code=e.code,message=str(e)) if isinstance(e,Error) else dict(code='local_error',message='Local file, state, or input failed verification')
        print(json.dumps(dict(ok=False,error=failure)))
        return 1


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    sys.exit(main())
