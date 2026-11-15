import json
from datetime import datetime, timezone

from factories import make_full_timeline, make_match, make_timeline

from leaguestats.export import build_export, latest_patches, write_export
from leaguestats.matches import parse_match

GAREN, DARIUS = 86, 122


def save(store, match, timeline=None):
    assert store.save(parse_match(match, timeline))


def garen_vs_darius(store, results, *, version="16.22.1.1", prefix="M"):
    """results: Liste von (Garen auf Blau?, Garen gewinnt?)"""
    for index, (garen_blue, garen_wins) in enumerate(results):
        blue, red = ((GAREN, 2, 3, 4, 5), (DARIUS, 7, 8, 9, 10))
        if not garen_blue:
            blue, red = ((DARIUS, 2, 3, 4, 5), (GAREN, 7, 8, 9, 10))
        save(store, make_match(f"{prefix}{index}", blue=blue, red=red, blue_wins=garen_wins == garen_blue, version=version))


def test_counts_matchups_once_from_the_lower_champion_id(store):
    garen_vs_darius(store, [(True, True), (True, True), (True, False), (False, False)])
    data = build_export(store, patches=1, min_games=1)
    assert data["matchups"]["top:86:122"] == {"games": 4, "wins": 2}
    assert "top:122:86" not in data["matchups"]
    assert data["matches"] == 4


def test_drops_matchups_below_min_games(store):
    garen_vs_darius(store, [(True, True)] * 3)
    assert "top:86:122" not in build_export(store, min_games=5)["matchups"]
    assert "top:86:122" in build_export(store, min_games=3)["matchups"]


def test_uses_only_the_newest_patches(store):
    garen_vs_darius(store, [(True, True)] * 2, version="16.9.1.1", prefix="old")
    garen_vs_darius(store, [(True, False)] * 3, version="16.10.1.1", prefix="new")
    assert latest_patches(store, 1) == ["16.10"]
    assert build_export(store, patches=1, min_games=1)["matchups"]["top:86:122"] == {"games": 3, "wins": 0}
    assert build_export(store, patches=2, min_games=1)["matchups"]["top:86:122"] == {"games": 5, "wins": 2}


def test_counts_games_per_role(store):
    garen_vs_darius(store, [(True, True)] * 2)
    roles = build_export(store, min_games=1)["roles"]
    assert roles["86"] == {"top": 2}
    assert roles["2"] == {"jungle": 2}


def test_gold_difference_is_from_the_lower_champion_id(store):
    # Darius (Blau, participantId 1) hat 800 Gold mehr als Garen (Rot, participantId 6).
    save(
        store,
        make_match("G1", blue=(DARIUS, 2, 3, 4, 5), red=(GAREN, 7, 8, 9, 10)),
        make_timeline({1: 6000, 6: 5200}),
    )
    record = build_export(store, min_games=1)["matchups"]["top:86:122"]
    assert record["goldDiff15"] == -800


def test_exports_lane_differences_from_timeline_snapshots(store):
    # Garen (Blau, participantId 1) holt pro Minute 30 Gold mehr als Darius (Rot, participantId 6).
    for index in range(2):
        save(
            store,
            make_match(f"L{index}", blue=(GAREN, 2, 3, 4, 5), red=(DARIUS, 7, 8, 9, 10)),
            make_full_timeline(gold_lead={1: 30}),
        )
    record = build_export(store, min_games=2)["matchups"]["top:86:122"]
    assert record["goldDiff10"] == 300
    assert record["goldDiff15"] == 450
    assert record["xpDiff15"] == 0 and record["csDiff15"] == 0


def test_omits_lane_differences_without_enough_timelines(store):
    save(store, make_match("T1", blue=(GAREN, 2, 3, 4, 5), red=(DARIUS, 7, 8, 9, 10)), make_full_timeline())
    save(store, make_match("T2", blue=(GAREN, 2, 3, 4, 5), red=(DARIUS, 7, 8, 9, 10)))
    assert build_export(store, min_games=2)["matchups"]["top:86:122"] == {"games": 2, "wins": 2}


def test_prune_keeps_only_the_newest_patches(store):
    garen_vs_darius(store, [(True, True)], version="16.20.1.1", prefix="a")
    garen_vs_darius(store, [(True, True)], version="16.21.1.1", prefix="b")
    garen_vs_darius(store, [(True, True)] * 2, version="16.22.1.1", prefix="c")
    assert store.prune(keep_patches=2) == ["16.20"]
    assert [row[0] for row in store.patch_summary()] == ["16.22", "16.21"]
    remaining = store.con.execute("SELECT count(*) FROM participants").fetchone()[0]
    assert remaining == 3 * 10
    assert store.prune(keep_patches=2) == []


def test_empty_database_exports_empty_tables(store):
    data = build_export(store)
    assert data["patches"] == [] and data["matchups"] == {} and data["matches"] == 0


def test_written_file_is_valid_json(store, tmp_path):
    garen_vs_darius(store, [(True, True)])
    data = build_export(store, min_games=1, now=datetime(2026, 11, 14, tzinfo=timezone.utc))
    path = tmp_path / "matchups.json"
    write_export(data, path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["generatedAt"] == "2026-11-14T00:00:00+00:00"
    assert loaded["matchups"]["top:86:122"] == {"games": 1, "wins": 1}
