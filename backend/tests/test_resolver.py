"""Tests for the 3-pass concept resolver (issue #12).

Why these paths are critical: the resolver is the only bridge between what a
coach types and what the safety layer can act on. A term that force-matches
to the wrong concept silently corrupts a safety decision; a term that is
silently dropped removes a constraint the coach believes is applied. So the
suite pins (a) the pass order and each pass's explicit threshold boundaries,
(b) graceful degradation on garbage, and (c) the messy real cases from
docs/data-overview.md that literal matching cannot handle.

The unit layer fakes the Neo4j driver so pass behavior and thresholds are
tested exactly and offline; the integration layer replays the documented
messy cases against the real built graph. No LLM is involved anywhere.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.kg import resolver as resolver_module
from app.kg.resolver import (
    FULLTEXT_MIN_SCORE,
    VECTOR_MIN_SCORE,
    _lucene_query,
    resolve_concepts,
)


class FakeDriver:
    """Answers the resolver's four query shapes from canned rows."""

    def __init__(
        self,
        exact: list[dict[str, Any]] | None = None,
        fulltext: list[dict[str, Any]] | None = None,
        vector: list[dict[str, Any]] | None = None,
        targets: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.exact = exact or []
        self.fulltext = fulltext or []
        self.vector = vector or []
        self.targets = targets or {}

    async def execute_query(self, query: str, **params: Any):
        if "fulltext.queryNodes" in query:
            rows = self.fulltext
        elif "vector.queryNodes" in query:
            rows = self.vector
        elif "toLower(c.pref_label)" in query:
            rows = self.exact
        elif "n.curie = $curie" in query:
            hit = self.targets.get(params["curie"])
            rows = [hit] if hit else []
        else:  # pragma: no cover - unexpected query shape
            raise AssertionError(f"unexpected query: {query}")
        return rows, None, None


def _node(concept_id: str, labels: list[str], score: float | None = None):
    node = {"id": concept_id, "label": concept_id, "pref_label": concept_id,
            "labels": labels}
    if score is not None:
        node["score"] = score
    return node


@pytest.fixture(autouse=True)
def _fixed_embedder(monkeypatch: pytest.MonkeyPatch):
    """The vector pass must not download a model in unit tests."""
    monkeypatch.setattr(
        resolver_module, "embed_texts", lambda texts: [[0.0] * 384 for _ in texts]
    )


async def test_exact_pass_wins_over_fulltext() -> None:
    # Guards: a curated synonym ("Deadlift" as an alt label) must never be
    # outranked by a fuzzy hit — the coach's exact word is the strongest signal.
    driver = FakeDriver(
        exact=[_node("mp_lower_pull_hip_lift", ["MovementPattern", "Concept"])],
        fulltext=[_node("eq_barbell", ["Equipment", "Concept"], score=9.0)],
    )
    results = await resolve_concepts(driver, "Deadlift")  # type: ignore[arg-type]
    assert results[0].concept_id == "mp_lower_pull_hip_lift"
    assert results[0].match_method == "exact"
    assert results[0].score == 1.0


async def test_fulltext_threshold_boundary() -> None:
    # Guards: incidental token overlap resolving as if it were a real match.
    # A score exactly at the threshold is accepted; just below falls through
    # to the vector pass.
    at_threshold = FakeDriver(
        fulltext=[_node("eq_kettlebell", ["Equipment", "Concept"],
                        score=FULLTEXT_MIN_SCORE)]
    )
    hit = (await resolve_concepts(at_threshold, "ketlebell"))[0]  # type: ignore[arg-type]
    assert hit.resolved and hit.match_method == "fulltext"

    below = FakeDriver(
        fulltext=[_node("eq_kettlebell", ["Equipment", "Concept"],
                        score=FULLTEXT_MIN_SCORE - 0.01)],
        vector=[],
    )
    miss = (await resolve_concepts(below, "ketlebell"))[0]  # type: ignore[arg-type]
    assert not miss.resolved


async def test_vector_threshold_boundary() -> None:
    # Guards: the semantic fallback force-matching word salad to the nearest
    # concept. At the threshold it resolves; below, the term comes back
    # unresolved WITH the near-miss in the reason for transparency.
    at_threshold = FakeDriver(
        vector=[_node("mp_cardio_plyometric", ["MovementPattern", "Concept"],
                      score=VECTOR_MIN_SCORE)]
    )
    hit = (await resolve_concepts(at_threshold, "explosive jumping stuff"))[0]  # type: ignore[arg-type]
    assert hit.resolved and hit.match_method == "vector"

    below = FakeDriver(
        vector=[_node("mp_cardio_plyometric", ["MovementPattern", "Concept"],
                      score=VECTOR_MIN_SCORE - 0.01)]
    )
    miss = (await resolve_concepts(below, "xylophone maintenance"))[0]  # type: ignore[arg-type]
    assert not miss.resolved
    assert "mp_cardio_plyometric" in (miss.reason or "")


async def test_garbage_input_degrades_gracefully() -> None:
    # Guards: unresolvable terms being silently dropped — downstream must be
    # able to tell the coach "I could not understand X".
    driver = FakeDriver()
    results = await resolve_concepts(driver, "zzghkq florble")  # type: ignore[arg-type]
    assert len(results) == 1
    assert not results[0].resolved
    assert results[0].reason
    assert (await resolve_concepts(driver, "   "))[0].reason == "empty input"  # type: ignore[arg-type]


async def test_vector_pass_unavailable_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guards: an offline embedding model crashing the request instead of
    # degrading to "unresolved with reason".
    def boom(texts):
        raise RuntimeError("model not downloaded")

    monkeypatch.setattr(resolver_module, "embed_texts", boom)
    result = (await resolve_concepts(FakeDriver(), "novel phrasing"))[0]  # type: ignore[arg-type]
    assert not result.resolved
    assert "vector pass unavailable" in (result.reason or "")


async def test_ambiguous_lower_back_needs_context() -> None:
    # Guards: the documented "lower back" ambiguity (muscle group vs lumbar
    # complaint) being silently merged. Injury context must pick the spinal
    # segment; no context must return BOTH readings flagged ambiguous.
    driver = FakeDriver(
        targets={
            "snomed:122496007": _node(
                "jt_lumbar_spine", ["AnatomicalStructure", "Joint", "Concept"]
            ),
            "snomed:48144002": _node("mg_lower_back", ["MuscleGroup", "Concept"]),
        }
    )
    injury = await resolve_concepts(driver, "bad lower back", context="injury")  # type: ignore[arg-type]
    assert [r.concept_id for r in injury] == ["jt_lumbar_spine"]
    assert injury[0].match_method == "policy"

    unknown = await resolve_concepts(driver, "bad lower back")  # type: ignore[arg-type]
    assert {r.concept_id for r in unknown} == {"jt_lumbar_spine", "mg_lower_back"}
    assert all(r.ambiguous for r in unknown)


async def test_concept_type_filter() -> None:
    # Guards: an injury-note lookup accepting a non-condition (e.g. a muscle
    # group) and handing the safety layer a concept its rules can't act on.
    driver = FakeDriver(
        exact=[_node("mg_lower_back", ["MuscleGroup", "Concept"])],
        fulltext=[],
        vector=[],
    )
    result = (
        await resolve_concepts(driver, "lower back pain-ish",  # type: ignore[arg-type]
                               concept_types=["condition"])
    )[0]
    assert not result.resolved


def test_lucene_query_escaping_and_fuzz() -> None:
    # Guards: coach input containing Lucene syntax ("resistance band - loop")
    # crashing or subverting the full-text query, and 3-letter tokens getting
    # fuzz (hip~1 would match "hit").
    query = _lucene_query("resistance band - loop")
    assert "\\-" in query and '"resistance band \\- loop"' in query
    assert "band~1" in query and "loop~1" in query
    short = _lucene_query("hip")
    assert short == "hip"


# --------------------------------------------------------------------------
# Integration: the documented messy cases against the real graph
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "context", "expected_id"),
    [
        # The brief's named resolutions (ASSESSMENT.md build step 3).
        ("knee", None, "jt_knee"),
        ("kettlebell", None, "eq_kettlebell"),
        ("bad lower back", "injury", "jt_lumbar_spine"),
        # Quirk 9: the dislikes match zero catalog names literally and must
        # land on their curated semantic relatives.
        ("Deadlift", None, "mp_lower_pull_hip_lift"),
        ("Burpees", None, "mp_cardio_plyometric"),
        # Typo tolerance through the full-text fuzzy pass.
        ("ketlebell", None, "eq_kettlebell"),
    ],
)
async def test_messy_real_cases_on_live_graph(
    graph_driver, text: str, context, expected_id: str
) -> None:
    # Guards: the exact user-facing resolutions the assessment names; any of
    # these failing means a coach's words stop reaching the graph.
    results = await resolve_concepts(graph_driver, text, context=context)
    assert results[0].resolved, results[0].reason
    assert results[0].concept_id == expected_id
