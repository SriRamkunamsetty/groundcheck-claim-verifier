"""Deterministic SYNTHETIC demo data (is_sample=1). Fictional titles; no real platform data.
Title 'Iron Meridian' has an injected spike that coincides with a logged TV-spot event."""
import math
import random
from datetime import datetime, timedelta, timezone

from .db import get_or_create_title, insert_id, iso
from .ingest import store_row

TITLES = [("Iron Meridian", "action"), ("Steel Tide", "action"), ("Last Bastion", "action"), ("Zero Hour Protocol", "action"),
          ("The Quiet Orchard", "drama"), ("Salt and Ember", "drama"), ("Paper Lanterns", "drama"), ("A Winter Letter", "drama"),
          ("Pause Button", "comedy"), ("Roommate Wanted", "comedy"), ("Bad Influence", "comedy"), ("Plus One", "comedy")]
KINDS = ("trailer", "teaser", "clip")


def seed(conn, seed_value=7, days=14):
    rng = random.Random(seed_value)
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    for ti, (name, genre) in enumerate(TITLES):
        tid = get_or_create_title(conn, name, genre, release_date=iso(base + timedelta(days=30)))
        pub = base + timedelta(days=ti, hours=rng.randint(0, 5))
        conn.execute("INSERT INTO events (title_id, occurred_at, kind, label) VALUES (?,?,?,?)",
                     (tid, iso(pub), "trailer_drop", "Trailer drop"))
        spike_at = 6.5 * 24 if ti == 0 else None
        if ti == 0:
            conn.execute("INSERT INTO events (title_id, occurred_at, kind, label) VALUES (?,?,?,?)",
                         (tid, iso(pub + timedelta(hours=spike_at - 2)), "tv_spot", "Primetime TV spot"))
        er = rng.uniform(0.03, 0.07)
        for k, kind in enumerate(KINDS):
            vol = math.exp(rng.uniform(math.log(4e5), math.log(7e6))) / (1 + k)
            tau = rng.uniform(30, 60)
            for step in range(1, days * 2 + 1):
                h = step * 12
                v = vol * (1 - math.exp(-h / tau))
                if spike_at and h > spike_at:
                    v += 0.8 * vol * (1 - math.exp(-(h - spike_at) / 8))
                views = int(v)
                store_row(conn, tid, "youtube", f"s{ti}{k}", kind, iso(pub), iso(pub + timedelta(hours=h)),
                          views, int(views * er), int(views * er * 0.1), int(views * er * 0.15), is_sample=1)
    conn.commit()


def seed_all(conn):
    seed(conn)
