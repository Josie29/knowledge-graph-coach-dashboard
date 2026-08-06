"""Three-pass concept resolver: free text → KG 1 concept nodes. No LLM involved.

``resolve_concepts`` maps a coach-typed surface form ("knee", "kettlebell",
"bad lower back", "Deadlift") onto the graph's ``Concept`` nodes in three
passes of decreasing confidence, per docs/tech-stack.md and issue #5:

1. **exact** — case-insensitive full-string match on ``pref_label``,
   ``catalog_term`` or any ``alt_label``. Score 1.0 by construction.
2. **fulltext** — the Neo4j full-text index (``concept_text``), queried with a
   phrase clause plus per-token fuzzy clauses, so word order, extra words and
   small typos still land ("ketlebell", "bad lower back"). Lucene scores are
   unbounded; the acceptance threshold is explicit and documented below.
3. **vector** — fastembed embeds the query with the same model the build
   script used for concept labels and asks the ``concept_embedding`` vector
   index for nearest neighbours. This is the semantic fallback for phrasings
   nobody curated.

Before all three, an **ambiguity policy** pass consults the curated
``ambiguous_surface_forms`` table (anatomy-partonomy.json): "lower back" is
both a catalog muscle group and how a coach names a lumbar complaint, and the
two readings must not be merged. When the caller supplies the slot being
filled (``context="injury"`` / ``"targeting"``) the policy picks the branch;
when it doesn't, both readings come back flagged ``ambiguous`` rather than a
guess — the graceful-degradation path the brief asks for.

Unresolvable terms are returned as ``resolved=False`` with a reason, never
silently dropped and never force-matched to the closest thing.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from neo4j import AsyncDriver
from pydantic import BaseModel

from app.config import settings
from app.kg.embeddings import embed_texts

# Acceptance thresholds, deliberately explicit (issue #5 asks for them).
# Lucene full-text scores are corpus-dependent; against this ~350-node graph,
# real hits score >2 (e.g. "knee" → 6.3, "kettlebell" → 7.5) and incidental
# token overlap scores below ~1.5, so 1.5 keeps garbage out without dropping
# legitimate fuzzy hits.
FULLTEXT_MIN_SCORE = 1.5
# Neo4j cosine vector scores are (1 + cos) / 2 ∈ [0, 1]. bge-small-en-v1.5
# puts related short phrases above ~0.80 and unrelated ones below ~0.75;
# 0.78 accepts "explosive jumping" → plyometric while rejecting word salad.
VECTOR_MIN_SCORE = 0.78
VECTOR_TOP_K = 5

# Tokens shorter than this get no Lucene fuzz — "hip"~1 would match "hit".
_MIN_FUZZY_TOKEN_LEN = 4

_LUCENE_SPECIALS = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')

ResolutionContext = Literal["injury", "targeting"]


class MatchMethod(StrEnum):
    """How a concept was matched, recorded for the provenance trace."""

    POLICY = "policy"
    EXACT = "exact"
    FULLTEXT = "fulltext"
    VECTOR = "vector"


class ResolvedConcept(BaseModel):
    """One resolution outcome for one surface form.

    ``resolved=False`` entries carry the term through the pipeline with a
    reason instead of dropping it — downstream consumers (and the provenance
    trace) must be able to show what could not be understood.
    """

    query: str
    resolved: bool
    concept_id: str | None = None
    label: str | None = None
    concept_type: str | None = None
    match_method: MatchMethod | None = None
    score: float = 0.0
    ambiguous: bool = False
    reason: str | None = None


# Label precedence for reporting a single concept_type per node; every vocab
# node also carries :Concept, and joints additionally :AnatomicalStructure.
_TYPE_BY_LABEL = (
    ("Joint", "joint"),
    ("MuscleGroup", "muscle_group"),
    ("MovementPattern", "movement_pattern"),
    ("Equipment", "equipment"),
    ("Condition", "condition"),
    ("AnatomicalStructure", "anatomical_structure"),
)


def _concept_type(labels: list[str]) -> str | None:
    for label, concept_type in _TYPE_BY_LABEL:
        if label in labels:
            return concept_type
    return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


@lru_cache(maxsize=1)
def _ambiguity_policy() -> list[dict[str, Any]]:
    """The curated ambiguous-surface-form table, with IRIs pre-expanded."""
    ontology_dir = Path(settings.data_dir) / "ontology"
    prefixes = {
        prefix: spec["iri"]
        for prefix, spec in json.loads(
            (ontology_dir / "namespaces.json").read_text(encoding="utf-8")
        )["prefixes"].items()
    }
    doc = json.loads(
        (ontology_dir / "anatomy-partonomy.json").read_text(encoding="utf-8")
    )
    entries = []
    for entry in doc["ambiguous_surface_forms"]:
        branches = []
        for branch in entry["resolution_policy"]:
            curie = branch["resolves_to"]
            prefix, _, local = curie.partition(":")
            branches.append(
                {
                    "context": branch["context"],
                    "curie": curie,
                    "iri": prefixes[prefix] + local,
                    "label": branch.get("label"),
                }
            )
        entries.append(
            {"surface_form": entry["surface_form"], "branches": branches}
        )
    return entries


async def _concept_by_target(
    driver: AsyncDriver, curie: str, iri: str
) -> dict[str, Any] | None:
    """Find the graph node a policy branch points at (by curie or exactMatch)."""
    records, _, _ = await driver.execute_query(
        "MATCH (n) WHERE (n:Concept OR n:AnatomicalStructure) "
        "AND (n.curie = $curie OR $iri IN coalesce(n.exact_match, [])) "
        "RETURN n.id AS id, coalesce(n.pref_label, n.label) AS label, "
        "labels(n) AS labels "
        "ORDER BY n.id IS NULL LIMIT 1",
        curie=curie,
        iri=iri,
    )
    return dict(records[0]) if records else None


async def _policy_pass(
    driver: AsyncDriver, query: str, normalized: str, context: ResolutionContext | None
) -> list[ResolvedConcept] | None:
    """Resolve through the curated ambiguity table; None means no policy applies.

    Only multi-branch forms are handled here — single-branch entries ("knee",
    "shoulder") already agree with what the exact pass returns.
    """
    for entry in _ambiguity_policy():
        if len(entry["branches"]) < 2 or entry["surface_form"] not in normalized:
            continue
        if context is not None:
            branch = next(
                (b for b in entry["branches"] if context in b["context"]), None
            )
            if branch is None:
                continue
            node = await _concept_by_target(driver, branch["curie"], branch["iri"])
            if node is None:
                continue
            return [
                ResolvedConcept(
                    query=query,
                    resolved=True,
                    concept_id=node["id"],
                    label=node["label"],
                    concept_type=_concept_type(node["labels"]),
                    match_method=MatchMethod.POLICY,
                    score=1.0,
                    reason=f"ambiguity policy, {context} context",
                )
            ]
        results = []
        for branch in entry["branches"]:
            node = await _concept_by_target(driver, branch["curie"], branch["iri"])
            if node is not None:
                results.append(
                    ResolvedConcept(
                        query=query,
                        resolved=True,
                        concept_id=node["id"],
                        label=node["label"],
                        concept_type=_concept_type(node["labels"]),
                        match_method=MatchMethod.POLICY,
                        score=1.0,
                        ambiguous=True,
                        reason=(
                            f"'{entry['surface_form']}' is ambiguous "
                            f"({branch['context']}); pass context to disambiguate"
                        ),
                    )
                )
        if results:
            return results
    return None


async def _exact_pass(driver: AsyncDriver, normalized: str) -> list[dict[str, Any]]:
    records, _, _ = await driver.execute_query(
        "MATCH (c:Concept) "
        "WHERE toLower(c.pref_label) = $q OR toLower(c.catalog_term) = $q "
        "OR $q IN [alt IN coalesce(c.alt_labels, []) | toLower(alt)] "
        "RETURN c.id AS id, c.pref_label AS label, labels(c) AS labels",
        q=normalized,
    )
    return [dict(record) for record in records]


def _lucene_query(normalized: str) -> str:
    """Phrase clause plus per-token fuzzy clauses, Lucene specials escaped."""
    escaped = _LUCENE_SPECIALS.sub(r"\\\1", normalized)
    tokens = escaped.split()
    clauses = [f'"{escaped}"'] if len(tokens) > 1 else []
    for token in tokens:
        clauses.append(
            f"{token}~1" if len(token) >= _MIN_FUZZY_TOKEN_LEN else token
        )
    return " OR ".join(clauses)


async def _fulltext_pass(
    driver: AsyncDriver, normalized: str
) -> list[dict[str, Any]]:
    records, _, _ = await driver.execute_query(
        "CALL db.index.fulltext.queryNodes('concept_text', $q) "
        "YIELD node, score "
        "RETURN node.id AS id, node.pref_label AS label, "
        "labels(node) AS labels, score "
        "ORDER BY score DESC LIMIT 5",
        q=_lucene_query(normalized),
    )
    return [dict(record) for record in records]


async def _vector_pass(
    driver: AsyncDriver, normalized: str
) -> list[dict[str, Any]] | str:
    """Nearest concepts by embedding; returns a reason string on degradation."""
    try:
        vector = embed_texts([normalized])[0]
    except Exception as exc:  # fastembed model missing/undownloadable
        return f"vector pass unavailable ({type(exc).__name__}: {exc})"
    records, _, _ = await driver.execute_query(
        "CALL db.index.vector.queryNodes('concept_embedding', $k, $vector) "
        "YIELD node, score "
        "RETURN node.id AS id, node.pref_label AS label, "
        "labels(node) AS labels, score",
        k=VECTOR_TOP_K,
        vector=vector,
    )
    return [dict(record) for record in records]


async def resolve_concepts(
    driver: AsyncDriver,
    text: str,
    *,
    context: ResolutionContext | None = None,
    concept_types: list[str] | None = None,
) -> list[ResolvedConcept]:
    """Resolve one free-text surface form to KG 1 concepts.

    Args:
        driver: Injected Neo4j async driver (the one on ``app.state``).
        text: The surface form to resolve, e.g. ``"bad lower back"``.
        context: The slot being filled, when the caller knows it. ``"injury"``
            picks the clinical reading of ambiguous forms, ``"targeting"`` the
            muscle reading; ``None`` returns both readings flagged ambiguous.
        concept_types: When given, only concepts of these types (e.g.
            ``["condition"]``) can match in the exact/fulltext/vector passes —
            used when the caller knows the slot's type, like resolving an
            injury note to a Condition. The policy pass is not filtered.

    Returns:
        A non-empty list. Usually one entry; two for an unresolved ambiguity;
        always at least an ``resolved=False`` entry with a reason.
    """
    query = text
    normalized = _normalize(text)
    if not normalized:
        return [
            ResolvedConcept(query=query, resolved=False, reason="empty input")
        ]

    def _type_ok(labels: list[str]) -> bool:
        return concept_types is None or _concept_type(labels) in concept_types

    policy_results = await _policy_pass(driver, query, normalized, context)
    if policy_results is not None:
        return policy_results

    exact = [n for n in await _exact_pass(driver, normalized) if _type_ok(n["labels"])]
    if exact:
        return [
            ResolvedConcept(
                query=query,
                resolved=True,
                concept_id=node["id"],
                label=node["label"],
                concept_type=_concept_type(node["labels"]),
                match_method=MatchMethod.EXACT,
                score=1.0,
            )
            for node in exact
        ]

    fulltext = [
        n for n in await _fulltext_pass(driver, normalized) if _type_ok(n["labels"])
    ]
    if fulltext and fulltext[0]["score"] >= FULLTEXT_MIN_SCORE:
        top = fulltext[0]
        return [
            ResolvedConcept(
                query=query,
                resolved=True,
                concept_id=top["id"],
                label=top["label"],
                concept_type=_concept_type(top["labels"]),
                match_method=MatchMethod.FULLTEXT,
                score=top["score"],
            )
        ]

    vector = await _vector_pass(driver, normalized)
    if not isinstance(vector, str):
        vector = [n for n in vector if _type_ok(n["labels"])]
    if isinstance(vector, str):
        best_fulltext = (
            f"; best fulltext candidate {fulltext[0]['id']} "
            f"scored {fulltext[0]['score']:.2f} < {FULLTEXT_MIN_SCORE}"
            if fulltext
            else ""
        )
        return [
            ResolvedConcept(
                query=query, resolved=False, reason=vector + best_fulltext
            )
        ]
    if vector and vector[0]["score"] >= VECTOR_MIN_SCORE:
        top = vector[0]
        return [
            ResolvedConcept(
                query=query,
                resolved=True,
                concept_id=top["id"],
                label=top["label"],
                concept_type=_concept_type(top["labels"]),
                match_method=MatchMethod.VECTOR,
                score=top["score"],
            )
        ]

    best = vector[0] if vector else None
    reason = (
        f"no pass met its threshold; nearest was {best['id']} "
        f"at vector score {best['score']:.2f} < {VECTOR_MIN_SCORE}"
        if best
        else "no candidates in any pass"
    )
    return [ResolvedConcept(query=query, resolved=False, reason=reason)]
