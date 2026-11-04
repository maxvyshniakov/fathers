#!/usr/bin/env python3
"""Serialize stamp publication and rollback of one verification run's stamp."""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def replace(path, contents):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(contents)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def owned(row, token):
    fields = row.split()
    return len(fields) == 4 and fields[3] == token


def mutate(directory, operation, arguments):
    ledger = directory / "verified-trees"
    token = arguments[3] if operation == "add" and len(arguments) == 4 else arguments[0] if operation != "add" else None
    if token is not None and not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid verification stamp token")
    journal = directory / ("verified-tree-transaction-" + token + ".json") if token else None
    with (directory / "verified-trees.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = ledger.read_text().splitlines() if ledger.exists() else []
        if operation == "add":
            if len(arguments) not in (3, 4):
                raise ValueError("add requires tree, commit, timestamp and optional token")
            record = " ".join(arguments)
            if journal:
                if journal.exists() or any(owned(row, token) for row in rows):
                    raise ValueError("verification stamp token already used")
                # Record evictions BEFORE publication, so interruption after mv
                # can remove only this run without losing its earlier stamps.
                replace(journal, json.dumps({"before": rows, "pruned": rows[:-49]}) + "\n")
            replace(ledger, "\n".join(rows[-49:] + [record]) + "\n")
        elif operation == "abort":
            published = any(owned(row, token) for row in rows)
            kept = [row for row in rows if not owned(row, token)]
            if published and journal and journal.exists():
                transaction = json.loads(journal.read_text())
                before, pruned = transaction["before"], transaction["pruned"]
                # Place evictions before the first surviving original successor.
                # This also preserves order if interleaved writers both abort.
                index = next((kept.index(row) for row in before[len(pruned):] if row in kept), 0)
                # Preserve duplicates: each row evicted by this publication is
                # restored once. Atomic token removal makes retry idempotent.
                kept[index:index] = pruned
            if kept != rows:
                replace(ledger, "\n".join(kept) + ("\n" if kept else ""))
            if journal:
                journal.unlink(missing_ok=True)
        elif operation == "complete":
            if journal:
                journal.unlink(missing_ok=True)
        else:
            raise ValueError("unknown stamp operation")


def main():
    try:
        directory, operation, *arguments = sys.argv[1:]
        mutate(Path(directory), operation, arguments)
    except (OSError, ValueError, IndexError) as error:
        print(f"verified-tree ledger: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
