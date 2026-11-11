"""Prometheus Metrics Collector for GroundCheck.
Exports standard Prometheus text exposition format without external dependencies."""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class PrometheusMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._start_time = time.time()
        self.requests_total: dict[tuple[str, str, int], int] = defaultdict(int)
        self.claims_verified_total = 0
        self.misattributions_total = 0
        self.unsupported_total = 0
        self.style_violations_total = 0
        self.latency_sum: dict[str, float] = defaultdict(float)
        self.latency_count: dict[str, int] = defaultdict(int)

    def record_request(self, method: str, endpoint: str, status: int, duration_sec: float):
        with self._lock:
            self.requests_total[(method, endpoint, status)] += 1
            self.latency_sum[endpoint] += duration_sec
            self.latency_count[endpoint] += 1

    def record_verification(self, n_numbers: int, n_misattributed: int, n_unsupported: int, n_style: int):
        with self._lock:
            self.claims_verified_total += n_numbers
            self.misattributions_total += n_misattributed
            self.unsupported_total += n_unsupported
            self.style_violations_total += n_style

    def render(self) -> str:
        lines = [
            "# HELP groundcheck_uptime_seconds Server uptime in seconds",
            "# TYPE groundcheck_uptime_seconds gauge",
            f"groundcheck_uptime_seconds {time.time() - self._start_time:.1f}",
            "",
            "# HELP groundcheck_claims_verified_total Total numeric claims checked",
            "# TYPE groundcheck_claims_verified_total counter",
            f"groundcheck_claims_verified_total {self.claims_verified_total}",
            "",
            "# HELP groundcheck_misattributions_total Total real numbers on wrong metric/window",
            "# TYPE groundcheck_misattributions_total counter",
            f"groundcheck_misattributions_total {self.misattributions_total}",
            "",
            "# HELP groundcheck_unsupported_total Total fabricated numbers lacking ground truth",
            "# TYPE groundcheck_unsupported_total counter",
            f"groundcheck_unsupported_total {self.unsupported_total}",
            "",
            "# HELP groundcheck_style_violations_total Total unhedged causal or hype claims",
            "# TYPE groundcheck_style_violations_total counter",
            f"groundcheck_style_violations_total {self.style_violations_total}",
            "",
            "# HELP groundcheck_http_requests_total Total HTTP requests",
            "# TYPE groundcheck_http_requests_total counter",
        ]
        with self._lock:
            for (method, endpoint, status), count in self.requests_total.items():
                lines.append(f'groundcheck_http_requests_total{{method="{method}",endpoint="{endpoint}",status="{status}"}} {count}')

            lines.extend([
                "",
                "# HELP groundcheck_http_request_duration_seconds Total time spent in seconds by endpoint",
                "# TYPE groundcheck_http_request_duration_seconds summary",
            ])
            for endpoint, total_dur in self.latency_sum.items():
                cnt = self.latency_count[endpoint]
                lines.append(f'groundcheck_http_request_duration_seconds_sum{{endpoint="{endpoint}"}} {total_dur:.6f}')
                lines.append(f'groundcheck_http_request_duration_seconds_count{{endpoint="{endpoint}"}} {cnt}')

        lines.append("")
        return "\n".join(lines)


METRICS = PrometheusMetrics()
