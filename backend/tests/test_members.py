from fastapi.testclient import TestClient

from app.main import app

JORDAN_ID = "mbr_01HX9JORDAN"


def test_get_member_returns_profile_header_and_goals() -> None:
    # Catches the bug where the member view renders blank because the API
    # stops returning the profile fields (age, tier, member-since) or goals
    # the dashboard header displays for Jordan Rivera.
    with TestClient(app) as client:
        response = client.get(f"/api/members/{JORDAN_ID}")

    assert response.status_code == 200
    body = response.json()
    profile = body["profile"]
    assert profile["id"] == JORDAN_ID
    assert profile["name"] == "Jordan Rivera"
    assert profile["age"] == 41
    assert profile["tier"] == "1:1 Coaching"
    assert profile["member_since"] == "2024-09-15"
    assert profile["coach_id"] == "coach_01HXSAM"
    goals = body["goals"]
    assert len(goals) == 3
    assert {"id", "text", "priority", "target_date"} <= set(goals[0])


def test_get_unknown_member_returns_404() -> None:
    # Catches the bug where a typo'd or stale member id silently returns
    # another member's data instead of a clear not-found error in the UI.
    with TestClient(app) as client:
        response = client.get("/api/members/mbr_DOES_NOT_EXIST")

    assert response.status_code == 404
