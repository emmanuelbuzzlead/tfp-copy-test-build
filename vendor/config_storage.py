"""Config and persistent storage for saved lists and cost tracking."""

import os
import json
import sqlite3
from typing import List, Dict, Optional
from pathlib import Path


class ConfigStorage:
    """Persistent storage for saved lists, costs, and config."""

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            base_dir = os.path.expanduser("~/.buzzlead")
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / "storage.db"
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database with required tables."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS saved_lists (
                    id INTEGER PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    list_type TEXT NOT NULL,  -- 'domains', 'titles', 'industries', etc.
                    items TEXT NOT NULL,  -- JSON array
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS costs (
                    id INTEGER PRIMARY KEY,
                    service TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    cost REAL NOT NULL,
                    details TEXT,
                    timestamp TEXT
                )
            """)
            conn.commit()

    def save_list(self, name: str, list_type: str, items: List[str]) -> None:
        """Save a named list (e.g., exclusion list, industry list)."""
        from datetime import datetime
        now = datetime.utcnow().isoformat() + "Z"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO saved_lists (name, list_type, items, created_at, updated_at)
                VALUES (?, ?, ?, COALESCE(
                    (SELECT created_at FROM saved_lists WHERE name = ?),
                    ?
                ), ?)
                """,
                (name, list_type, json.dumps(items), name, now, now)
            )
            conn.commit()

    def load_list(self, name: str) -> Optional[List[str]]:
        """Load a saved list by name."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT items FROM saved_lists WHERE name = ?",
                (name,)
            )
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
        return None

    def list_saved_lists(self, list_type: Optional[str] = None) -> List[Dict]:
        """List all saved lists, optionally filtered by type."""
        with sqlite3.connect(self.db_path) as conn:
            if list_type:
                cursor = conn.execute(
                    "SELECT name, list_type, created_at, updated_at FROM saved_lists WHERE list_type = ? ORDER BY updated_at DESC",
                    (list_type,)
                )
            else:
                cursor = conn.execute(
                    "SELECT name, list_type, created_at, updated_at FROM saved_lists ORDER BY updated_at DESC"
                )
            return [
                {"name": row[0], "type": row[1], "created": row[2], "updated": row[3]}
                for row in cursor.fetchall()
            ]

    def delete_list(self, name: str) -> bool:
        """Delete a saved list."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM saved_lists WHERE name = ?", (name,))
            conn.commit()
            return cursor.rowcount > 0

    def log_cost(self, service: str, operation: str, cost: float, details: Optional[str] = None) -> None:
        """Log a single cost entry."""
        from datetime import datetime
        now = datetime.utcnow().isoformat() + "Z"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO costs (service, operation, cost, details, timestamp) VALUES (?, ?, ?, ?, ?)",
                (service, operation, cost, details, now)
            )
            conn.commit()

    def get_session_costs(self) -> List[Dict]:
        """Get all costs logged in the current session."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT service, operation, cost, details, timestamp FROM costs ORDER BY timestamp DESC"
            )
            return [
                {"service": row[0], "operation": row[1], "cost": row[2], "details": row[3], "timestamp": row[4]}
                for row in cursor.fetchall()
            ]

    def get_costs_summary(self) -> Dict:
        """Get cost summary grouped by service."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT service, SUM(cost) as total FROM costs GROUP BY service ORDER BY total DESC"
            )
            return {row[0]: row[1] for row in cursor.fetchall()}

    def reset_costs(self) -> int:
        """Clear all cost entries."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM costs")
            conn.commit()
            return cursor.rowcount
