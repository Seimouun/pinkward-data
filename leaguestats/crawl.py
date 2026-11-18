"""Matches von Spielern aus der Rangliste laden.

Ablauf: Spieler der gewählten Tiers aus League-V4 holen, zufällig mischen, je Spieler die letzten
Ranked-Solo-Matches abfragen und unbekannte Matches speichern, bis das Ziel erreicht ist.
PUUIDs bleiben nur im Speicher; die Datenbank enthält keine Spieler.

Mehrere Server (z. B. EUW und NA) laufen mit `crawl_platforms` parallel in eigenen Threads: Riot zählt das
Rate-Limit je Region, jeder Server hat also sein eigenes Kontingent. Woher ein Match kommt, steht in
`matches.platform`.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .matches import LadderEntry, SkipMatch, parse_match
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


def seed_players(api: Api, tiers: list[str], pages_per_division: int = 2) -> dict[str, LadderEntry]:
    """PUUID → Platz in der Rangliste (Reihenfolge wie geladen).

    Dient zweimal: als Startpunkte für den Crawl und, um Spielern in gefundenen Matches ihr Tier und
    ihre LP zuzuordnen, ohne zusätzliche Anfrage. Die PUUIDs bleiben dabei nur im Arbeitsspeicher.
    """
    players: dict[str, LadderEntry] = {}
    for tier in tiers:
        if tier in APEX_LEAGUES:
            league = api.apex_league(tier) or {}
            entries = [{**entry, "tier": tier, "rank": None} for entry in league.get("entries", [])]
        else:
            entries = []
            for division in DIVISIONS:
                for page in range(1, pages_per_division + 1):
                    batch = api.league_entries(tier, division, page) or []
                    entries.extend({**entry, "tier": tier, "rank": entry.get("rank", division)} for entry in batch)
                    if not batch:
                        break
        for entry in entries:
            if entry.get("puuid"):
                label = entry["tier"] if entry["rank"] is None else f"{entry['tier']} {entry['rank']}"
                players.setdefault(entry["puuid"], LadderEntry(tier=label, league_points=entry.get("leaguePoints")))
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
    ladder = seed_players(api, tiers)
    players = list(ladder)
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
                    match,
                    api.timeline(match_id) if timelines else None,
                    seed_tier=ladder[puuid].tier,
                    ladder=ladder,
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


def crawl_platforms(
    apis: dict[str, Api],
    store: Store,
    *,
    log: Callable[[str], None] = print,
    on_saved: Callable[[int], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    **options,
) -> dict[str, CrawlResult | BaseException]:
    """Crawlt jeden Server in einem eigenen Thread; `max_matches` gilt je Server.

    `on_saved` bekommt die Summe über alle Server. Bricht ein Server mit einem Fehler ab, laufen die anderen
    weiter; der Fehler steht dann statt des Ergebnisses im Rückgabewert. Strg+C stoppt alle.
    """
    stop = threading.Event()
    lock = threading.Lock()
    total = 0
    results: dict[str, CrawlResult | BaseException] = {}

    def saved_one(_: int) -> None:
        nonlocal total
        with lock:
            total += 1
            count = total
        if on_saved:
            on_saved(count)

    def run(platform: str, api: Api) -> None:
        try:
            results[platform] = crawl(
                api,
                store,
                log=lambda message: log(f"[{platform}] {message}"),
                on_saved=saved_one,
                should_stop=lambda: stop.is_set() or should_stop(),
                **options,
            )
        except BaseException as err:  # noqa: BLE001 – Fehler eines Servers sollen die anderen nicht stoppen
            results[platform] = err
            log(f"[{platform}] abgebrochen: {err}")

    threads = [threading.Thread(target=run, args=item, name=f"crawl-{item[0]}", daemon=True) for item in apis.items()]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(timeout=0.5)
    except KeyboardInterrupt:
        stop.set()
        for thread in threads:
            thread.join()
        raise
    return results
