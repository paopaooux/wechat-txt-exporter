from __future__ import annotations

import html
import re
import zlib
from pathlib import Path
from xml.etree import ElementTree

ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\x00\r\n<>|\"]+")
USER_ID_PATTERN = r"(?:wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+@(?:chatroom|openim))"
WXID_RE = re.compile(USER_ID_PATTERN)
MD5_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


def decode_payload(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not isinstance(value, bytes):
        return str(value)

    candidates: list[bytes] = []
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
        try:
            candidates.append(zlib.decompress(value, wbits))
        except zlib.error:
            pass
    candidates.append(value)
    for candidate in candidates:
        for encoding in ("utf-8", "utf-16-le", "gb18030"):
            try:
                text = candidate.decode(encoding).replace("\x00", "")
            except UnicodeDecodeError:
                continue
            printable = sum(character.isprintable() or character in "\r\n\t" for character in text)
            if text and printable / len(text) > 0.8:
                return text
    return ""


def extract_xml_text(content: str, *paths: str) -> str:
    start = content.find("<")
    if start < 0:
        return ""
    try:
        root = ElementTree.fromstring(content[start:])
    except ElementTree.ParseError:
        return ""
    for path in paths:
        if "/@" in path:
            node_path, attribute = path.rsplit("/@", 1)
            node = root.find(node_path)
            if node is not None and node.get(attribute):
                return html.unescape(node.get(attribute, "")).strip()
        else:
            node = root.find(path)
            if node is not None and node.text:
                return html.unescape(node.text).strip()
    return ""


def sender_hint(content: str, origin_source: str = "", packed_info: str = "") -> str | None:
    prefix = re.match(rf"^({USER_ID_PATTERN}):\s*\n", content)
    if prefix:
        return prefix.group(1)
    for source in (origin_source, packed_info):
        match = WXID_RE.search(source)
        if match:
            return match.group(0)
    return None


def strip_group_sender_prefix(content: str) -> str:
    return re.sub(rf"^{USER_ID_PATTERN}:\s*\n", "", content, count=1)


def media_tokens(content: str) -> tuple[list[Path], set[str], set[str]]:
    paths = [Path(match.group(0).rstrip(" .")) for match in ABSOLUTE_PATH_RE.finditer(content)]
    md5s = {value.lower() for value in MD5_RE.findall(content)}
    names: set[str] = set()
    for pattern in (
        r'(?i)(?:filename|file_name|title)\s*=\s*["\']([^"\']+)["\']',
        r"(?is)<(?:filename|file_name|title)>([^<]+)</(?:filename|file_name|title)>",
    ):
        for match in re.finditer(pattern, content):
            name = html.unescape(match.group(1)).strip()
            if name and len(name) <= 255:
                names.add(Path(name).name.lower())
    return paths, md5s, names


def app_message_type(content: str) -> int | None:
    value = extract_xml_text(content, ".//appmsg/type")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def human_content(message_type: int, content: str, media_path: Path | None = None) -> str:
    content = strip_group_sender_prefix(content).strip()
    base_type = message_type & 0xFFFF
    if base_type == 1:
        return content or "[空文本消息]"
    labels = {
        3: "图片",
        34: "语音",
        43: "视频",
        47: "表情",
        48: "位置",
        50: "通话",
        42: "名片",
        62: "小视频",
        10000: "系统消息",
        10002: "撤回消息",
    }
    if base_type == 49:
        subtype = app_message_type(content)
        labels_49 = {
            3: "音乐",
            4: "视频链接",
            5: "链接",
            6: "文件",
            8: "表情",
            19: "聊天记录",
            33: "小程序",
            2000: "转账",
            2001: "红包",
        }
        label = labels_49.get(subtype, "应用消息")
        detail = extract_xml_text(content, ".//appmsg/title", ".//appmsg/des", ".//title")
    else:
        label = labels.get(base_type, f"未知消息 type={message_type}")
        detail = ""
        if base_type in {48, 10000}:
            detail = extract_xml_text(content, ".//location/@label", ".//content") or content
    result = f"[{label}]"
    if detail and not detail.lstrip().startswith("<"):
        result += f" {detail.strip()}"
    if media_path:
        result += f" {media_path}"
    return result
