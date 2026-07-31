import sqlite3
import hashlib
import threading
from pathlib import Path

import wechat_txt_exporter.exporter as exporter_module
from wechat_txt_exporter.adapter import Weixin411Adapter
from wechat_txt_exporter.exporter import _unique_filename, export_all, safe_filename
from wechat_txt_exporter.models import Account
from wechat_txt_exporter.voice import VoiceResult


def _create_fixture(root: Path) -> Account:
    account_dir = root / "wxid_me_abcd"
    contact_dir = account_dir / "db_storage" / "contact"
    session_dir = account_dir / "db_storage" / "session"
    message_dir = account_dir / "db_storage" / "message"
    for directory in (contact_dir, session_dir, message_dir):
        directory.mkdir(parents=True)

    connection = sqlite3.connect(contact_dir / "contact.db")
    connection.execute(
        "CREATE TABLE contact (id INTEGER, username TEXT, alias TEXT, remark TEXT, nick_name TEXT)"
    )
    connection.executemany(
        "INSERT INTO contact VALUES (?, ?, ?, ?, ?)",
        [
            (1, "wxid_friend", "friend", "好友备注", "好友昵称"),
            (2, "group@chatroom", "", "测试群", ""),
            (3, "gh_official", "", "公众号", ""),
        ],
    )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(session_dir / "session.db")
    connection.execute('CREATE TABLE mapping (_user TEXT, table_name TEXT, _table INTEGER)')
    connection.executemany(
        "INSERT INTO mapping VALUES (?, ?, ?)",
        [
            ("wxid_friend", "friend_table", 0),
            ("group@chatroom", "group_table", 0),
            ("gh_official", "official_table", 0),
        ],
    )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(message_dir / "message_0.db")
    schema = """(
        local_id INTEGER, server_id INTEGER, local_type INTEGER, sort_seq INTEGER,
        real_sender_id INTEGER, create_time INTEGER, origin_source BLOB,
        message_content BLOB, compress_content BLOB, packed_info_data BLOB
    )"""
    connection.execute(f"CREATE TABLE friend_table {schema}")
    connection.execute(f"CREATE TABLE group_table {schema}")
    connection.executemany(
        "INSERT INTO friend_table VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (2, 2, 1, 20, 0, 200, b"", "回复", b"", b""),
            (1, 1, 1, 10, 1, 100, b"", "你好", b"", b""),
        ],
    )
    connection.execute(
        "INSERT INTO group_table VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, 1, 10, 1, 300, b"", "wxid_friend:\n群消息", b"", b""),
    )
    connection.commit()
    connection.close()
    return Account("wxid_me", account_dir, None, "abcd")


def _factory(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def test_exports_private_and_group_but_not_official(tmp_path):
    account = _create_fixture(tmp_path)
    output_root = tmp_path / "exports"
    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, output_root)

    assert result.succeeded == 2
    assert result.failed == 0
    assert result.messages == 3
    files = sorted(result.output_dir.rglob("*.txt"))
    assert len(files) == 2
    assert (result.output_dir / "个人会话").is_dir()
    assert (result.output_dir / "群聊").is_dir()
    private = next(path for path in files if "好友备注" in path.name)
    text = private.read_text(encoding="utf-8")
    assert text.index("你好") < text.index("回复")
    assert "好友备注：你好" in text
    assert "我：回复" in text
    group = next(path for path in files if "测试群" in path.name)
    assert "好友备注：群消息" in group.read_text(encoding="utf-8")


def test_repeated_export_rewrites_the_same_txt_atomically(tmp_path):
    account = _create_fixture(tmp_path)
    output_root = tmp_path / "exports"

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        first = export_all(adapter, output_root)

    target = first.output_dir / "个人会话" / "好友备注.txt"
    original = target.read_text(encoding="utf-8")
    target.write_text("不应保留的旧内容", encoding="utf-8")

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        second = export_all(adapter, output_root, force_full=True)

    assert first.output_dir == output_root / account.wxid
    assert second.output_dir == first.output_dir
    assert target.read_text(encoding="utf-8") == original
    assert not list(first.output_dir.rglob("*.tmp"))


def test_incremental_export_skips_unchanged_conversations(tmp_path, monkeypatch):
    account = _create_fixture(tmp_path)
    output_root = tmp_path / "exports"
    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        first = export_all(adapter, output_root)

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        monkeypatch.setattr(
            adapter,
            "iter_messages",
            lambda _conversation: (_ for _ in ()).throw(
                AssertionError("unchanged conversation was read")
            ),
        )
        second = export_all(adapter, output_root)

    assert first.succeeded == 2
    assert second.succeeded == 0
    assert second.unchanged == 2
    assert second.messages == 3


def test_incremental_export_rewrites_only_changed_conversation(tmp_path):
    account = _create_fixture(tmp_path)
    output_root = tmp_path / "exports"
    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        export_all(adapter, output_root)

    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    connection = sqlite3.connect(message_path)
    connection.execute(
        "INSERT INTO friend_table "
        "(local_id, server_id, local_type, sort_seq, real_sender_id, create_time, "
        "origin_source, message_content, compress_content, packed_info_data) "
        "VALUES (3, 3, 1, 30, 1, 400, '', '新增消息', '', '')"
    )
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, output_root)

    assert result.succeeded == 1
    assert result.unchanged == 1
    assert result.messages == 4
    target = result.output_dir / "个人会话" / "好友备注.txt"
    assert "新增消息" in target.read_text(encoding="utf-8")


def test_incremental_export_reuses_voice_transcript_cache(tmp_path, monkeypatch):
    account = _create_fixture(tmp_path)
    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    connection = sqlite3.connect(message_path)
    connection.execute(
        "INSERT INTO friend_table "
        "(local_id, server_id, local_type, real_sender_id, create_time, message_content) "
        "VALUES (9, 9988, 34, 1, 500, '')"
    )
    connection.commit()
    connection.close()

    media_path = account.data_dir / "db_storage" / "message" / "media_0.db"
    connection = sqlite3.connect(media_path)
    connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
    connection.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, 'wxid_friend')")
    connection.execute(
        "CREATE TABLE VoiceInfo "
        "(chat_name_id INTEGER, create_time INTEGER, msg_svr_id INTEGER, voice_data BLOB)"
    )
    connection.execute(
        "INSERT INTO VoiceInfo VALUES (1, 500, 9988, ?)",
        (b"#!SILK_V3 cached",),
    )
    connection.commit()
    connection.close()

    class FakeTranscriber:
        calls = 0

        def __init__(self, _model):
            pass

        def process(self, _silk, _work_dir, cancel_event=None):
            assert cancel_event is None
            type(self).calls += 1
            return VoiceResult(transcript="缓存语音")

    monkeypatch.setattr(exporter_module, "VoiceTranscriber", FakeTranscriber)
    output_root = tmp_path / "exports"
    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        first = export_all(adapter, output_root, transcribe_voice=True)

    connection = sqlite3.connect(message_path)
    connection.execute(
        "INSERT INTO friend_table "
        "(local_id, server_id, local_type, real_sender_id, create_time, message_content) "
        "VALUES (10, 9999, 1, 1, 600, '触发会话更新')"
    )
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        second = export_all(adapter, output_root, transcribe_voice=True)

    assert first.voices_transcribed == 1
    assert second.voices_transcribed == 0
    assert second.voices_cached == 1
    assert FakeTranscriber.calls == 1


def test_export_can_be_cancelled_before_next_conversation(tmp_path):
    account = _create_fixture(tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, tmp_path / "exports", cancel_event=cancel_event)

    assert result.cancelled is True
    assert result.succeeded == 0


def test_safe_filename_handles_windows_rules():
    assert safe_filename("A:B/C", "wxid_a") == "A_B_C.txt"
    assert safe_filename("CON", "wxid_a") == "_CON.txt"
    used: set[str] = set()
    assert _unique_filename("好友.txt", used) == "好友.txt"
    assert _unique_filename("好友.txt", used) == "好友 (2).txt"


def test_derives_v4_message_table_from_session_username(tmp_path):
    account = _create_fixture(tmp_path)
    session_path = account.data_dir / "db_storage" / "session" / "session.db"
    connection = sqlite3.connect(session_path)
    connection.execute("DROP TABLE mapping")
    connection.execute("CREATE TABLE Session (username TEXT, sort_timestamp INTEGER)")
    connection.execute("INSERT INTO Session VALUES ('wxid_friend', 123)")
    connection.commit()
    connection.close()

    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    hashed = "Msg_" + hashlib.md5(b"wxid_friend").hexdigest()
    connection = sqlite3.connect(message_path)
    connection.execute(f'ALTER TABLE friend_table RENAME TO "{hashed}"')
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        conversations = adapter.load_conversations()
        assert len(conversations) == 1
        assert conversations[0].username == "wxid_friend"
        assert conversations[0].table_name == hashed
        assert len(list(adapter.iter_messages(conversations[0]))) == 2


def test_scans_business_message_shards(tmp_path):
    account = _create_fixture(tmp_path)
    message_dir = account.data_dir / "db_storage" / "message"
    (message_dir / "message_0.db").rename(message_dir / "biz_message_0.db")

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, tmp_path / "exports")
    assert result.succeeded == 2
    assert result.failed == 0


def test_filters_system_messages_and_labels_self_from_is_send(tmp_path):
    account = _create_fixture(tmp_path)
    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    connection = sqlite3.connect(message_path)
    connection.execute("ALTER TABLE friend_table ADD COLUMN is_send INTEGER")
    connection.execute(
        "INSERT INTO friend_table "
        "(local_id, local_type, real_sender_id, create_time, message_content, is_send) "
        "VALUES (3, 1, 1, 300, '我发送的消息', 1)"
    )
    connection.execute(
        "INSERT INTO friend_table "
        "(local_id, local_type, real_sender_id, create_time, message_content, is_send) "
        "VALUES (4, 10000, 1, 400, '系统提示', 0)"
    )
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, tmp_path / "exports")
    private = next(path for path in result.output_dir.rglob("好友备注*.txt"))
    text = private.read_text(encoding="utf-8")
    assert "我：我发送的消息" in text
    assert "系统提示" not in text


def test_distinguishes_private_sender_when_schema_only_has_is_sender(tmp_path):
    account = _create_fixture(tmp_path)
    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    connection = sqlite3.connect(message_path)
    connection.execute("DROP TABLE friend_table")
    connection.execute(
        "CREATE TABLE friend_table ("
        "local_id INTEGER, local_type INTEGER, create_time INTEGER, "
        "message_content TEXT, is_sender INTEGER)"
    )
    connection.executemany(
        "INSERT INTO friend_table VALUES (?, ?, ?, ?, ?)",
        [
            (1, 1, 100, "对方发送", 0),
            (2, 1, 200, "自己发送", 1),
        ],
    )
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, tmp_path / "exports")
    private = next(path for path in result.output_dir.rglob("好友备注*.txt"))
    text = private.read_text(encoding="utf-8")
    assert "好友备注：对方发送" in text
    assert "我：自己发送" in text


def test_uses_status_only_when_sender_identity_is_missing(tmp_path):
    account = _create_fixture(tmp_path)
    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    connection = sqlite3.connect(message_path)
    connection.execute("DROP TABLE friend_table")
    connection.execute(
        "CREATE TABLE friend_table ("
        "local_id INTEGER, local_type INTEGER, create_time INTEGER, "
        "message_content TEXT, status INTEGER)"
    )
    connection.executemany(
        "INSERT INTO friend_table VALUES (?, ?, ?, ?, ?)",
        [
            (1, 1, 100, "状态表示对方", 4),
            (2, 1, 200, "状态表示自己", 2),
        ],
    )
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, tmp_path / "exports")
    private = next(path for path in result.output_dir.rglob("好友备注*.txt"))
    text = private.read_text(encoding="utf-8")
    assert "好友备注：状态表示对方" in text
    assert "我：状态表示自己" in text


def test_group_receive_with_missing_name2id_is_not_labeled_as_self(tmp_path):
    account = _create_fixture(tmp_path)
    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    connection = sqlite3.connect(message_path)
    connection.execute("ALTER TABLE group_table ADD COLUMN status INTEGER")
    connection.execute(
        "INSERT INTO group_table "
        "(local_id, local_type, real_sender_id, create_time, message_content, status) "
        "VALUES (2, 49, 10, 400, '<msg><appmsg><type>5</type></appmsg></msg>', 4)"
    )
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, tmp_path / "exports")
    group = next(path for path in result.output_dir.rglob("测试群*.txt"))
    text = group.read_text(encoding="utf-8")
    assert "群成员（发送者记录缺失，ID 10）：" in text
    assert "我：[链接]" not in text


def test_resolves_sender_id_from_message_name2id(tmp_path):
    account = _create_fixture(tmp_path)
    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    connection = sqlite3.connect(message_path)
    connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
    connection.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (account.wxid,))
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        result = export_all(adapter, tmp_path / "exports")
    private = next(path for path in result.output_dir.rglob("好友备注*.txt"))
    text = private.read_text(encoding="utf-8")
    assert "我：你好" in text
    assert "微信团队：" not in text


def test_locates_voice_blob_from_media_database(tmp_path):
    account = _create_fixture(tmp_path)
    message_path = account.data_dir / "db_storage" / "message" / "message_0.db"
    connection = sqlite3.connect(message_path)
    connection.execute(
        "INSERT INTO friend_table "
        "(local_id, server_id, local_type, real_sender_id, create_time, message_content) "
        "VALUES (9, 9988, 34, 1, 500, '')"
    )
    connection.commit()
    connection.close()

    media_path = account.data_dir / "db_storage" / "message" / "media_0.db"
    connection = sqlite3.connect(media_path)
    connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
    connection.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, 'wxid_friend')")
    connection.execute(
        "CREATE TABLE VoiceInfo "
        "(chat_name_id INTEGER, create_time INTEGER, msg_svr_id INTEGER, voice_data BLOB)"
    )
    silk = b"#!SILK_V3 synthetic"
    connection.execute("INSERT INTO VoiceInfo VALUES (1, 500, 9988, ?)", (silk,))
    connection.commit()
    connection.close()

    with Weixin411Adapter(account, b"x" * 32, connection_factory=_factory) as adapter:
        conversation = next(
            item for item in adapter.load_conversations() if item.username == "wxid_friend"
        )
        voice = next(
            item
            for item in adapter.iter_messages(conversation)
            if (item.message_type & 0xFFFF) == 34
        )
        assert adapter.voice_blob(conversation, voice) == silk
