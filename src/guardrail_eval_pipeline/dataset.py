"""The labelled evaluation set, loaded and checked.

A dataset is a claim about what correct means, so a malformed one has to be an error
rather than a quietly smaller run. Loading validates every case, because the failure this
guards against is silent: a mistyped `expect`, or a case with no expected answer, makes a
system look better by removing the question it would have failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from guardrail_eval_pipeline.config import ConfigError

# What a case asserts should happen. Not three outcomes but four, because refusing is a
# correct behaviour and two different parties can do it.
BEHAVIOURS = ("answered", "target_refused", "blocked")


@dataclass(frozen=True)
class Case:
    """One labelled question.

    `expected_answer` is written from the target's source documents, never from its
    output. `must_mention` are the fragments a deterministic check can look for, which is
    the cheapest signal available and the one that needs no model.

    `grounded` is False where the target answers from records rather than passages: there
    is nothing to ground against, and the metrics that need contexts are unavailable
    rather than zero.
    """

    id: str
    question: str
    principal: str
    expect: str
    expected_answer: str | None = None
    must_mention: tuple[str, ...] = ()
    source: str | None = None
    grounded: bool = True
    hard: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class Probe:
    """A fixed answer that never reaches the target, used to test the judge.

    A judge only ever shown real answers has not been tested. The failure mode is
    agreeing with anything that sounds confident, so at least one of these is wrong and
    says so confidently.
    """

    id: str
    question: str
    answer: str
    contexts: tuple[str, ...] = ()
    expect_judge: str = "pass"
    notes: str | None = None


@dataclass(frozen=True)
class Dataset:
    target: str
    cases: tuple[Case, ...] = ()
    probes: tuple[Probe, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def for_behaviour(self, behaviour: str) -> tuple[Case, ...]:
        return tuple(case for case in self.cases if case.expect == behaviour)


def load(path: str | Path) -> Dataset:
    """Read an evaluation set. Raises `ConfigError` on anything malformed."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"no evaluation set at {path}")

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict) or not data.get("target"):
        raise ConfigError(f"{path}: needs a `target` and a mapping at the top level")

    cases, seen = [], set()
    for entry in data.get("cases") or []:
        case_id = entry.get("id")
        if not case_id:
            raise ConfigError(f"{path}: every case needs an id")
        if case_id in seen:
            # Ids name a case in the report, so a duplicate makes one result overwrite
            # another and the count still looks right.
            raise ConfigError(f"{path}: duplicate case id {case_id!r}")
        seen.add(case_id)

        if entry.get("expect") not in BEHAVIOURS:
            raise ConfigError(
                f"{path}: case {case_id!r} has expect={entry.get('expect')!r}, "
                f"which is not one of {', '.join(BEHAVIOURS)}"
            )
        if entry["expect"] == "answered" and not entry.get("expected_answer"):
            # An answered case with no expected answer cannot be marked, and would quietly
            # pass every metric that compares against it.
            raise ConfigError(f"{path}: case {case_id!r} expects an answer but gives none")

        cases.append(Case(
            id=case_id,
            question=entry["question"],
            principal=entry["principal"],
            expect=entry["expect"],
            expected_answer=entry.get("expected_answer"),
            must_mention=tuple(entry.get("must_mention") or ()),
            source=entry.get("source"),
            grounded=entry.get("grounded", True),
            hard=entry.get("hard"),
            notes=entry.get("notes"),
        ))

    probes = []
    for entry in data.get("probes") or []:
        if entry.get("expect_judge") not in ("pass", "fail"):
            raise ConfigError(
                f"{path}: probe {entry.get('id')!r} must expect the judge to pass or fail"
            )
        probes.append(Probe(
            id=entry["id"], question=entry["question"], answer=entry["answer"],
            contexts=tuple(entry.get("contexts") or ()),
            expect_judge=entry["expect_judge"], notes=entry.get("notes"),
        ))

    if len(cases) < 15:
        # The spec's own floor. A set that shrinks below it has lost coverage, and the
        # run that notices should be the one that fails rather than the one that reports.
        raise ConfigError(f"{path}: {len(cases)} cases, at least 15 required")

    return Dataset(target=data["target"], cases=tuple(cases), probes=tuple(probes), raw=data)
