"""The formats of a round's records: a measurement of `topa round measure`, and the trace of `topa round trace`.

These classes are the definition. topa writes a record only through them, so a record that does
not fit its format is refused before it reaches a file. `schemas/*.json` hold the same formats as
JSON Schema, for a reader outside topa such as the perf dashboard: `python -m topa.schema`
rewrites them, and a test holds them equal to the classes.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

TRACE = 2
SCHEMAS = Path(__file__).parent / "schemas"


def _aware(value: str) -> str:
    try:
        instant = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("not an ISO 8601 time") from None
    if instant.tzinfo is None:
        raise ValueError("the time has no timezone")
    return value


# The text is kept as written; only its form is checked.
Timestamp = Annotated[str, AfterValidator(_aware), Field(json_schema_extra={"format": "date-time"})]
Title = Annotated[str, Field(min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Comparator(Record):
    """Another implementation's time on a case, taken in the same benchmark run."""

    us: float
    timing: str | None
    l2_flush: str | None
    no_ratio: bool
    unverified: str | None


class Case(Record):
    """One benchmark case in one measurement; only a measured case has a time."""

    case_id: str
    status: Literal["measured", "failed", "unavailable", "skipped"]
    us: float | None
    timing: str | None
    l2_flush: str | None
    bytes: int | None
    comparators: dict[str, Comparator]

    @model_validator(mode="after")
    def _timed_when_measured(self) -> Case:
        if (self.status == "measured") != (self.us is not None):
            raise ValueError("a measured case has `us`, and no other case has")
        return self


class OpCase(Case):
    """A case of a measurement record, which can hold the cases of several ops."""

    op: str


class Measurement(Record):
    """One line of `measures.jsonl`: what one `topa round measure` timed."""

    ts: Timestamp
    title: Title
    commit: str
    base: bool
    xml: str
    gpu: str | None
    busy_card: bool
    target: str | None
    hbm_bandwidth: float
    cases: list[OpCase]


class Phase(Record):
    ts: Timestamp
    t: Literal["phase"]
    to: str
    reason: str | None


class Foundry(Record):
    """A TileFoundry analysis: the reference check, or the floor of one case."""

    ts: Timestamp
    t: Literal["foundry"]
    kind: Literal["reference", "floor"]
    case_id: str | None
    command: str | None
    ideal_ns: float | None
    bound_by: str | None
    ts_source: Literal["report-file"]


class Measure(Record):
    """A measurement in a trace; the DRAM floor of a case is its `bytes` over `hbm_bandwidth`."""

    ts: Timestamp
    t: Literal["measure"]
    title: Title
    commit: str
    base: bool
    busy_card: bool
    hbm_bandwidth: float
    cases: list[Case]


class Think(Record):
    ts: Timestamp
    t: Literal["think"]
    text: str


class Tool(Record):
    ts: Timestamp
    t: Literal["tool"]
    name: str | None
    arg: str


class PublishedPhase(Record):
    ts: Timestamp
    t: Literal["phase"]
    to: str


class PublishedFoundry(Record):
    ts: Timestamp
    t: Literal["foundry"]
    kind: Literal["reference", "floor"]
    case_id: str | None
    ideal_ns: float | None
    bound_by: str | None
    ts_source: Literal["report-file"]


class Source(Record):
    """Where the round's task came from; `ref` is the wiki page's or the issue's URL."""

    kind: Literal["wiki", "issue", "none"]
    ref: str | None


class Activity(Record):
    """What a published trace leaves out, counted."""

    think_n: int
    tool_n: int
    tokens: int | None


class _Head(Record):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: Literal[2] = Field(alias="schema")
    round: str
    # The issue's number, or the op's name for a round from the wiki queue.
    item: int | str
    # A trace holds one op.
    op: str
    source: Source
    method: str
    model: str | None
    effort: str | None
    gpu: str | None
    base: str | None
    pr: str | None


class Trace(_Head):
    """A round's full trace: every entry in time order, the agent's session included."""

    tokens: int | None
    entries: list[Annotated[Phase | Foundry | Measure | Think | Tool, Field(discriminator="t")]]


class PublishedTrace(_Head):
    """What leaves the machine: no session, and none of the agent's text except a measurement's title."""

    activity: Activity
    entries: list[Annotated[PublishedPhase | PublishedFoundry | Measure, Field(discriminator="t")]]


FILES: dict[str, type[Record]] = {
    "measurement.schema.json": Measurement,
    f"trace.v{TRACE}.schema.json": Trace,
    f"published-trace.v{TRACE}.schema.json": PublishedTrace,
}


def checked(form: type[Record], value: Any) -> dict[str, Any]:
    """*value* as a record of *form*, in the form's field order; a value that does not fit refuses."""
    try:
        return form.model_validate(value).model_dump(mode="json", by_alias=True)
    except ValidationError as error:
        found = "\n".join(f"  {'.'.join(map(str, e['loc'])) or '.'}: {e['msg']}" for e in error.errors())
        raise SystemExit(f"not a {form.__name__} record:\n{found}") from None


def json_schema(form: type[Record]) -> str:
    return json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                       **form.model_json_schema(by_alias=True)}, indent=2) + "\n"


if __name__ == "__main__":
    SCHEMAS.mkdir(exist_ok=True)
    for name, form in FILES.items():
        (SCHEMAS / name).write_text(json_schema(form), encoding="utf-8")
