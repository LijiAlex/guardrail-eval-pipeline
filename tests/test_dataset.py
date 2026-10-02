"""The evaluation set, and the validation that stops it rotting quietly.

A dataset is a claim about what correct means. The failures worth guarding against are
silent ones: a case that cannot be marked, a duplicate id that makes one result overwrite
another, a set that shrinks below the floor. Each of those makes a system look better.
"""

from __future__ import annotations

import pytest

from guardrail_eval_pipeline.config import ConfigError
from guardrail_eval_pipeline.dataset import load

SET = "evaluation/medibot.yaml"


def write(tmp_path, body: str):
    path = tmp_path / "set.yaml"
    path.write_text(body)
    return path


# --- the shipped set ----------------------------------------------------------
def test_the_set_meets_the_floor():
    assert len(load(SET).cases) >= 15


def test_it_covers_all_three_behaviours():
    """A set of only answerable questions measures half a system: refusing correctly and
    being blocked are behaviours too."""
    dataset = load(SET)
    for behaviour in ("answered", "target_refused", "blocked"):
        assert dataset.for_behaviour(behaviour), f"nothing expects {behaviour}"


def test_it_contains_deliberately_hard_cases():
    """Faithfulness and relevancy look fine on easy questions. The set has to include ones
    where they separate a good pipeline from a lucky one."""
    assert sum(1 for case in load(SET).cases if case.hard) >= 3


def test_it_covers_a_target_branch_with_no_passages():
    """The analytical branch answers from records, so the context metrics are unavailable
    rather than zero — and a set without one would never exercise that distinction."""
    assert any(case.grounded is False for case in load(SET).cases)


def test_the_same_question_appears_under_two_roles():
    """The sharpest test of access control the set can make: identical wording, one role
    entitled and one not, and the expected behaviours differ."""
    cases = {case.id: case for case in load(SET).cases}
    assert cases["mri-code"].question == cases["nurse-asks-billing"].question
    assert cases["mri-code"].expect == "answered"
    assert cases["nurse-asks-billing"].expect == "target_refused"


def test_the_probes_can_catch_a_flattering_judge():
    """A judge shown only real answers has not been tested. At least one probe has to be
    wrong and confident, and at least one has to be a correct refusal — a judge that marks
    that down is punishing the system for working."""
    probes = load(SET).probes
    assert any(probe.expect_judge == "fail" for probe in probes)
    assert any(probe.expect_judge == "pass" for probe in probes)


def test_every_answered_case_names_the_document_it_came_from():
    """Ground truth has to be traceable. scripts/verify_labels.py checks the fragments
    against that document; this checks one was named at all."""
    for case in load(SET).cases:
        if case.expect == "answered" and case.grounded:
            assert case.source, f"{case.id} has no source document"


# --- the validation -----------------------------------------------------------
def test_an_answered_case_without_an_expected_answer_is_rejected(tmp_path):
    """It cannot be marked, and would quietly pass every metric that compares against it."""
    path = write(tmp_path, "target: t\ncases:\n" + "".join(
        f"  - {{id: c{i}, question: q, principal: p, expect: answered, expected_answer: a}}\n"
        for i in range(15)) +
        "  - {id: bad, question: q, principal: p, expect: answered}\n")
    with pytest.raises(ConfigError, match="expects an answer but gives none"):
        load(path)


def test_a_duplicate_id_is_rejected(tmp_path):
    """Ids name a case in the report, so a duplicate makes one result overwrite another
    while the count still looks right."""
    path = write(tmp_path, "target: t\ncases:\n" + "".join(
        f"  - {{id: c{i}, question: q, principal: p, expect: blocked}}\n" for i in range(15)) +
        "  - {id: c1, question: q, principal: p, expect: blocked}\n")
    with pytest.raises(ConfigError, match="duplicate case id"):
        load(path)


def test_an_unknown_behaviour_is_rejected(tmp_path):
    path = write(tmp_path, "target: t\ncases:\n" + "".join(
        f"  - {{id: c{i}, question: q, principal: p, expect: blocked}}\n" for i in range(15)) +
        "  - {id: odd, question: q, principal: p, expect: probably}\n")
    with pytest.raises(ConfigError, match="not one of"):
        load(path)


def test_a_set_below_the_floor_is_rejected(tmp_path):
    """The run that notices should be the one that fails, not the one that reports."""
    path = write(tmp_path, "target: t\ncases:\n"
                 "  - {id: only, question: q, principal: p, expect: blocked}\n")
    with pytest.raises(ConfigError, match="at least 15 required"):
        load(path)


def test_a_probe_must_say_what_the_judge_should_do(tmp_path):
    path = write(tmp_path, "target: t\ncases:\n" + "".join(
        f"  - {{id: c{i}, question: q, principal: p, expect: blocked}}\n" for i in range(15)) +
        "probes:\n  - {id: p1, question: q, answer: a, expect_judge: maybe}\n")
    with pytest.raises(ConfigError, match="pass or fail"):
        load(path)
