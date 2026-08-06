"""Dump the FastAPI OpenAPI schema for frontend type generation.

The Pydantic models (WorkoutPlan etc.) are the API contract; this exports
them so `npm run gen:api` can turn them into TypeScript types.

Usage::

    cd backend && uv run python ../scripts/export_openapi.py ../frontend/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

for _candidate in (Path(__file__).resolve().parents[1] / "backend",
                   Path(__file__).resolve().parents[1]):
    if (_candidate / "app").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from app.main import app


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    target.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
