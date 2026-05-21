from fastapi.testclient import TestClient
from src.api.main import app
from src.api import routes
from src.api.routes import _best_window_reason, _diversify_windows_by_time, _top_active_species


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
    breakdown = {
        "diel_activity": 1.0,
        "pressure_factor": 1.05,
        "sun_factor": 0.98,
        "temperature": 1.0,
        "thermal_profile": "class-level thermal model",
    }
    regime = {"code": "NYMPH", "label": "Nymph"}

    reason = _best_window_reason(row, model, breakdown, regime)

    assert reason == ["nymph", "nymphing play", "ideal water"]


def test_best_window_reason_prefers_spring_buffered_context():
    row = {"nymph_score": 0.82, "dry_score": 0.10}
    model = {"surface_signal": 0.05}
    breakdown = {
        "diel_activity": 1.0,
        "pressure_factor": 1.05,
        "temperature": 1.0,
        "thermal_profile": "spring-creek thermal damping (0.67)",
    }

    reason = _best_window_reason(row, model, breakdown, {"code": "NORMAL"})

    assert reason == ["nymphing play", "spring-buffered water", "low light"]


def test_top_active_species_picks_highest_probability():
    species = [
        {"id": "bwo", "common_name": "BWO", "probability": 0.20},
        {"id": "sulphur", "common_name": "Sulphur", "probability": 0.42},
    ]

    assert _top_active_species(species)["id"] == "sulphur"


def test_hatch_windows_endpoint_surfaces_species(monkeypatch):
    monkeypatch.setattr(routes, "hatch_windows", lambda hours, limit, min_surface: [{
        "reach_id": "upper-iowa-decorah",
        "stream_name": "Upper Iowa River",
        "segment_name": "near Decorah",
        "state": "IA",
        "valid_at": "2026-05-26T16:00:00-05:00",
        "nymph_score": 0.25,
        "dry_score": 0.15,
        "active_species": '[{"id":"sulphur","common_name":"Sulphur","probability":0.46}]',
        "regime": '{"code":"NORMAL"}',
        "score_breakdown": '{"sun_factor":0.88}',
        "combined_score": 0.36,
        "surface_signal": 0.46,
        "surface_rank_score": 0.39,
        "confidence_score": 0.90,
        "water_temp_f": 62.0,
        "fish_stress": 0,
        "explanation": "surface signal building",
    }])
    client = TestClient(app)

    response = client.get("/hatch-windows?hours=168&limit=6&min_surface=0.25")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["top_species"]["common_name"] == "Sulphur"
    assert payload[0]["fish_stress"] is False
    assert payload[0]["reason"] == ["surface signal", "Sulphur", "bright-sun drag"]
