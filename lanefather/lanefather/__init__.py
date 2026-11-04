"""lanefather: the lane workflow for agents working in parallel on one repository.

Lanes and their path claims, the ranked queue, the receipts of each lane's checks, the landing lock, the post-merge
smoke, the independent review, the batch gate and the promotion of dev and main. A project names its own commands in
`.lanefather.toml` (see lanefather.config); the machine's slots come from slotfather.
"""
from __future__ import annotations

__version__ = "0.1.0"
