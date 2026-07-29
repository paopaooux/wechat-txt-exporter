from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .content import decode_payload, sender_hint
from .database import EncryptedDatabase, quote_identifier, tables_with_columns, user_tables
from .errors import DatabaseError, SchemaError
from .models import Account, Contact, Conversation, Message

SYSTEM_USERS = {
    "brandsessionholder",
    "filehelper",
    "fmessage",
    "helper_entry",
    "medianote",
    "newsapp",
    "notification_messages",
    "notifymessage",
    "qmessage",
    "qqmail",
    "tmessage",
    "weixin",
    "weibo",
}
SYSTEM_MESSAGE_TYPES = {10000, 10002}


def _as_text(value: object) -> str:
    return decode_payload(value).strip()


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _first(mapping: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        if name.lower() in lowered:
            value = lowered[name.lower()]
            if value is not None:
                return value
    return default


def _column(columns: list[str], names: tuple[str, ...]) -> str | None:
    lookup = {value.lower(): value for value in columns}
    return next((lookup[name.lower()] for name in names if name.lower() in lookup), None)


def _row_dict(row: Any, description: Any = None) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    if description:
        return {str(item[0]): value for item, value in zip(description, row)}
    raise SchemaError("数据库驱动没有提供字段名。")


class Weixin411Adapter:
    """Schema adapter for Weixin 4.1.11.55 databases."""

    def __init__(
        self,
        account: Account,
        key: bytes,
        connection_factory: Callable[[Path], Any] | None = None,
        verbose: bool = False,
    ):
        self.account = account
        self.key = key
        self.connection_factory = connection_factory
        self.verbose = verbose
        self._databases: list[EncryptedDatabase] = []
        self._connections: list[Any] = []
        self._contact_by_username: dict[str, Contact] = {}
        self._contact_by_id: dict[int, Contact] = {}
        self._message_connections: list[tuple[Path, Any]] = []
        self._sender_names_by_db: dict[Path, dict[int, str]] = {}
        self._media_connections: list[tuple[Path, Any]] = []
        self._voice_schema_by_db: dict[Path, list[tuple[str, dict[str, str]]]] = {}
        self._media_name_ids_by_db: dict[Path, dict[str, int]] = {}

    def _open(self, path: Path) -> Any:
        if self.connection_factory:
            connection = self.connection_factory(path)
            self._connections.append(connection)
            return connection
        database = EncryptedDatabase(path, self.key)
        connection = database.open()
        self._databases.append(database)
        return connection

    def close(self) -> None:
        for database in self._databases:
            database.close()
        self._databases.clear()
        for connection in self._connections:
            with contextlib.suppress(Exception):
                connection.close()
        self._connections.clear()
        self._message_connections.clear()
        self._sender_names_by_db.clear()
        self._media_connections.clear()
        self._voice_schema_by_db.clear()
        self._media_name_ids_by_db.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @property
    def db_storage(self) -> Path:
        return self.account.data_dir / "db_storage"

    def validate_key(self) -> str:
        contact_path = self.db_storage / "contact" / "contact.db"
        with EncryptedDatabase(contact_path, self.key) as connection:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return "ok"

    def load_contacts(self) -> dict[str, Contact]:
        if self._contact_by_username:
            return self._contact_by_username
        path = self.db_storage / "contact" / "contact.db"
        connection = self._open(path)
        candidate: tuple[str, list[str]] | None = None
        for table, columns in tables_with_columns(connection):
            username = _column(columns, ("username", "user_name", "str_username"))
            if username and _column(columns, ("nick_name", "nickname", "remark", "alias")):
                candidate = (table, columns)
                break
        if candidate is None:
            raise SchemaError("contact.db 中未找到 4.1.11.55 联系人表。")

        table, columns = candidate
        cursor = connection.execute(f"SELECT rowid AS __rowid__, * FROM {quote_identifier(table)}")
        for row in cursor:
            data = _row_dict(row, cursor.description)
            username = _as_text(_first(data, ("username", "user_name", "str_username")))
            if not username:
                continue
            remark = _as_text(_first(data, ("remark", "con_remark")))
            nickname = _as_text(_first(data, ("nick_name", "nickname")))
            alias = _as_text(_first(data, ("alias",)))
            display_name = remark or nickname or alias or username
            numeric = _first(data, ("contact_id", "user_id", "id", "__rowid__"))
            numeric_id = _as_int(numeric, -1)
            if numeric_id < 0:
                numeric_id = None
            contact = Contact(
                username=username,
                display_name=display_name,
                numeric_id=numeric_id,
                alias=alias,
                is_group=username.endswith("@chatroom"),
            )
            self._contact_by_username[username] = contact
            if numeric_id is not None:
                self._contact_by_id[numeric_id] = contact
        return self._contact_by_username

    def _session_connection(self) -> Any:
        path = self.db_storage / "session" / "session.db"
        return self._open(path)

    def load_conversations(self) -> list[Conversation]:
        contacts = self.load_contacts()
        connection = self._session_connection()
        conversations: dict[str, Conversation] = {}
        diagnostics: list[str] = []
        for table, columns in tables_with_columns(connection):
            diagnostics.append(f"{table}({', '.join(columns)})")
            table_column = _column(columns, ("table_name", "message_table", "msg_table"))
            user_column = _column(
                columns,
                (
                    "_user",
                    "username",
                    "user_name",
                    "userName",
                    "usrName",
                    "UsrName",
                    "chat_name",
                    "str_talker",
                    "talker",
                    "talker_id",
                    "talkerId",
                    "session_id",
                    "sessionId",
                ),
            )
            if not user_column:
                continue
            shard_column = _column(columns, ("_table", "db_index", "shard", "message_db"))
            selected = [user_column]
            if table_column:
                selected.append(table_column)
            if shard_column:
                selected.append(shard_column)
            sql = ", ".join(quote_identifier(value) for value in selected)
            cursor = connection.execute(f"SELECT {sql} FROM {quote_identifier(table)}")
            for row in cursor:
                data = _row_dict(row, cursor.description)
                raw_user = data.get(user_column)
                if isinstance(raw_user, int) and raw_user in self._contact_by_id:
                    username = self._contact_by_id[raw_user].username
                else:
                    username = _as_text(raw_user)
                if not username:
                    continue
                table_name = (
                    _as_text(data.get(table_column))
                    if table_column
                    else f"Msg_{hashlib.md5(username.encode('utf-8')).hexdigest()}"
                )
                contact = contacts.get(username)
                is_group = username.endswith("@chatroom") or bool(contact and contact.is_group)
                if not self._is_exportable(username, contact, is_group):
                    continue
                display_name = contact.display_name if contact else username
                shard_hint = _as_text(data.get(shard_column)) if shard_column else None
                conversations[username] = Conversation(
                    username=username,
                    display_name=display_name,
                    table_name=table_name,
                    is_group=is_group,
                    shard_hint=shard_hint or None,
                )
        if not conversations:
            detail = "; ".join(diagnostics[:20])
            raise SchemaError(
                "session.db 中未找到会话与消息表映射。"
                + (f" 已检查：{detail}" if detail else " 数据库中没有可识别的会话表。")
            )
        return sorted(conversations.values(), key=lambda item: item.display_name.casefold())

    @staticmethod
    def _is_exportable(username: str, contact: Contact | None, is_group: bool) -> bool:
        lowered = username.lower()
        if is_group:
            return True
        if lowered in SYSTEM_USERS or lowered.startswith("gh_"):
            return False
        if contact is not None:
            return True
        return lowered.startswith("wxid_")

    def _open_message_connections(self) -> list[tuple[Path, Any]]:
        if self._message_connections:
            return self._message_connections
        root = self.db_storage / "message"
        paths = []
        for pattern, prefix in (("message_*.db", "message_"), ("biz_message_*.db", "biz_message_")):
            paths.extend(
                path
                for path in root.glob(pattern)
                if path.stem.removeprefix(prefix).isdigit()
            )
        paths.sort(key=lambda path: (path.stem.startswith("biz_"), int(path.stem.split("_")[-1])))
        if not paths:
            raise DatabaseError(f"没有找到消息分片：{root / 'message_N.db'}")
        for path in paths:
            self._message_connections.append((path, self._open(path)))
        return self._message_connections

    @staticmethod
    def _message_table_candidates(table_name: str) -> list[str]:
        values = [table_name]
        if not table_name.casefold().startswith("msg_"):
            values.extend((f"Msg_{table_name}", f"msg_{table_name}"))
        values.append(f"Message_{table_name}")
        return values

    def _locate_message_tables(self, conversation: Conversation) -> list[tuple[Path, Any, str]]:
        found: list[tuple[Path, Any, str]] = []
        candidates = {value.casefold() for value in self._message_table_candidates(conversation.table_name)}
        for path, connection in self._open_message_connections():
            names = user_tables(connection)
            name_map = {name.casefold(): name for name in names}
            match = next((name_map[value] for value in candidates if value in name_map), None)
            if match:
                found.append((path, connection, match))
        if found:
            return found

        # Some builds use one shared message table with an explicit talker column.
        for path, connection in self._open_message_connections():
            for table, columns in tables_with_columns(connection):
                if _column(columns, ("local_id",)) and _column(
                    columns, ("talker", "str_talker", "username", "chat_name")
                ):
                    found.append((path, connection, table))
        return found

    def _sender_name_map(self, path: Path, connection: Any) -> dict[int, str]:
        cached = self._sender_names_by_db.get(path)
        if cached is not None:
            return cached
        result: dict[int, str] = {}
        name_tables = [
            (table, columns)
            for table, columns in tables_with_columns(connection)
            if table.casefold().startswith("name2id")
        ]
        for table, columns in sorted(name_tables, key=lambda item: item[0], reverse=True):
            user_column = _column(columns, ("user_name", "userName", "username"))
            if not user_column:
                continue
            cursor = connection.execute(
                f"SELECT rowid AS __sender_id__, {quote_identifier(user_column)} "
                f"FROM {quote_identifier(table)}"
            )
            for row in cursor:
                data = _row_dict(row, cursor.description)
                sender_id = _as_int(data.get("__sender_id__"), -1)
                username = _as_text(data.get(user_column))
                if sender_id > 0 and username:
                    result[sender_id] = username
            break
        self._sender_names_by_db[path] = result
        return result

    def _open_media_connections(self) -> list[tuple[Path, Any]]:
        if self._media_connections:
            return self._media_connections
        root = self.db_storage / "message"
        paths = sorted(
            (
                path
                for path in root.glob("media_*.db")
                if path.stem.removeprefix("media_").isdigit()
            ),
            key=lambda path: int(path.stem.split("_")[-1]),
        )
        for path in paths:
            self._media_connections.append((path, self._open(path)))
        return self._media_connections

    def _voice_schemas(self, path: Path, connection: Any) -> list[tuple[str, dict[str, str]]]:
        cached = self._voice_schema_by_db.get(path)
        if cached is not None:
            return cached
        schemas: list[tuple[str, dict[str, str]]] = []
        aliases = {
            "data": ("voice_data", "buf", "voicebuf", "data"),
            "chat": ("chat_name_id", "chatnameid", "chat_nameid"),
            "time": ("create_time", "createtime", "time"),
            "server": (
                "msg_svr_id",
                "msgsvrid",
                "svr_id",
                "svrid",
                "server_id",
                "serverid",
            ),
        }
        for table, columns in tables_with_columns(connection):
            if not table.casefold().startswith("voiceinfo"):
                continue
            resolved = {
                key: column
                for key, names in aliases.items()
                if (column := _column(columns, names)) is not None
            }
            if "data" in resolved:
                schemas.append((table, resolved))
        self._voice_schema_by_db[path] = schemas
        return schemas

    @staticmethod
    def _voice_bytes(value: object) -> bytes | None:
        if isinstance(value, bytes):
            return value
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            clean = value.strip()
            try:
                if len(clean) % 2 == 0:
                    return bytes.fromhex(clean)
            except ValueError:
                pass
        return None

    @staticmethod
    def _query_voice_rows(
        connection: Any,
        table: str,
        data_column: str,
        conditions: list[tuple[str, object]],
    ) -> list[bytes]:
        where = " AND ".join(f"{quote_identifier(column)} = ?" for column, _ in conditions)
        sql = (
            f"SELECT {quote_identifier(data_column)} FROM {quote_identifier(table)}"
            + (f" WHERE {where}" if where else "")
            + " ORDER BY rowid ASC"
        )
        rows = connection.execute(sql, tuple(value for _, value in conditions)).fetchall()
        output: list[bytes] = []
        for row in rows:
            value = row[0] if not hasattr(row, "keys") else row[row.keys()[0]]
            decoded = Weixin411Adapter._voice_bytes(value)
            if decoded:
                output.append(decoded)
        return output

    def _media_name_ids(self, path: Path, connection: Any) -> dict[str, int]:
        cached = self._media_name_ids_by_db.get(path)
        if cached is not None:
            return cached
        result: dict[str, int] = {}
        tables = [
            (table, columns)
            for table, columns in tables_with_columns(connection)
            if table.casefold().startswith("name2id")
        ]
        if not tables:
            self._media_name_ids_by_db[path] = result
            return result
        table, columns = sorted(tables, key=lambda item: item[0], reverse=True)[0]
        user_column = _column(columns, ("user_name", "userName", "username"))
        if not user_column:
            self._media_name_ids_by_db[path] = result
            return result
        rows = connection.execute(
            f"SELECT rowid, {quote_identifier(user_column)} FROM {quote_identifier(table)}"
        ).fetchall()
        for row in rows:
            value = _as_int(row[0], 0)
            username = _as_text(row[1])
            if value > 0 and username:
                result[username] = value
        self._media_name_ids_by_db[path] = result
        return result

    def voice_blob(self, conversation: Conversation, message: Message) -> bytes | None:
        """Locate a type-34 Silk blob in media_N.db using WeFlow's fallback order."""
        if (message.message_type & 0xFFFF) != 34:
            return None
        server_id = _as_int(_first(message.raw, ("server_id", "serverid", "msg_svr_id")), 0)
        candidates = [
            value
            for value in (
                str(message.sender_id or "").strip(),
                conversation.username,
                self.account.wxid,
            )
            if value and not value.isdigit()
        ]
        candidates = list(dict.fromkeys(candidates))
        for path, connection in self._open_media_connections():
            name_ids = self._media_name_ids(path, connection)
            chat_ids = list(dict.fromkeys(name_ids[value] for value in candidates if value in name_ids))
            for table, schema in self._voice_schemas(path, connection):
                data_column = schema["data"]
                server_column = schema.get("server")
                chat_column = schema.get("chat")
                time_column = schema.get("time")
                if server_column and server_id:
                    if chat_column:
                        for chat_id in chat_ids:
                            rows = self._query_voice_rows(
                                connection,
                                table,
                                data_column,
                                [(chat_column, chat_id), (server_column, server_id)],
                            )
                            if rows:
                                return rows[0]
                    rows = self._query_voice_rows(
                        connection, table, data_column, [(server_column, server_id)]
                    )
                    if rows:
                        return rows[0]
                if chat_column and time_column:
                    for chat_id in chat_ids:
                        rows = self._query_voice_rows(
                            connection,
                            table,
                            data_column,
                            [(chat_column, chat_id), (time_column, message.timestamp)],
                        )
                        if rows:
                            return rows[0]
                if time_column:
                    rows = self._query_voice_rows(
                        connection, table, data_column, [(time_column, message.timestamp)]
                    )
                    if rows:
                        return rows[0]
        return None

    def iter_messages(self, conversation: Conversation) -> Iterator[Message]:
        locations = self._locate_message_tables(conversation)
        if not locations:
            # session.db may retain stale entries after the corresponding local
            # message table was cleaned. This is an empty local conversation,
            # not an export failure.
            return
        messages: list[Message] = []
        for shard_index, (_path, connection, table) in enumerate(locations):
            columns = next(columns for name, columns in tables_with_columns(connection) if name == table)
            required = {
                "local_id": _column(columns, ("local_id", "localid")),
                "local_type": _column(columns, ("local_type", "type", "msg_type")),
                "create_time": _column(columns, ("create_time", "createtime", "timestamp")),
            }
            if not all(required.values()):
                continue
            talker_column = _column(columns, ("talker", "str_talker", "username", "chat_name"))
            sql = f"SELECT * FROM {quote_identifier(table)}"
            parameters: tuple[object, ...] = ()
            if talker_column:
                sql += f" WHERE {quote_identifier(talker_column)} = ?"
                parameters = (conversation.username,)
            cursor = connection.execute(sql, parameters)
            for row in cursor:
                data = _row_dict(row, cursor.description)
                local_id = _as_int(_first(data, ("local_id", "localid")))
                message_type = _as_int(_first(data, ("local_type", "type", "msg_type")))
                if (message_type & 0xFFFF) in SYSTEM_MESSAGE_TYPES:
                    continue
                timestamp = _as_int(_first(data, ("create_time", "createtime", "timestamp")))
                if timestamp > 10_000_000_000:
                    timestamp //= 1000
                sort_seq = _as_int(_first(data, ("sort_seq", "server_seq", "server_id")), local_id)
                content = _as_text(
                    _first(data, ("message_content", "str_content", "content"), "")
                )
                if not content:
                    content = _as_text(_first(data, ("compress_content",), ""))
                origin = _as_text(_first(data, ("origin_source",), ""))
                packed = _as_text(_first(data, ("packed_info_data", "packed_info"), ""))
                raw_sender = _first(data, ("real_sender_id", "sender_id", "is_sender"))
                numeric_sender = _as_int(raw_sender, -1)
                if numeric_sender > 0:
                    raw_sender = self._sender_name_map(_path, connection).get(
                        numeric_sender, raw_sender
                    )
                is_send = _first(
                    data,
                    ("is_send", "is_sender", "computed_is_send", "isSend", "computedIsSend"),
                )
                hint = sender_hint(content, origin, packed)
                sender_id, sender_name = self._resolve_sender(
                    conversation, raw_sender, hint, is_send
                )
                messages.append(
                    Message(
                        sort_key=(timestamp, sort_seq, local_id + shard_index),
                        conversation_id=conversation.username,
                        timestamp=timestamp,
                        local_id=local_id,
                        message_type=message_type,
                        sender_id=sender_id,
                        sender_name=sender_name,
                        content=content,
                        raw=data,
                    )
                )
        messages.sort()
        yield from messages

    def _resolve_sender(
        self,
        conversation: Conversation,
        raw_sender: object,
        hint: str | None,
        is_send: object = None,
    ) -> tuple[str | int | None, str]:
        send_flag = _as_int(is_send, -1)
        if send_flag == 1:
            return self.account.wxid, "我"
        if hint:
            if hint == self.account.wxid:
                return hint, "我"
            contact = self._contact_by_username.get(hint)
            return hint, contact.display_name if contact else hint
        numeric = _as_int(raw_sender, -1)
        if numeric == 0 or raw_sender in (None, "", "0"):
            return 0, "我"
        if send_flag == 0 and not conversation.is_group:
            return raw_sender, conversation.display_name
        raw_text = _as_text(raw_sender)
        if raw_text == self.account.wxid:
            return raw_text, "我"
        if raw_text in self._contact_by_username:
            return raw_text, self._contact_by_username[raw_text].display_name
        if not conversation.is_group:
            return raw_sender, conversation.display_name
        return raw_sender, f"成员#{raw_text or numeric}"
