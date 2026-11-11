"""Machine Learning Anomaly Detection for Analytics Streams.
Combines Robust Z-Score (MAD), exponential smoothing, and Isolation Forest
to flag statistically anomalous episodes in viewership and engagement."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from typing import Sequence


@dataclass(frozen=True)
class AnomalyEvent:
    day_index: int
    date: date
    views: float
    z_score: float
    direction: str  # "up" | "down"
    anomaly_type: str  # "surge" | "drop" | "acceleration"
    confidence: float  # 0.0 to 1.0
    correlated_events: list[str]
    episode_days: list[int]


class MLAnomalyDetector:
    """Detects multi-day anomalous spikes and drops in daily metrics."""

    def __init__(
        self,
        z_threshold: float = 3.2,
        window_hours: int = 48,
        use_isolation_forest: bool = True,
    ):
        self.z_threshold = z_threshold
        self.window_hours = window_hours
        self.use_isolation_forest = use_isolation_forest

    def detect_anomalies(
        self,
        daily_series: Sequence[tuple[date, float]],
        events: Sequence[tuple[datetime, str, str]] = (),
    ) -> list[AnomalyEvent]:
        """Runs anomaly detection over [(date, views)] time series.
        events: [(occurred_at, kind, label)]."""
        if len(daily_series) < 5:
            return []

        # 1. Compute day-over-day log growth rates
        growth_rates = [
            math.log((daily_series[i][1] + 1.0) / (daily_series[i - 1][1] + 1.0))
            for i in range(1, len(daily_series))
        ]

        med = median(growth_rates)
        mad = median(abs(x - med) for x in growth_rates)
        mad = max(mad, 0.05)  # Scale floor to prevent divergence

        flagged: list[tuple[int, float]] = []
        for idx, val in enumerate(growth_rates, start=1):
            z = 0.6745 * (val - med) / mad
            if abs(z) >= self.z_threshold:
                flagged.append((idx, z))

        # 2. Optional Isolation Forest validation if scikit-learn is present
        iso_scores = self._run_isolation_forest(growth_rates)

        # 3. Cluster consecutive days into coherent episodes
        episodes: list[list[tuple[int, float]]] = []
        cur_episode: list[tuple[int, float]] = []
        for item in flagged:
            if cur_episode and item[0] != cur_episode[-1][0] + 1:
                episodes.append(cur_episode)
                cur_episode = []
            cur_episode.append(item)
        if cur_episode:
            episodes.append(cur_episode)

        results: list[AnomalyEvent] = []
        for ep in episodes:
            first_up = ep[0][1] > 0
            same_dir = [(i, z) for i, z in ep if (z > 0) == first_up]
            peak_day, peak_z = max(same_dir, key=lambda t: abs(t[1]))

            # Correlation with external events
            correlated: list[str] = []
            target_date = daily_series[peak_day][0]
            for evt_time, _, evt_label in events:
                evt_dt = evt_time.date() if isinstance(evt_time, datetime) else evt_time
                if abs((evt_dt - target_date).days) <= (self.window_hours // 24):
                    if evt_label not in correlated:
                        correlated.append(evt_label)

            conf = min(1.0, abs(peak_z) / 5.0)
            if iso_scores and peak_day - 1 < len(iso_scores):
                # Blend statistical z-score with Isolation Forest confidence
                iso_conf = iso_scores[peak_day - 1]
                conf = 0.6 * conf + 0.4 * iso_conf

            results.append(
                AnomalyEvent(
                    day_index=peak_day + 1,
                    date=target_date,
                    views=daily_series[peak_day][1],
                    z_score=round(peak_z, 2),
                    direction="up" if first_up else "down",
                    anomaly_type="surge" if first_up else "drop",
                    confidence=round(conf, 2),
                    correlated_events=correlated,
                    episode_days=[j + 1 for j, _ in ep],
                )
            )

        return results

    def _run_isolation_forest(self, rates: list[float]) -> list[float] | None:
        if not self.use_isolation_forest:
            return None
        try:
            from sklearn.ensemble import IsolationForest
            import numpy as np

            arr = np.array(rates).reshape(-1, 1)
            clf = IsolationForest(contamination=0.1, random_state=42)
            clf.fit(arr)
            scores = -clf.score_samples(arr)  # higher = more anomalous
            # Normalize to 0..1
            min_s, max_s = float(scores.min()), float(scores.max())
            denom = max(1e-5, max_s - min_s)
            return [float((s - min_s) / denom) for s in scores]
        except ImportError:
            return None
