"""SQLite-backed distributed lock with TTL and automatic expiration."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from openagent_bridge.db import Database


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class LockManager:
    """Manages named locks stored in SQLite."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def acquire(self, name: str, owner: str, ttl_seconds: int = 30) -> bool:
        """Attempt to acquire a named lock. Returns True if acquired, False otherwise."""
        now = _utcnow()
        expires = now + timedelta(seconds=ttl_seconds)
        now_str = _iso(now)
        expires_str = _iso(expires)

        conn = self.db.connection
        cursor = conn.cursor()

        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT owner, expires FROM locks WHERE name = ?", (name,))
            row = cursor.fetchone()

            if row is None:
                # Lock doesn't exist, create it
                cursor.execute(
                    "INSERT INTO locks (name, owner, acquired, expires) VALUES (?, ?, ?, ?)",
                    (name, owner, now_str, expires_str),
                )
                conn.commit()
                return True

            current_owner = row["owner"]
            current_expires = row["expires"]

            # If current owner is same, renew
            if current_owner == owner:
                cursor.execute(
                    "UPDATE locks SET acquired = ?, expires = ? WHERE name = ? AND owner = ?",
                    (now_str, expires_str, name, owner),
                )
                conn.commit()
                return True

            # If expired, steal/reclaim
            if current_expires and current_expires < now_str:
                cursor.execute(
                    "UPDATE locks SET owner = ?, acquired = ?, expires = ? WHERE name = ?",
                    (owner, now_str, expires_str, name),
                )
                conn.commit()
                return True

            conn.commit()
            return False
        except Exception:
            conn.rollback()
            return False

    def release(self, name: str, owner: str) -> bool:
        """Release a lock held by owner."""
        conn = self.db.connection
        try:
            cur = conn.execute(
                "DELETE FROM locks WHERE name = ? AND owner = ?",
                (name, owner),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            return False

    def renew(self, name: str, owner: str, ttl_seconds: int = 30) -> bool:
        """Renew the expiration of an existing lock held by owner."""
        now = _utcnow()
        expires = now + timedelta(seconds=ttl_seconds)
        now_str = _iso(now)
        expires_str = _iso(expires)

        conn = self.db.connection
        try:
            cur = conn.execute(
                "UPDATE locks SET expires = ? WHERE name = ? AND owner = ?",
                (expires_str, name, owner),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            return False
