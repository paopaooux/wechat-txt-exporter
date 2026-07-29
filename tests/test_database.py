import hashlib
import hmac
import struct

from wechat_txt_exporter.database import (
    _aes_cbc_decrypt_prefix,
    normalize_sqlcipher4_key,
    quick_validate_sqlcipher_key,
)


def test_windows_bcrypt_aes256_cbc_vector():
    key = bytes.fromhex(
        "603deb1015ca71be2b73aef0857d7781"
        "1f352c073b6108d72d9810a30914dff4"
    )
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    ciphertext = bytes.fromhex("f58c4c04d6e5f1ba779eabfb5f7bfbd6")
    expected = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    assert _aes_cbc_decrypt_prefix(key, iv, ciphertext) == expected


def _sqlcipher4_page(password: bytes) -> tuple[bytes, bytes]:
    salt = bytes(range(16))
    encryption_key = hashlib.pbkdf2_hmac("sha512", password, salt, 256000, 32)
    body = bytes((index * 17) % 256 for index in range(4016))
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", encryption_key, mac_salt, 2, 32)
    mac = hmac.new(mac_key, body + struct.pack("<I", 1), "sha512").digest()
    return salt + body + mac, encryption_key


def test_sqlcipher4_password_and_raw_key_validation(tmp_path):
    password = bytes(range(32))
    page, encryption_key = _sqlcipher4_page(password)
    path = tmp_path / "encrypted.db"
    path.write_bytes(page)

    assert quick_validate_sqlcipher_key(path, password)
    assert quick_validate_sqlcipher_key(path, encryption_key, include_passphrase_kdf=False)
    assert normalize_sqlcipher4_key(path, password) == encryption_key
    assert normalize_sqlcipher4_key(path, encryption_key) == encryption_key
    assert not quick_validate_sqlcipher_key(path, b"x" * 32)


def test_database_derives_a_distinct_key_for_each_database_salt(tmp_path):
    from wechat_txt_exporter.database import EncryptedDatabase

    master = bytes(range(32))
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    first.write_bytes(b"a" * 16 + b"x")
    second.write_bytes(b"b" * 16 + b"x")

    first_keys = EncryptedDatabase(first, master)._key_candidates()
    second_keys = EncryptedDatabase(second, master)._key_candidates()
    assert first_keys[0] == ("raw", master)
    assert first_keys[1][1] != second_keys[1][1]
