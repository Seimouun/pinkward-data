import pytest
from factories import kill, make_full_timeline, make_match, make_timeline

from leaguestats.matches import PARSER_VERSION, PARTICIPANT_COLUMNS, SkipMatch, parse_match, patch_of


def test_parses_patch_roles_and_results():
    parsed = parse_match(make_match(blue=(86, 64, 103, 222, 412), blue_wins=False), seed_tier="MASTER")
    assert parsed.match["patch"] == "16.22"
    assert parsed.match["duration_s"] == 1800
    assert parsed.match["has_timeline"] is False
    assert parsed.match["platform"] == "EUW1"
    assert parsed.match["seed_tier"] == "MASTER"
    assert parsed.match["parser_version"] == PARSER_VERSION
    assert len(parsed.participants) == 10
    garen = parsed.participants[0]
    assert (garen["team_id"], garen["role"], garen["champion_id"], garen["win"]) == (100, "top", 86, False)
    assert {p["role"] for p in parsed.participants} == {"top", "jungle", "middle", "bottom", "utility"}


def test_stores_no_player_identity():
    parsed = parse_match(make_match(), make_full_timeline())
    rows = [parsed.match, *parsed.participants, *parsed.frames, *parsed.kills, *parsed.objective_events]
    text = repr(rows)
    assert "puuid" not in text and "Spieler" not in text
    assert not set(parsed.participants[0]) & {"puuid", "riot_id", "name", "summoner_id"}


def test_only_emits_known_participant_columns():
    parsed = parse_match(make_match())
    assert set(parsed.participants[0]) <= set(PARTICIPANT_COLUMNS)


def test_reads_loadout_and_role_assignment():
    p = parse_match(make_match()).participants[0]
    assert (p["summoner1"], p["summoner2"]) == (4, 12)
    assert (p["keystone"], p["primary_style"], p["sub_style"]) == (8010, 8000, 8400)
    assert p["perks"] == [8010, 9111, 9104, 8299, 8444, 8242]
    assert p["stat_perks"] == [5005, 5008, 5001]
    assert p["items"] == [3000, 3001, 3002, 3003, 3004, 3005, 3340]
    assert p["assigned_role"] == "top"
    assert p["role_preferences"] == "TOP.PRIMARY.TOP.MIDDLE"
    assert p["first_blood"] is True


def test_reads_stats_and_riot_lane_metrics():
    blue_top, red_top = parse_match(make_match()).participants[0], parse_match(make_match()).participants[5]
    assert (blue_top["kills"], blue_top["deaths"], blue_top["assists"]) == (5, 3, 7)
    assert blue_top["solo_kills"] == 1
    assert blue_top["lane_advantage"] == 1 and red_top["lane_advantage"] == 0
    assert blue_top["max_cs_advantage"] == pytest.approx(13)
    assert blue_top["damage_mitigated"] is None  # fehlt in der Antwort → NULL statt Fehler


def test_reads_gold_at_minute_15_from_timeline():
    parsed = parse_match(make_match(), make_timeline({1: 6000, 6: 5200}))
    assert parsed.match["has_timeline"] is True
    assert parsed.participants[0]["gold_15"] == 6000
    assert parsed.participants[5]["gold_15"] == 5200
    assert parsed.participants[1]["gold_15"] is None


def test_takes_snapshots_at_fixed_minutes():
    parsed = parse_match(make_match(), make_full_timeline(minutes=21))
    minutes = sorted({row["minute"] for row in parsed.frames})
    assert minutes == [5, 10, 15, 20]
    assert len(parsed.frames) == 4 * 10
    top_10 = next(r for r in parsed.frames if r["minute"] == 10 and (r["team_id"], r["role"]) == (100, "top"))
    assert top_10["total_gold"] == 500 + 10 * 400
    assert top_10["cs"] == 10 * 7 + 10
    assert top_10["damage_to_champions"] == 5000


def test_keeps_early_kills_with_roles():
    events = [kill(180, killer=2, victim=6, assists=(1,)), kill(1500, killer=7, victim=3)]
    parsed = parse_match(make_match(), make_full_timeline(events=events))
    assert len(parsed.kills) == 1  # der Kill nach Minute 20 fällt weg
    gank = parsed.kills[0]
    assert (gank["killer_team"], gank["killer_role"]) == (100, "jungle")
    assert (gank["victim_team"], gank["victim_role"]) == (200, "top")
    assert gank["assist_roles"] == ["top"]
    assert gank["at_s"] == 180


def test_records_objectives_for_the_team_that_took_them():
    events = [
        {"type": "ELITE_MONSTER_KILL", "timestamp": 400_000, "killerId": 7, "killerTeamId": 200,
         "monsterType": "DRAGON", "monsterSubType": "HEXTECH_DRAGON"},
        {"type": "BUILDING_KILL", "timestamp": 750_000, "killerId": 3, "teamId": 200,
         "buildingType": "TOWER_BUILDING", "laneType": "MID_LANE", "towerType": "OUTER_TURRET"},
        {"type": "TURRET_PLATE_DESTROYED", "timestamp": 230_000, "killerId": 0, "teamId": 100, "laneType": "BOT_LANE"},
    ]
    parsed = parse_match(make_match(), make_full_timeline(events=events))
    by_kind = {row["kind"]: row for row in parsed.objective_events}
    assert by_kind["DRAGON"]["team_id"] == 200 and by_kind["DRAGON"]["subtype"] == "HEXTECH_DRAGON"
    assert by_kind["DRAGON"]["killer_role"] == "jungle"
    assert by_kind["TOWER"] == {**by_kind["TOWER"], "team_id": 100, "lane": "middle", "subtype": "OUTER_TURRET"}
    assert by_kind["PLATE"]["team_id"] == 200 and by_kind["PLATE"]["lane"] == "bottom"
    assert by_kind["PLATE"]["killer_role"] is None  # von Vasallen zerstört


def test_reads_team_objectives_and_skips_empty_bans():
    parsed = parse_match(make_match())
    gator = {(r["team_id"], r["is_first"], r["kills"]) for r in parsed.team_objectives if r["objective"] == "voidGator"}
    assert gator == {(100, False, 0), (200, True, 1)}
    assert [(b["team_id"], b["champion_id"]) for b in parsed.bans] == [(100, 157), (200, 238)]


@pytest.mark.parametrize(
    "match, reason",
    [
        (make_match(duration=200), "remake"),
        (make_match(remake=True), "remake"),
        (make_match(positions=("TOP", "TOP", "MIDDLE", "BOTTOM", "UTILITY")), "positions"),
        (make_match(positions=("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "")), "positions"),
        (make_match(version="kaputt"), "version"),
    ],
)
def test_skips_unusable_matches(match, reason):
    with pytest.raises(SkipMatch) as skip:
        parse_match(match)
    assert skip.value.reason == reason


def test_patch_strips_build_numbers():
    assert patch_of("16.9.123.4") == "16.9"
