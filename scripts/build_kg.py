"""Build both knowledge graphs: KG 1 (movement/clinical) and KG 2 (member context).

KG 1 pipeline (docs/kg1-schema.md §8): parse the hand-curated JSON in
``data/ontology/`` with rdflib to materialise the derived SKOS triples (RDF is
derived, never hand-maintained), validate the curation against
``data/exercises.json``, then emit Cypher and load Neo4j. KG 2 (issue #7)
ingests ``data/member-context.json`` afterwards and cross-links it into KG 1
(injury → Joint, equipment → Equipment). Safe to re-run: every node and
relationship is MERGEd on a stable key and properties are overwritten with the
current curated values.

Usage (local, from the repo root)::

    cd backend && uv run python ../scripts/build_kg.py

or in Docker via the ``kg-build`` one-shot service in docker-compose.yml, which
runs automatically before the API starts.

Flags:
    --dry-run           Parse, validate and report; skip Neo4j entirely.
    --skip-embeddings   Load the graph without concept embeddings (the vector
                        index is still created; pass 3 of the resolver will
                        return nothing until a full build runs).
    --emit-ttl PATH     Also serialise the derived SKOS graph as Turtle.
    --reset             DETACH DELETE all KG 1 nodes before loading.

Data quirks handled here (numbering from docs/data-overview.md §4):
    q1  ``is_bilateral`` actually means "has a bilateral pair" — stored as
        ``has_bilateral_pair``; unilateral-ness comes from ``side``.
    q2  ``bilateral_pair_id`` is dangling on all 18 rows — dropped.
    q3  ``priority_tier`` and ``is_duration`` are zero-entropy — dropped.
    q4  Empty ``joints_loaded`` means *unknown*, not safe: stored as
        ``stresses_recorded: false`` so the rule layer knows anatomy-gated
        rules cannot vouch for these rows (mechanics-only rules still fire).
    q5  The truncated ``"car"`` pattern maps through movement-patterns.json's
        ``catalog_term`` to ``mp_car`` (Controlled articular rotation).
    q6  ``joints_loaded`` is deduped (one row lists ``shoulder`` twice).
    q10 Workout-history exercise names join to nothing in the catalog — they
        are stored as a free-text array on the session node, never linked.
    q11 The "login frequency" churn reason has no backing field anywhere; it
        is stored only as text on the CoachBrief node, so the copilot's only
        citable source for it is the brief itself.
    q13 "Now" is anchored to the coach brief's generated_for date
        (2026-06-04), stored as ``Member.now_anchor`` — recency and trend
        computations must use it, never the wall clock.
    q14 ``attachments`` is absent (not null) on most chat messages — read
        with ``.get()`` semantics.
    q15 ``estimated_rep_duration`` is reps per second, not seconds — inverted
        and stored as ``rep_seconds`` so no consumer has to know.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Make ``app`` importable both from the repo checkout (backend/app) and from the
# backend Docker image, where the backend contents live at the working dir root.
for _candidate in (_REPO_ROOT / "backend", _REPO_ROOT):
    if (_candidate / "app").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SKOS

from app.kg.embeddings import EMBEDDING_DIM, embed_texts

ONTOLOGY_DIR_NAME = "ontology"

# Labels owned by this script; --reset removes exactly these.
KG1_LABELS = (
    "Exercise",
    "MuscleGroup",
    "Joint",
    "MovementPattern",
    "Equipment",
    "AnatomicalStructure",
    "Condition",
    "SafetyRule",
)

EXPECTED_COUNTS = {
    "Exercise": 50,
    "MuscleGroup": 19,
    "Joint": 9,
    "MovementPattern": 36,
    "Equipment": 32,
    "AnatomicalStructure": 173,
    "Condition": 16,
    "SafetyRule": 22,
}

# JSON mapping predicate -> (rdflib predicate, Neo4j property name).
SKOS_PREDICATES = {
    "skos:exactMatch": (SKOS.exactMatch, "exact_match"),
    "skos:closeMatch": (SKOS.closeMatch, "close_match"),
    "skos:broader": (SKOS.broader, "broader"),
    "skos:narrower": (SKOS.narrower, "narrower"),
    "skos:related": (SKOS.related, "related"),
}

MECHANICS_KEYS = (
    "impact",
    "kinetic_chain",
    "external_load_typical",
    "axial_spinal_load",
    "knee_flexion_demand",
    "spinal_flexion_demand",
    "spinal_rotation_demand",
    "overhead_shoulder_demand",
    "end_range_rom",
    "unilateral_lower_limb",
    "is_therapeutic",
)


class CurationError(Exception):
    """A curated file contradicts the catalog or itself; the build must not load."""


@dataclass
class Kg1Payload:
    """Everything the loader needs, already validated and quirk-corrected."""

    structures: list[dict[str, Any]]
    part_of_edges: list[dict[str, Any]]
    is_a_edges: list[dict[str, Any]]
    joints: list[dict[str, Any]]
    muscle_groups: list[dict[str, Any]]
    patterns: list[dict[str, Any]]
    equipment: list[dict[str, Any]]
    conditions: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    exercises: list[dict[str, Any]]
    # (source concept id, target curie) pairs for MAPS_TO into the partonomy.
    maps_to: list[dict[str, str]] = field(default_factory=list)
    substitutions: list[dict[str, Any]] = field(default_factory=list)
    exercise_edges: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    rdf: Graph = field(default_factory=Graph)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _expand(curie: str, prefixes: dict[str, str]) -> str:
    """Expand ``prefix:local`` through the namespaces.json registry."""
    prefix, _, local = curie.partition(":")
    if not local or prefix not in prefixes:
        raise CurationError(f"IRI token {curie!r} uses an unregistered prefix")
    return prefixes[prefix] + local


def _concept_common(
    concept: dict[str, Any], prefixes: dict[str, str]
) -> dict[str, Any]:
    """Shared Concept-node properties: labels, search text and SKOS mapping IRIs."""
    alt_labels = list(concept.get("alt_labels", []))
    props: dict[str, Any] = {
        "id": concept["id"],
        "pref_label": concept["pref_label"],
        "alt_labels": alt_labels,
        "alt_labels_text": " | ".join(alt_labels),
    }
    if concept.get("catalog_term"):
        props["catalog_term"] = concept["catalog_term"]
    low_confidence: list[str] = []
    for mapping in concept.get("mappings", []):
        predicate = mapping["predicate"]
        if predicate not in SKOS_PREDICATES:
            raise CurationError(
                f"{concept['id']}: unsupported mapping predicate {predicate!r}"
            )
        iri = _expand(mapping["target"], prefixes)
        prop = SKOS_PREDICATES[predicate][1]
        props.setdefault(prop, []).append(iri)
        if mapping.get("confidence") == "low":
            low_confidence.append(iri)
    if low_confidence:
        # Policy (data/ontology/README.md): low-confidence mappings are
        # display-and-traversal only, never the sole basis for a safety decision.
        props["low_confidence_targets"] = low_confidence
    return props


def _embedding_text(concept: dict[str, Any]) -> str:
    """Text embedded for a concept: preferred label, catalog term, synonyms."""
    parts = [concept["pref_label"]]
    if concept.get("catalog_term"):
        parts.append(concept["catalog_term"])
    parts.extend(concept.get("alt_labels", []))
    seen: set[str] = set()
    unique = [p for p in parts if not (p.lower() in seen or seen.add(p.lower()))]
    return ". ".join(unique)


def _add_concept_triples(
    rdf: Graph, kgc: Namespace, concept: dict[str, Any], prefixes: dict[str, str]
) -> None:
    subject = kgc[concept["id"]]
    rdf.add((subject, RDF.type, SKOS.Concept))
    rdf.add((subject, SKOS.prefLabel, Literal(concept["pref_label"])))
    for alt in concept.get("alt_labels", []):
        rdf.add((subject, SKOS.altLabel, Literal(alt)))
    for mapping in concept.get("mappings", []):
        predicate = SKOS_PREDICATES[mapping["predicate"]][0]
        rdf.add((subject, predicate, URIRef(_expand(mapping["target"], prefixes))))


def _validate_vocabulary(
    name: str, concepts: list[dict[str, Any]], used_terms: Counter[str]
) -> None:
    """Every catalog term must be curated, with no extras and correct counts."""
    curated = {c["catalog_term"]: c for c in concepts}
    missing = set(used_terms) - set(curated)
    extra = set(curated) - set(used_terms)
    if missing or extra:
        raise CurationError(
            f"{name}: catalog/curation mismatch (missing={sorted(missing)}, "
            f"extra={sorted(extra)})"
        )
    for term, concept in curated.items():
        declared = concept.get("exercise_count")
        if declared is not None and declared != used_terms[term]:
            raise CurationError(
                f"{name}: {term!r} declares exercise_count={declared}, "
                f"recount says {used_terms[term]}"
            )


def load_and_validate(data_dir: Path) -> Kg1Payload:
    """Parse every curated file, run the integrity checks, apply the quirks.

    Raises:
        CurationError: On any contradiction between the curated files and the
            catalog — the graph is a safety artifact, so nothing loads partially.
    """
    ontology_dir = data_dir / ONTOLOGY_DIR_NAME
    prefixes = {
        prefix: spec["iri"]
        for prefix, spec in _load_json(ontology_dir / "namespaces.json")[
            "prefixes"
        ].items()
    }
    kgc = Namespace(prefixes["kgc"])

    exercises_raw = _load_json(data_dir / "exercises.json")
    joints_doc = _load_json(ontology_dir / "joints.json")
    muscles_doc = _load_json(ontology_dir / "muscle-groups.json")
    patterns_doc = _load_json(ontology_dir / "movement-patterns.json")
    equipment_doc = _load_json(ontology_dir / "equipment.json")
    conditions_doc = _load_json(ontology_dir / "conditions.json")
    partonomy_doc = _load_json(ontology_dir / "anatomy-partonomy.json")
    rules_doc = _load_json(ontology_dir / "contraindications.json")

    # --- integrity: catalog vocabulary coverage (with quirk q6 dedupe) --------
    used: dict[str, Counter[str]] = {
        "joints": Counter(),
        "muscles": Counter(),
        "patterns": Counter(),
        "equipment": Counter(),
    }
    for row in exercises_raw:
        used["joints"].update(set(row["joints_loaded"]))  # q6: dedupe
        used["muscles"].update(set(row["muscle_groups"]))
        used["patterns"].update(set(row["movement_patterns"]))
        used["equipment"].update(set(row["equipment_required"]))
    _validate_vocabulary("joints", joints_doc["concepts"], used["joints"])
    _validate_vocabulary("muscle-groups", muscles_doc["concepts"], used["muscles"])
    _validate_vocabulary("movement-patterns", patterns_doc["concepts"], used["patterns"])
    _validate_vocabulary("equipment", equipment_doc["concepts"], used["equipment"])

    # --- integrity: partonomy is closed -------------------------------------
    structures_raw: dict[str, dict[str, Any]] = partonomy_doc["structures"]
    part_of_edges: list[dict[str, Any]] = []
    is_a_edges: list[dict[str, Any]] = []
    for edge in partonomy_doc["edges"]:
        for endpoint in (edge["child"], edge["parent"]):
            if endpoint not in structures_raw:
                raise CurationError(f"partonomy edge references undeclared {endpoint}")
        bucket = part_of_edges if edge["edge"] == "PART_OF" else is_a_edges
        bucket.append(
            {"child": edge["child"], "parent": edge["parent"], "basis": edge["basis"]}
        )

    # --- integrity: rules reference real concepts ----------------------------
    condition_ids = {c["id"] for c in conditions_doc["concepts"]}
    pattern_ids = {c["id"] for c in patterns_doc["concepts"]}
    for rule in rules_doc["rules"]:
        if rule["condition"] not in condition_ids:
            raise CurationError(f"{rule['id']}: unknown condition {rule['condition']}")
        for pattern_id in rule["exercise_match"].get("pattern_id_any_of", []):
            if pattern_id not in pattern_ids:
                raise CurationError(f"{rule['id']}: unknown pattern {pattern_id}")
        for target in rule["exercise_match"].get("targets_any_of", []):
            if target not in structures_raw:
                raise CurationError(f"{rule['id']}: unknown anatomy target {target}")
        for key in rule["exercise_match"]:
            if key.startswith("mechanics_"):
                for mech_key in rule["exercise_match"][key]:
                    if mech_key not in MECHANICS_KEYS:
                        raise CurationError(
                            f"{rule['id']}: unknown mechanics attribute {mech_key!r}"
                        )

    # --- integrity: conditions anchor into the partonomy ---------------------
    for condition in conditions_doc["concepts"]:
        if condition["anatomy_anchor"] not in structures_raw:
            raise CurationError(
                f"{condition['id']}: anatomy_anchor {condition['anatomy_anchor']} "
                "is not a declared structure"
            )

    # --- integrity: substitution graph is referentially closed ---------------
    equipment_ids = {c["id"] for c in equipment_doc["concepts"]}
    substitutions: list[dict[str, Any]] = []
    for item in equipment_doc["concepts"]:
        for sub in item.get("substitutable_for", []):
            if sub["target"] not in equipment_ids:
                raise CurationError(
                    f"{item['id']}: substitution target {sub['target']} unknown"
                )
            substitutions.append(
                {
                    "src": item["id"],
                    "dst": sub["target"],
                    "degree": sub["degree"],
                    "note": sub.get("note"),
                }
            )

    # --- derive the RDF (SKOS) graph and validate the PROV Turtle ------------
    rdf = Graph()
    for prefix, iri in prefixes.items():
        rdf.bind(prefix, Namespace(iri))
    vocab_concepts = (
        joints_doc["concepts"]
        + muscles_doc["concepts"]
        + patterns_doc["concepts"]
        + equipment_doc["concepts"]
        + conditions_doc["concepts"]
    )
    for concept in vocab_concepts:
        _add_concept_triples(rdf, kgc, concept, prefixes)
    for ttl_name in ("prov-model.ttl", "prov-example.ttl"):
        prov_graph = Graph()
        prov_graph.parse(ontology_dir / ttl_name, format="turtle")
        print(f"  parsed {ttl_name}: {len(prov_graph)} triples")

    # --- node payloads --------------------------------------------------------
    structures = [
        {
            "curie": curie,
            "iri": _expand(curie, prefixes),
            "label": spec["label"],
            "kind": spec.get("kind"),
        }
        for curie, spec in structures_raw.items()
    ]

    joints = []
    for concept in joints_doc["concepts"]:
        exact = [
            m for m in concept["mappings"] if m["predicate"] == "skos:exactMatch"
        ]
        if len(exact) != 1:
            raise CurationError(f"{concept['id']}: joints need exactly one exactMatch")
        curie = exact[0]["target"]
        if curie not in structures_raw:
            raise CurationError(
                f"{concept['id']}: exactMatch {curie} missing from partonomy — "
                "the closure query would never terminate at this joint"
            )
        props = _concept_common(concept, prefixes)
        props["is_catalog_joint"] = True
        laterality = concept.get("laterality") or {}
        for side in ("left", "right"):
            if side in laterality:
                props[f"laterality_{side}"] = laterality[side]["target"]
        if concept.get("region_concept"):
            props["region_curie"] = concept["region_concept"]["target"]
        joints.append({"curie": curie, "props": props})

    maps_to: list[dict[str, str]] = []

    def _plain_concepts(doc: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for concept in doc["concepts"]:
            props = _concept_common(concept, prefixes)
            out.append({"id": concept["id"], "props": props})
            for mapping in concept.get("mappings", []):
                if mapping["target"] in structures_raw:
                    # The predicate travels onto the edge: a narrower/broader
                    # mapping must not be mistaken for identity downstream
                    # (e.g. "glutes" skos:narrower "gluteus medius" is not
                    # "this exercise targets gluteus medius").
                    maps_to.append(
                        {
                            "src": concept["id"],
                            "dst": mapping["target"],
                            "predicate": SKOS_PREDICATES[mapping["predicate"]][1],
                            "confidence": mapping.get("confidence"),
                        }
                    )
        return out

    muscle_groups = _plain_concepts(muscles_doc)

    patterns = []
    for concept in patterns_doc["concepts"]:
        props = _concept_common(concept, prefixes)
        props["facet"] = concept["facet"]
        if concept.get("category"):
            props["category"] = concept["category"]
        if concept.get("subtype"):
            props["subtype"] = concept["subtype"]
        for key in MECHANICS_KEYS:
            props[key] = concept["mechanics"][key]
        patterns.append({"id": concept["id"], "props": props})

    equipment = []
    for concept in equipment_doc["concepts"]:
        props = _concept_common(concept, prefixes)
        props["equipment_class"] = concept["equipment_class"]
        props["home_gym_typical"] = concept["home_gym_typical"]
        equipment.append({"id": concept["id"], "props": props})

    conditions = []
    for concept in conditions_doc["concepts"]:
        props = _concept_common(concept, prefixes)
        props["anatomy_anchor"] = concept["anatomy_anchor"]
        props["hops_to_catalog_joint"] = concept["hops_to_catalog_joint"]
        conditions.append(
            {"id": concept["id"], "props": props, "anchor": concept["anatomy_anchor"]}
        )

    rules = []
    for rule in rules_doc["rules"]:
        match = rule["exercise_match"]
        rules.append(
            {
                "id": rule["id"],
                "condition": rule["condition"],
                "props": {
                    "id": rule["id"],
                    "decision": rule["decision"],
                    "priority": rule["priority"],
                    "score_delta": rule.get("score_delta"),
                    "coach_overridable": rule.get("coach_overridable", False),
                    "escalate_to_exclude_when_acute": rule.get(
                        "escalate_to_exclude_when_acute", False
                    ),
                    "rationale": rule["rationale"],
                    "require_anatomy_overlap": match["require_anatomy_overlap"],
                    # Nested match/applicability structures are evaluated in
                    # Python by safe_exercise_pool; Neo4j properties are flat,
                    # so they travel as JSON strings.
                    "applies_when_json": json.dumps(rule["applies_when"]),
                    "exercise_match_json": json.dumps(match),
                },
            }
        )

    # --- exercises, with quirks q1-q6 applied ---------------------------------
    joint_by_term = {j["props"]["catalog_term"]: j["curie"] for j in joints}
    id_by_term = {
        vocab: {c["props"]["catalog_term"]: c["id"] for c in concepts}
        for vocab, concepts in (
            ("muscles", muscle_groups),
            ("patterns", patterns),
            ("equipment", equipment),
        )
    }
    exercises = []
    edges: dict[str, list[dict[str, str]]] = {
        "TARGETS": [],
        "STRESSES": [],
        "REQUIRES": [],
        "HAS_PATTERN": [],
    }
    for row in exercises_raw:
        joints_loaded = sorted(set(row["joints_loaded"]))  # q6
        rate = row["estimated_rep_duration"]  # q15: reps per second
        exercises.append(
            {
                "id": row["id"],
                "props": {
                    "id": row["id"],
                    "name": row["name"],
                    "is_reps": row["is_reps"],
                    "supports_weight": row["supports_weight"],
                    # q15: 7 rows ship a 0 rate with no reciprocal. Keyed off
                    # the rate, not `is_reps` — one duration-only row (Kneeling
                    # Stability Ball Lat Stretch) carries 0.2 anyway.
                    "rep_seconds": 1 / rate if rate else 0.0,
                    "side": row["side"],  # null clears the property
                    "is_unilateral": row["side"] is not None,
                    "has_bilateral_pair": row["is_bilateral"],  # q1
                    "stresses_recorded": bool(joints_loaded),  # q4
                    # q2 bilateral_pair_id, q3 priority_tier / is_duration and
                    # q15 estimated_rep_duration are dropped: dangling,
                    # zero-entropy, and superseded by rep_seconds respectively.
                },
            }
        )
        for term in set(row["muscle_groups"]):
            edges["TARGETS"].append({"src": row["id"], "dst": id_by_term["muscles"][term]})
        for term in joints_loaded:
            edges["STRESSES"].append({"src": row["id"], "curie": joint_by_term[term]})
        for term in set(row["equipment_required"]):
            edges["REQUIRES"].append({"src": row["id"], "dst": id_by_term["equipment"][term]})
        for term in set(row["movement_patterns"]):  # q5 resolves "car" here
            edges["HAS_PATTERN"].append({"src": row["id"], "dst": id_by_term["patterns"][term]})

    return Kg1Payload(
        structures=structures,
        part_of_edges=part_of_edges,
        is_a_edges=is_a_edges,
        joints=joints,
        muscle_groups=muscle_groups,
        patterns=patterns,
        equipment=equipment,
        conditions=conditions,
        rules=rules,
        exercises=exercises,
        maps_to=maps_to,
        substitutions=substitutions,
        exercise_edges=edges,
        rdf=rdf,
    )


# ---------------------------------------------------------------------------
# KG 2 — member context
# ---------------------------------------------------------------------------

# Labels owned by the KG 2 phase. Every non-Member node also carries
# :MemberFact, which gives one uniqueness constraint and one reset handle.
KG2_LABELS = ("Member", "MemberFact")

EXPECTED_KG2 = {
    "goals": 3,
    "equipment_links": 5,
    "injuries": 1,
    "workouts": 4,
    "adherence_weeks": 4,
    "weight_samples": 3,
    "lab_results": 12,
    "chat_messages": 4,
    "brief_tasks": 2,
}


def load_member_context(data_dir: Path) -> dict[str, Any]:
    """Parse and quirk-correct member-context.json into loadable rows."""
    doc = _load_json(data_dir / "member-context.json")
    profile = doc["profile"]
    member_id = profile["id"]
    preferences = doc["preferences"]
    brief = doc["coach_brief"]

    member_props = {
        **profile,
        "preferred_session_minutes": preferences["preferred_session_minutes"],
        "training_days_per_week": preferences["training_days_per_week"],
        "preferred_days": preferences["preferred_days"],
        # Dislikes are raw member language on purpose: neither string appears
        # in the catalog (quirk 9), so exclusion happens through the resolver
        # at request time, not through a precomputed join.
        "dislikes": preferences["dislikes"],
        "preference_notes": preferences["notes"],
        "adherence_trend": doc["adherence"]["trend"],
        # q13: every recency/trend computation anchors here, not at the wall
        # clock — the dataset lives in mid-2026.
        "now_anchor": brief["generated_for"],
    }

    injuries = []
    for injury in doc["injuries"]:
        region = injury["region"]
        side = next(
            (s for s in ("left", "right") if region.lower().startswith(s)), None
        )
        injuries.append(
            {
                "id": injury["id"],
                "joint_term": injury["joint"],
                "props": {
                    "id": injury["id"],
                    "region": region,
                    "side": side,
                    "joint": injury["joint"],
                    "status": injury["status"],
                    "severity": injury["severity"],
                    "since": injury["since"],
                    "notes": injury["notes"],
                },
            }
        )

    workouts = [
        {
            "id": f"wo_{w['date']}",
            "date": w["date"],
            "title": w["title"],
            "planned": w["planned"],
            "completed": w["completed"],
            "duration_min": w["duration_min"],
            "rpe": w["rpe"],
            # q10: these names match nothing in the catalog — free text only.
            "exercise_names": w["exercises"],
        }
        for w in doc["workout_history"]
    ]

    lab_panels = []
    lab_results = []
    for panel_type, panel in doc["labs"].items():
        panel_id = f"lab_{panel_type}_{panel['date']}"
        lab_panels.append(
            {"id": panel_id, "panel_type": panel_type, "date": panel["date"]}
        )
        for key, value in panel.items():
            if key == "date":
                continue
            # q12 lives downstream: values carry no reference ranges, so no
            # high/low verdict is stored here — only the measurements.
            lab_results.append(
                {"id": f"{panel_id}_{key}", "panel_id": panel_id,
                 "name": key, "value": value}
            )

    chat_messages = []
    for message in doc["chat_history"]:
        attachments = message.get("attachments", [])  # q14: absent, not null
        chat_messages.append(
            {
                "id": f"chat_{message['ts']}",
                "ts": message["ts"],
                "sender": message["from"],
                "text": message["text"],
                "has_attachments": bool(attachments),
                "attachment_types": [a.get("type") for a in attachments],
                "attachment_captions": [a.get("caption") for a in attachments],
            }
        )

    brief_id = f"brief_{brief['generated_for']}"
    brief_props = {
        "id": brief_id,
        "generated_for": brief["generated_for"],
        "churn_risk_level": brief["churn_risk"]["level"],
        # q11: the third reason (login frequency) has no backing field in the
        # dataset; this text IS the only citable source for it.
        "churn_risk_reasons": brief["churn_risk"]["reasons"],
    }
    brief_tasks = [
        {"id": f"{brief_id}_task_{i}", "type": t["type"], "text": t["text"],
         "position": i}
        for i, t in enumerate(brief["morning_tasks"])
    ]

    return {
        "member_id": member_id,
        "member_props": member_props,
        "goals": doc["goals"],
        "equipment_terms": doc["equipment_available"],
        "injuries": injuries,
        "workouts": workouts,
        "adherence_weeks": doc["adherence"]["weekly_completion_pct"],
        "biomarkers": {
            "id": f"bio_{member_id}",
            "resting_hr_bpm": doc["biomarkers"]["resting_hr_bpm"],
            "hrv_ms": doc["biomarkers"]["hrv_ms"],
            "sleep_hours_last_7_days": doc["biomarkers"]["sleep_hours_last_7_days"],
        },
        "weight_samples": doc["biomarkers"]["weight_trend_kg"],
        "lab_panels": lab_panels,
        "lab_results": lab_results,
        "chat_messages": chat_messages,
        "brief": brief_props,
        "brief_tasks": brief_tasks,
    }


KG2_SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT member_id IF NOT EXISTS "
    "FOR (m:Member) REQUIRE m.id IS UNIQUE",
    "CREATE CONSTRAINT member_fact_id IF NOT EXISTS "
    "FOR (f:MemberFact) REQUIRE f.id IS UNIQUE",
]


def load_kg2_neo4j(session: Any, member: dict[str, Any], reset: bool) -> None:
    """MERGE the member-context graph and cross-link it into KG 1."""
    if reset:
        for label in KG2_LABELS:
            session.run(f"MATCH (n:{label}) DETACH DELETE n")
    for statement in KG2_SCHEMA_STATEMENTS:
        session.run(statement)

    member_id = member["member_id"]
    session.run(
        "MERGE (m:Member {id: $id}) SET m += $props",
        id=member_id,
        props=member["member_props"],
    )

    def link(label: str, rel: str, rows: list[dict[str, Any]]) -> None:
        session.run(
            f"UNWIND $rows AS row MATCH (m:Member {{id: $member_id}}) "
            f"MERGE (f:{label}:MemberFact {{id: row.id}}) SET f += row "
            f"MERGE (m)-[:{rel}]->(f)",
            rows=rows,
            member_id=member_id,
        )

    link("Goal", "HAS_GOAL", member["goals"])
    link("WorkoutSession", "PERFORMED", member["workouts"])
    link(
        "AdherenceWeek",
        "HAS_ADHERENCE_WEEK",
        [
            {"id": f"aw_{w['week_of']}", "week_of": w["week_of"], "pct": w["pct"]}
            for w in member["adherence_weeks"]
        ],
    )
    link("BiomarkerSnapshot", "HAS_BIOMARKERS", [member["biomarkers"]])
    link(
        "WeightSample",
        "HAS_WEIGHT_SAMPLE",
        [
            {"id": f"wt_{w['date']}", "date": w["date"], "kg": w["kg"]}
            for w in member["weight_samples"]
        ],
    )
    link("LabPanel", "HAS_LAB_PANEL", member["lab_panels"])
    session.run(
        "UNWIND $rows AS row MATCH (p:LabPanel {id: row.panel_id}) "
        "MERGE (r:LabResult:MemberFact {id: row.id}) "
        "SET r.name = row.name, r.value = row.value "
        "MERGE (p)-[:HAS_RESULT]->(r)",
        rows=member["lab_results"],
    )
    link("ChatMessage", "HAS_CHAT_MESSAGE", member["chat_messages"])
    link("CoachBrief", "HAS_BRIEF", [member["brief"]])
    session.run(
        "UNWIND $rows AS row MATCH (b:CoachBrief {id: $brief_id}) "
        "MERGE (t:BriefTask:MemberFact {id: row.id}) SET t += row "
        "MERGE (b)-[:HAS_TASK]->(t)",
        rows=member["brief_tasks"],
        brief_id=member["brief"]["id"],
    )

    # Cross-links into KG 1. Equipment names in the member file are exact
    # catalog terms; the injury joins through its structured `joint` field to
    # the Joint node, which carries the SNOMED mapping (issue #7 acceptance).
    equipment_result = session.run(
        "UNWIND $terms AS term "
        "MATCH (m:Member {id: $member_id}) "
        "MATCH (q:Equipment {catalog_term: term}) "
        "MERGE (m)-[:HAS_EQUIPMENT]->(q) RETURN count(q) AS n",
        terms=member["equipment_terms"],
        member_id=member_id,
    ).single()
    if equipment_result["n"] != len(member["equipment_terms"]):
        raise CurationError(
            f"only {equipment_result['n']} of "
            f"{len(member['equipment_terms'])} equipment terms matched "
            "catalog Equipment nodes"
        )
    for injury in member["injuries"]:
        link("Injury", "HAS_INJURY", [injury["props"]])
        joined = session.run(
            "MATCH (i:Injury {id: $id}) "
            "MATCH (j:Joint {catalog_term: $term}) "
            "MERGE (i)-[:AFFECTS]->(j) RETURN count(j) AS n",
            id=injury["id"],
            term=injury["joint_term"],
        ).single()
        if joined["n"] != 1:
            raise CurationError(
                f"injury {injury['id']} joint {injury['joint_term']!r} "
                "matched no catalog Joint node"
            )

    _verify_kg2(session, member_id)


def _verify_kg2(session: Any, member_id: str) -> None:
    """Post-load checks: the acceptance queries from issue #7."""
    counts_query = (
        "MATCH (m:Member {id: $id}) RETURN "
        "count{(m)-[:HAS_GOAL]->()} AS goals, "
        "count{(m)-[:HAS_EQUIPMENT]->()} AS equipment_links, "
        "count{(m)-[:HAS_INJURY]->()} AS injuries, "
        "count{(m)-[:PERFORMED]->()} AS workouts, "
        "count{(m)-[:HAS_ADHERENCE_WEEK]->()} AS adherence_weeks, "
        "count{(m)-[:HAS_WEIGHT_SAMPLE]->()} AS weight_samples, "
        "count{(m)-[:HAS_LAB_PANEL]->()-[:HAS_RESULT]->()} AS lab_results, "
        "count{(m)-[:HAS_CHAT_MESSAGE]->()} AS chat_messages, "
        "count{(m)-[:HAS_BRIEF]->()-[:HAS_TASK]->()} AS brief_tasks"
    )
    record = session.run(counts_query, id=member_id).single()
    failures = [
        f"{key}: expected {expected}, got {record[key]}"
        for key, expected in EXPECTED_KG2.items()
        if record[key] != expected
    ]
    for key in EXPECTED_KG2:
        print(f"  kg2 {key:16s} {record[key]:3d} (expected {EXPECTED_KG2[key]})")

    snomed = session.run(
        "MATCH (:Member {id: $id})-[:HAS_INJURY]->(i)-[:AFFECTS]->(j:Joint) "
        "RETURN i.id AS injury, j.id AS joint, j.exact_match AS snomed",
        id=member_id,
    ).single()
    print(
        f"  kg2 injury cross-link: {snomed['injury']} → {snomed['joint']} "
        f"({snomed['snomed']})"
    )
    if snomed["joint"] != "jt_knee" or not snomed["snomed"]:
        failures.append("injury did not cross-link to the SNOMED-mapped knee joint")

    if failures:
        raise CurationError("KG 2 verification failed: " + "; ".join(failures))


SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT exercise_id IF NOT EXISTS "
    "FOR (e:Exercise) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT concept_id IF NOT EXISTS "
    "FOR (c:Concept) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT anatomy_curie IF NOT EXISTS "
    "FOR (a:AnatomicalStructure) REQUIRE a.curie IS UNIQUE",
    "CREATE CONSTRAINT rule_id IF NOT EXISTS "
    "FOR (r:SafetyRule) REQUIRE r.id IS UNIQUE",
    "CREATE FULLTEXT INDEX concept_text IF NOT EXISTS "
    "FOR (c:Concept) ON EACH [c.pref_label, c.alt_labels_text, c.catalog_term]",
    "CREATE VECTOR INDEX concept_embedding IF NOT EXISTS "
    "FOR (c:Concept) ON (c.embedding) OPTIONS "
    "{indexConfig: {`vector.dimensions`: "
    f"{EMBEDDING_DIM}"
    ", `vector.similarity_function`: 'cosine'}}",
]


def load_neo4j(payload: Kg1Payload, member: dict[str, Any], uri: str, user: str,
               password: str, reset: bool, skip_embeddings: bool) -> None:
    """MERGE both graphs into Neo4j and build the indexes."""
    from neo4j import GraphDatabase

    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        with driver.session() as session:
            if reset:
                for label in KG1_LABELS:
                    session.run(f"MATCH (n:{label}) DETACH DELETE n")
            for statement in SCHEMA_STATEMENTS:
                session.run(statement)

            def run(query: str, **params: Any) -> None:
                session.run(query, **params)

            run(
                "UNWIND $rows AS row MERGE (a:AnatomicalStructure {curie: row.curie}) "
                "SET a.iri = row.iri, a.label = row.label, a.kind = row.kind",
                rows=payload.structures,
            )
            for rel, rows in (("PART_OF", payload.part_of_edges),
                              ("IS_A", payload.is_a_edges)):
                run(
                    "UNWIND $rows AS row "
                    "MATCH (c:AnatomicalStructure {curie: row.child}) "
                    "MATCH (p:AnatomicalStructure {curie: row.parent}) "
                    f"MERGE (c)-[r:{rel}]->(p) SET r.basis = row.basis",
                    rows=rows,
                )
            # The 9 catalog joints are the same nodes as their SNOMED structures:
            # that identity is what lets the safety closure end on a node that
            # exercises STRESS directly (docs/kg1-schema.md §4).
            run(
                "UNWIND $rows AS row "
                "MATCH (a:AnatomicalStructure {curie: row.curie}) "
                "SET a:Joint:Concept, a += row.props",
                rows=payload.joints,
            )
            for label, rows in (
                ("MuscleGroup", payload.muscle_groups),
                ("MovementPattern", payload.patterns),
                ("Equipment", payload.equipment),
                ("Condition", payload.conditions),
            ):
                run(
                    f"UNWIND $rows AS row MERGE (c:{label}:Concept {{id: row.id}}) "
                    "SET c += row.props",
                    rows=[{"id": r["id"], "props": r["props"]} for r in rows],
                )
            run(
                "UNWIND $rows AS row MATCH (c:Concept {id: row.src}) "
                "MATCH (a:AnatomicalStructure {curie: row.dst}) "
                "MERGE (c)-[m:MAPS_TO]->(a) "
                "SET m.predicate = row.predicate, m.confidence = row.confidence",
                rows=payload.maps_to,
            )
            run(
                "UNWIND $rows AS row MATCH (c:Condition {id: row.id}) "
                "MATCH (a:AnatomicalStructure {curie: row.anchor}) "
                "MERGE (c)-[:ANCHORED_AT]->(a)",
                rows=[{"id": r["id"], "anchor": r["anchor"]} for r in payload.conditions],
            )
            run(
                "UNWIND $rows AS row MERGE (r:SafetyRule {id: row.id}) "
                "SET r += row.props "
                "WITH r, row MATCH (c:Condition {id: row.condition}) "
                "MERGE (r)-[:CONTRAINDICATED_FOR]->(c)",
                rows=payload.rules,
            )
            run(
                "UNWIND $rows AS row MATCH (a:Equipment {id: row.src}) "
                "MATCH (b:Equipment {id: row.dst}) "
                "MERGE (a)-[s:SUBSTITUTABLE_FOR]->(b) "
                "SET s.degree = row.degree, s.note = row.note",
                rows=payload.substitutions,
            )
            run(
                # `=` rather than `+=`, unlike the concept writes above: this
                # script advertises *dropping* catalog fields (q2, q3, q15) and
                # an additive write can only ever add, so on a pre-existing
                # graph the dropped properties would survive forever. Replacing
                # wholesale is what makes those drops real. `row.props` carries
                # `id`, so the MERGE key round-trips.
                "UNWIND $rows AS row MERGE (e:Exercise {id: row.id}) SET e = row.props",
                rows=payload.exercises,
            )
            for rel, rows in payload.exercise_edges.items():
                if rel == "STRESSES":
                    run(
                        "UNWIND $rows AS row MATCH (e:Exercise {id: row.src}) "
                        "MATCH (j:Joint {curie: row.curie}) MERGE (e)-[:STRESSES]->(j)",
                        rows=rows,
                    )
                else:
                    run(
                        "UNWIND $rows AS row MATCH (e:Exercise {id: row.src}) "
                        f"MATCH (c:Concept {{id: row.dst}}) MERGE (e)-[:{rel}]->(c)",
                        rows=rows,
                    )

            if not skip_embeddings:
                concepts = (
                    [j["props"] for j in payload.joints]
                    + [c["props"] for c in payload.muscle_groups]
                    + [c["props"] for c in payload.patterns]
                    + [c["props"] for c in payload.equipment]
                    + [c["props"] for c in payload.conditions]
                )
                print(f"  embedding {len(concepts)} concepts with fastembed …")
                vectors = embed_texts(
                    _embedding_text(
                        {
                            "pref_label": c["pref_label"],
                            "catalog_term": c.get("catalog_term"),
                            "alt_labels": c["alt_labels"],
                        }
                    )
                    for c in concepts
                )
                run(
                    "UNWIND $rows AS row MATCH (c:Concept {id: row.id}) "
                    "CALL db.create.setNodeVectorProperty(c, 'embedding', row.vector) "
                    "RETURN count(*)",
                    rows=[
                        {"id": c["id"], "vector": v}
                        for c, v in zip(concepts, vectors, strict=True)
                    ],
                )

            _verify(session)
            load_kg2_neo4j(session, member, reset)


def _verify(session: Any) -> None:
    """Post-load checks: node counts and the two acceptance traversals."""
    failures: list[str] = []
    for label, expected in EXPECTED_COUNTS.items():
        got = session.run(
            f"MATCH (n:{label}) RETURN count(n) AS n"
        ).single()["n"]
        status = "ok" if got == expected else "MISMATCH"
        if got != expected:
            failures.append(f"{label}: expected {expected}, got {got}")
        print(f"  {label:22s} {got:4d} (expected {expected}) {status}")

    knee = session.run(
        "MATCH (j:Joint {id: 'jt_knee'})<-[:STRESSES]-(ex:Exercise) "
        "RETURN count(DISTINCT ex) AS n"
    ).single()["n"]
    print(f"  exercises stressing the knee: {knee} (expected 21)")
    if knee != 21:
        failures.append(f"knee STRESSES count {knee} != 21")

    # Acceptance walk: condition anchor -> anatomy closure -> catalog joint ->
    # stressed exercises, exactly the query shape in docs/kg1-schema.md §4.
    closure = (
        "MATCH (c:Condition)-[:ANCHORED_AT]->(a) "
        "MATCH (a)-[:PART_OF|IS_A*0..5]->(anc) "
        "MATCH (j:Joint) WHERE j = anc OR (j)-[:PART_OF]->(anc) "
        "RETURN c.id AS cond, collect(DISTINCT j.id) AS joints"
    )
    reached = {rec["cond"]: rec["joints"] for rec in session.run(closure)}
    unreachable = [
        rec["id"]
        for rec in session.run("MATCH (c:Condition) RETURN c.id AS id")
        if not reached.get(rec["id"])
    ]
    print(f"  conditions reaching a catalog joint via closure: {len(reached)}/16")
    if unreachable:
        failures.append(f"conditions unreachable under closure: {unreachable}")

    pfps = session.run(
        "MATCH (c:Condition {id: 'cond_pfps'})-[:ANCHORED_AT]->(a) "
        "MATCH (a)-[:PART_OF*0..5]->(j:Joint {id: 'jt_knee'}) "
        "MATCH (ex:Exercise)-[:STRESSES]->(j) "
        "RETURN count(DISTINCT ex) AS n"
    ).single()["n"]
    print(f"  cond_pfps → knee closure → stressed exercises: {pfps} (expected 21)")
    if pfps != 21:
        failures.append(f"cond_pfps closure exercise count {pfps} != 21")

    rules = session.run(
        "MATCH (r:SafetyRule)-[:CONTRAINDICATED_FOR]->(:Condition) "
        "RETURN count(r) AS n"
    ).single()["n"]
    print(f"  SafetyRule -CONTRAINDICATED_FOR-> Condition edges: {rules} (expected 22)")
    if rules != 22:
        failures.append(f"rule edge count {rules} != 22")

    if failures:
        raise CurationError("post-load verification failed: " + "; ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-embeddings", action="store_true")
    parser.add_argument("--emit-ttl", type=Path, default=None)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR", _REPO_ROOT / "data"))
    print(f"KG build — data dir {data_dir}")
    payload = load_and_validate(data_dir)
    member = load_member_context(data_dir)
    edge_total = sum(len(v) for v in payload.exercise_edges.values())
    print(
        f"  validated: {len(payload.exercises)} exercises, "
        f"{len(payload.structures)} structures, "
        f"{len(payload.part_of_edges)}+{len(payload.is_a_edges)} partonomy edges, "
        f"{edge_total} exercise edges, {len(payload.rules)} rules, "
        f"{len(payload.rdf)} derived SKOS triples"
    )
    print(
        f"  member context: {member['member_id']} "
        f"({len(member['goals'])} goals, {len(member['chat_messages'])} chat "
        f"messages, anchor {member['member_props']['now_anchor']})"
    )
    if args.emit_ttl:
        payload.rdf.serialize(destination=args.emit_ttl, format="turtle")
        print(f"  wrote derived Turtle to {args.emit_ttl}")
    if args.dry_run:
        print("  dry run — skipping Neo4j load")
        return 0

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "password")
    print(f"  loading Neo4j at {uri}")
    load_neo4j(
        payload,
        member,
        uri=uri,
        user=user,
        password=password,
        reset=args.reset,
        skip_embeddings=args.skip_embeddings,
    )
    print("KG build complete (KG 1 + KG 2).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
