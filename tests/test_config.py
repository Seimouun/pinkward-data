import pytest

from leaguestats.config import load_settings, read_env_file


def test_reads_env_file_with_comments_and_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# Kommentar\nRIOT_API_KEY="RGAPI-abc"\nRIOT_PLATFORM=KR\n', encoding="utf-8")
    assert read_env_file(env) == {"RIOT_API_KEY": "RGAPI-abc", "RIOT_PLATFORM": "KR"}


def test_maps_platform_to_region(tmp_path, monkeypatch):
    monkeypatch.delenv("RIOT_API_KEY", raising=False)
    monkeypatch.delenv("RIOT_PLATFORM", raising=False)
    env = tmp_path / ".env"
    env.write_text("RIOT_API_KEY=RGAPI-abc\nRIOT_PLATFORM=kr\n", encoding="utf-8")
    settings = load_settings(env)
    assert (settings.platform, settings.region) == ("kr", "asia")


def test_rejects_missing_or_placeholder_key(tmp_path, monkeypatch):
    monkeypatch.delenv("RIOT_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("RIOT_API_KEY=RGAPI-xxxxxxxx-xxxx\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="RIOT_API_KEY"):
        load_settings(env)
