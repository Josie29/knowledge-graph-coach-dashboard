import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from neo4j import AsyncDriver
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from pydantic import BaseModel, Field

from app.kg.resolver import resolve_concepts
from app.kg.safety import InjuryConstraint, PoolConstraints, safe_exercise_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/graph", tags=["graph"])

GRAPH_READ_TIMEOUT_SECONDS = 3.0
OVERLAY_TIMEOUT_SECONDS = 15.0
"""Much longer than the structural read: the overlay runs the resolver's
embedding pass, which loads a local ONNX model on the first call."""

# `Concept` is a mixin applied alongside the real type, and `MemberFact` is the
# KG 2 umbrella label — neither is what a coach should see on a node. Anything
# not listed keeps whatever label Neo4j returns first.
_LABEL_PRIORITY = (
    "Member",
    "Injury",
    "Goal",
    "Exercise",
    "Joint",
    "MuscleGroup",
    "MovementPattern",
    "Equipment",
    "Condition",
    "SafetyRule",
    "AnatomicalStructure",
)
_GENERIC_LABELS = frozenset({"Concept", "MemberFact"})

# Embeddings are 384 floats per Concept node — useless to the UI and enough to
# dominate the payload if they ride along in `properties(n)`.
_HIDDEN_PROPERTIES = frozenset({"embedding"})

# Display names live under a different property per label (Exercise.name,
# Concept.pref_label, Injury.region, Goal.text, SafetyRule.rationale, ...).
# Normalising here means the client never branches on node type to find a name.
_NODE_PROJECTION = (
    "{{id: coalesce({var}.id, {var}.curie, elementId({var})), "
    "label: coalesce({var}.name, {var}.pref_label, {var}.label, {var}.region, "
    "{var}.text, {var}.rationale, {var}.id, {var}.curie), "
    "labels: labels({var}), properties: properties({var})}}"
)


def _projection(var: str) -> str:
    """Build the normalised node projection for a Cypher variable.

    Args:
        var: The Cypher variable name bound to a node.

    Returns:
        A Cypher map-projection string yielding id, label, labels, properties.
    """
    return _NODE_PROJECTION.format(var=var)


_MEMBER_CORE_QUERY = (
    "MATCH (m:Member {id: $id}) "
    "OPTIONAL MATCH (m)-[r]->(n) "
    "WHERE type(r) IN ['HAS_GOAL', 'HAS_EQUIPMENT', 'HAS_INJURY'] "
    f"RETURN {_projection('m')} AS member, "
    f"collect(DISTINCT {{rel: type(r), node: {_projection('n')}}}) AS links"
)

_INJURY_CHAIN_QUERY = (
    "MATCH (m:Member {id: $id})-[:HAS_INJURY]->(i:Injury)-[:AFFECTS]->(j:Joint) "
    "OPTIONAL MATCH (j)<-[:STRESSES]-(e:Exercise) "
    f"RETURN {_projection('i')} AS injury, {_projection('j')} AS joint, "
    f"collect(DISTINCT {_projection('e')}) AS exercises"
)

_EXERCISE_NODES_QUERY = (
    "MATCH (e:Exercise) WHERE e.id IN $ids "
    f"RETURN {_projection('e')} AS exercise"
)

_EXERCISE_NEIGHBOURS_QUERY = (
    "MATCH (e:Exercise) WHERE e.id IN $ids "
    "MATCH (e)-[r:TARGETS|REQUIRES|HAS_PATTERN]->(n) "
    f"RETURN e.id AS exercise_id, type(r) AS rel, {_projection('n')} AS node"
)

_SAFETY_OVERLAY_QUERY = (
    "MATCH (c:Condition) WHERE c.id IN $condition_ids "
    "OPTIONAL MATCH (r:SafetyRule)-[:CONTRAINDICATED_FOR]->(c) "
    "OPTIONAL MATCH (c)-[:ANCHORED_AT]->(a:AnatomicalStructure) "
    f"RETURN {_projection('c')} AS condition, {_projection('r')} AS rule, "
    f"{_projection('a')} AS anchor"
)


class _MemberInjury(BaseModel):
    """One injury node from the structural walk, kept for the overlay."""

    node_id: str
    notes: str
    status: str = "recovering"
    severity: str = "mild"


class _StructuralLayer(BaseModel):
    """What the stored-edge walk found that the overlay still needs."""

    exercise_ids: set[str] = Field(default_factory=set)
    injuries: list[_MemberInjury] = Field(default_factory=list)


class _InjuryResolution(BaseModel):
    """The outcome of resolving injury prose to clinical conditions."""

    constraints: list[InjuryConstraint] = Field(default_factory=list)
    condition_by_injury: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class GraphNode(BaseModel):
    """One node in the member subgraph, normalised for rendering."""

    id: str
    label: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    blocked: bool = False


class GraphEdge(BaseModel):
    """One edge in the member subgraph.

    ``derived`` marks edges the graph does not store — currently only
    ``BLOCKS``, which the safety layer computes. The UI dashes them so the
    picture never implies a relationship that is not in Neo4j.
    """

    id: str
    source: str
    target: str
    type: str
    derived: bool = False


class MemberSubgraphResponse(BaseModel):
    """The member-centric subgraph plus what the legend needs."""

    member_id: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    counts: dict[str, int]
    notes: list[str] = Field(default_factory=list)


def _node_type(labels: list[str]) -> str:
    """Pick the display type for a node from its Neo4j labels.

    Args:
        labels: All labels Neo4j reports for the node.

    Returns:
        The most specific known label, else the first non-generic label, else
        ``"Unknown"`` for a node carrying only mixin labels.
    """
    for candidate in _LABEL_PRIORITY:
        if candidate in labels:
            return candidate
    specific = [label for label in labels if label not in _GENERIC_LABELS]
    return specific[0] if specific else "Unknown"


def _clean_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Drop bulky properties and coerce Neo4j temporals to JSON-safe values.

    Neo4j returns its own Date/DateTime/Duration types, which Pydantic cannot
    serialise; the inspector panel only ever displays them, so string form is
    the right shape.

    Args:
        properties: Raw ``properties(n)`` map from the driver.

    Returns:
        A JSON-serialisable copy without hidden properties.
    """
    cleaned: dict[str, Any] = {}
    for key, value in properties.items():
        if key in _HIDDEN_PROPERTIES:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            cleaned[key] = value
        elif isinstance(value, list):
            cleaned[key] = [
                item if isinstance(item, (str, int, float, bool)) else str(item)
                for item in value
            ]
        else:
            cleaned[key] = str(value)
    return cleaned


class _SubgraphBuilder:
    """Accumulates nodes and edges, deduplicating both.

    Nodes arrive from several queries and edges from several paths, so the
    same node or edge is offered more than once by design. Collecting through
    one object keeps that dedup in a single place and guarantees the
    referential integrity the renderer depends on.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}

    def add_node(self, projected: dict[str, Any] | None) -> str | None:
        """Add a projected node, keeping the first version seen.

        Args:
            projected: A row from ``_projection``, or None from an
                OPTIONAL MATCH that did not bind.

        Returns:
            The node id, or None when there was no node.
        """
        if not projected or projected.get("id") is None:
            return None
        node_id = str(projected["id"])
        if node_id not in self._nodes:
            self._nodes[node_id] = GraphNode(
                id=node_id,
                label=str(projected.get("label") or node_id),
                type=_node_type(projected.get("labels") or []),
                properties=_clean_properties(projected.get("properties") or {}),
            )
        return node_id

    def add_edge(
        self, source: str | None, edge_type: str | None, target: str | None, *,
        derived: bool = False,
    ) -> None:
        """Add an edge, ignoring it unless both endpoints are already nodes.

        Silently dropping half-bound edges is what keeps every edge's source
        and target resolvable in the response — a dangling edge crashes the
        force-graph renderer on load.
        """
        if not source or not target or not edge_type:
            return
        if source not in self._nodes or target not in self._nodes:
            logger.warning(
                "Dropping %s edge with unknown endpoint (%s -> %s)",
                edge_type, source, target,
            )
            return
        edge_id = f"{source}|{edge_type}|{target}"
        if edge_id not in self._edges:
            self._edges[edge_id] = GraphEdge(
                id=edge_id,
                source=source,
                target=target,
                type=edge_type,
                derived=derived,
            )

    def mark_blocked(self, node_id: str) -> None:
        """Flag a node as excluded by a safety rule, if it is in the graph."""
        node = self._nodes.get(node_id)
        if node is not None:
            node.blocked = True

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def build(self, member_id: str, notes: list[str]) -> MemberSubgraphResponse:
        """Freeze the accumulated graph into the response model.

        Nodes and edges are sorted because a force layout seeds from input
        order: unsorted output would rearrange the picture on every reload
        even when the underlying graph has not changed.
        """
        counts: dict[str, int] = {}
        for node in self._nodes.values():
            counts[node.type] = counts.get(node.type, 0) + 1
        return MemberSubgraphResponse(
            member_id=member_id,
            nodes=sorted(self._nodes.values(), key=lambda n: (n.type, n.id)),
            edges=sorted(self._edges.values(), key=lambda e: e.id),
            counts=dict(sorted(counts.items())),
            notes=notes,
        )


async def _add_structural_layer(
    driver: AsyncDriver, builder: _SubgraphBuilder, member_id: str
) -> _StructuralLayer:
    """Walk the member's real stored edges into the builder.

    Covers ``HAS_GOAL`` / ``HAS_EQUIPMENT`` / ``HAS_INJURY`` off the member,
    then ``Injury-[:AFFECTS]->Joint<-[:STRESSES]-Exercise``.

    Returns:
        The exercises reached through the injured joints, plus the injuries
        themselves — the overlay needs their free-text notes to resolve.

    Raises:
        HTTPException: 404 when the member is not in KG 2.
    """
    records, _, _ = await driver.execute_query(
        _MEMBER_CORE_QUERY, id=member_id, routing_="r"
    )
    if not records or records[0]["member"] is None:
        raise HTTPException(status_code=404, detail=f"Member {member_id!r} not found")

    member_node_id = builder.add_node(records[0]["member"])
    for link in records[0]["links"]:
        target_id = builder.add_node(link.get("node"))
        builder.add_edge(member_node_id, link.get("rel"), target_id)

    chain_records, _, _ = await driver.execute_query(
        _INJURY_CHAIN_QUERY, id=member_id, routing_="r"
    )
    layer = _StructuralLayer()
    for record in chain_records:
        injury_id = builder.add_node(record["injury"])
        joint_id = builder.add_node(record["joint"])
        builder.add_edge(injury_id, "AFFECTS", joint_id)

        injury_props = (record["injury"] or {}).get("properties") or {}
        # One row per affected joint, so the same injury can arrive twice.
        already_seen = any(item.node_id == injury_id for item in layer.injuries)
        if injury_id and not already_seen and injury_props.get("notes"):
            layer.injuries.append(
                _MemberInjury(
                    node_id=injury_id,
                    notes=str(injury_props["notes"]),
                    status=str(injury_props.get("status") or "recovering"),
                    severity=str(injury_props.get("severity") or "mild"),
                )
            )
        for exercise in record["exercises"]:
            exercise_id = builder.add_node(exercise)
            if exercise_id is None:
                continue
            # STRESSES points exercise -> joint in the graph; keep that direction.
            builder.add_edge(exercise_id, "STRESSES", joint_id)
            layer.exercise_ids.add(exercise_id)
    return layer


async def _add_exercise_neighbours(
    driver: AsyncDriver, builder: _SubgraphBuilder, exercise_ids: set[str]
) -> None:
    """Attach each exercise's muscle groups, equipment, and movement patterns."""
    if not exercise_ids:
        return
    records, _, _ = await driver.execute_query(
        _EXERCISE_NEIGHBOURS_QUERY, ids=sorted(exercise_ids), routing_="r"
    )
    for record in records:
        neighbour_id = builder.add_node(record["node"])
        builder.add_edge(record["exercise_id"], record["rel"], neighbour_id)


async def _resolve_injuries(
    driver: AsyncDriver, injuries: list[_MemberInjury]
) -> _InjuryResolution:
    """Resolve each injury's free-text note to a Condition.

    KG 2 stores no ``Injury -> Condition`` edge: the link is made at request
    time by the same resolver the workout generator uses, against the injury's
    prose. That jump is the least obvious step in the whole chain, which is
    why the caller draws it as a derived ``RESOLVES_TO`` edge.

    Args:
        driver: Injected Neo4j async driver.
        injuries: Injuries found by the structural walk.

    Returns:
        The resolved constraints, the injury-to-condition mapping, and one
        note per attempt — including failures, which mean rules did not fire.
    """
    resolution = _InjuryResolution()
    constraints = resolution.constraints
    notes = resolution.notes
    for injury in injuries:
        hits = await resolve_concepts(
            driver, injury.notes, context="injury", concept_types=["condition"]
        )
        hit = hits[0]
        if not hit.resolved or not hit.concept_id:
            notes.append(
                f"injury {injury.node_id} note did not resolve to a condition "
                f"({hit.reason}); its safety rules CANNOT be applied — "
                "surface this to the coach"
            )
            continue
        constraints.append(
            InjuryConstraint(
                condition_id=hit.concept_id,
                status=injury.status,
                severity=injury.severity,
            )
        )
        resolution.condition_by_injury[injury.node_id] = hit.concept_id
        notes.append(
            f"injury {injury.node_id} note resolved to condition "
            f"{hit.concept_id} ({hit.match_method}, score {hit.score:.2f})"
        )
    return resolution


async def _add_safety_overlay(
    driver: AsyncDriver, builder: _SubgraphBuilder, injuries: list[_MemberInjury]
) -> list[str]:
    """Overlay which exercises are contraindicated, and by which rule.

    Runs the same two steps the workout generator does — resolve each injury
    to a Condition, then fire the rules via ``safe_exercise_pool`` — so the
    view and the generated plan agree about what is unsafe.

    Equipment is deliberately left unconstrained. ``safe_exercise_pool`` drops
    equipment-infeasible exercises *before* evaluating rules, so passing the
    member's kit would leave a contraindicated exercise they happen to lack
    equipment for looking unblocked. This view answers "what does the injury
    rule out", which must not depend on what dumbbells someone owns.

    An exercise can also be blocked by mechanics the injured-joint walk never
    reached, so any newly-named exercise is fetched before its ``BLOCKS`` edge.

    Returns:
        Resolution notes worth surfacing to the coach.
    """
    resolution = await _resolve_injuries(driver, injuries)
    condition_ids = sorted(set(resolution.condition_by_injury.values()))
    if not condition_ids:
        return resolution.notes

    # Every rule guarding the member's conditions, not only the ones that
    # fired: "four rules watch this knee, two of them blocked something" is
    # the more honest picture, and the rule set is tiny (22 graph-wide).
    records, _, _ = await driver.execute_query(
        _SAFETY_OVERLAY_QUERY, condition_ids=condition_ids, routing_="r"
    )
    for record in records:
        condition_id = builder.add_node(record["condition"])
        rule_id = builder.add_node(record["rule"])
        anchor_id = builder.add_node(record["anchor"])
        builder.add_edge(rule_id, "CONTRAINDICATED_FOR", condition_id)
        builder.add_edge(condition_id, "ANCHORED_AT", anchor_id)

    # The resolver's jump, drawn now that the Condition nodes exist.
    for injury_node_id, condition_id in resolution.condition_by_injury.items():
        builder.add_edge(injury_node_id, "RESOLVES_TO", condition_id, derived=True)

    pool = await safe_exercise_pool(
        driver, PoolConstraints(equipment_ids=None, injuries=resolution.constraints)
    )
    notes = resolution.notes + pool.notes
    safety_excluded = [item for item in pool.excluded if item.kind == "safety"]
    if not safety_excluded:
        return notes

    # Blocked exercises outside the structural walk are not in the graph yet.
    missing = {
        item.exercise_id
        for item in safety_excluded
        if not builder.has_node(item.exercise_id)
    }
    if missing:
        records, _, _ = await driver.execute_query(
            _EXERCISE_NODES_QUERY, ids=sorted(missing), routing_="r"
        )
        for record in records:
            builder.add_node(record["exercise"])

    for item in safety_excluded:
        builder.mark_blocked(item.exercise_id)
        for firing in item.fired_rules:
            if firing.decision != "exclude":
                continue
            # Derived: the graph stores rule -> condition, not rule -> exercise.
            builder.add_edge(
                firing.rule_id, "BLOCKS", item.exercise_id, derived=True
            )
    return notes


@router.get("/member/{member_id}", response_model=MemberSubgraphResponse)
async def get_member_subgraph(
    member_id: str,
    request: Request,
    include_neighbors: bool = Query(
        default=True,
        description="Include each exercise's muscle groups, equipment, and "
        "movement patterns. Turn off for a smaller, more legible graph.",
    ),
) -> MemberSubgraphResponse:
    """Return the member-centric subgraph behind the coach's safety decisions.

    Two layers in one payload: the stored edges from the member out through
    their injuries to the exercises that load the affected joints, and a
    computed overlay marking which of those exercises a safety rule excludes.

    Args:
        member_id: Member identifier, e.g. ``mbr_01HX9JORDAN``.
        request: Current request; carries the app-lifetime Neo4j driver.
        include_neighbors: Whether to expand exercises to their concepts.

    Returns:
        MemberSubgraphResponse whose every edge endpoint resolves to a node in
        the same payload.

    Raises:
        HTTPException: 404 when the member is not in the graph; 503 when Neo4j
            is unreachable. Unlike the members route there is no seed-file
            fallback — a graph view served from a JSON file would misrepresent
            what the graph actually holds.
    """
    driver: AsyncDriver = request.app.state.neo4j
    builder = _SubgraphBuilder()
    try:
        async with asyncio.timeout(GRAPH_READ_TIMEOUT_SECONDS):
            structural = await _add_structural_layer(driver, builder, member_id)
            if include_neighbors:
                await _add_exercise_neighbours(
                    driver, builder, structural.exercise_ids
                )
    except (ServiceUnavailable, Neo4jError, OSError, TimeoutError) as exc:
        logger.warning("Neo4j unavailable for member subgraph: %s", exc)
        raise HTTPException(status_code=503, detail="Graph unavailable")

    # The overlay is the slower, more failure-prone half (it runs the resolver).
    # Degrade to the structural graph rather than losing the whole view: a
    # picture without the safety layer is still worth showing, and the note
    # tells the coach the blocked markers are missing rather than absent.
    try:
        async with asyncio.timeout(OVERLAY_TIMEOUT_SECONDS):
            notes = await _add_safety_overlay(driver, builder, structural.injuries)
    except (ServiceUnavailable, Neo4jError, OSError, TimeoutError) as exc:
        logger.warning("Safety overlay failed for member %s: %s", member_id, exc)
        notes = [
            "safety overlay unavailable — no exercise is marked blocked, "
            "which does not mean none are contraindicated"
        ]
    return builder.build(member_id, notes)
