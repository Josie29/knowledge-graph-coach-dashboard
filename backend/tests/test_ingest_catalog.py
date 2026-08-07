from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_build_kg() -> Any:
    """Import ``scripts/build_kg.py``, which is a script rather than a package.

    The script already puts ``backend`` on ``sys.path`` at import time for its
    own ``app.kg`` imports, so loading it by path is enough — no restructuring
    and no dependency on the caller's working directory.
    """
    spec = importlib.util.spec_from_file_location(
        "build_kg", _REPO_ROOT / "scripts" / "build_kg.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_kg"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exercise_props() -> dict[str, dict[str, Any]]:
    """Every Exercise node's properties as ingest would write them, by name."""
    build_kg = _load_build_kg()
    payload = build_kg.load_and_validate(_REPO_ROOT / "data")
    return {row["props"]["name"]: row["props"] for row in payload.exercises}


def test_the_catalogs_rep_rate_is_stored_as_seconds(
    exercise_props: dict[str, dict[str, Any]],
) -> None:
    # Quirk 15, pinned on the two rows that prove it. `estimated_rep_duration`
    # is reps per second despite the name: single-leg jump rope, the fastest
    # movement in the catalog, carries the largest value (1.9), while a
    # controlled dumbbell bench press carries 0.2. Read as seconds those are
    # absurd — a 0.2s bench rep — and read as a rate they are exact. If someone
    # "simplifies" the ingest by storing the raw value, plans silently come out
    # at roughly half the requested window again.
    jump_rope = exercise_props["Jump Rope - Single-Leg"]
    bench = exercise_props["Dumbbell Neutral-Grip Bench Press"]

    assert jump_rope["rep_seconds"] == pytest.approx(0.526, abs=0.001)
    assert bench["rep_seconds"] == pytest.approx(5.0)
    # The slowest movement must cost more per rep than the fastest, which is
    # the whole argument and is false under the raw values.
    assert bench["rep_seconds"] > jump_rope["rep_seconds"]


def test_a_zero_rate_survives_the_inversion(
    exercise_props: dict[str, dict[str, Any]],
) -> None:
    # Seven rows ship a 0 rate, which has no reciprocal. Without the guard the
    # build raises ZeroDivisionError and no graph loads at all — the difference
    # between a working stack and none.
    #
    # Note the count is 7, not the 8 `is_reps: false` rows: Kneeling Stability
    # Ball Lat Stretch is duration-based yet carries a 0.2 rate. That row is
    # why the guard keys off the rate rather than off `is_reps` — pricing it
    # from `is_reps` would divide by a rate that is sometimes present anyway.
    zero_rate = [p for p in exercise_props.values() if p["rep_seconds"] == 0.0]
    duration_only = [p for p in exercise_props.values() if not p["is_reps"]]

    assert len(zero_rate) == 7
    assert len(duration_only) == 8
    assert all(p["rep_seconds"] == 0.0 for p in zero_rate)


def test_the_raw_rate_is_not_written_to_the_graph(
    exercise_props: dict[str, dict[str, Any]],
) -> None:
    # Keeping the misnamed field alongside the derived one is the failure mode
    # this move exists to prevent: anyone browsing Neo4j would still read
    # `estimated_rep_duration: 0.2` on a bench press as 0.2 seconds.
    assert all("estimated_rep_duration" not in p for p in exercise_props.values())
