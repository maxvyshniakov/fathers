"""Every tunable of Junkfather with its default, in one table. Seconds unless said otherwise.

Each module imports the names it needs (`from .defaults import GRACE`), so a value is bound when the module loads
and, as a default argument, when the function is defined: changing it here at run time changes nothing. Settings
that override these (fathers#75) must read them at the point of use instead."""
from __future__ import annotations

# Owning
TTL = 7200.0  # `own`, `own-resource` and `adopt` without --ttl
DETACH_WAIT = 15.0  # `own --detach` waits this long for the supervisor to start the command
STARTING_STUCK = 60.0  # a sweep fails an entry still starting after this long ("never started")

# The supervisor
KNOWN_EVERY = 15.0  # between refreshes of an entry's known members
POLL = 0.5  # between checks of the child and the entry
SIGNAL_GRACE = 3.0  # a signalled junkfather gives its command this long before SIGKILL
RETRY_FIRST = 1.0  # before a failed TTL reap is tried again; doubled per failure
RETRY_MAX = 60.0  # the longest wait between two tries

# Reaping
GRACE = 10.0  # between SIGTERM and SIGKILL (`reap --grace`)
PS_TIMEOUT = 10.0  # a ps or lsof call may take this long

# The sweep
SWEEP_INTERVAL = 3600  # the launchd agent's interval (`sweep --plist --interval`), whole seconds
SWEEP_INTERVAL_MIN = 60  # the shortest interval it accepts
KEEP_FINISHED = 7 * 86400.0  # finished entries and their logs are pruned after this long
REAPING_STUCK = 300.0  # an entry in `reaping` this long, whose reaper is gone, runs again

# Claims (owner.py)
CLAIM_WINDOW = 1800.0  # a claim may wait this long for its command (a permission prompt can take a while)
CLAIM_AMBIGUOUS = 60.0  # two agents' matching claims this close together: ambiguous
CLAIM_KEEP = 86400.0  # claims older than this are pruned

# Resources
RESOURCE_TIMEOUT = 20.0  # a resource liveness or listing call may take this long
STOP_TIMEOUT = 120.0  # a stop command may take this long (a simulator shutdown is slow)
SETTLE = 15.0  # `own --kind` waits this long for the resource its command made to show up
CREATE_TIMEOUT = 600.0  # `own --kind` lets its command run this long
