from fastapi.testclient import TestClient

from app.main import app

JORDAN_ID = "mbr_01HX9JORDAN"


def test_member_subgraph_edges_all_reference_returned_nodes(
    graph_available: str,
) -> None:
    # Catches the bug that hard-crashes the graph view on load: force-graph
    # throws when an edge names a source or target that is not in the node
    # list, so the whole tab renders blank instead of the graph.
    with TestClient(app) as client:
        response = client.get(f"/api/graph/member/{JORDAN_ID}")

    assert response.status_code == 200
    body = response.json()
    node_ids = {node["id"] for node in body["nodes"]}
    assert len(node_ids) == len(body["nodes"]), "node ids must be unique"

    dangling = [
        edge
        for edge in body["edges"]
        if edge["source"] not in node_ids or edge["target"] not in node_ids
    ]
    assert dangling == []

    edge_ids = [edge["id"] for edge in body["edges"]]
    assert len(set(edge_ids)) == len(edge_ids), "edges must be deduplicated"


def test_member_subgraph_walks_member_through_injury_to_exercises(
    graph_available: str,
) -> None:
    # Catches the bug where the view degrades to a member node with a few
    # orphan goals: the injury -> joint -> exercise chain is the whole point,
    # so losing any hop makes the picture look fine but explain nothing.
    with TestClient(app) as client:
        response = client.get(f"/api/graph/member/{JORDAN_ID}")

    body = response.json()
    types = {node["type"] for node in body["nodes"]}
    assert {"Member", "Injury", "Joint", "Exercise"} <= types

    edge_types = {edge["type"] for edge in body["edges"]}
    assert {"HAS_INJURY", "AFFECTS", "STRESSES"} <= edge_types


def test_member_subgraph_marks_contraindicated_exercises_blocked(
    graph_available: str,
) -> None:
    # Catches the overlay silently falling back to a structural-only graph.
    # Jordan's knee injury resolves to patellofemoral pain syndrome, whose
    # safety rules must exclude the plyometric work; if nothing is blocked the
    # view contradicts what the workout generator refuses.
    with TestClient(app) as client:
        response = client.get(f"/api/graph/member/{JORDAN_ID}")

    body = response.json()
    blocked = [node for node in body["nodes"] if node["blocked"]]
    assert blocked, f"expected blocked exercises, notes were: {body['notes']}"
    assert all(node["type"] == "Exercise" for node in blocked)

    # Every blocked exercise must be traceable back to the rule that blocked
    # it, otherwise the UI can show a red node with no explanation.
    blocks_targets = {
        edge["target"] for edge in body["edges"] if edge["type"] == "BLOCKS"
    }
    assert {node["id"] for node in blocked} <= blocks_targets
    assert all(
        edge["derived"] for edge in body["edges"] if edge["type"] == "BLOCKS"
    ), "BLOCKS is computed, not stored — it must be flagged derived"

    rule_types = {node["type"] for node in body["nodes"]}
    assert {"SafetyRule", "Condition"} <= rule_types


def test_member_subgraph_draws_the_resolver_jump(graph_available: str) -> None:
    # The Injury -> Condition link is not stored in Neo4j; the resolver makes
    # it from the injury's free text at request time. Catches that jump going
    # undrawn, which leaves the clinical half of the graph looking unrelated
    # to the member it was derived from.
    with TestClient(app) as client:
        response = client.get(f"/api/graph/member/{JORDAN_ID}")

    body = response.json()
    by_id = {node["id"]: node for node in body["nodes"]}
    resolves = [edge for edge in body["edges"] if edge["type"] == "RESOLVES_TO"]
    assert resolves, f"no injury resolved to a condition; notes: {body['notes']}"
    assert all(edge["derived"] for edge in resolves)
    for edge in resolves:
        assert by_id[edge["source"]]["type"] == "Injury"
        assert by_id[edge["target"]]["type"] == "Condition"


def test_member_subgraph_shows_rules_that_did_not_fire(
    graph_available: str,
) -> None:
    # Every rule guarding the member's condition is returned, not just the
    # ones that blocked something. Catches the graph implying a condition is
    # governed only by the rules that happened to exclude an exercise.
    with TestClient(app) as client:
        response = client.get(f"/api/graph/member/{JORDAN_ID}")

    body = response.json()
    rules = [node for node in body["nodes"] if node["type"] == "SafetyRule"]
    blocking = {edge["source"] for edge in body["edges"] if edge["type"] == "BLOCKS"}
    assert len(rules) > len(blocking), (
        "expected some rules to guard the condition without blocking anything"
    )
    guarded = {
        edge["source"]
        for edge in body["edges"]
        if edge["type"] == "CONTRAINDICATED_FOR"
    }
    assert {rule["id"] for rule in rules} == guarded


def test_member_subgraph_labels_every_node(graph_available: str) -> None:
    # Display names live under a different property per label (Exercise.name,
    # Concept.pref_label, Injury.region, SafetyRule.rationale). Catches a new
    # node type joining the walk and rendering as an unlabelled dot.
    with TestClient(app) as client:
        response = client.get(f"/api/graph/member/{JORDAN_ID}")

    body = response.json()
    unlabelled = [
        node
        for node in body["nodes"]
        if not node["label"] or node["label"] == node["id"]
    ]
    assert unlabelled == []
    assert all(node["type"] != "Unknown" for node in body["nodes"])
    # Embeddings are 384 floats per concept — they must never reach the client.
    assert all("embedding" not in node["properties"] for node in body["nodes"])


def test_member_subgraph_without_neighbors_drops_concept_expansion(
    graph_available: str,
) -> None:
    # Catches include_neighbors=false being ignored, which is the escape hatch
    # when the full graph is too dense to read.
    with TestClient(app) as client:
        response = client.get(
            f"/api/graph/member/{JORDAN_ID}", params={"include_neighbors": False}
        )

    body = response.json()
    edge_types = {edge["type"] for edge in body["edges"]}
    assert not {"TARGETS", "HAS_PATTERN"} & edge_types
    assert {"HAS_INJURY", "AFFECTS", "STRESSES"} <= edge_types


def test_unknown_member_subgraph_returns_404(graph_available: str) -> None:
    # Catches a typo'd member id rendering an empty canvas that looks like a
    # member with no data, rather than an error the coach can act on.
    with TestClient(app) as client:
        response = client.get("/api/graph/member/mbr_does_not_exist")

    assert response.status_code == 404
