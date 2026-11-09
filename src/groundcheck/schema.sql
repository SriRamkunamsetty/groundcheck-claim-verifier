-- One template, two dialects ({PK} is substituted in db.py). Timestamps are ISO-8601 UTC text.
CREATE TABLE IF NOT EXISTS titles (
  id {PK},
  name TEXT NOT NULL UNIQUE,
  genre TEXT,
  studio TEXT,
  release_date TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  pid TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  title_id BIGINT NOT NULL REFERENCES titles(id),
  kind TEXT,
  published_at TEXT NOT NULL,
  url TEXT,
  is_sample INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
  pid TEXT NOT NULL REFERENCES posts(pid),
  captured_at TEXT NOT NULL,
  views BIGINT NOT NULL CHECK (views >= 0),
  likes BIGINT NOT NULL DEFAULT 0 CHECK (likes >= 0),
  comments BIGINT NOT NULL DEFAULT 0 CHECK (comments >= 0),
  shares BIGINT NOT NULL DEFAULT 0 CHECK (shares >= 0),
  PRIMARY KEY (pid, captured_at)
);
CREATE TABLE IF NOT EXISTS events (
  id {PK},
  title_id BIGINT NOT NULL REFERENCES titles(id),
  occurred_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_pid_time ON snapshots (pid, captured_at);
CREATE INDEX IF NOT EXISTS idx_posts_title ON posts (title_id);
CREATE INDEX IF NOT EXISTS idx_events_title ON events (title_id, occurred_at);
