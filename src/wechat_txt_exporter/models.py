from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Account:
    wxid: str
    data_dir: Path
    login_dir: Path | None
    suffix: str = ""

    @property
    def label(self) -> str:
        return self.wxid


@dataclass(slots=True)
class Contact:
    username: str
    display_name: str
    numeric_id: int | None = None
    alias: str = ""
    is_group: bool = False


@dataclass(slots=True)
class Conversation:
    username: str
    display_name: str
    table_name: str
    is_group: bool = False
    shard_hint: str | None = None


@dataclass(order=True, slots=True)
class Message:
    sort_key: tuple[int, int, int]
    conversation_id: str = field(compare=False)
    timestamp: int = field(compare=False)
    local_id: int = field(compare=False)
    message_type: int = field(compare=False)
    sender_id: str | int | None = field(compare=False)
    sender_name: str = field(compare=False)
    content: str = field(compare=False)
    media_path: Path | None = field(default=None, compare=False)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(slots=True)
class ExportResult:
    output_dir: Path
    succeeded: int = 0
    skipped: int = 0
    unchanged: int = 0
    failed: int = 0
    messages: int = 0
    voices_transcribed: int = 0
    voices_cached: int = 0
    voices_failed: int = 0
    cancelled: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)
