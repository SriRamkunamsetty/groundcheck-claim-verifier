"""Metrics engine. Definitions live in docs/METRICS.md; keep them in sync.

Cumulative counters are interpolated linearly between snapshots, anchored at
(published_at, 0). A value is only reported for a window that has actually been
observed (a snapshot exists at or after it); otherwise the result is None, not a guess.
"""
import math
from dataclasses import dataclass
from datetime import timedelta
from statistics import median

from .db import parse_ts


@dataclass(frozen=True)
class Fact:
    id: str          # stable key the narrator cites, e.g. "velocity_72h"
    label: str
    value: float
    unit: str        # views | ratio | pct | count
    query: str       # how it was computed (shown for auditability)


def _points(conn, pid):
    pub = conn.execute("SELECT published_at FROM posts WHERE pid=?", (pid,)).fetchone()["published_at"]
    rows = conn.execute("SELECT captured_at, views FROM snapshots WHERE pid=? AND captured_at>? "
                        "ORDER BY captured_at", (pid, pub)).fetchall()
    return [(parse_ts(pub), 0.0)] + [(parse_ts(r["captured_at"]), float(r["views"])) for r in rows]


def _cum_at(pts, t):
    if t <= pts[0][0]:
        return 0.0
    if t > pts[-1][0]:
        return None
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            return v0 + (v1 - v0) * ((t - t0) / (t1 - t0))
    return None


def cumulative_series(conn, title_id, step_h=12):
    """[(hours_since_first_post, total cumulative views)] on a regular grid, for charts."""
    pts = {pid: _points(conn, pid) for pid in title_posts(conn, title_id)}
    if not pts:
        return []
    start = min(p[0][0] for p in pts.values())
    end = min(p[-1][0] for p in pts.values())
    out, h = [], 0
    while start + timedelta(hours=h) <= end:
        t = start + timedelta(hours=h)
        out.append((h, sum(_cum_at(p, t) or 0.0 for p in pts.values())))
        h += step_h
    return out


def title_posts(conn, title_id):
    return [r["pid"] for r in conn.execute("SELECT pid FROM posts WHERE title_id=? ORDER BY pid", (title_id,))]


def engagement_rate(conn, title_id):
    """(likes+comments+shares) / views on each post's latest snapshot, view-weighted across posts."""
    r = conn.execute(
        "SELECT SUM(s.likes+s.comments+s.shares) AS i, SUM(s.views) AS v FROM snapshots s "
        "JOIN posts p ON p.pid=s.pid WHERE p.title_id=? AND s.captured_at="
        "(SELECT MAX(captured_at) FROM snapshots WHERE pid=s.pid)", (title_id,)).fetchone()
    return None if not r["v"] else float(r["i"]) / float(r["v"])


def launch_velocity(conn, title_id, hours):
    """Total views across the title's posts within `hours` of each post's publish time."""
    total, used = 0.0, 0
    for pid in title_posts(conn, title_id):
        pts = _points(conn, pid)
        v = _cum_at(pts, pts[0][0] + timedelta(hours=hours))
        if v is not None:
            total, used = total + v, used + 1
    return (total, used) if used else (None, 0)


def share_of_voice(conn, title_ids, hours=168):
    vals = {t: launch_velocity(conn, t, hours)[0] for t in title_ids}
    vals = {t: v for t, v in vals.items() if v is not None}
    s = sum(vals.values())
    return {t: v / s for t, v in vals.items()} if s > 0 else {}


def benchmark_percentile(conn, title_id, hours=72, min_peers=3):
    """Percentile rank (0-100) of launch velocity among titles of the same genre (ties count half)."""
    g = conn.execute("SELECT genre FROM titles WHERE id=?", (title_id,)).fetchone()["genre"]
    if not g:
        return None
    ids = [r["id"] for r in conn.execute("SELECT id FROM titles WHERE genre=?", (g,))]
    vals = {t: launch_velocity(conn, t, hours)[0] for t in ids}
    vals = {t: v for t, v in vals.items() if v is not None}
    if title_id not in vals or len(vals) < min_peers:
        return None
    me = vals[title_id]
    below = sum(v < me for t, v in vals.items() if t != title_id)
    ties = sum(v == me for t, v in vals.items() if t != title_id)
    return 100.0 * (below + 0.5 * ties) / (len(vals) - 1)


def daily_views(conn, title_id):
    """Full-UTC-day view totals for a title, ending at the earliest 'last snapshot' across its posts.
    Returns [(date, views)]; the partial first day is dropped."""
    pts = {pid: _points(conn, pid) for pid in title_posts(conn, title_id)}
    if not pts:
        return []
    start = min(p[0][0] for p in pts.values())
    end = min(p[-1][0] for p in pts.values())
    b = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    bounds = []
    while b <= end:
        bounds.append(b)
        b += timedelta(days=1)
    cum = [sum(_cum_at(p, t) or 0.0 for p in pts.values()) for t in bounds]
    return [(bounds[i].date(), cum[i] - cum[i - 1]) for i in range(1, len(bounds))]


def anomalies(conn, title_id, z_thresh=3.5, window_h=48):
    """Flag days whose day-over-day log growth is a robust-z outlier (MAD-based), and attach
    any logged events within +/- window_h hours so the spike can be explained, not just seen."""
    series = daily_views(conn, title_id)
    if len(series) < 6:
        return []
    r = [math.log((series[i][1] + 1) / (series[i - 1][1] + 1)) for i in range(1, len(series))]
    med = median(r)
    mad = median(abs(x - med) for x in r)
    mad = max(mad, 0.05)  # scale floor: smooth decay curves otherwise make MAD ~ 0 and z explode
    events = conn.execute("SELECT occurred_at, kind, label FROM events WHERE title_id=?", (title_id,)).fetchall()
    flagged = []
    for i, x in enumerate(r, start=1):
        z = 0.6745 * (x - med) / mad
        if abs(z) >= z_thresh:
            flagged.append((i, z))
    # consecutive flagged days are ONE episode (a spike plus its rebound), not several anomalies
    runs, cur = [], []
    for item in flagged:
        if cur and item[0] != cur[-1][0] + 1:
            runs.append(cur); cur = []
        cur.append(item)
    if cur:
        runs.append(cur)
    out = []
    for run in runs:
        first_up = run[0][1] > 0
        same = [(i, z) for i, z in run if (z > 0) == first_up]
        i, z = max(same, key=lambda t: abs(t[1]))      # peak day of the episode's lead direction
        near = []
        for j, _ in run:
            mid = parse_ts(f"{series[j][0].isoformat()}T12:00:00Z")
            near += [e["label"] for e in events
                     if abs((parse_ts(e["occurred_at"]) - mid).total_seconds()) <= window_h * 3600 and e["label"] not in near]
        out.append({"day_index": i + 1, "date": series[i][0], "z": z, "views": series[i][1],
                    "direction": "up" if first_up else "down", "events": near,
                    "episode_days": [j + 1 for j, _ in run]})
    return out


def build_facts(conn, title_id, peer_ids=()):
    """Compute every number the narrator is allowed to cite, each with its provenance."""
    f = []
    n_posts = len(title_posts(conn, title_id))
    f.append(Fact("n_posts", "posts tracked", n_posts, "count", "COUNT(posts) for title"))
    for h, key in ((24, "velocity_24h"), (72, "velocity_72h"), (168, "velocity_7d")):
        v, used = launch_velocity(conn, title_id, h)
        if v is not None:
            f.append(Fact(key, f"views in first {h}h", v, "views", f"interpolated cumulative views at +{h}h, {used} posts"))
    er = engagement_rate(conn, title_id)
    if er is not None:
        f.append(Fact("eng_rate", "engagement rate", er, "ratio", "(likes+comments+shares)/views, latest snapshot"))
    sov = share_of_voice(conn, [title_id, *peer_ids], 168)
    if title_id in sov:
        f.append(Fact("sov_7d", "share of voice (7d views)", sov[title_id], "ratio", f"7d views / sum over {len(sov)} titles"))
    bp = benchmark_percentile(conn, title_id, 72)
    if bp is not None:
        f.append(Fact("benchmark_72h", "genre percentile (72h views)", bp, "pct", "percentile rank within genre"))
    if bp is not None:
        n = conn.execute("SELECT COUNT(*) c FROM titles WHERE genre=(SELECT genre FROM titles WHERE id=?)", (title_id,)).fetchone()["c"]
        f.append(Fact("benchmark_n", "titles in genre benchmark", n, "count", "titles sharing this genre"))
    for k, a in enumerate(anomalies(conn, title_id)[:2], start=1):
        f.append(Fact(f"anomaly_{k}_day", f"anomaly {k}: day since first post ({a['direction']})", a["day_index"], "count", "robust-z on log growth"))
        f.append(Fact(f"anomaly_{k}_views", f"anomaly {k}: views that day", a["views"], "views", "daily views from interpolated counters"))
    return f
