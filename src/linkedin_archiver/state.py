"""Durable SQLite state for post and media-recovery progress."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from linkedin_archiver.linkedin_data import Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    item_index INTEGER NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    author TEXT,
    username TEXT,
    media_count INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT,
    error TEXT,
    media_recovered INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_item_index ON posts(item_index);

CREATE TABLE IF NOT EXISTS recovery (
    post_id TEXT PRIMARY KEY,
    item_index INTEGER NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    media_count INTEGER NOT NULL DEFAULT 0,
    video_json TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_recovery_status ON recovery(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Persistent state store. SQLite is the source of truth."""

    def __init__(self, archive_root: Path):
        self.archive_root = archive_root
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.path = self.archive_root / "state.sqlite3"
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate_json_snapshots()

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.sync_legacy_snapshots()
        finally:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        return dict(row) if row else None

    def is_post_done(self, post_id: str) -> bool:
        row = self.conn.execute("SELECT status FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        return bool(row and row["status"] in Status.TERMINAL_SUCCESS)

    def record_post(
        self,
        post_id: str,
        *,
        index: int,
        url: str,
        status: str,
        author: str | None = None,
        username: str | None = None,
        media_count: int = 0,
        error: str | None = None,
        timestamp: str | None = None,
        media_recovered: int = 0,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO posts
                (post_id, item_index, url, status, author, username, media_count, timestamp, error, media_recovered, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                item_index=excluded.item_index,
                url=excluded.url,
                status=excluded.status,
                author=COALESCE(excluded.author, posts.author),
                username=COALESCE(excluded.username, posts.username),
                media_count=excluded.media_count,
                timestamp=COALESCE(excluded.timestamp, posts.timestamp),
                error=excluded.error,
                media_recovered=MAX(posts.media_recovered, excluded.media_recovered),
                updated_at=excluded.updated_at
            """,
            (post_id, index, url, status, author, username, media_count, timestamp, error, media_recovered, _now()),
        )
        self.conn.commit()

    def update_media_recovered(self, post_id: str, count: int) -> None:
        self.conn.execute(
            "UPDATE posts SET media_recovered = ?, updated_at = ? WHERE post_id = ?",
            (count, _now(), post_id),
        )
        self.conn.commit()

    def unresolved_posts(self) -> list[tuple[int, str, str]]:
        rows = self.conn.execute(
            """
            SELECT item_index, post_id, url
            FROM posts
            WHERE url <> ? AND status <> ?
            ORDER BY item_index, post_id
            """,
            ("", Status.COMPLETED),
        ).fetchall()
        return [(int(row["item_index"]), row["post_id"], row["url"]) for row in rows]

    def get_recovery(self, post_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM recovery WHERE post_id = ?", (post_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["video"] = json.loads(result.pop("video_json")) if result.get("video_json") else None
        return result

    def record_recovery(
        self,
        post_id: str,
        *,
        index: int,
        url: str,
        status: str,
        media_count: int = 0,
        video: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO recovery
                (post_id, item_index, url, status, media_count, video_json, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                item_index=excluded.item_index,
                url=excluded.url,
                status=excluded.status,
                media_count=excluded.media_count,
                video_json=excluded.video_json,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (post_id, index, url, status, media_count, json.dumps(video, ensure_ascii=False) if video is not None else None, error, _now()),
        )
        self.conn.commit()

    def post_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS count FROM posts GROUP BY status").fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def recovered_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM recovery WHERE status = 'completed'"
        ).fetchone()
        return int(row["count"])

    def sync_legacy_snapshots(self) -> None:
        """Keep readable JSON snapshots for compatibility/inspection."""
        self._atomic_write_json(self.archive_root / "manifest.json", self._post_snapshot())
        self._atomic_write_json(self.archive_root / "recovery_manifest.json", self._recovery_snapshot())

    def _post_snapshot(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT * FROM posts ORDER BY item_index, post_id").fetchall()
        return {
            row["post_id"]: {
                "index": row["item_index"],
                "url": row["url"],
                "status": row["status"],
                "author": row["author"],
                "username": row["username"],
                "media_count": row["media_count"],
                "media_recovered": row["media_recovered"],
                "error": row["error"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        }

    def _recovery_snapshot(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT * FROM recovery ORDER BY item_index, post_id").fetchall()
        return {
            row["post_id"]: {
                "index": row["item_index"],
                "url": row["url"],
                "status": row["status"],
                "media_count": row["media_count"],
                "video": json.loads(row["video_json"]) if row["video_json"] else None,
                "error": row["error"],
            }
            for row in rows
        }

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def _migrate_json_snapshots(self) -> None:
        """Import old manifest JSON once when SQLite has no state yet."""
        count = self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        manifest_path = self.archive_root / "manifest.json"
        recovery_path = self.archive_root / "recovery_manifest.json"
        if count == 0 and manifest_path.exists():
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            if isinstance(raw, dict):
                for post_id, entry in raw.items():
                    if not isinstance(entry, dict) or not entry.get("url"):
                        continue
                    self.record_post(
                        post_id,
                        index=int(entry.get("index") or 0),
                        url=entry["url"],
                        status=entry.get("status") or Status.FAILED,
                        author=entry.get("author"),
                        username=entry.get("username"),
                        media_count=int(entry.get("media_count") or 0),
                        error=entry.get("error"),
                        timestamp=entry.get("timestamp"),
                        media_recovered=int(entry.get("media_recovered") or 0),
                    )

        recovery_count = self.conn.execute("SELECT COUNT(*) FROM recovery").fetchone()[0]
        if recovery_count == 0 and recovery_path.exists():
            try:
                raw = json.loads(recovery_path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            if isinstance(raw, dict):
                for post_id, entry in raw.items():
                    if not isinstance(entry, dict) or not entry.get("url"):
                        continue
                    self.record_recovery(
                        post_id,
                        index=int(entry.get("index") or 0),
                        url=entry["url"],
                        status=entry.get("status") or "failed",
                        media_count=int(entry.get("media_count") or 0),
                        video=entry.get("video"),
                        error=entry.get("error"),
                    )
