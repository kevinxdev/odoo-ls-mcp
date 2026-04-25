from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))
