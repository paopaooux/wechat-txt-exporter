from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable, Iterable
from ctypes import wintypes

from .errors import KeyRecoveryError


_DB_KEY_PATTERN = bytes(
    (
        0x24,
        0x50,
        0x48,
        0xC7,
        0x45,
        0x00,
        0xFE,
        0xFF,
        0xFF,
        0xFF,
        0x44,
        0x89,
        0xCF,
        0x44,
        0x89,
        0xC3,
        0x49,
        0x89,
        0xD6,
        0x48,
        0x89,
        0xCE,
        0x48,
        0x89,
    )
)
_DB_KEY_PATTERN_OFFSET = -3
_KEY_BUFFER_POINTER_OFFSET = 0x08
_KEY_SIZE_OFFSET = 0x10
_KEY_LENGTH = 32

_PROCESS_ACCESS = 0x0400 | 0x0010 | 0x0020 | 0x0008
_THREAD_ACCESS = 0x0008 | 0x0010
_PAGE_EXECUTE_READWRITE = 0x40
_LIST_MODULES_ALL = 0x03
_EXCEPTION_DEBUG_EVENT = 1
_EXIT_PROCESS_DEBUG_EVENT = 5
_EXCEPTION_BREAKPOINT = 0x80000003
_DBG_CONTINUE = 0x00010002
_DBG_EXCEPTION_NOT_HANDLED = 0x80010001
_ERROR_SEM_TIMEOUT = 121
_CONTEXT_CONTROL_INTEGER = 0x00100003


class _ModuleInfo(ctypes.Structure):
    _fields_ = [
        ("base", ctypes.c_void_p),
        ("size", wintypes.DWORD),
        ("entry_point", ctypes.c_void_p),
    ]


class _ExceptionRecord(ctypes.Structure):
    _fields_ = [
        ("code", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("record", ctypes.c_void_p),
        ("address", ctypes.c_void_p),
        ("parameter_count", wintypes.DWORD),
        ("_alignment", wintypes.DWORD),
        ("information", ctypes.c_size_t * 15),
    ]


class _ExceptionDebugInfo(ctypes.Structure):
    _fields_ = [("record", _ExceptionRecord), ("first_chance", wintypes.DWORD)]


class _DebugEventData(ctypes.Union):
    _fields_ = [("exception", _ExceptionDebugInfo), ("raw", ctypes.c_ubyte * 160)]


class _DebugEvent(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [
        ("code", wintypes.DWORD),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
        ("data", _DebugEventData),
    ]


class _Context(ctypes.Structure):
    _fields_ = [
        ("p1_home", ctypes.c_ulonglong),
        ("p2_home", ctypes.c_ulonglong),
        ("p3_home", ctypes.c_ulonglong),
        ("p4_home", ctypes.c_ulonglong),
        ("p5_home", ctypes.c_ulonglong),
        ("p6_home", ctypes.c_ulonglong),
        ("flags", wintypes.DWORD),
        ("mx_csr", wintypes.DWORD),
        ("seg_cs", wintypes.WORD),
        ("seg_ds", wintypes.WORD),
        ("seg_es", wintypes.WORD),
        ("seg_fs", wintypes.WORD),
        ("seg_gs", wintypes.WORD),
        ("seg_ss", wintypes.WORD),
        ("eflags", wintypes.DWORD),
        ("dr0", ctypes.c_ulonglong),
        ("dr1", ctypes.c_ulonglong),
        ("dr2", ctypes.c_ulonglong),
        ("dr3", ctypes.c_ulonglong),
        ("dr6", ctypes.c_ulonglong),
        ("dr7", ctypes.c_ulonglong),
        ("rax", ctypes.c_ulonglong),
        ("rcx", ctypes.c_ulonglong),
        ("rdx", ctypes.c_ulonglong),
        ("rbx", ctypes.c_ulonglong),
        ("rsp", ctypes.c_ulonglong),
        ("rbp", ctypes.c_ulonglong),
        ("rsi", ctypes.c_ulonglong),
        ("rdi", ctypes.c_ulonglong),
        ("r8", ctypes.c_ulonglong),
        ("r9", ctypes.c_ulonglong),
        ("r10", ctypes.c_ulonglong),
        ("r11", ctypes.c_ulonglong),
        ("r12", ctypes.c_ulonglong),
        ("r13", ctypes.c_ulonglong),
        ("r14", ctypes.c_ulonglong),
        ("r15", ctypes.c_ulonglong),
        ("rip", ctypes.c_ulonglong),
        ("_remaining", ctypes.c_ubyte * (1232 - 256)),
    ]


def _windows_libraries() -> tuple[object, object]:
    if os.name != "nt":
        raise KeyRecoveryError("登录密钥捕获仅支持 Windows。")
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.WriteProcessMemory.restype = wintypes.BOOL
    kernel32.WaitForDebugEventEx.restype = wintypes.BOOL
    psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    psapi.GetModuleBaseNameW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    psapi.GetModuleInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_ModuleInfo),
        wintypes.DWORD,
    ]
    return kernel32, psapi


def _read_process(kernel32: object, process: int, address: int, size: int) -> bytes | None:
    if address <= 0 or size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    bytes_read = ctypes.c_size_t()
    if not kernel32.ReadProcessMemory(  # type: ignore[attr-defined]
        process, ctypes.c_void_p(address), buffer, size, ctypes.byref(bytes_read)
    ):
        return None
    return buffer.raw[: bytes_read.value]


def _read_login_key_argument(
    read: Callable[[int, int], bytes | None], argument_address: int
) -> bytes | None:
    """Read the key from the Weixin login function's second argument."""
    key_pointer_data = read(argument_address + _KEY_BUFFER_POINTER_OFFSET, 8)
    key_size_data = read(argument_address + _KEY_SIZE_OFFSET, 8)
    if not key_pointer_data or not key_size_data:
        return None
    key_pointer = int.from_bytes(key_pointer_data, "little")
    key_size = int.from_bytes(key_size_data, "little")
    if key_pointer <= 0 or key_size != _KEY_LENGTH:
        return None
    candidate = read(key_pointer, key_size)
    return candidate if candidate and len(candidate) == _KEY_LENGTH else None


def _find_weixin_module(process: int) -> tuple[int, int] | None:
    _kernel32, psapi = _windows_libraries()
    modules = (ctypes.c_void_p * 2048)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcessModulesEx(
        process,
        modules,
        ctypes.sizeof(modules),
        ctypes.byref(needed),
        _LIST_MODULES_ALL,
    ):
        return None
    count = min(len(modules), needed.value // ctypes.sizeof(ctypes.c_void_p))
    for module in modules[:count]:
        name = ctypes.create_unicode_buffer(260)
        if not psapi.GetModuleBaseNameW(process, module, name, len(name)):
            continue
        if name.value.casefold() != "weixin.dll":
            continue
        info = _ModuleInfo()
        if not psapi.GetModuleInformation(
            process, module, ctypes.byref(info), ctypes.sizeof(info)
        ):
            return None
        return int(info.base or 0), int(info.size)
    return None


def find_login_key_hook(pid: int) -> tuple[int, int] | None:
    """Return ``(process handle, hook address)`` for a compatible Weixin process."""
    kernel32, _psapi = _windows_libraries()
    process = kernel32.OpenProcess(_PROCESS_ACCESS, False, int(pid))
    if not process:
        if kernel32.GetLastError() == 5:
            raise KeyRecoveryError(
                "权限不足，无法设置微信登录密钥断点；请通过 run.bat 以管理员身份运行。"
            )
        return None
    module = _find_weixin_module(process)
    if module is None:
        kernel32.CloseHandle(process)
        return None
    base, size = module
    matches: list[int] = []
    chunk_size = 1 << 20
    overlap = b""
    for offset in range(0, size, chunk_size):
        data = _read_process(kernel32, process, base + offset, min(chunk_size, size - offset))
        if not data:
            overlap = b""
            continue
        block = overlap + data
        start = 0
        while True:
            found = block.find(_DB_KEY_PATTERN, start)
            if found < 0:
                break
            matches.append(
                base + offset - len(overlap) + found + _DB_KEY_PATTERN_OFFSET
            )
            start = found + 1
        overlap = block[-len(_DB_KEY_PATTERN) :]
    if len(matches) != 1:
        kernel32.CloseHandle(process)
        return None
    return int(process), matches[0]


def _write_breakpoint(kernel32: object, process: int, address: int, value: bytes) -> bool:
    old_protect = wintypes.DWORD()
    if not kernel32.VirtualProtectEx(  # type: ignore[attr-defined]
        process,
        ctypes.c_void_p(address),
        len(value),
        _PAGE_EXECUTE_READWRITE,
        ctypes.byref(old_protect),
    ):
        return False
    written = ctypes.c_size_t()
    try:
        ok = kernel32.WriteProcessMemory(  # type: ignore[attr-defined]
            process,
            ctypes.c_void_p(address),
            value,
            len(value),
            ctypes.byref(written),
        )
        if ok and written.value == len(value):
            kernel32.FlushInstructionCache(process, ctypes.c_void_p(address), len(value))
            return True
        return False
    finally:
        restored = wintypes.DWORD()
        kernel32.VirtualProtectEx(  # type: ignore[attr-defined]
            process,
            ctypes.c_void_p(address),
            len(value),
            old_protect.value,
            ctypes.byref(restored),
        )


def _aligned_context() -> tuple[ctypes.Array[ctypes.c_char], _Context]:
    raw = ctypes.create_string_buffer(ctypes.sizeof(_Context) + 16)
    offset = (-ctypes.addressof(raw)) % 16
    return raw, _Context.from_buffer(raw, offset)


def _capture_one(pid: int, process: int, target: int, timeout_seconds: float) -> bytes | None:
    kernel32, _psapi = _windows_libraries()
    original = _read_process(kernel32, process, target, 1)
    if not original:
        return None
    if not kernel32.DebugActiveProcess(pid):
        error = kernel32.GetLastError()
        raise KeyRecoveryError(f"无法附加微信进程调试器（WinError {error}）。")
    kernel32.DebugSetProcessKillOnExit(False)
    patched = False
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    try:
        while time.monotonic() < deadline:
            event = _DebugEvent()
            remaining_ms = max(1, min(500, int((deadline - time.monotonic()) * 1000)))
            if not kernel32.WaitForDebugEventEx(ctypes.byref(event), remaining_ms):
                if kernel32.GetLastError() == _ERROR_SEM_TIMEOUT:
                    continue
                break
            continue_status = _DBG_CONTINUE
            candidate: bytes | None = None
            target_breakpoint = False
            try:
                if not patched:
                    patched = _write_breakpoint(kernel32, process, target, b"\xcc")
                    if not patched:
                        raise KeyRecoveryError("无法在微信密钥函数设置临时断点。")
                if event.code == _EXCEPTION_DEBUG_EVENT:
                    record = event.exception.record
                    exception_address = int(record.address or 0)
                    target_breakpoint = (
                        record.code == _EXCEPTION_BREAKPOINT and exception_address == target
                    )
                    if target_breakpoint:
                        thread = kernel32.OpenThread(
                            _THREAD_ACCESS, False, int(event.thread_id)
                        )
                        if thread:
                            try:
                                _raw, context = _aligned_context()
                                context.flags = _CONTEXT_CONTROL_INTEGER
                                if kernel32.GetThreadContext(thread, ctypes.byref(context)):
                                    candidate = _read_login_key_argument(
                                        lambda address, size: _read_process(
                                            kernel32, process, address, size
                                        ),
                                        int(context.rdx),
                                    )
                                    _write_breakpoint(kernel32, process, target, original)
                                    patched = False
                                    context.rip = target
                                    kernel32.SetThreadContext(thread, ctypes.byref(context))
                            finally:
                                kernel32.CloseHandle(thread)
                    elif record.code != _EXCEPTION_BREAKPOINT:
                        continue_status = _DBG_EXCEPTION_NOT_HANDLED
                if event.code == _EXIT_PROCESS_DEBUG_EVENT:
                    patched = False
            finally:
                kernel32.ContinueDebugEvent(
                    event.process_id, event.thread_id, continue_status
                )
            if target_breakpoint:
                return candidate if candidate and len(candidate) == 32 else None
            if event.code == _EXIT_PROCESS_DEBUG_EVENT:
                return None
        return None
    finally:
        if patched:
            _write_breakpoint(kernel32, process, target, original)
        kernel32.DebugActiveProcessStop(pid)


def recover_from_login_debugger(
    pids: Callable[[], Iterable[int]],
    validator: Callable[[bytes], bool],
    *,
    timeout_seconds: float = 180,
    status: Callable[[str], None] | None = None,
) -> bytes | None:
    """Capture the transient account master key during a Weixin login call."""
    if os.name != "nt":
        return None
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    attempted: set[int] = set()
    while time.monotonic() < deadline:
        located: tuple[int, int, int] | None = None
        for pid_value in pids():
            pid = int(pid_value)
            result = find_login_key_hook(pid)
            if result is None:
                continue
            process, target = result
            located = pid, process, target
            break
        if located is None:
            if status:
                status("正在等待微信主进程启动……")
            time.sleep(0.5)
            continue
        pid, process, target = located
        try:
            if status and pid not in attempted:
                status("密钥捕获已就绪，请在微信中登录目标账号……")
            attempted.add(pid)
            candidate = _capture_one(
                pid, process, target, max(1.0, deadline - time.monotonic())
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
        if candidate is not None:
            if validator(candidate):
                return candidate
            if status:
                status("捕获到的密钥与所选账号不匹配，请登录目标账号后重试……")
        time.sleep(0.25)
    return None
