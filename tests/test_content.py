from pathlib import Path

from wechat_txt_exporter.content import (
    app_message_type,
    decode_payload,
    human_content,
    media_tokens,
    sender_hint,
)


def test_decodes_zlib_payload():
    import zlib

    assert decode_payload(zlib.compress("你好".encode())) == "你好"


def test_sender_hint_and_group_prefix():
    value = "wxid_member:\n大家好"
    assert sender_hint(value) == "wxid_member"
    assert human_content(1, value) == "大家好"


def test_app_file_message():
    content = "<msg><appmsg><title>报告.pdf</title><type>6</type></appmsg></msg>"
    assert app_message_type(content) == 6
    assert human_content(49, content) == "[文件] 报告.pdf"


def test_media_tokens_extracts_path_md5_and_name():
    content = (
        r'<msg md5="0123456789abcdef0123456789abcdef" filename="photo.jpg">'
        r"C:\Users\tester\photo.jpg</msg>"
    )
    paths, md5s, names = media_tokens(content)
    assert paths == [Path(r"C:\Users\tester\photo.jpg")]
    assert "0123456789abcdef0123456789abcdef" in md5s
    assert "photo.jpg" in names
