"""Same-user Windows named pipes and protected binding storage, without dependencies."""
import _winapi as win
from contextlib import contextmanager, suppress
import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
import stat
import time
import uuid

KERNEL = c.WinDLL('kernel32', use_last_error=True)
SECURITY = c.WinDLL('advapi32', use_last_error=True)
DWORD_P = c.POINTER(w.DWORD)
VOID_P = c.POINTER(c.c_void_p)


def bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, arguments
    return function


class Attributes(c.Structure):
    _fields_ = [('length', w.DWORD), ('descriptor', c.c_void_p), ('inherit', w.BOOL)]


class Acl(c.Structure):
    _fields_ = [('revision', c.c_ubyte), ('reserved', c.c_ubyte), ('size', w.WORD),
                ('count', w.WORD), ('reserved2', w.WORD)]


class FileAttributeTag(c.Structure):
    _fields_ = [('attributes', w.DWORD), ('reparse_tag', w.DWORD)]


local_free = bind(KERNEL, 'LocalFree', w.HLOCAL, w.HLOCAL)
current_process = bind(KERNEL, 'GetCurrentProcess', w.HANDLE)
open_token = bind(SECURITY, 'OpenProcessToken', w.BOOL, w.HANDLE, w.DWORD, c.POINTER(w.HANDLE))
token_info = bind(SECURITY, 'GetTokenInformation', w.BOOL, w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, DWORD_P)
sid_string = bind(SECURITY, 'ConvertSidToStringSidW', w.BOOL, c.c_void_p, c.POINTER(w.LPWSTR))
make_descriptor = bind(SECURITY, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', w.BOOL,
                       w.LPCWSTR, w.DWORD, VOID_P, DWORD_P)
get_security = bind(SECURITY, 'GetSecurityInfo', w.DWORD, w.HANDLE, w.DWORD, w.DWORD,
                    VOID_P, VOID_P, VOID_P, VOID_P, VOID_P)
get_ace = bind(SECURITY, 'GetAce', w.BOOL, c.c_void_p, w.DWORD, VOID_P)
is_valid_sid = bind(SECURITY, 'IsValidSid', w.BOOL, c.c_void_p)
pipe_client_pid = bind(KERNEL, 'GetNamedPipeClientProcessId', w.BOOL, w.HANDLE, DWORD_P)
create_directory = bind(KERNEL, 'CreateDirectoryW', w.BOOL, w.LPCWSTR, c.POINTER(Attributes))
create_file = bind(KERNEL, 'CreateFileW', w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD,
                   c.POINTER(Attributes), w.DWORD, w.DWORD, w.HANDLE)
file_information = bind(KERNEL, 'GetFileInformationByHandleEx', w.BOOL, w.HANDLE,
                        c.c_int, c.c_void_p, w.DWORD)
create_pipe = bind(KERNEL, 'CreateNamedPipeW', w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                   w.DWORD, w.DWORD, w.DWORD, c.POINTER(Attributes))

PIPE_REJECT_REMOTE_CLIENTS = 8
PIPE_ACCESS_OUTBOUND = 2
SECURITY_IDENTIFICATION = 0x00110000  # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION
OWNER_DACL = 5  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
FILE_OBJECT = 1
TOKEN_QUERY = 8
TOKEN_USER = 1
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_TAG_INFO = 9  # FILE_INFO_BY_HANDLE_CLASS
READ_CONTROL = 0x00020000
CREATE_NEW = 1
ACCESS_ALLOWED_ACE_TYPE = 0
SID_OFFSET = 8  # ACE_HEADER plus ACCESS_MASK in an ACCESS_ALLOWED_ACE.
ERROR_IO_INCOMPLETE = 996
INVALID_HANDLE = c.c_void_p(-1).value


def sid_text(sid):
    text = w.LPWSTR()
    if not sid or not sid_string(sid, c.byref(text)):
        raise c.WinError(c.get_last_error())
    try:
        return text.value
    finally:
        local_free(c.cast(text, c.c_void_p))


def user_sid():
    token = w.HANDLE()
    if not open_token(current_process(), TOKEN_QUERY, c.byref(token)):
        raise c.WinError(c.get_last_error())
    try:
        size = w.DWORD()
        token_info(token, TOKEN_USER, None, 0, c.byref(size))
        if not size.value:
            raise c.WinError(c.get_last_error())
        info = c.create_string_buffer(size.value)
        if not token_info(token, TOKEN_USER, info, size, c.byref(size)):
            raise c.WinError(c.get_last_error())
        return sid_text(c.cast(info, VOID_P)[0])
    finally:
        if token.value is not None:
            win.CloseHandle(token.value)


SID = user_sid()


@contextmanager
def only_user(*, directory=False):
    # Protect each object explicitly; an existing directory need not inherit its ACEs.
    inheritance = 'OICI' if directory else ''
    descriptor = c.c_void_p()
    if not make_descriptor(f'O:{SID}D:P(A;{inheritance};FA;;;{SID})', 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    try:
        yield Attributes(c.sizeof(Attributes), descriptor, False)
    finally:
        local_free(descriptor)


def verify_handle(handle):
    owner, acl, descriptor = c.c_void_p(), c.c_void_p(), c.c_void_p()
    error = get_security(handle, FILE_OBJECT, OWNER_DACL, c.byref(owner), None, c.byref(acl), None,
                         c.byref(descriptor))
    try:
        if error:
            raise c.WinError(error)
        if sid_text(owner) != SID or not acl.value:
            raise PermissionError('Expected a private Windows object owned by the current user')
        count = c.cast(acl, c.POINTER(Acl)).contents.count
        for index in range(count):
            ace = c.c_void_p()
            if not get_ace(acl, index, c.byref(ace)):
                raise c.WinError(c.get_last_error())
            address = ace.value
            if (address is None or c.c_ubyte.from_address(address).value != ACCESS_ALLOWED_ACE_TYPE
                    or c.c_ushort.from_address(address + 2).value < SID_OFFSET + 8):
                raise PermissionError('Unexpected Windows access-control entry')
            sid = address + SID_OFFSET
            if not is_valid_sid(sid) or sid_text(sid) != SID:
                raise PermissionError('Windows object exposes access to another account')
    finally:
        if descriptor.value:
            local_free(descriptor)


def verify_state_handle(handle, *, directory=False):
    tag = FileAttributeTag()
    if not file_information(handle, FILE_ATTRIBUTE_TAG_INFO, c.byref(tag), c.sizeof(tag)):
        raise c.WinError(c.get_last_error())
    if tag.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PermissionError('State path must not be a Windows reparse point')
    if bool(tag.attributes & stat.FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise PermissionError('State path has the wrong file type')
    verify_handle(handle)


def private_directory(path):
    with only_user(directory=True) as flags:
        if not create_directory(str(path), c.byref(flags)):
            error = c.get_last_error()
            if error != win.ERROR_ALREADY_EXISTS:
                raise c.WinError(error)
    handle = win.CreateFile(str(path), win.GENERIC_READ, 7, 0, win.OPEN_EXISTING,
                            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, 0)
    try:
        verify_state_handle(handle, directory=True)
    finally:
        win.CloseHandle(handle)
    return path


def state_stream(handle, *, write=False):
    try:
        verify_state_handle(handle)
        mode = os.O_WRONLY if write else os.O_RDONLY
        fd = msvcrt.open_osfhandle(handle, mode | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        win.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, 'w' if write else 'r', encoding='utf-8')
    except BaseException:
        os.close(fd)
        raise


def create_state_file(path):
    with only_user() as security:
        handle = create_file(str(path), win.GENERIC_WRITE | READ_CONTROL, 0, c.byref(security),
                             CREATE_NEW, FILE_FLAG_OPEN_REPARSE_POINT, None)
        if handle == INVALID_HANDLE:
            raise c.WinError(c.get_last_error())
    try:
        return state_stream(handle, write=True)
    except BaseException:
        with suppress(OSError):
            path.unlink(missing_ok=True)
        raise


def read_state_file(path):
    handle = win.CreateFile(str(path), win.GENERIC_READ, 1, 0, win.OPEN_EXISTING,
                            FILE_FLAG_OPEN_REPARSE_POINT, 0)
    return state_stream(handle)


def pipe_name(suffix):
    return '\\\\.\\pipe\\hermes-herdr-agent-manager-' + suffix


def listener_handle(name, *, outbound=False, first=False):
    access = PIPE_ACCESS_OUTBOUND if outbound else win.PIPE_ACCESS_DUPLEX
    flags = access | win.FILE_FLAG_OVERLAPPED
    if first:
        flags |= win.FILE_FLAG_FIRST_PIPE_INSTANCE
    with only_user() as security:
        handle = create_pipe(name, flags, PIPE_REJECT_REMOTE_CLIENTS, 1 if outbound else win.PIPE_UNLIMITED_INSTANCES,
                             65536, 65536, 0, c.byref(security))
        if handle == INVALID_HANDLE:
            raise c.WinError(c.get_last_error())
    return handle


def finish(pending, deadline):
    try:
        if deadline is None:
            timeout = win.INFINITE
        else:
            timeout = max(0, int((deadline - time.monotonic()) * 1000))
        if win.WaitForSingleObject(pending.event, timeout) != win.WAIT_OBJECT_0:
            raise TimeoutError('Windows pipe I/O deadline elapsed')
        count, error = pending.GetOverlappedResult(False)
        if error:
            raise c.WinError(error)
        return count
    except BaseException:
        # Drain cancellation before the overlapped object/buffer is released.
        with suppress(OSError):
            pending.cancel()
        with suppress(OSError):
            pending.GetOverlappedResult(True)
        raise


class Pipe:
    def __init__(self, handle: int):
        self._handle: int | None = handle

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise OSError('Windows pipe is closed')
        return self._handle

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._handle is not None:
            handle, self._handle = self._handle, None
            win.CloseHandle(handle)

    def write(self, data, deadline):
        pending, _ = win.WriteFile(self.handle, data, overlapped=True)
        return finish(pending, deadline)

    def send(self, data, deadline):
        if self.write(data, deadline) != len(data):
            raise OSError('Incomplete Windows pipe write')

    def line(self, limit, deadline):
        data = bytearray()
        while len(data) < limit:
            pending, _ = win.ReadFile(self.handle, min(65536, limit - len(data)), overlapped=True)
            count = finish(pending, deadline)
            if not count:
                break
            chunk = pending.getbuffer()
            if chunk is None or len(chunk) != count:
                raise OSError('Incomplete Windows pipe read')
            end = chunk.find(b'\n')
            if end >= 0:
                data.extend(chunk[:end + 1])
                break
            data.extend(chunk)
        return bytes(data)


class Listener:
    def __init__(self, name):
        self.name = name
        self.next = listener_handle(name, first=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        win.CloseHandle(self.next)

    def accept(self):
        while True:
            handle = self.next
            self.next = listener_handle(self.name)
            try:
                pending = win.ConnectNamedPipe(handle, overlapped=True)
                finish(pending, None)
                return Pipe(handle)
            except OSError as exc:
                # CPython may report an already-connected pipe via a synthetic
                # signaled event with no pending operation. Verify the peer exists.
                if (exc.winerror in (win.ERROR_PIPE_CONNECTED, ERROR_IO_INCOMPLETE)
                        and pipe_client_pid(handle, c.byref(w.DWORD()))):
                    return Pipe(handle)
                win.CloseHandle(handle)
                if exc.winerror not in (win.ERROR_BROKEN_PIPE, win.ERROR_NO_DATA):
                    raise
            except BaseException:
                win.CloseHandle(handle)
                raise


def client(name):
    flags = win.FILE_FLAG_OVERLAPPED | SECURITY_IDENTIFICATION
    access = win.GENERIC_READ | win.GENERIC_WRITE
    try:
        handle = win.CreateFile(name, access, 0, 0, win.OPEN_EXISTING, flags, 0)
    except OSError as exc:
        if exc.winerror != win.ERROR_PIPE_BUSY:
            raise
        win.WaitNamedPipe(name, 2000)
        handle = win.CreateFile(name, access, 0, 0, win.OPEN_EXISTING, flags, 0)
    try:
        verify_handle(handle)
    except BaseException:
        win.CloseHandle(handle)
        raise
    return Pipe(handle)


@contextmanager
def child_stdin():
    name = pipe_name('stdin-' + uuid.uuid4().hex)
    with Pipe(listener_handle(name, outbound=True, first=True)) as pipe:
        pending = win.ConnectNamedPipe(pipe.handle, overlapped=True)
        try:
            reader = win.CreateFile(name, win.GENERIC_READ, 0, 0, win.OPEN_EXISTING, SECURITY_IDENTIFICATION, 0)
        except BaseException:
            with suppress(OSError):
                pending.cancel()
            with suppress(OSError):
                pending.GetOverlappedResult(True)
            raise
        try:
            finish(pending, time.monotonic() + 3)
            fd = msvcrt.open_osfhandle(reader, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            win.CloseHandle(reader)
            raise
        with os.fdopen(fd, 'rb', buffering=0) as child_read:
            yield child_read, pipe
