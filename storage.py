# -*- coding: utf-8 -*-
"""Persistencia do bot: rotas monitoradas e viagens ja vistas (SQLite).

Chamadas sao sincronas e rapidas; o bot as executa via `asyncio.to_thread`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

DEFAULT_DB = Path(__file__).with_name("blablacarfind.db")

# Sobrescreve tudo quando definido (usado nos testes). Em uso normal fica None e
# o caminho vem de BLABLACARFIND_DB ou do padrao.
DB_PATH: Path | None = None


def db_path() -> Path:
    """Resolvido a cada conexao, e nao no import: assim o .env ja foi carregado."""
    if DB_PATH is not None:
        return Path(DB_PATH)
    configured = (os.environ.get("BLABLACARFIND_DB") or "").strip()
    return Path(configured) if configured else DEFAULT_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    origin_name     TEXT NOT NULL,
    origin_place_id TEXT NOT NULL,
    origin_lat      REAL NOT NULL,
    origin_lng      REAL NOT NULL,
    dest_name       TEXT NOT NULL,
    dest_place_id   TEXT NOT NULL,
    dest_lat        REAL NOT NULL,
    dest_lng        REAL NOT NULL,
    travel_date     TEXT NOT NULL,
    seats           INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS seen_trips (
    watch_id   INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    trip_key   TEXT NOT NULL,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    price      TEXT,
    PRIMARY KEY (watch_id, trip_key)
);

CREATE INDEX IF NOT EXISTS idx_watches_active ON watches(active, travel_date);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


@dataclass
class Watch:
    id: int
    chat_id: int
    origin_name: str
    origin_place_id: str
    origin_lat: float
    origin_lng: float
    dest_name: str
    dest_place_id: str
    dest_lat: float
    dest_lng: float
    travel_date: str
    seats: int

    def label(self) -> str:
        day, month = self.travel_date[8:10], self.travel_date[5:7]
        return f"{self.origin_name} -> {self.dest_name} em {day}/{month}"


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def init() -> None:
    with _lock:
        _connect()


def _row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=row["id"],
        chat_id=row["chat_id"],
        origin_name=row["origin_name"],
        origin_place_id=row["origin_place_id"],
        origin_lat=row["origin_lat"],
        origin_lng=row["origin_lng"],
        dest_name=row["dest_name"],
        dest_place_id=row["dest_place_id"],
        dest_lat=row["dest_lat"],
        dest_lng=row["dest_lng"],
        travel_date=row["travel_date"],
        seats=row["seats"],
    )


def add_watch(
    chat_id: int,
    origin: tuple[str, str, float, float],
    dest: tuple[str, str, float, float],
    travel_date: str,
    seats: int = 1,
) -> int:
    """Cria a rota monitorada. Se ja existir uma igual e ativa, devolve a existente."""
    with _lock:
        conn = _connect()
        existing = conn.execute(
            """SELECT id FROM watches
               WHERE chat_id = ? AND origin_place_id = ? AND dest_place_id = ?
                 AND travel_date = ? AND seats = ? AND active = 1""",
            (chat_id, origin[1], dest[1], travel_date, seats),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO watches (chat_id, origin_name, origin_place_id, origin_lat, origin_lng,
                                    dest_name, dest_place_id, dest_lat, dest_lng, travel_date, seats)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (chat_id, *origin, *dest, travel_date, seats),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_watches(chat_id: int) -> list[Watch]:
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM watches WHERE chat_id = ? AND active = 1 ORDER BY travel_date, id",
            (chat_id,),
        ).fetchall()
    return [_row_to_watch(r) for r in rows]


def active_watches() -> list[Watch]:
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM watches WHERE active = 1 ORDER BY id"
        ).fetchall()
    return [_row_to_watch(r) for r in rows]


def get_watch(watch_id: int, chat_id: int | None = None) -> Watch | None:
    query = "SELECT * FROM watches WHERE id = ?"
    args: list[object] = [watch_id]
    if chat_id is not None:
        query += " AND chat_id = ?"
        args.append(chat_id)
    with _lock:
        row = _connect().execute(query, args).fetchone()
    return _row_to_watch(row) if row else None


def stop_watch(watch_id: int, chat_id: int) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE watches SET active = 0 WHERE id = ? AND chat_id = ? AND active = 1",
            (watch_id, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0


def expire_past_watches(today: str | None = None) -> list[Watch]:
    """Desativa rotas cuja data ja passou. Devolve as que foram encerradas."""
    today = today or _date.today().isoformat()
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM watches WHERE active = 1 AND travel_date < ?", (today,)
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE watches SET active = 0 WHERE active = 1 AND travel_date < ?", (today,)
            )
            conn.commit()
    return [_row_to_watch(r) for r in rows]


def known_keys(watch_id: int) -> set[str]:
    with _lock:
        rows = _connect().execute(
            "SELECT trip_key FROM seen_trips WHERE watch_id = ?", (watch_id,)
        ).fetchall()
    return {r["trip_key"] for r in rows}


def mark_seen(watch_id: int, entries: list[tuple[str, str]]) -> None:
    """Grava (trip_key, preco). Reaparecer depois nao gera nova notificacao."""
    if not entries:
        return
    with _lock:
        conn = _connect()
        conn.executemany(
            "INSERT OR IGNORE INTO seen_trips (watch_id, trip_key, price) VALUES (?,?,?)",
            [(watch_id, key, price) for key, price in entries],
        )
        conn.commit()


def seen_count(watch_id: int) -> int:
    with _lock:
        row = _connect().execute(
            "SELECT COUNT(*) AS n FROM seen_trips WHERE watch_id = ?", (watch_id,)
        ).fetchone()
    return int(row["n"])
