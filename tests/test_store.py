import duckdb
from factories import kill, make_full_timeline, make_match

from leaguestats.matches import PARTICIPANT_COLUMNS, parse_match
from leaguestats.store import SCHEMA_VERSION, Store


def columns(store, table):
    return {row[0] for row in store.con.execute(f"DESCRIBE {table}").fetchall()}


def test_fresh_database_has_the_latest_schema(store):
    assert store.schema_version == SCHEMA_VERSION
    assert set(PARTICIPANT_COLUMNS) <= columns(store, "participants")
    for table in ("participant_frames", "kills", "objective_events", "team_objectives", "bans"):
        assert columns(store, table)


def test_migrates_a_v1_database_without_losing_data(tmp_path):
    path = tmp_path / "alt.duckdb"
    old = duckdb.connect(str(path))
    old.execute(
        "CREATE TABLE matches (match_id VARCHAR PRIMARY KEY, patch VARCHAR NOT NULL, game_start TIMESTAMP, "
        "duration_s INTEGER, queue_id INTEGER, has_timeline BOOLEAN NOT NULL)"
    )
    old.execute(
        "CREATE TABLE participants (match_id VARCHAR NOT NULL, team_id INTEGER NOT NULL, role VARCHAR NOT NULL, "
        "champion_id INTEGER NOT NULL, win BOOLEAN NOT NULL, gold_15 INTEGER, PRIMARY KEY (match_id, team_id, role))"
    )
    old.execute("CREATE TABLE skipped_matches (match_id VARCHAR PRIMARY KEY, reason VARCHAR NOT NULL)")
    old.execute("INSERT INTO matches VALUES ('OLD_1', '16.21', NULL, 1800, 420, true)")
    old.execute("INSERT INTO participants VALUES ('OLD_1', 100, 'top', 86, true, 6000)")
    old.close()

    store = Store(path)
    try:
        assert store.schema_version == SCHEMA_VERSION
        assert store.con.execute("SELECT champion_id, gold_15, items FROM participants").fetchall() == [(86, 6000, None)]
        assert store.con.execute("SELECT parser_version FROM matches").fetchone()[0] == 1
        assert store.save(parse_match(make_match("NEW_1"), make_full_timeline()))
        assert store.known_match_ids() == {"OLD_1", "NEW_1"}
    finally:
        store.close()

    # Erneutes Öffnen migriert nichts doppelt.
    again = Store(path)
    assert again.schema_version == SCHEMA_VERSION
    again.close()


def test_saves_every_table_once(store):
    parsed = parse_match(make_match(), make_full_timeline(events=[kill(200, 2, 6)]))
    assert store.save(parsed) is True
    assert store.save(parsed) is False
    count = lambda table: store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: E731
    assert (count("matches"), count("participants"), count("kills")) == (1, 10, 1)
    assert count("participant_frames") == 6 * 10
    assert count("bans") == 2 and count("team_objectives") == 4
    items = store.con.execute("SELECT items FROM participants WHERE team_id = 100 AND role = 'top'").fetchone()[0]
    assert items == [3000, 3001, 3002, 3003, 3004, 3005, 3340]


def test_snapshot_and_restore_round_trip(store, tmp_path):
    store.save(parse_match(make_match("A"), make_full_timeline(events=[kill(200, 2, 6)]), seed_tier="MASTER"))
    store.save(parse_match(make_match("B", version="16.21.1.1")))
    store.save_skipped("R", "remake")
    files = store.snapshot(tmp_path / "snap")
    assert all(path.exists() for path in files)

    copy = Store(":memory:")
    try:
        assert copy.restore(tmp_path / "snap") == 2
        for table in ("matches", "participants", "participant_frames", "kills", "bans", "skipped_matches"):
            original = store.con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
            restored = copy.con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
            assert restored == original, table
        try:
            copy.restore(tmp_path / "snap")
            raise AssertionError("Restore in eine volle Datenbank muss scheitern")
        except ValueError:
            pass
    finally:
        copy.close()


def test_restore_accepts_snapshots_of_older_schemas(tmp_path):
    old = duckdb.connect(":memory:")
    old.execute("CREATE TABLE m AS SELECT 'OLD' AS match_id, '16.21' AS patch, 1800 AS duration_s, true AS has_timeline")
    old.execute("CREATE TABLE p AS SELECT 'OLD' AS match_id, 100 AS team_id, 'top' AS role, 86 AS champion_id, true AS win")
    (tmp_path / "snap").mkdir()
    old.execute(f"COPY m TO '{(tmp_path / 'snap' / 'matches.parquet').as_posix()}' (FORMAT parquet)")
    old.execute(f"COPY p TO '{(tmp_path / 'snap' / 'participants.parquet').as_posix()}' (FORMAT parquet)")

    store = Store(":memory:")
    try:
        assert store.restore(tmp_path / "snap") == 1
        assert store.con.execute("SELECT champion_id, items FROM participants").fetchall() == [(86, None)]
    finally:
        store.close()


def test_prune_forgets_old_skipped_ids(store):
    store.save_skipped("ALT", "remake")
    store.save_skipped("NEU", "remake")
    store.con.execute("UPDATE skipped_matches SET skipped_at = current_timestamp - INTERVAL 40 DAY WHERE match_id = 'ALT'")
    store.prune(keep_patches=3)
    assert store.known_match_ids() == {"NEU"}


def test_prune_removes_all_rows_of_old_patches(store):
    store.save(parse_match(make_match("OLD", version="16.20.1.1"), make_full_timeline(events=[kill(200, 2, 6)])))
    store.save(parse_match(make_match("NEW", version="16.22.1.1"), make_full_timeline()))
    assert store.prune(keep_patches=1) == ["16.20"]
    for table in ("participants", "participant_frames", "kills", "team_objectives", "bans"):
        leftover = store.con.execute(f"SELECT count(*) FROM {table} WHERE match_id = 'OLD'").fetchone()[0]
        assert leftover == 0, table


def test_fills_the_platform_from_the_match_id(store, tmp_path):
    match = make_match("NA1_42")
    match["info"].pop("platformId")
    assert store.save(parse_match(match, None))
    assert store.con.execute("SELECT platform FROM matches").fetchone()[0] == "NA1"

    # Snapshots aus der Zeit ohne Server-Spalte bekommen ihn beim Wiederherstellen.
    store.con.execute("UPDATE matches SET platform = NULL")
    store.snapshot(tmp_path)
    restored = Store(":memory:")
    try:
        restored.restore(tmp_path)
        assert restored.con.execute("SELECT platform FROM matches").fetchone()[0] == "NA1"
    finally:
        restored.close()
