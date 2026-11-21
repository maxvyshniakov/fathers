"""Provider adapters that normalize each harness's transcript format into RequestRecord rows."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..schema import RequestRecord
from .antigravity import AntigravityAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter


class ProviderAdapter(Protocol):
    name: str

    def discover(self, home: Path) -> list[Path]: ...

    def parse_lines(
        self,
        path: Path,
        lines: list[str],
        cursor: dict[str, Any],
        host: str,
    ) -> list[RequestRecord]: ...


def default_adapters() -> list[ProviderAdapter]:
    return [ClaudeCodeAdapter(), CodexAdapter(), AntigravityAdapter()]
