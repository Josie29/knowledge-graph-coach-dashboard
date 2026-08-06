"""Generate the README's example-run traces from the live graph.

Runs the three assessment scenarios through the deterministic half of the
workout pipeline — member defaults, constraint resolution, safety traversal —
and writes the provenance to ``docs/example-runs.md``. Everything in the
output is real graph data; no LLM is involved (the plan-composition step is
the one LLM call in the live system, and the doc labels it as such).

Usage::

    cd backend && uv run python ../scripts/example_runs.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (_REPO_ROOT / "backend", _REPO_ROOT):
    if (_candidate / "app").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from neo4j import AsyncGraphDatabase

from app.agents.workout import ConstraintMentions, _merge_mentions, member_defaults
from app.kg.safety import PoolResult, safe_exercise_pool

MEMBER_ID = "mbr_01HX9JORDAN"
OUT_PATH = _REPO_ROOT / "docs" / "example-runs.md"

HEADER = """\
# Example runs — provenance and filtering traces

Three scenarios through the workout pipeline's deterministic core (member
defaults → constraint resolution → safety traversal), generated from the live
graph by `scripts/example_runs.py`. Every number, rule firing, and graph path
below is real output — no LLM is involved in any of it. In the live system the
one LLM step is plan *composition*, which can only arrange exercises from the
"safe pool" tables below (an output validator rejects anything else), so these
traces are exactly the provenance a generated plan carries.

Regenerate against a running graph with:

```bash
cd backend && uv run python ../scripts/example_runs.py
```
"""


def render_pool(result: PoolResult, notes: list[str]) -> list[str]:
    lines: list[str] = []
    lines.append("**Constraint resolution trace**\n")
    for note in notes:
        lines.append(f"- {note}")
    lines.append("")

    safety = [e for e in result.excluded if e.kind == "safety"]
    explicit = [e for e in result.excluded if e.kind == "explicit"]
    equipment = [e for e in result.excluded if e.kind == "equipment"]

    if safety:
        lines.append("**Filtered out for safety**\n")
        for exclusion in safety:
            lines.append(f"- **{exclusion.name}** — {exclusion.reason}")
            if exclusion.path:
                lines.append(f"  - anatomy path: {exclusion.path.description}")
        lines.append("")
    if explicit:
        lines.append("**Explicitly excluded**\n")
        for exclusion in explicit:
            lines.append(f"- **{exclusion.name}** — {exclusion.reason}")
        lines.append("")
    if equipment:
        lines.append(
            f"**Not feasible with available equipment** — {len(equipment)} "
            "exercises; substitution suggestions shown for the first three\n"
        )
        for exclusion in equipment[:3]:
            alternatives = ", ".join(a.name for a in exclusion.alternatives) or "—"
            lines.append(
                f"- **{exclusion.name}** (missing: "
                f"{', '.join(exclusion.missing_equipment)}) → alternatives: "
                f"{alternatives}"
            )
        lines.append("")

    lines.append(
        f"**Safe pool ({len(result.included)} exercises the planner may use)**\n"
    )
    lines.append("| Exercise | Score | Notes |")
    lines.append("|---|---:|---|")
    for exercise in result.included:
        note_text = "; ".join(exercise.notes) if exercise.notes else ""
        lines.append(f"| {exercise.name} | {exercise.score:+.2f} | {note_text} |")
    lines.append("")
    return lines


async def main() -> int:
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    auth = (
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    driver = AsyncGraphDatabase.driver(uri, auth=auth)
    out: list[str] = [HEADER]

    # -- Scenario 1: the injury case ------------------------------------
    base = await member_defaults(driver, MEMBER_ID)
    out.append(
        "\n## 1 · Injury case — \"Lower-body strength session, 50 minutes\"\n\n"
        "Member defaults auto-applied from KG 2: five home equipment items, "
        "dislikes down-ranked via the resolver, and the injury note resolved "
        "to its clinical condition so the rule layer can fire.\n"
    )
    result = await safe_exercise_pool(driver, base.to_pool_constraints())
    out.extend(render_pool(result, base.notes))

    # -- Scenario 2: limited equipment ----------------------------------
    limited = base.model_copy(deep=True)
    notes = list(limited.notes)
    notes.append("--- adjustment: \"no barbell, only dumbbells and a kettlebell\" ---")
    await _merge_mentions(
        driver,
        limited,
        ConstraintMentions(equipment_only=["dumbbells", "a kettlebell"]),
        notes,
    )
    out.append(
        "\n## 2 · Limited-equipment case — adjustment: "
        "\"no barbell, only dumbbells and a kettlebell\"\n\n"
        "The prior constraint set carries forward; the equipment restriction "
        "resolves through the concept resolver and replaces the availability "
        "list. Bodyweight exercises always remain feasible; everything else "
        "must REQUIRE a subset of {Dumbbell, Kettlebell}.\n"
    )
    result = await safe_exercise_pool(driver, limited.to_pool_constraints())
    out.extend(render_pool(result, notes))

    # -- Scenario 3: exclude deadlifts ----------------------------------
    excluded = base.model_copy(deep=True)
    notes = list(excluded.notes)
    notes.append("--- adjustment: \"exclude deadlifts\" ---")
    await _merge_mentions(
        driver, excluded, ConstraintMentions(exclusions=["deadlifts"]), notes
    )
    out.append(
        "\n## 3 · Explicit exclusion — adjustment: \"exclude deadlifts\"\n\n"
        "\"Deadlift\" matches no catalog exercise name (data quirk 9); the "
        "resolver lands it on the hip-hinge movement pattern via curated "
        "synonyms, and the pattern exclusion removes every hinge exercise.\n"
    )
    result = await safe_exercise_pool(driver, excluded.to_pool_constraints())
    out.extend(render_pool(result, notes))

    OUT_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    await driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
