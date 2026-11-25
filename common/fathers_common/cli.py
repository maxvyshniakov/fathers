"""Usage errors based on argparse's own option parsing."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import sys
from typing import Any, NoReturn, TypeVar, cast, overload


_Namespace = TypeVar("_Namespace")


class UsageParser(argparse.ArgumentParser):
    """Name an unknown option even when argparse fails before returning its unconsumed arguments."""

    def __init__(self, *args: Any, parent: UsageParser | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._parent = parent
        self._arguments: list[str] = []
        self._unknown_options: list[str] = []

    @overload
    def parse_known_args(self, args: Sequence[str] | None = None,
                         namespace: None = None) -> tuple[argparse.Namespace, list[str]]: ...

    @overload
    def parse_known_args(self, args: Sequence[str] | None, namespace: _Namespace) -> tuple[_Namespace, list[str]]: ...

    @overload
    def parse_known_args(self, *, namespace: _Namespace) -> tuple[_Namespace, list[str]]: ...

    def parse_known_args(self, args: Sequence[str] | None = None,
                         namespace: Any = None) -> tuple[Any, list[str]]:
        self._unknown_options.clear()
        self._arguments = list(sys.argv[1:] if args is None else args)
        try:
            if self._parent is not None:
                # Argparse passes just the words after the selected command to its parser.
                self._parent._reject_unknown_before_command(len(self._arguments))
            return super().parse_known_args(self._arguments, namespace)
        finally:
            self._arguments = []

    def _reject_unknown_before_command(self, command_arguments: int) -> None:
        stop = len(self._arguments) - command_arguments - 1
        for word in self._arguments[:max(0, stop)]:
            if word in self._unknown_options:
                self.unknown_option(word)

    def _parse_optional(self, arg_string: str) -> Any:
        # Python 3.12 and earlier return one candidate tuple; 3.13 and later return a list of candidates.
        parsed: Any = super()._parse_optional(arg_string)
        if parsed and (parsed[0][0] if isinstance(parsed, list) else parsed[0]) is None:
            self._unknown_options.append(arg_string)
        return cast(Any, parsed)

    def error(self, message: str) -> NoReturn:
        if self._unknown_options:
            self.unknown_option(self._unknown_options[0])
        self.usage_error(message)

    def rejects_option(self, word: str) -> bool:
        return word in self._unknown_options

    def unknown_option(self, word: str) -> NoReturn:
        self.usage_error(f"unknown option {self.option_name(word.partition('=')[0])}")

    def option_name(self, name: str) -> str:
        return name

    def usage_error(self, message: str) -> NoReturn:
        raise NotImplementedError


def format_top_help(
    usage: str | Sequence[str],
    summary: str,
    groups: Sequence[tuple[str, Sequence[tuple[str, str]]]],
    options: Sequence[tuple[str, str]],
    footer: str | Sequence[str] = (),
) -> str:
    """Format a compact, single-screen top-level --help with aligned grouped commands and options."""
    usage_lines = [usage] if isinstance(usage, str) else list(usage)
    lines: list[str] = [
        f"usage: {line}" if index == 0 else f"       {line}"
        for index, line in enumerate(usage_lines)
    ]
    if summary:
        lines.extend(["", *summary.splitlines()])
    labels = [name for _, items in groups for name, _ in items] + [name for name, _ in options]
    width = max((len(label) for label in labels), default=0) + 2
    for title, items in groups:
        if not items:
            continue
        lines.extend(["", f"{title}:"])
        for name, desc in items:
            lines.append(f"  {name:<{width}}{desc}")
    if options:
        lines.extend(["", "options:"])
        for name, desc in options:
            lines.append(f"  {name:<{width}}{desc}")
    footer_lines = [footer] if isinstance(footer, str) else list(footer)
    if footer_lines:
        lines.extend(["", *footer_lines])
    return "\n".join(lines) + "\n"
