import json
from datetime import datetime, timezone

from factories import buy, make_full_timeline, make_match, make_timeline

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


def test_counts_wins_per_role(store):
    garen_vs_darius(store, [(True, True), (True, False), (False, True)])
    data = build_export(store, min_games=1)
    assert data["roles"]["86"] == {"top": 3}
    assert data["roleWins"]["86"] == {"top": 2}
    assert data["roleWins"]["122"] == {"top": 1}


def test_exports_the_most_common_runes_and_spells(store):
    garen_vs_darius(store, [(True, True), (True, False)])
    build = build_export(store, min_games=2)["builds"]["top:86"]
    # Nebenrunen sortiert, Werte getrennt gezählt, Spells ohne Reihenfolge (siehe factories.make_match)
    assert build["runes"] == {
        "primaryStyle": 8000,
        "subStyle": 8400,
        "perks": [8010, 9111, 9104, 8299, 8242, 8444],
        "statPerks": [5005, 5008, 5001],
        "games": 2,
        "wins": 1,
    }
    assert build["spells"] == {"ids": [4, 12], "games": 2, "wins": 1}
    assert "top:86" not in build_export(store, min_games=3)["builds"]


def test_exports_items_with_first_buy_minute_and_starters(store):
    # Garen (participantId 1) kauft Doran's Blade und Trank zum Start, dann 3001 nach 10 Minuten.
    events = [buy(10, 1, 1055), buy(11, 1, 2003), buy(600, 1, 3001)]
    for index in range(2):
        save(store, make_match(f"I{index}", blue=(GAREN, 2, 3, 4, 5), red=(DARIUS, 7, 8, 9, 10)), make_full_timeline(events=events))
    data = build_export(store, min_games=2)
    build = data["builds"]["top:86"]
    # End-Inventar aus factories.make_match: 3000–3005 (Trinket 3340 zählt nicht)
    items = {entry[0]: entry for entry in build["items"]}
    assert set(items) == {3000, 3001, 3002, 3003, 3004, 3005}
    assert items[3001] == [3001, 2, 2, 10.0]
    assert items[3000] == [3000, 2, 2]  # nie gekauft laut Timeline: keine Kaufminute
    assert build["starters"] == {"ids": [1055, 2003], "games": 2, "wins": 2}
    assert data["matchupItems"]["top:86:122"][0][1:] == [2, 2]
    assert data["matchupItems"]["top:122:86"][0][1:] == [2, 0]
    assert "top:86:122" not in build_export(store, min_games=3)["matchupItems"]


def test_counts_boots_in_the_role_slot_of_bot_laners(store):
    # make_match: Bot-Laner haben 3006 im roleBoundItem-Slot, alle anderen den Platzhalter 1220
    garen_vs_darius(store, [(True, True), (True, False)])
    builds = build_export(store, min_games=2)["builds"]
    bottom = {entry[0]: entry for entry in builds["bottom:4"]["items"]}
    assert bottom[3006][1:3] == [2, 1]
    top = {entry[0] for entry in builds["top:86"]["items"]}
    assert 1220 not in top and 3006 not in top
    assert any(entry[0] == 3006 for entry in build_export(store, min_games=2)["matchupItems"]["bottom:4:9"])


def test_exports_the_most_common_skill_order(store):
    from factories import skill_ups

    # Garen (participantId 1) maxt zweimal Q > E > W und einmal E > Q > W; Start immer Q, E, W
    orders = ["QEWQQRQEQEREEWWRWW", "QEWQQRQEQEREEWWRWW", "QEWEERE"]
    for index, order in enumerate(orders):
        save(
            store,
            make_match(f"K{index}", blue=(GAREN, 2, 3, 4, 5), red=(DARIUS, 7, 8, 9, 10), blue_wins=index != 1),
            make_full_timeline(events=skill_ups(1, order)),
        )
    skills = build_export(store, min_games=2)["builds"]["top:86"]["skills"]
    assert skills == {"max": "QEW", "games": 2, "wins": 1, "start": "QEW"}
    assert "skills" not in build_export(store, min_games=3)["builds"].get("top:86", {})


def test_items_work_without_purchase_data(store):
    garen_vs_darius(store, [(True, True), (True, False)])
    build = build_export(store, min_games=2)["builds"]["top:86"]
    assert all(len(entry) == 3 for entry in build["items"])
    assert "starters" not in build


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


def test_ignores_games_shorter_than_15_minutes(store):
    save(store, make_match("KURZ", blue=(GAREN, 2, 3, 4, 5), red=(DARIUS, 7, 8, 9, 10), duration=6 * 60))
    save(store, make_match("NORMAL", blue=(GAREN, 2, 3, 4, 5), red=(DARIUS, 7, 8, 9, 10), duration=15 * 60))
    data = build_export(store, min_games=1)
    assert data["matches"] == 1 and data["minMinutes"] == 15
    assert data["matchups"]["top:86:122"]["games"] == 1
    assert data["roles"]["86"] == {"top": 1}
    assert build_export(store, min_games=1, min_minutes=5)["matches"] == 2


def test_exports_stats_per_patch(store):
    garen_vs_darius(store, [(True, True), (False, False)], version="16.21.1.1", prefix="old")
    garen_vs_darius(store, [(True, False)] * 3, version="16.22.1.1", prefix="new")
    stats = build_export(store, patches=2, min_games=1)["patchStats"]
    assert set(stats) == {"16.22", "16.21"}
    assert stats["16.21"]["matches"] == 2
    assert stats["16.21"]["champions"]["86"] == {"top": [2, 1]}
    assert stats["16.22"]["champions"]["86"] == {"top": [3, 0]}
    assert stats["16.22"]["champions"]["122"] == {"top": [3, 3]}


def test_counts_bans_once_per_game(store):
    # make_match: Blau bannt 157 (dazu ein leerer Ban -1), Rot 238
    garen_vs_darius(store, [(True, True)] * 2)
    bans = build_export(store, min_games=1)["patchStats"]["16.22"]["bans"]
    assert bans == {"157": 2, "238": 2}


def test_exports_bot_lane_duos_per_patch(store):
    # Blau: Bot 4 + Support 5, Rot: Bot 9 + Support 10
    garen_vs_darius(store, [(True, True), (True, False), (True, True)])
    duos = build_export(store, min_games=1)["patchStats"]["16.22"]["duos"]
    assert duos["4:5"] == [3, 2]
    assert duos["9:10"] == [3, 1]
    garen_vs_darius(store, [(True, True)], prefix="X")
    assert len(build_export(store, min_games=1)["patchStats"]["16.22"]["duos"]) == 2


def test_empty_database_exports_empty_tables(store):
    data = build_export(store)
    assert data["patches"] == [] and data["matchups"] == {} and data["matches"] == 0
    assert data["patchStats"] == {}


def test_written_file_is_valid_json(store, tmp_path):
    garen_vs_darius(store, [(True, True)])
    data = build_export(store, min_games=1, now=datetime(2026, 11, 14, tzinfo=timezone.utc))
    path = tmp_path / "matchups.json"
    write_export(data, path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["generatedAt"] == "2026-11-14T00:00:00+00:00"
    assert loaded["matchups"]["top:86:122"] == {"games": 1, "wins": 1}
