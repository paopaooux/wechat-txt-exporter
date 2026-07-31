import ctypes

from wechat_txt_exporter.debug_key import (
    _Context,
    _DebugEvent,
    _read_login_key_argument,
)
def test_debugger_structure_layout_matches_windows_x64():
    assert ctypes.sizeof(_Context) == 1232
    assert _Context.rdx.offset == 136
    assert _Context.rip.offset == 248
    assert ctypes.sizeof(_DebugEvent) == 176


def test_debugger_reads_key_pointer_at_rdx_plus_08():
    argument_address = 0x1000
    key_address = 0x2000
    key = bytes(range(32))
    memory = {
        (argument_address + 0x08, 8): key_address.to_bytes(8, "little"),
        (argument_address + 0x10, 8): len(key).to_bytes(8, "little"),
        (key_address, len(key)): key,
    }
    reads = []

    def read(address, size):
        reads.append((address, size))
        return memory.get((address, size))

    assert _read_login_key_argument(read, argument_address) == key
    assert reads == [
        (argument_address + 0x08, 8),
        (argument_address + 0x10, 8),
        (key_address, len(key)),
    ]
