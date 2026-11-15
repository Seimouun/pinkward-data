import email.message
import io
import json
import urllib.error

import pytest

from leaguestats.riot_api import RateLimiter, RiotApi, RiotApiError, parse_limits


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def headers(values: dict[str, str] | None = None) -> email.message.Message:
    message = email.message.Message()
    for key, value in (values or {}).items():
        message[key] = value
    return message


class FakeResponse(io.BytesIO):
    def __init__(self, payload, response_headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers(response_headers)


def http_error(code: int, response_headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example", code, "error", headers(response_headers), io.BytesIO(b"{}"))


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_api(opener, clock=None):
    clock = clock or FakeClock()
    limiter = RateLimiter([(100, 1.0)], clock=clock, sleep=clock.sleep)
    return RiotApi("RGAPI-test", "euw1", "europe", limiter=limiter, opener=opener, sleep=clock.sleep), clock


def test_parse_limits_keeps_one_request_buffer():
    assert parse_limits("20:1,100:120") == [(19, 1.0), (99, 120.0)]
    assert parse_limits("") is None
    assert parse_limits("kaputt") is None


def test_rate_limiter_waits_when_window_is_full():
    clock = FakeClock()
    limiter = RateLimiter([(3, 1.0)], clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire()
    assert clock.slept == []
    limiter.acquire()
    assert clock.slept == [1.0]


def test_rate_limiter_respects_the_longer_window():
    clock = FakeClock()
    limiter = RateLimiter([(10, 1.0), (4, 10.0)], clock=clock, sleep=clock.sleep)
    for _ in range(5):
        limiter.acquire()
    assert sum(clock.slept) == pytest.approx(10.0)


def test_sends_key_only_in_header_and_uses_regional_host():
    opener = FakeOpener(FakeResponse(["EUW1_1"]))
    api, _ = make_api(opener)
    assert api.match_ids("abc", count=5) == ["EUW1_1"]
    request = opener.requests[0]
    assert request.full_url.startswith("https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/abc/ids?")
    assert "queue=420" in request.full_url
    assert "RGAPI" not in request.full_url
    assert request.get_header("X-riot-token") == "RGAPI-test"


def test_retries_after_429_with_retry_after():
    opener = FakeOpener(http_error(429, {"Retry-After": "3"}), FakeResponse({"ok": 1}))
    api, clock = make_api(opener)
    assert api.match("EUW1_1") == {"ok": 1}
    assert 3.0 in clock.slept
    assert api.requests == 2


def test_returns_none_for_404():
    api, _ = make_api(FakeOpener(http_error(404)))
    assert api.match("EUW1_404") is None


def test_explains_rejected_key():
    api, _ = make_api(FakeOpener(http_error(403)))
    with pytest.raises(RiotApiError, match="24 Stunden"):
        api.apex_league("CHALLENGER")


def test_retries_server_errors_with_backoff():
    api, clock = make_api(FakeOpener(http_error(503), http_error(503), FakeResponse({"entries": []})))
    assert api.apex_league("MASTER") == {"entries": []}
    assert clock.slept == [1.0, 2.0]


def test_adopts_app_limits_from_response_headers():
    api, _ = make_api(FakeOpener(FakeResponse({}, {"X-App-Rate-Limit": "50:1,500:600"})))
    api.apex_league("CHALLENGER")
    assert api.limiter.limits == [(49, 1.0), (499, 600.0)]
