from __future__ import annotations

import ctypes
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from .errors import KeyRecoveryError

_HEX_KEY = re.compile(r"^[0-9a-fA-F]{64}$")


def find_wx_key_dll() -> Path | None:
    """Find WeFlow's Windows key helper without downloading binaries."""
    candidates: list[Path] = []
    configured = os.environ.get("WX_KEY_DLL_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())
    project_root = Path(__file__).resolve().parents[2]
    workspace_root = project_root.parent
    candidates.extend(
        (
            project_root / "resources" / "key" / "win32" / "x64" / "wx_key.dll",
            workspace_root / "WeFlow" / "resources" / "key" / "win32" / "x64" / "wx_key.dll",
        )
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _decode_c_string(buffer: ctypes.Array[ctypes.c_char]) -> str:
    return buffer.value.decode("utf-8", errors="replace").strip()


def _last_error(dll: object) -> str:
    try:
        value = dll.GetLastErrorMsg()  # type: ignore[attr-defined]
        if not value:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace").strip()
        raw = ctypes.cast(value, ctypes.c_char_p).value
        return (raw or b"").decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _configure_dll(dll: object) -> None:
    dll.InitializeHook.argtypes = [ctypes.c_uint32]  # type: ignore[attr-defined]
    dll.InitializeHook.restype = ctypes.c_bool  # type: ignore[attr-defined]
    dll.PollKeyData.argtypes = [ctypes.POINTER(ctypes.c_char), ctypes.c_int]  # type: ignore[attr-defined]
    dll.PollKeyData.restype = ctypes.c_bool  # type: ignore[attr-defined]
    dll.GetStatusMessage.argtypes = [  # type: ignore[attr-defined]
        ctypes.POINTER(ctypes.c_char),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    dll.GetStatusMessage.restype = ctypes.c_bool  # type: ignore[attr-defined]
    dll.CleanupHook.argtypes = []  # type: ignore[attr-defined]
    dll.CleanupHook.restype = ctypes.c_bool  # type: ignore[attr-defined]
    dll.GetLastErrorMsg.argtypes = []  # type: ignore[attr-defined]
    dll.GetLastErrorMsg.restype = ctypes.c_char_p  # type: ignore[attr-defined]


def recover_with_wx_key_dll(
    pids: Iterable[int],
    normalizer: Callable[[bytes], bytes | None],
    *,
    timeout_seconds: float = 180,
    status: Callable[[str], None] | None = None,
    pid_provider: Callable[[], Iterable[int]] | None = None,
) -> bytes | None:
    """Temporarily hook the active local Weixin process and verify its DB key."""
    if os.name != "nt":
        return None
    dll_path = find_wx_key_dll()
    if dll_path is None:
        return None
    initial_pids = list(dict.fromkeys(int(pid) for pid in pids if int(pid) > 0))
    if not initial_pids:
        raise KeyRecoveryError("未找到微信进程。请先打开微信登录界面后再开始。")
    try:
        dll = ctypes.WinDLL(str(dll_path))
        _configure_dll(dll)
    except (OSError, AttributeError) as exc:
        raise KeyRecoveryError(f"无法加载密钥组件 wx_key.dll：{exc}") from exc

    deadline = time.monotonic() + max(1.0, timeout_seconds)
    last_status = ""
    pid_list = initial_pids
    while time.monotonic() < deadline:
        if pid_provider is not None:
            pid_list = list(dict.fromkeys(int(pid) for pid in pid_provider() if int(pid) > 0))
        if not pid_list:
            if status and last_status != "正在等待微信重新启动……":
                last_status = "正在等待微信重新启动……"
                status(last_status)
            time.sleep(0.5)
            continue

        # Toolhelp returns the Weixin main process first, matching WeFlow's flow.
        pid = pid_list[0]
        if not dll.InitializeHook(pid):
            detail = _last_error(dll) or "初始化失败"
            current = list(pid_provider()) if pid_provider is not None else pid_list
            if pid_provider is not None and pid not in current:
                time.sleep(0.25)
                continue
            if "0xC0000022" in detail or "ACCESS_DENIED" in detail.upper() or "拒绝" in detail:
                raise KeyRecoveryError(
                    "权限不足，无法读取微信进程。请右键 run.bat 选择“以管理员身份运行”后重试。"
                )
            raise KeyRecoveryError(f"密钥组件初始化失败：{detail}")

        process_lost = False
        try:
            next_process_check = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                key_buffer = ctypes.create_string_buffer(128)
                if dll.PollKeyData(key_buffer, len(key_buffer)):
                    text = _decode_c_string(key_buffer)
                    if _HEX_KEY.fullmatch(text):
                        candidate = bytes.fromhex(text)
                        normalized = normalizer(candidate)
                        if normalized is not None:
                            return normalized
                        raise KeyRecoveryError(
                            "已从当前微信账号取得密钥，但与所选账号数据库不匹配；"
                            "请在微信中登录所选账号后重试。"
                        )
                for _ in range(5):
                    message_buffer = ctypes.create_string_buffer(512)
                    level = ctypes.c_int()
                    if not dll.GetStatusMessage(
                        message_buffer, len(message_buffer), ctypes.byref(level)
                    ):
                        break
                    message = _decode_c_string(message_buffer)
                    if message and message != last_status:
                        last_status = message
                        if status:
                            status(message)
                if pid_provider is not None and time.monotonic() >= next_process_check:
                    next_process_check = time.monotonic() + 1.0
                    if pid not in set(pid_provider()):
                        process_lost = True
                        if status:
                            status("检测到微信进程已切换，正在重新安装 Hook……")
                        break
                time.sleep(0.12)
        finally:
            try:
                dll.CleanupHook()
            except Exception:
                pass
        if not process_lost:
            break
    raise KeyRecoveryError(
        "获取密钥超时。请先打开微信登录界面，再点击验证；"
        "看到 Hook 安装成功后完成目标账号登录。"
    )
