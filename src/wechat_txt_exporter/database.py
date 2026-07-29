from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import os
import re
import struct
from pathlib import Path
from typing import Any, Iterator

from .errors import DatabaseError

IDENTIFIER_RE = re.compile(r"^[\w@.-]+$", re.UNICODE)


def _aes_cbc_decrypt_prefix(key: bytes, iv: bytes, ciphertext: bytes) -> bytes | None:
    if os.name != "nt" or len(key) != 32 or len(iv) != 16 or len(ciphertext) % 16:
        return None
    bcrypt = ctypes.WinDLL("bcrypt.dll")
    handle = ctypes.c_void_p()
    status = bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(handle), ctypes.c_wchar_p("AES"), None, 0
    )
    if status < 0:
        return None
    key_handle = ctypes.c_void_p()
    try:
        chaining = ctypes.create_unicode_buffer("ChainingModeCBC")
        status = bcrypt.BCryptSetProperty(
            handle,
            ctypes.c_wchar_p("ChainingMode"),
            ctypes.cast(chaining, ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.sizeof(chaining),
            0,
        )
        if status < 0:
            return None
        object_length = ctypes.c_ulong()
        copied = ctypes.c_ulong()
        status = bcrypt.BCryptGetProperty(
            handle,
            ctypes.c_wchar_p("ObjectLength"),
            ctypes.byref(object_length),
            ctypes.sizeof(object_length),
            ctypes.byref(copied),
            0,
        )
        if status < 0:
            return None
        key_object = ctypes.create_string_buffer(object_length.value)
        key_buffer = ctypes.create_string_buffer(key)
        status = bcrypt.BCryptGenerateSymmetricKey(
            handle,
            ctypes.byref(key_handle),
            key_object,
            object_length.value,
            key_buffer,
            len(key),
            0,
        )
        if status < 0:
            return None
        iv_buffer = ctypes.create_string_buffer(iv)
        input_buffer = ctypes.create_string_buffer(ciphertext)
        output_buffer = ctypes.create_string_buffer(len(ciphertext))
        output_length = ctypes.c_ulong()
        status = bcrypt.BCryptDecrypt(
            key_handle,
            input_buffer,
            len(ciphertext),
            None,
            iv_buffer,
            len(iv),
            output_buffer,
            len(ciphertext),
            ctypes.byref(output_length),
            0,
        )
        if status < 0:
            return None
        return output_buffer.raw[: output_length.value]
    finally:
        if key_handle:
            bcrypt.BCryptDestroyKey(key_handle)
        bcrypt.BCryptCloseAlgorithmProvider(handle, 0)


def normalize_sqlcipher4_key(
    path: Path, candidate: bytes, include_passphrase_kdf: bool = True
) -> bytes | None:
    """Return the verified post-PBKDF2 encryption key for a candidate."""
    if len(candidate) != 32:
        return None
    try:
        with path.open("rb") as handle:
            page = handle.read(4096)
    except OSError:
        return None
    if len(page) < 4096 or page.startswith(b"SQLite format 3\x00"):
        return None
    salt = page[:16]
    derived_candidates = [candidate]
    if include_passphrase_kdf:
        derived_candidates.append(
            hashlib.pbkdf2_hmac("sha512", candidate, salt, 256000, 32)
        )
    mac_salt = bytes(value ^ 0x3A for value in salt)
    expected = page[4032:4096]
    for encryption_key in derived_candidates:
        mac_key = hashlib.pbkdf2_hmac("sha512", encryption_key, mac_salt, 2, 32)
        actual = hmac.new(
            mac_key, page[16:4032] + struct.pack("<I", 1), "sha512"
        ).digest()
        if hmac.compare_digest(actual, expected):
            return encryption_key
    return None


def quick_validate_sqlcipher_key(
    path: Path, candidate: bytes, include_passphrase_kdf: bool = True
) -> bool:
    """Verify a password or raw encryption key against SQLCipher 4 page 1."""
    return normalize_sqlcipher4_key(path, candidate, include_passphrase_kdf) is not None


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _import_sqlcipher():
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except ImportError as exc:
        raise DatabaseError(
            "缺少 SQLCipher 运行库，请通过 run.bat 安装项目依赖。"
        ) from exc
    return sqlcipher


class EncryptedDatabase:
    """A read-only SQLCipher connection with Weixin-compatible profile fallback."""

    PROFILES = (
        ("sqlcipher-4", ()),
        ("sqlcipher-3", ("PRAGMA cipher_compatibility = 3",)),
        (
            "wechat-sha1",
            (
                "PRAGMA cipher_page_size = 4096",
                "PRAGMA kdf_iter = 64000",
                "PRAGMA cipher_hmac_algorithm = HMAC_SHA1",
                "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA1",
            ),
        ),
    )

    def __init__(self, path: Path, key: bytes):
        self.path = path
        self.key = key
        self.connection: Any | None = None
        self.profile: str | None = None

    def _key_candidates(self) -> tuple[tuple[str, bytes], ...]:
        """Return raw key plus the per-database SQLCipher 4 derived key.

        The hook returns the account master password. Every database has its
        own 16-byte salt, so reusing contact.db's post-KDF key for session.db
        is invalid even though both databases belong to the same account.
        """
        try:
            with self.path.open("rb") as handle:
                salt = handle.read(16)
        except OSError:
            return (("raw", self.key),)
        if len(salt) != 16:
            return (("raw", self.key),)
        derived = hashlib.pbkdf2_hmac("sha512", self.key, salt, 256000, 32)
        if derived == self.key:
            return (("raw", self.key),)
        return (("raw", self.key), ("master-password", derived))

    def open(self):
        if self.connection is not None:
            return self.connection
        if not self.path.is_file():
            raise DatabaseError(f"数据库不存在：{self.path}")
        sqlcipher = _import_sqlcipher()
        failures: list[str] = []
        for key_mode, raw_key in self._key_candidates():
            for profile_name, pragmas in self.PROFILES:
                connection = None
                try:
                    uri = self.path.resolve().as_uri() + "?mode=ro"
                    try:
                        connection = sqlcipher.connect(uri, uri=True, timeout=5)
                    except TypeError:
                        connection = sqlcipher.connect(str(self.path), timeout=5)
                    connection.row_factory = sqlcipher.Row
                    connection.execute("PRAGMA query_only = ON")
                    connection.execute(f"PRAGMA key = \"x'{raw_key.hex()}'\"")
                    for pragma in pragmas:
                        connection.execute(pragma)
                    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                    self.connection = connection
                    self.profile = f"{profile_name}/{key_mode}"
                    return connection
                except Exception as exc:  # sqlcipher uses implementation-specific exception classes
                    failures.append(f"{profile_name}/{key_mode}: {exc}")
                    if connection is not None:
                        with contextlib.suppress(Exception):
                            connection.close()
        raise DatabaseError(
            f"无法解密数据库 {self.path.name}；密钥无效或加密参数不兼容。"
            + (f" 诊断：{' | '.join(failures)}" if failures else "")
        )

    def close(self) -> None:
        if self.connection is not None:
            with contextlib.suppress(Exception):
                self.connection.close()
            self.connection = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def table_columns(connection: Any, table_name: str) -> list[str]:
    rows = connection.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
    return [str(row[1]) for row in rows]


def user_tables(connection: Any) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def tables_with_columns(connection: Any) -> Iterator[tuple[str, list[str]]]:
    for table in user_tables(connection):
        yield table, table_columns(connection, table)
