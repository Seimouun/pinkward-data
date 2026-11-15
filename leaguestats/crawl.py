"""Matches von Spielern aus der Rangliste laden.

Ablauf: Spieler der gewählten Tiers aus League-V4 holen, zufällig mischen, je Spieler die letzten
Ranked-Solo-Matches abfragen und unbekannte Matches speichern, bis das Ziel erreicht ist.
PUUIDs bleiben nur im Speicher; die Datenbank enthält keine Spieler.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .matches import SkipMatch, parse_match
from .riot_api import APEX_LEAGUES
from .store import Store

TIERS = ("CHALLENGER", "GRANDMASTER", "MASTER", "DIAMOND", "EMERALD", "PLATINUM", "GOLD", "SILVER", "BRONZE", "IRON")
DIVISIONS = ("I", "II", "III", "IV")


class Api(Protocol):
    def apex_league(self, tier: str) -> dict | None: ...
    def league_entries(self, tier: str, division: str, page: int) -> list | None: ...
    def match_ids(self, puuid: str, *, start_time: int | None = None, count: int = 20) -> list | None: ...
    def match(self, match_id: str) -> dict | None: ...
    def timeline(self, match_id: str) -> dict | None: ...


@dataclass
class CrawlResult:
    players: int = 0
    players_visited: int = 0
    saved: int = 0
    skipped: int = 0


def parse_tiers(value: str) -> list[str]:
    tiers = [tier.strip().upper() for tier in value.split(",") if tier.strip()]
    unknown = [tier for tier in tiers if tier not in TIERS]
    if unknown or not tiers:
        raise ValueError(f"Unbekannte Tiers: {', '.join(unknown) or '(leer)'}. Erlaubt: {', '.join(TIERS).lower()}")
    return tiers


def seed_players(api: Api, tiers: list[str], pages_per_division: int = 2) -> dict[str, str]:
    """PUUID → Tier, in dem der Spieler gefunden wurde (Reihenfolge wie geladen)."""
    players: dict[str, str] = {}
    for tier in tiers:
        if tier in APEX_LEAGUES:
            league = api.apex_league(tier) or {}
            entries = league.get("entries", [])
        else:
            entries = []
            for division in DIVISIONS:
                for page in range(1, pages_per_division + 1):
                    batch = api.league_entries(tier, division, page) or []
                    entries.extend(batch)
                    if not batch:
                        break
        for entry in entries:
            if entry.get("puuid"):
                players.setdefault(entry["puuid"], tier)
    return players


def crawl(
    api: Api,
    store: Store,
    *,
    tiers: list[str],
    max_matches: int,
    days: int = 14,
    timelines: bool = False,
    matches_per_player: int = 20,
    log: Callable[[str], None] = print,
    rng: random.Random | None = None,
    now: Callable[[], float] = time.time,
    on_saved: Callable[[int], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> CrawlResult:
    result = CrawlResult()
    tier_of = seed_players(api, tiers)
    players = list(tier_of)
    (rng or random.Random()).shuffle(players)
    result.players = len(players)
    log(f"{len(players)} Spieler aus {', '.join(t.lower() for t in tiers)} gefunden.")

    known = store.known_match_ids()
    start_time = int(now()) - days * 86400

    for puuid in players:
        if result.saved >= max_matches or should_stop():
            break
        result.players_visited += 1
        for match_id in api.match_ids(puuid, start_time=start_time, count=matches_per_player) or []:
            if result.saved >= max_matches or should_stop():
                break
            if match_id in known:
                continue
            known.add(match_id)

            match = api.match(match_id)
            if match is None:
                store.save_skipped(match_id, "not_found")
                result.skipped += 1
                continue
            try:
                parsed = parse_match(
                    match, api.timeline(match_id) if timelines else None, seed_tier=tier_of[puuid]
                )
            except SkipMatch as skip:
                store.save_skipped(match_id, skip.reason)
                result.skipped += 1
                continue

            if not store.save(parsed):
                continue
            result.saved += 1
            if result.saved % 50 == 0:
                log(f"  {result.saved}/{max_matches} Matches gespeichert")
            if on_saved:
                on_saved(result.saved)
    return result
