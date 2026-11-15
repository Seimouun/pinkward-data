import random

import pytest
from factories import make_match

from leaguestats.crawl import crawl, parse_tiers, seed_players


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
        return {"entries": [{"puuid": "p1"}, {"puuid": "p2"}]} if tier == "CHALLENGER" else None

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
    assert list(players) == ["p1", "p2", "DIAMOND-I", "DIAMOND-II", "DIAMOND-III", "DIAMOND-IV"]
    assert players["p1"] == "CHALLENGER" and players["DIAMOND-II"] == "DIAMOND"


def test_records_the_tier_a_match_was_found_in(store):
    run(FakeApi(), store)
    assert {row[0] for row in store.con.execute("SELECT seed_tier FROM matches").fetchall()} == {"CHALLENGER"}


def test_parse_tiers_validates_input():
    assert parse_tiers("master, Diamond") == ["MASTER", "DIAMOND"]
    with pytest.raises(ValueError):
        parse_tiers("legende")
