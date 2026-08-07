from app.agents.workout import ConstraintSet, member_defaults

JORDAN_ID = "mbr_01HX9JORDAN"


def test_constraint_set_carries_no_prose() -> None:
    # Guards the round-trip contract: `constraints_used` goes back to the API
    # as `prior_constraints` on every adjustment turn. Re-adding a notes field
    # here would grow that payload turn over turn and duplicate the trace the
    # coach already reads from `resolution_notes`.
    assert "notes" not in ConstraintSet.model_fields


async def test_member_defaults_separates_state_from_its_trace(graph_driver) -> None:
    # Guards the coach-visible resolution trace for the reference member: the
    # injury note must land on a Condition (or the safety rules cannot fire at
    # all) and dislikes must down-rank rather than exclude. Returning the trace
    # alongside the state — not inside it — is what keeps the two from
    # aliasing into one list, which previously shipped the notes twice.
    defaults = await member_defaults(graph_driver, JORDAN_ID)

    assert [i.condition_id for i in defaults.constraints.injuries] == ["cond_pfps"]
    assert defaults.constraints.downrank_concept_ids
    assert not defaults.constraints.exclude_concept_ids  # dislikes never exclude
    assert any("resolved to condition cond_pfps" in note for note in defaults.notes)

    defaults.notes.append("mutating the trace must not touch the state")
    assert "notes" not in defaults.constraints.model_dump()


async def test_unknown_member_reports_the_miss_instead_of_failing(
    graph_driver,
) -> None:
    # Guards against a typo'd member id silently generating a plan with no
    # equipment limits and no injury rules — an unconstrained pool is the most
    # dangerous possible output, so the miss has to reach the trace.
    defaults = await member_defaults(graph_driver, "mbr_DOES_NOT_EXIST")

    assert defaults.constraints.injuries == []
    assert any("not found in KG 2" in note for note in defaults.notes)
