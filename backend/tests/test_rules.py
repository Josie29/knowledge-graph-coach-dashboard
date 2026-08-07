"""Schema tests for the curated safety rules (`app.kg.rules`).

`safe_exercise_pool` is the safety boundary the whole system rests on, and it
only ever sees what `contraindications.json` says. The dangerous failure here
is not a crash — it is a rule that loads cleanly and then never fires, because
a key was misspelled or a value fell outside the vocabulary the evaluator
compares against. Nothing else in the suite would notice: the pool would simply
come back one filter short.

These tests pin the schema at both ends of the round trip and are pure — no
Neo4j, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import settings
from app.kg.rules import (
    SAFETY_RULES_ADAPTER,
    ExerciseMatch,
    InjuryStatus,
    MechanicsAttr,
    SafetyRule,
)

EXPECTED_RULE_COUNT = 22


def _curated_rules() -> list[dict[str, Any]]:
    path = Path(settings.data_dir) / "ontology" / "contraindications.json"
    return json.loads(path.read_text(encoding="utf-8"))["rules"]


def test_curated_rules_validate() -> None:
    # Catches the curated file drifting away from what the evaluator can read —
    # a renamed field or a new decision value would otherwise only surface as a
    # rule that quietly stops filtering.
    rules = SAFETY_RULES_ADAPTER.validate_python(_curated_rules())
    assert len(rules) == EXPECTED_RULE_COUNT


def test_unknown_mechanics_attribute_is_rejected() -> None:
    # THE failure this schema exists to prevent: `mechanics_any_off` (or a
    # mechanics attribute the movement patterns do not carry) would be dropped
    # in silence, and the rule would match nothing forever.
    typo_clause = dict(_curated_rules()[0])
    typo_clause["exercise_match"] = {
        "require_anatomy_overlap": False,
        "mechanics_any_off": {"impact": ["high"]},
    }
    with pytest.raises(ValidationError, match="mechanics_any_off"):
        SAFETY_RULES_ADAPTER.validate_python([typo_clause])

    with pytest.raises(ValidationError):
        ExerciseMatch.model_validate(
            {"require_anatomy_overlap": False, "mechanics_any_of": {"impcat": ["high"]}}
        )


def test_chronic_is_part_of_the_status_vocabulary() -> None:
    # Four curated rules (knee OA, both low-back rules, carpal tunnel) scope
    # themselves to chronic injuries. If the enum omitted it, a member recorded
    # as chronic could not be represented at all and those rules would be
    # unreachable for exactly the people they were written for.
    assert InjuryStatus.CHRONIC in set(InjuryStatus)
    chronic_rules = [
        rule
        for rule in SAFETY_RULES_ADAPTER.validate_python(_curated_rules())
        if InjuryStatus.CHRONIC in rule.applies_when.status
    ]
    assert len(chronic_rules) == 4


def test_mechanics_flags_stay_booleans() -> None:
    # `is_therapeutic: [true]` is compared against the movement pattern's own
    # boolean. Coerced to the string "True" it would never match, silently
    # disabling the allow rule that rescues unloaded therapeutic work over an
    # irritable joint — the pool would lose its mobility exercises.
    match = ExerciseMatch.model_validate(
        {
            "require_anatomy_overlap": False,
            "mechanics_all_of": {"is_therapeutic": [True], "impact": ["none"]},
        }
    )
    assert match.mechanics_all_of is not None
    assert match.mechanics_all_of[MechanicsAttr.IS_THERAPEUTIC] == [True]
    assert match.mechanics_all_of[MechanicsAttr.IMPACT] == ["none"]


def test_stored_json_round_trips() -> None:
    # The graph cannot hold nested maps, so rules travel to Neo4j as JSON
    # strings and are re-validated on the way back out. A serializer that lost
    # the enum keys or the booleans would break filtering only at request time,
    # long after the build reported success.
    for rule in SAFETY_RULES_ADAPTER.validate_python(_curated_rules()):
        stored = rule.exercise_match.model_dump_json(exclude_none=True)
        assert ExerciseMatch.model_validate_json(stored) == rule.exercise_match
        assert json.loads(rule.applies_when.model_dump_json())["status"] == [
            str(status) for status in rule.applies_when.status
        ]


def test_curation_prose_is_declared_not_ignored() -> None:
    # extra="forbid" only protects the file if every legitimate key is declared;
    # otherwise the next curator to add a rationale note breaks the build and
    # learns to loosen the model instead of trusting it.
    rule = SafetyRule.model_validate(
        {
            **_curated_rules()[0],
            "member_evidence": "why this rule exists",
            "note": "a defensive guard",
            "rescues": ["Cow Pose"],
        }
    )
    assert rule.rescues == ["Cow Pose"]

    with pytest.raises(ValidationError, match="totally_new_field"):
        SafetyRule.model_validate(
            {**_curated_rules()[0], "totally_new_field": "unexpected"}
        )
