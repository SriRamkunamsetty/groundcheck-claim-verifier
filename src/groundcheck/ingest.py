"""Minimal writer used by the bundled synthetic launch data."""


def store_row(conn, title_id, platform, native_id, kind, published_at, captured_at,
              views, likes=0, comments=0, shares=0, url=None, is_sample=0):
    pid = f"{platform}:{native_id}"
    conn.execute(
        "INSERT INTO posts (pid, platform, title_id, kind, published_at, url, is_sample) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT (pid) DO NOTHING",
        (pid, platform, title_id, kind, published_at, url, is_sample))
    conn.execute(
        "INSERT INTO snapshots (pid, captured_at, views, likes, comments, shares) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT (pid, captured_at) DO UPDATE SET views=excluded.views, likes=excluded.likes, "
        "comments=excluded.comments, shares=excluded.shares",
        (pid, captured_at, views, likes, comments, shares))
