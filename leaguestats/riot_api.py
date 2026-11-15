"""Schmaler Client für die Riot Web API mit Rate-Limiting.

Riot begrenzt Anfragen pro API-Key (App-Limit, beim Development/Personal Key 20 pro Sekunde und
100 pro 2 Minuten). Wir halten die Limits clientseitig ein, übernehmen die tatsächlichen Werte aus
den Antwort-Headern und warten bei HTTP 429 so lange, wie Riot im Retry-After-Header verlangt.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Callable, Iterable

DEFAULT_APP_LIMITS: list[tuple[int, float]] = [(20, 1.0), (100, 120.0)]
APEX_LEAGUES = {
    "CHALLENGER": "challengerleagues",
    "GRANDMASTER": "grandmasterleagues",
    "MASTER": "masterleagues",
}
RANKED_SOLO = "RANKED_SOLO_5x5"
RANKED_SOLO_QUEUE_ID = 420


class RiotApiError(Exception):
    def __init__(self, status: int, path: str):
        if status in (401, 403):
            message = (
                f"Riot API lehnt den Key ab (HTTP {status}). Development Keys laufen nach 24 Stunden ab; "
                "hol dir unter https://developer.riotgames.com einen neuen."
            )
        else:
            message = f"Riot API: HTTP {status} bei {path}"
        super().__init__(message)
        self.status = status


def parse_limits(header: str | None, margin: int = 1) -> list[tuple[int, float]] | None:
    """`"20:1,100:120"` → `[(19, 1.0), (99, 120.0)]`; ein Request Puffer gegen Uhren-Abweichung."""
    if not header:
        return None
    limits = []
    for part in header.split(","):
        count, _, seconds = part.strip().partition(":")
        if not count.isdigit() or not seconds.isdigit():
            return None
        limits.append((max(1, int(count) - margin), float(seconds)))
    return limits or None


class RateLimiter:
    """Gleitende Zeitfenster: höchstens `count` Anfragen je `seconds`."""

    def __init__(
        self,
        limits: Iterable[tuple[int, float]] = DEFAULT_APP_LIMITS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._clock = clock
        self._sleep = sleep
        self._windows: dict[float, tuple[int, deque[float]]] = {}
        self.set_limits(limits)

    @property
    def limits(self) -> list[tuple[int, float]]:
        return [(count, seconds) for seconds, (count, _) in sorted(self._windows.items())]

    def set_limits(self, limits: Iterable[tuple[int, float]]) -> None:
        previous = self._windows
        self._windows = {
            seconds: (count, previous[seconds][1] if seconds in previous else deque())
            for count, seconds in limits
        }

    def acquire(self) -> None:
        while True:
            now = self._clock()
            wait = 0.0
            for seconds, (count, stamps) in self._windows.items():
                while stamps and now - stamps[0] >= seconds:
                    stamps.popleft()
                if len(stamps) >= count:
                    wait = max(wait, seconds - (now - stamps[0]))
            if wait <= 0:
                for _, stamps in self._windows.values():
                    stamps.append(now)
                return
            self._sleep(wait)


class RiotApi:
    def __init__(
        self,
        api_key: str,
        platform: str,
        region: str,
        *,
        limiter: RateLimiter | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 6,
    ):
        self._key = api_key
        self.platform = platform
        self.region = region
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self._opener = opener
        self._sleep = sleep
        self._max_attempts = max_attempts
        self.requests = 0

    # --- Endpunkte -------------------------------------------------------------------------

    def apex_league(self, tier: str) -> dict | None:
        return self._get(self.platform, f"/lol/league/v4/{APEX_LEAGUES[tier]}/by-queue/{RANKED_SOLO}")

    def league_entries(self, tier: str, division: str, page: int) -> list | None:
        return self._get(
            self.platform, f"/lol/league/v4/entries/{RANKED_SOLO}/{tier}/{division}", {"page": page}
        )

    def match_ids(self, puuid: str, *, start_time: int | None = None, count: int = 20) -> list | None:
        params: dict[str, Any] = {"queue": RANKED_SOLO_QUEUE_ID, "type": "ranked", "start": 0, "count": count}
        if start_time is not None:
            params["startTime"] = start_time
        return self._get(self.region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params)

    def match(self, match_id: str) -> dict | None:
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}")

    def timeline(self, match_id: str) -> dict | None:
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}/timeline")

    # --- HTTP ------------------------------------------------------------------------------

    def _get(self, host: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"https://{host}.api.riotgames.com{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        # Der Key steht nur im Header, nie in der URL oder in Fehlermeldungen.
        request = urllib.request.Request(
            url, headers={"X-Riot-Token": self._key, "User-Agent": "LeagueAnalyser-pipeline"}
        )

        for attempt in range(self._max_attempts):
            self.limiter.acquire()
            self.requests += 1
            try:
                with self._opener(request, timeout=30) as response:
                    self._update_limits(response.headers)
                    return json.load(response)
            except urllib.error.HTTPError as err:
                self._update_limits(err.headers)
                if err.code == 404:
                    return None
                if err.code == 429:
                    self._sleep(_retry_after(err.headers, default=5.0))
                    continue
                if err.code >= 500:
                    self._sleep(min(60.0, 2.0**attempt))
                    continue
                raise RiotApiError(err.code, path) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                self._sleep(min(60.0, 2.0**attempt))
        raise RiotApiError(0, f"{path} (nach {self._max_attempts} Versuchen)")

    def _update_limits(self, headers: Any) -> None:
        if headers is None:
            return
        limits = parse_limits(headers.get("X-App-Rate-Limit"))
        if limits and limits != self.limiter.limits:
            self.limiter.set_limits(limits)


def _retry_after(headers: Any, default: float) -> float:
    try:
        return max(1.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError, AttributeError):
        return default
