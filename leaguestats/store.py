"""Lokale DuckDB-Datenbank mit den geladenen Matches.

Das Schema wächst über nummerierte Migrationen. Eine bestehende Datenbank (lokal oder die aus dem
GitHub-Release) wird beim Öffnen automatisch auf den neuesten Stand gebracht; vorhandene Daten
bleiben erhalten, neue Spalten sind bei alten Matches NULL.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import duckdb

from .matches import PARTICIPANT_COLUMNS, ParsedMatch

# Tabellen, die je Match Zeilen haben und beim Aufräumen mitgelöscht werden.
MATCH_TABLES = (
    "participants",
    "participant_frames",
    "kills",
    "objective_events",
    "team_objectives",
    "bans",
    "item_purchases",
)


def _v1(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            match_id     VARCHAR PRIMARY KEY,
            patch        VARCHAR NOT NULL,
            game_start   TIMESTAMP,
            duration_s   INTEGER,
            queue_id     INTEGER,
            has_timeline BOOLEAN NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS participants (
            match_id    VARCHAR NOT NULL,
            team_id     INTEGER NOT NULL,
            role        VARCHAR NOT NULL,
            champion_id INTEGER NOT NULL,
            win         BOOLEAN NOT NULL,
            gold_15     INTEGER,
            PRIMARY KEY (match_id, team_id, role)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS skipped_matches (
            match_id VARCHAR PRIMARY KEY,
            reason   VARCHAR NOT NULL
        )
        """
    )


def _v2(con: duckdb.DuckDBPyConnection) -> None:
    """Breitere Erfassung: Loadout, Leistungswerte, Zeit-Schnappschüsse, frühe Kills, Objectives, Bans."""
    for column, sql_type in {
        "platform": "VARCHAR",
        "seed_tier": "VARCHAR",
        "ended_in_surrender": "BOOLEAN",
        "end_result": "VARCHAR",
        "parser_version": "INTEGER",
    }.items():
        con.execute(f"ALTER TABLE matches ADD COLUMN IF NOT EXISTS {column} {sql_type}")
    con.execute("UPDATE matches SET parser_version = 1 WHERE parser_version IS NULL")

    for column, sql_type in PARTICIPANT_COLUMNS.items():
        con.execute(f"ALTER TABLE participants ADD COLUMN IF NOT EXISTS {column} {sql_type}")

    # Damit sich übersprungene Matches nach einiger Zeit aufräumen lassen.
    con.execute("ALTER TABLE skipped_matches ADD COLUMN IF NOT EXISTS skipped_at TIMESTAMP")
    con.execute("UPDATE skipped_matches SET skipped_at = current_timestamp WHERE skipped_at IS NULL")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS participant_frames (
            match_id            VARCHAR NOT NULL,
            team_id             INTEGER NOT NULL,
            role                VARCHAR NOT NULL,
            minute              INTEGER NOT NULL,
            total_gold          INTEGER,
            xp                  INTEGER,
            level               INTEGER,
            cs                  INTEGER,
            damage_to_champions INTEGER,
            damage_taken        INTEGER,
            PRIMARY KEY (match_id, team_id, role, minute)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS kills (
            match_id        VARCHAR NOT NULL,
            at_s            INTEGER NOT NULL,
            killer_team     INTEGER,
            killer_role     VARCHAR,
            victim_team     INTEGER NOT NULL,
            victim_role     VARCHAR,
            assist_roles    VARCHAR[],
            x               INTEGER,
            y               INTEGER,
            bounty          INTEGER,
            shutdown_bounty INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS objective_events (
            match_id    VARCHAR NOT NULL,
            at_s        INTEGER NOT NULL,
            kind        VARCHAR NOT NULL,
            subtype     VARCHAR,
            team_id     INTEGER,
            lane        VARCHAR,
            killer_role VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS team_objectives (
            match_id  VARCHAR NOT NULL,
            team_id   INTEGER NOT NULL,
            objective VARCHAR NOT NULL,
            is_first  BOOLEAN,
            kills     INTEGER,
            PRIMARY KEY (match_id, team_id, objective)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS bans (
            match_id    VARCHAR NOT NULL,
            team_id     INTEGER NOT NULL,
            pick_turn   INTEGER NOT NULL,
            champion_id INTEGER NOT NULL,
            PRIMARY KEY (match_id, team_id, pick_turn)
        )
        """
    )


def _v3(con: duckdb.DuckDBPyConnection) -> None:
    """Rang und LP je Spieler aus der Rangliste, Kayns Form."""
    for column, sql_type in {"ladder_tier": "VARCHAR", "league_points": "INTEGER", "champion_form": "INTEGER"}.items():
        con.execute(f"ALTER TABLE participants ADD COLUMN IF NOT EXISTS {column} {sql_type}")


def _v4(con: duckdb.DuckDBPyConnection) -> None:
    """Item-Käufe aus der Timeline: Start-Items und Kaufreihenfolge je Spieler (seq zählt ab 0)."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS item_purchases (
            match_id VARCHAR NOT NULL,
            team_id  INTEGER NOT NULL,
            role     VARCHAR NOT NULL,
            seq      INTEGER NOT NULL,
            at_s     INTEGER NOT NULL,
            item_id  INTEGER NOT NULL,
            PRIMARY KEY (match_id, team_id, role, seq)
        )
        """
    )


# Server aus der Match-ID ("EUW1_123" → "EUW1"), falls `platform` fehlt.
FILL_PLATFORM = "UPDATE matches SET platform = upper(split_part(match_id, '_', 1)) WHERE platform IS NULL"


def _v5(con: duckdb.DuckDBPyConnection) -> None:
    """Server je Match: alte Matches ohne `platform` bekommen ihn aus der Match-ID."""
    con.execute(FILL_PLATFORM)


def _v6(con: duckdb.DuckDBPyConnection) -> None:
    """Skill-Reihenfolge je Spieler aus der Timeline (Parser v5)."""
    for column in ("skill_order", "skill_max"):
        con.execute(f"ALTER TABLE participants ADD COLUMN IF NOT EXISTS {column} VARCHAR")


# Reihenfolge ist verbindlich; neue Migrationen nur hinten anhängen, bestehende nie ändern.
# Migrationen müssen additiv bleiben (siehe Store.restore).
MIGRATIONS: list[Callable[[duckdb.DuckDBPyConnection], None]] = [_v1, _v2, _v3, _v4, _v5, _v6]
SCHEMA_VERSION = len(MIGRATIONS)


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        # Mehrere Server crawlen in eigenen Threads; die Verbindung darf nur einer zur Zeit benutzen.
        self.lock = threading.RLock()
        self._types: dict[str, dict[str, str]] = {}
        self._migrate()

    def close(self) -> None:
        self.con.close()

    # --- Schema -----------------------------------------------------------------------------

    @property
    def schema_version(self) -> int:
        return self.con.execute("SELECT version FROM schema_info").fetchone()[0]

    def _migrate(self) -> None:
        self.con.execute("CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL)")
        row = self.con.execute("SELECT version FROM schema_info").fetchone()
        if row is None:
            # Datenbanken aus der Zeit vor der Versionierung haben schon die v1-Tabellen.
            has_v1 = self.con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'matches'"
            ).fetchone()[0]
            self.con.execute("INSERT INTO schema_info VALUES (?)", [1 if has_v1 else 0])
            version = 1 if has_v1 else 0
        else:
            version = row[0]

        for number, migration in enumerate(MIGRATIONS, start=1):
            if number <= version:
                continue
            with self._transaction():
                migration(self.con)
                self.con.execute("UPDATE schema_info SET version = ?", [number])

    # --- Schreiben ----------------------------------------------------------------------------

    def known_match_ids(self) -> set[str]:
        with self.lock:
            rows = self.con.execute(
                "SELECT match_id FROM matches UNION ALL SELECT match_id FROM skipped_matches"
            ).fetchall()
        return {row[0] for row in rows}

    def save(self, parsed: ParsedMatch) -> bool:
        """Speichert ein Match mit allen Teiltabellen; False, wenn es schon da war."""
        match_id = parsed.match["match_id"]
        if not parsed.match.get("platform") and "_" in match_id:
            parsed.match["platform"] = match_id.split("_", 1)[0].upper()
        with self.lock:
            return self._save(parsed, match_id)

    def _save(self, parsed: ParsedMatch, match_id: str) -> bool:
        exists = self.con.execute("SELECT count(*) FROM matches WHERE match_id = ?", [match_id]).fetchone()[0]
        if exists:
            return False
        with self._transaction():
            self._insert("matches", [parsed.match])
            self._insert("participants", parsed.participants)
            self._insert("participant_frames", parsed.frames)
            self._insert("kills", parsed.kills)
            self._insert("objective_events", parsed.objective_events)
            self._insert("team_objectives", parsed.team_objectives)
            self._insert("bans", parsed.bans)
            self._insert("item_purchases", parsed.item_purchases)
        return True

    def save_skipped(self, match_id: str, reason: str) -> None:
        with self.lock:
            self.con.execute(
                "INSERT OR IGNORE INTO skipped_matches (match_id, reason, skipped_at) VALUES (?, ?, current_timestamp)",
                [match_id, reason],
            )

    def _insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        # Spalten immer namentlich: Nach Migrationen stimmt die Reihenfolge in der Tabelle nicht mehr.
        columns = list(rows[0])
        types = self._column_types(table)
        structure = json.dumps([{c: [types[c][:-2]] if types[c].endswith("[]") else types[c] for c in columns}])
        payload = json.dumps([{c: _jsonable(row.get(c)) for c in columns} for row in rows])
        # Alle Zeilen als ein JSON-Dokument übergeben: DuckDBs Python-Parameterübergabe kostet etwa
        # 1 ms pro Wert (≈ 1,7 s pro Match), der JSON-Weg ~50-mal weniger.
        self.con.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            "SELECT unnest(from_json(?::JSON, ?), recursive := true)",
            [payload, structure],
        )

    def _column_types(self, table: str) -> dict[str, str]:
        if table not in self._types:
            rows = self.con.execute(f"DESCRIBE {table}").fetchall()
            self._types[table] = {row[0]: row[1] for row in rows}
        return self._types[table]

    def _transaction(self):
        store = self

        class _Tx:
            def __enter__(self):
                store.con.execute("BEGIN TRANSACTION")

            def __exit__(self, exc_type, exc, tb):
                # Auch bei Strg+C (BaseException) zurückrollen, damit keine Transaktion offen bleibt.
                store.con.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Tx()

    # --- Lesen und Aufräumen ------------------------------------------------------------------

    def patch_summary(self) -> list[tuple[str, int, int]]:
        """(Patch, Matches, davon mit Timeline), neuester Patch zuerst."""
        rows = self.con.execute(
            "SELECT patch, count(*), count(*) FILTER (WHERE has_timeline) FROM matches GROUP BY patch"
        ).fetchall()
        return sorted(rows, key=lambda row: version_key(row[0]), reverse=True)

    def platform_summary(self) -> list[tuple[str, int]]:
        """Matches je Server (z. B. EUW1, NA1), häufigster zuerst."""
        return self.con.execute(
            "SELECT coalesce(platform, '?'), count(*) FROM matches GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()

    def skipped_count(self) -> int:
        return self.con.execute("SELECT count(*) FROM skipped_matches").fetchone()[0]

    def prune(self, keep_patches: int, skipped_days: int = 30) -> list[str]:
        """Löscht Matches aller Patches außer den neuesten `keep_patches` und alte übersprungene IDs.

        Gibt die gelöschten Patches zurück. Übersprungene IDs werden nur gebraucht, solange der Crawl
        sie wieder finden könnte (Standard: Matches der letzten 14 Tage).
        """
        patches = [row[0] for row in self.patch_summary()]
        dropped = patches[keep_patches:]
        with self._transaction():
            if dropped:
                for table in MATCH_TABLES:
                    self.con.execute(
                        f"DELETE FROM {table} WHERE match_id IN "
                        "(SELECT match_id FROM matches WHERE list_contains(?, patch))",
                        [dropped],
                    )
                self.con.execute("DELETE FROM matches WHERE list_contains(?, patch)", [dropped])
            self.con.execute(
                "DELETE FROM skipped_matches WHERE skipped_at < current_timestamp - to_days(?)", [skipped_days]
            )
        self.con.execute("CHECKPOINT")
        return dropped

    # --- Austausch über Parquet ---------------------------------------------------------------
    #
    # Eine DuckDB-Datei braucht etwa 10 KB je Match, zstd-komprimiertes Parquet etwa 2 KB. Für die
    # Ablage im GitHub-Release (höchstens 2 GB je Datei) wird deshalb Parquet verwendet, eine Datei
    # je Tabelle.

    def snapshot(self, directory: str | Path) -> list[Path]:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        files = []
        for table in ("matches", *MATCH_TABLES, "skipped_matches"):
            path = target / f"{table}.parquet"
            self.con.execute(
                f"COPY (SELECT * FROM {table} ORDER BY match_id) TO '{path.as_posix()}' "
                "(FORMAT parquet, COMPRESSION zstd)"
            )
            files.append(path)
        return files

    def restore(self, directory: str | Path) -> int:
        """Lädt einen Snapshot in eine leere Datenbank; gibt die Zahl der Matches zurück.

        `BY NAME`: Snapshots älterer Schema-Versionen passen weiter, fehlende Spalten werden NULL.
        Migrationen müssen deshalb additiv bleiben (Spalten hinzufügen, nie umbenennen).
        """
        if self.con.execute("SELECT count(*) FROM matches").fetchone()[0]:
            raise ValueError("Die Datenbank ist nicht leer; ein Snapshot lässt sich nur in eine leere laden.")
        source = Path(directory)
        with self._transaction():
            for table in ("matches", *MATCH_TABLES, "skipped_matches"):
                path = source / f"{table}.parquet"
                if path.exists():
                    self.con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM read_parquet('{path.as_posix()}')")
            self.con.execute(FILL_PLATFORM)
        return self.con.execute("SELECT count(*) FROM matches").fetchone()[0]


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def version_key(patch: str) -> tuple[int, ...]:
    return tuple(int(part) for part in patch.split(".") if part.isdigit())
