"""Shared data models for odoo-ls-mcp."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field


class DiagnosticSeverity(IntEnum):
    ERROR = 1
    WARNING = 2
    INFORMATION = 3
    HINT = 4

    @classmethod
    def label(cls, value: int) -> str:
        try:
            return cls(value).name.lower()
        except ValueError:
            return "unknown"


class Position(BaseModel):
    line: int = Field(ge=0)
    character: int = Field(ge=0)


class Range(BaseModel):
    start: Position
    end: Position


class Diagnostic(BaseModel):
    file: str
    range: Range
    severity: int
    code: str | int | None = None
    source: str | None = None
    message: str

    @property
    def severity_label(self) -> str:
        return DiagnosticSeverity.label(self.severity)


class ParseResult(BaseModel):
    """Result of a --parse mode invocation."""

    workspace: str
    files_analyzed: int
    diagnostics: list[Diagnostic]
    errors: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
