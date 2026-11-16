"""The state graph of a TileFoundry round (perf or new-op) and the checks on its edges.

`topa round transition` moves a round along the graph; `topa round start` puts it in REQ.
A rule is added by attaching a check from `topa.gate` to an edge in `CHECKS`.
"""

from __future__ import annotations

from typing import Callable

from topa import gate

# Each kind's forward edges, in the order of its path.
FORWARD = {
    "perf": {"REQ": {"HIR"}, "HIR": {"FLOOR"}, "FLOOR": {"IMPL"}, "IMPL": {"CLEAN"}, "CLEAN": {"REVIEW"},
             "REVIEW": {"FINAL", "PR"}, "FINAL": {"PR"}, "PR": {"CLEAN", "CI"}, "CI": {"DONE", "CLEAN"}},
    "new-op": {"REQ": {"HIR"}, "HIR": {"FLOOR"}, "FLOOR": {"OP"}, "OP": {"IMPL"}, "IMPL": {"CLEAN"},
               "CLEAN": {"REVIEW"}, "REVIEW": {"FINAL", "PR"}, "FINAL": {"PR"}, "PR": {"CLEAN", "CI"},
               "CI": {"DONE", "CLEAN"}},
}
# A failed check returns to the state that owns the fix: one of these, at or before the current state.
REWORK = ("HIR", "FLOOR", "OP", "IMPL", "CLEAN")
STATES = ("REQ", "HIR", "FLOOR", "OP", "IMPL", "CLEAN", "REVIEW", "FINAL", "PR", "CI", "DONE", "STOPPED")


def _table(forward: dict[str, set[str]]) -> dict[str, frozenset[str]]:
    """Every active state also repeats itself, returns to REQ or a rework state behind it, or stops."""
    order = list(forward)
    return {state: frozenset(forward[state] | {s for s in order[:i + 1] if s in REWORK} | {state, "REQ", "STOPPED"})
            for i, state in enumerate(order)}


TRANSITIONS = {kind: _table(forward) for kind, forward in FORWARD.items()}

# The `topa gate` checks a move must pass; `*` is any state.
CHECKS: dict[tuple[str, str], tuple[Callable[[gate.Evidence], None], ...]] = {
    ("HIR", "FLOOR"): (gate.check_reference,),
    ("FLOOR", "OP"): (gate.check_floor,),
    ("FLOOR", "IMPL"): (gate.check_floor,),
    ("OP", "IMPL"): (gate.check_op,),
    ("IMPL", "CLEAN"): (gate.check_implementation,),
    ("REVIEW", "PR"): (gate.check_published,),
    ("*", "STOPPED"): (gate.check_stop_records,),
}


def path(kind: str) -> str:
    return " -> ".join([*FORWARD[kind], "DONE"])


def checks(source: str, target: str) -> tuple[Callable[[gate.Evidence], None], ...]:
    return CHECKS.get((source, target), ()) + CHECKS.get(("*", target), ())
