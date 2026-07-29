from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from .content import media_tokens


class MediaResolver:
    def __init__(self, account_dir: Path):
        self.account_dir = account_dir
        self._built = False
        self._by_name: dict[str, list[Path]] = defaultdict(list)
        self._by_token: dict[str, list[Path]] = defaultdict(list)

    def _build_index(self) -> None:
        if self._built:
            return
        roots = [self.account_dir / "msg", self.account_dir / "cache"]
        for root in roots:
            if not root.is_dir():
                continue
            for current, _directories, files in os.walk(root):
                current_path = Path(current)
                parent_tokens = {part.lower() for part in current_path.parts if len(part) == 32}
                for filename in files:
                    path = (current_path / filename).resolve()
                    self._by_name[filename.lower()].append(path)
                    stem = path.stem.lower()
                    if len(stem) == 32:
                        self._by_token[stem].append(path)
                    for token in parent_tokens:
                        self._by_token[token].append(path)
        self._built = True

    def resolve(self, content: str) -> Path | None:
        direct_paths, md5s, names = media_tokens(content)
        for path in direct_paths:
            if path.is_file():
                return path.resolve()
        if not md5s and not names:
            return None
        self._build_index()
        for name in names:
            paths = self._by_name.get(name)
            if paths:
                return paths[0]
        for token in md5s:
            paths = self._by_token.get(token)
            if paths:
                return paths[0]
        return None
