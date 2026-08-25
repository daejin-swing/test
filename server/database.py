import json
import sqlite3
import datetime
from pathlib import Path
from typing import Any
from config import DB_PATH, DEFAULT_DEVICE_CONFIG


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initializes SQLite tables if they do not exist."""
    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Devices Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT,
                target_branch TEXT DEFAULT 'master',
                current_branch TEXT,
                current_version TEXT,
                git_commit TEXT,
                system_status TEXT,
                config_json TEXT,
                config_version INTEGER DEFAULT 1,
                last_heartbeat_at TIMESTAMP,
                status TEXT DEFAULT 'offline'
            )
        """)

        # 2. Logs Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                log_type TEXT,
                level TEXT,
                module TEXT,
                message TEXT,
                traceback TEXT,
                context TEXT,
                created_at TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
            )
        """)

        # 3. Events Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_events (
                event_id TEXT PRIMARY KEY,
                device_id TEXT,
                event_type TEXT,
                g_force REAL,
                speed_kph REAL,
                latitude REAL,
                longitude REAL,
                video_url TEXT,
                video_size_bytes INTEGER,
                occurred_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
            )
        """)

        conn.commit()


def upsert_device_heartbeat(device_id: str, software: dict, system: dict, dashcam: dict, applied_config_version: int) -> dict:
    """Updates device heartbeat status and returns the latest config & version."""
    now = datetime.datetime.now(datetime.UTC).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT target_branch, config_json, config_version FROM devices WHERE device_id = ?", (device_id,))
        row = cursor.fetchone()

        if row is None:
            # First time seeing device: Initialize with default config
            initial_config = dict(DEFAULT_DEVICE_CONFIG)
            config_json_str = json.dumps(initial_config)
            config_version = 1
            target_branch = initial_config.get("target_branch", "master")

            cursor.execute("""
                INSERT INTO devices (
                    device_id, name, target_branch, current_branch, current_version,
                    git_commit, system_status, config_json, config_version, last_heartbeat_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                device_id,
                device_id,
                target_branch,
                software.get("git_branch"),
                software.get("version"),
                software.get("git_commit"),
                json.dumps(system),
                config_json_str,
                config_version,
                now,
                "online",
            ))
            conn.commit()
            return {"config": initial_config, "config_version": config_version}
        else:
            # Device exists: Update status
            config_version = row["config_version"]
            config_json = json.loads(row["config_json"]) if row["config_json"] else dict(DEFAULT_DEVICE_CONFIG)

            cursor.execute("""
                UPDATE devices SET
                    current_branch = ?,
                    current_version = ?,
                    git_commit = ?,
                    system_status = ?,
                    last_heartbeat_at = ?,
                    status = 'online'
                WHERE device_id = ?
            """, (
                software.get("git_branch"),
                software.get("version"),
                software.get("git_commit"),
                json.dumps(system),
                now,
                device_id,
            ))
            conn.commit()
            return {"config": config_json, "config_version": config_version}


def update_device_config(device_id: str, new_config_fields: dict) -> dict:
    """Updates specific config fields for a device and increments config_version."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT config_json, config_version, target_branch FROM devices WHERE device_id = ?", (device_id,))
        row = cursor.fetchone()

        current_config = dict(DEFAULT_DEVICE_CONFIG)
        current_version = 0

        if row:
            if row["config_json"]:
                current_config = json.loads(row["config_json"])
            current_version = row["config_version"] or 0

        # Deep merge/update
        current_config.update(new_config_fields)
        new_version = current_version + 1
        new_target_branch = current_config.get("target_branch", "master")

        cursor.execute("""
            UPDATE devices SET
                target_branch = ?,
                config_json = ?,
                config_version = ?
            WHERE device_id = ?
        """, (new_target_branch, json.dumps(current_config), new_version, device_id))
        conn.commit()

        return {"config": current_config, "config_version": new_version}


def list_devices() -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM devices ORDER BY last_heartbeat_at DESC")
        rows = cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("system_status"):
                d["system_status"] = json.loads(d["system_status"])
            if d.get("config_json"):
                d["config_json"] = json.loads(d["config_json"])
            result.append(d)
        return result


def get_device(device_id: str) -> dict | None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        row = cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("system_status"):
            d["system_status"] = json.loads(d["system_status"])
        if d.get("config_json"):
            d["config_json"] = json.loads(d["config_json"])
        return d


def insert_logs(device_id: str, log_type: str, entries: list[dict]):
    with get_db() as conn:
        cursor = conn.cursor()
        for e in entries:
            ts = e.get("timestamp")
            if isinstance(ts, (int, float)):
                ts_str = datetime.datetime.fromtimestamp(ts, datetime.UTC).isoformat()
            else:
                ts_str = str(ts or datetime.datetime.now(datetime.UTC).isoformat())

            cursor.execute("""
                INSERT INTO device_logs (device_id, log_type, level, module, message, traceback, context, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                device_id,
                log_type,
                e.get("level", "INFO"),
                e.get("module", ""),
                e.get("message", ""),
                e.get("traceback"),
                json.dumps(e.get("context", {})),
                ts_str,
            ))
        conn.commit()


def list_logs(device_id: str | None = None, limit: int = 100, level: str | None = None) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM device_logs WHERE 1=1"
        params = []
        if device_id:
            query += " AND device_id = ?"
            params.append(device_id)
        if level:
            query += " AND level = ?"
            params.append(level)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("context"):
                try:
                    d["context"] = json.loads(d["context"])
                except Exception:
                    pass
            result.append(d)
        return result


def insert_event(event_id: str, device_id: str, event_type: str, g_force: float, speed_kph: float,
                 location: dict, video_url: str, video_size_bytes: int, occurred_at: str):
    with get_db() as conn:
        cursor = conn.cursor()
        lat = location.get("lat") if location else None
        lng = location.get("lng") if location else None

        cursor.execute("""
            INSERT OR REPLACE INTO device_events (
                event_id, device_id, event_type, g_force, speed_kph, latitude, longitude,
                video_url, video_size_bytes, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id, device_id, event_type, g_force, speed_kph, lat, lng,
            video_url, video_size_bytes, occurred_at
        ))
        conn.commit()


def list_events(device_id: str | None = None, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM device_events WHERE 1=1"
        params = []
        if device_id:
            query += " AND device_id = ?"
            params.append(device_id)
        query += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]
