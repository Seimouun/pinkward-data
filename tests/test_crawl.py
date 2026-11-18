import random

import pytest
from factories import make_match

from leaguestats.crawl import crawl, crawl_platforms, parse_tiers, seed_players


class FakeApi:
    def __init__(self):
        self.matches = {
            "A": make_match("A"),
            "B": make_match("B"),
            "C": make_match("C"),
            "R": make_match("R", remake=True),
        }
        self.match_calls: list[str] = []
        self.timeline_calls: list[str] = []

    def apex_league(self, tier):
        if tier != "CHALLENGER":
            return None
        # "puuid-0" ist Blau-Top in jedem Test-Match (siehe factories.make_match).
        return {"entries": [{"puuid": "p1", "leaguePoints": 900}, {"puuid": "p2"}, {"puuid": "puuid-0", "leaguePoints": 1234}]}

    def league_entries(self, tier, division, page):
        return [{"puuid": f"{tier}-{division}"}] if page == 1 else []

    def match_ids(self, puuid, *, start_time=None, count=20):
        return {"p1": ["A", "B", "R"], "p2": ["B", "C", "GONE"]}.get(puuid, [])

    def match(self, match_id):
        self.match_calls.append(match_id)
        return self.matches.get(match_id)

    def timeline(self, match_id):
        self.timeline_calls.append(match_id)
        return None


def run(api, store, **kwargs):
    options = {"tiers": ["CHALLENGER"], "max_matches": 100, "log": lambda _: None, "rng": random.Random(1)}
    return crawl(api, store, **{**options, **kwargs})


def test_saves_each_match_once_and_records_skips(store):
    api = FakeApi()
    result = run(api, store)
    assert result.saved == 3
    assert result.skipped == 2  # Remake + nicht gefunden
    assert sorted(api.match_calls) == ["A", "B", "C", "GONE", "R"]
    assert store.known_match_ids() == {"A", "B", "C", "R", "GONE"}


def test_stops_at_the_requested_number_of_matches(store):
    result = run(FakeApi(), store, max_matches=2)
    assert result.saved == 2


def test_stops_when_time_is_up(store):
    checks = iter([False, False, True])
    result = run(FakeApi(), store, should_stop=lambda: next(checks, True))
    assert result.saved == 1


def test_reports_each_saved_match(store):
    seen = []
    run(FakeApi(), store, on_saved=seen.append)
    assert seen == [1, 2, 3]


def test_second_run_skips_known_matches(store):
    run(FakeApi(), store)
    api = FakeApi()
    result = run(api, store)
    assert result.saved == 0
    assert api.match_calls == []


def test_loads_timelines_only_when_requested(store):
    api = FakeApi()
    run(api, store)
    assert api.timeline_calls == []
    api = FakeApi()
    run(api, store=type(store)(":memory:"), timelines=True)
    assert sorted(api.timeline_calls) == ["A", "B", "C", "R"]


def test_seeds_divisions_for_non_apex_tiers():
    players = seed_players(FakeApi(), ["CHALLENGER", "DIAMOND"])
    assert list(players) == ["p1", "p2", "puuid-0", "DIAMOND-I", "DIAMOND-II", "DIAMOND-III", "DIAMOND-IV"]
    assert players["p1"] == ("CHALLENGER", 900)
    assert players["DIAMOND-II"].tier == "DIAMOND II"


def test_tags_participants_found_in_the_ladder(store):
    run(FakeApi(), store)
    tagged = store.con.execute(
        "SELECT DISTINCT team_id, role, ladder_tier, league_points FROM participants WHERE ladder_tier IS NOT NULL"
    ).fetchall()
    assert tagged == [(100, "top", "CHALLENGER", 1234)]
    untagged = store.con.execute("SELECT count(*) FROM participants WHERE ladder_tier IS NULL").fetchone()[0]
    assert untagged == 3 * 9


def test_records_the_tier_a_match_was_found_in(store):
    run(FakeApi(), store)
    assert {row[0] for row in store.con.execute("SELECT seed_tier FROM matches").fetchall()} == {"CHALLENGER"}


def test_parse_tiers_validates_input():
    assert parse_tiers("master, Diamond") == ["MASTER", "DIAMOND"]
    with pytest.raises(ValueError):
        parse_tiers("legende")


class PrefixedApi(FakeApi):
    """Wie FakeApi, aber mit Match-IDs eines Servers (z. B. NA1_A)."""

    def __init__(self, prefix, fail=False):
        super().__init__()
        self.prefix = prefix
        self.fail = fail
        self.matches = {f"{prefix}_{key}": make_match(f"{prefix}_{key}") for key in ("A", "B", "C")}

    def match_ids(self, puuid, *, start_time=None, count=20):
        if self.fail:
            raise RuntimeError("Key abgelehnt")
        return [f"{self.prefix}_{key}" for key in {"p1": ["A", "B"], "p2": ["C"]}.get(puuid, [])]


def test_crawls_several_platforms_in_parallel(store):
    seen = []
    results = crawl_platforms(
        {"euw1": PrefixedApi("EUW1"), "na1": PrefixedApi("NA1")},
        store,
        tiers=["CHALLENGER"],
        max_matches=100,
        log=lambda _: None,
        on_saved=seen.append,
    )
    assert {p: r.saved for p, r in results.items()} == {"euw1": 3, "na1": 3}
    assert sorted(seen) == [1, 2, 3, 4, 5, 6]
    rows = store.con.execute("SELECT split_part(match_id, '_', 1), count(*) FROM matches GROUP BY 1 ORDER BY 1")
    assert rows.fetchall() == [("EUW1", 3), ("NA1", 3)]


def test_one_failing_platform_does_not_stop_the_others(store):
    results = crawl_platforms(
        {"euw1": PrefixedApi("EUW1"), "na1": PrefixedApi("NA1", fail=True)},
        store,
        tiers=["CHALLENGER"],
        max_matches=100,
        log=lambda _: None,
    )
    assert results["euw1"].saved == 3
    assert isinstance(results["na1"], RuntimeError)
