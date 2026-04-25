from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class CachedDiagnostic:
    uri: str
    range_start_line: int
    range_start_char: int
    range_end_line: int
    range_end_char: int
    severity: int | None
    message: str
    source: str | None


class DiagnosticsCache:
    """Thread-safe cache of diagnostics keyed by file URI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, list[CachedDiagnostic]] = {}

    def update(self, uri: str, diagnostics: list[dict]) -> None:
        parsed: list[CachedDiagnostic] = []
        for d in diagnostics:
            r = d.get("range", {})
            start = r.get("start", {})
            end = r.get("end", {})
            parsed.append(
                CachedDiagnostic(
                    uri=uri,
                    range_start_line=start.get("line", 0),
                    range_start_char=start.get("character", 0),
                    range_end_line=end.get("line", 0),
                    range_end_char=end.get("character", 0),
                    severity=d.get("severity"),
                    message=d.get("message", ""),
                    source=d.get("source"),
                )
            )
        with self._lock:
            self._data[uri] = parsed

    def get(self, uri: str) -> list[CachedDiagnostic]:
        with self._lock:
            return list(self._data.get(uri, []))

    def get_all(self) -> dict[str, list[CachedDiagnostic]]:
        with self._lock:
            return {uri: list(diags) for uri, diags in self._data.items()}

    def clear(self, uri: str | None = None) -> None:
        with self._lock:
            if uri is None:
                self._data.clear()
            else:
                self._data.pop(uri, None)
