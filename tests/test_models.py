from src.models.degree_days import daily_degree_day, accumulate_degree_days
from src.models.nymph_score import compute_nymph_score
from src.models.dry_score import compute_dry_score


def test_daily_degree_day():
    temps = [10, 12, 14, 16]
    assert daily_degree_day(temps, 5.0) == 5.5


def test_accumulate_degree_days():
    temps = [5, 7, 10]
    result = accumulate_degree_days(temps, 3.0)
    assert result == [2.0, 6.0, 13.0]


def test_nymph_score_bounds():
    score = compute_nymph_score(55.0, 0.4, [100.0, 90.0], "2026-04-22T10:00:00Z", 150.0, [180.0, 260.0])
    assert 0.0 <= score <= 1.0


def test_dry_score_with_species():
    species_list = [
        {
            "species_id": "hendrickson",
            "dd_threshold_mean": 260,
            "dd_threshold_sd": 40,
            "weather_prefs": {"clouds": "any", "wind": "<15"},
            "emergence_hr_start": 12,
            "emergence_hr_end": 15,
        }
    ]
    result = compute_dry_score(260, species_list, 0.2, 5.0, 55.0, 13)
    assert result["dry_score"] >= 0.0
    assert isinstance(result["active_species"], list)
