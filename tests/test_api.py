from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import _best_window_reason, _diversify_windows_by_time


def test_reaches_endpoint():
    client = TestClient(app)
    response = client.get("/reaches")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_reach_not_found():
    client = TestClient(app)
    response = client.get("/reach/nonexistent")
    assert response.status_code == 404


def test_best_windows_diversifies_repeated_hours():
    rows = [
        {"reach_id": f"same-{i}", "valid_at": "2026-05-21T20:00:00-05:00", "rank_score": 1.0 - i * 0.01}
        for i in range(8)
    ] + [
        {"reach_id": "morning", "valid_at": "2026-05-22T06:00:00-05:00", "rank_score": 0.80},
        {"reach_id": "evening", "valid_at": "2026-05-22T19:00:00-05:00", "rank_score": 0.79},
    ]

    picked = _diversify_windows_by_time(rows, limit=6, max_per_hour=4)

    assert len(picked) == 6
    assert sum(1 for row in picked if row["valid_at"] == "2026-05-21T20:00:00-05:00") == 4
    assert any(row["reach_id"] == "morning" for row in picked)
    assert any(row["reach_id"] == "evening" for row in picked)


def test_best_window_reason_surfaces_actionable_drivers():
    row = {"nymph_score": 0.82, "dry_score": 0.10}
    model = {"surface_signal": 0.05}
    breakdown = {"diel_activity": 1.0, "pressure_factor": 1.05, "sun_factor": 0.98}
    regime = {"code": "NYMPH", "label": "Nymph"}

    reason = _best_window_reason(row, model, breakdown, regime)

    assert reason == ["nymph", "nymphing play", "low light"]
