from datetime import datetime
from typing import Iterable, List


def daily_degree_day(temps_c: Iterable[float], base_temp_c: float) -> float:
    temps = list(temps_c)
    if not temps:
        return 0.0
    t_max = max(temps)
    t_min = min(temps)
    return max(0.0, ((t_max + t_min) / 2.0) - base_temp_c)


def accumulate_degree_days(daily_temps_c: Iterable[float], base_temp_c: float) -> List[float]:
    total = 0.0
    result: List[float] = []
    for temp in daily_temps_c:
        total += max(0.0, temp - base_temp_c)
        result.append(total)
    return result


def hourly_to_daily_temps(hourly_values_c: Iterable[float]) -> List[float]:
    day_bins = []
    current_day = []
    temps = list(hourly_values_c)
    for idx, value in enumerate(temps):
        current_day.append(value)
        if (idx + 1) % 24 == 0:
            if current_day:
                day_bins.append(sum(current_day) / len(current_day))
            current_day = []
    if current_day:
        day_bins.append(sum(current_day) / len(current_day))
    return day_bins


def parse_iso_timestamp(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
