import hashlib
import sqlite3

from wechat_txt_exporter.key_recovery import (
    _check_memory_block,
    load_key_records,
    load_manual_key,
)
from wechat_txt_exporter.models import Account


def test_loads_empty_key_hash_and_prioritizes_current_blob(tmp_path):
    wxid = "wxid_test"
    account_dir = tmp_path / "wxid_test_abcd"
    account_dir.mkdir()
    login_dir = tmp_path / "login" / wxid
    login_dir.mkdir(parents=True)
    current_blob = b"current" * 30
    (login_dir / "key_info.dat").write_bytes(current_blob)

    database_dir = tmp_path / "all_users" / "login" / wxid
    database_dir.mkdir(parents=True)
    connection = sqlite3.connect(database_dir / "key_info.db")
    connection.execute(
        "CREATE TABLE LoginKeyInfoTable "
        "(user_name_md5 TEXT, key_md5 TEXT, key_info_md5 TEXT, key_info_data BLOB)"
    )
    user_hash = hashlib.md5(wxid.encode()).hexdigest()
    for blob in (b"old" * 60, current_blob):
        connection.execute(
            "INSERT INTO LoginKeyInfoTable VALUES (?, '', ?, ?)",
            (user_hash, hashlib.md5(blob).hexdigest(), blob),
        )
    connection.commit()
    connection.close()

    account = Account(wxid, account_dir, login_dir, "abcd")
    records = load_key_records(account, tmp_path)
    assert records[0] == ("", current_blob)


def test_manual_key_file(tmp_path):
    path = tmp_path / "key.txt"
    path.write_text("ab" * 32, encoding="ascii")
    assert load_manual_key(path) == bytes.fromhex("ab" * 32)


def test_extracts_key_from_wcdb_key_and_salt_literal():
    key = bytes(range(32))
    salt = bytes(range(16))
    block = b"prefix x'" + key.hex().encode() + salt.hex().encode() + b"' suffix"
    found = _check_memory_block(
        block,
        0,
        0,
        set(),
        [],
        lambda candidate: candidate == key,
        set(),
        [1],
        128,
    )
    assert found == key
