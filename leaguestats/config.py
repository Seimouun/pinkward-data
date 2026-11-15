"""Einstellungen aus Umgebungsvariablen oder pipeline/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = PIPELINE_DIR.parent
DEFAULT_DB = PIPELINE_DIR / "data" / "stats.duckdb"
DEFAULT_EXPORT = REPO_DIR / "app" / "src" / "data" / "matchups.json"

# Match-V5 läuft über regionale Hosts, League-V4 über den Server (Plattform).
PLATFORM_TO_REGION = {
    "euw1": "europe",
    "eun1": "europe",
    "tr1": "europe",
    "ru": "europe",
    "me1": "europe",
    "na1": "americas",
    "br1": "americas",
    "la1": "americas",
    "la2": "americas",
    "kr": "asia",
    "jp1": "asia",
    "oc1": "sea",
    "sg2": "sea",
    "tw2": "sea",
    "vn2": "sea",
    "ph2": "sea",
    "th2": "sea",
}


@dataclass(frozen=True)
class Settings:
    api_key: str
    platform: str
    region: str


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    # utf-8-sig: Windows-Editoren schreiben gern ein BOM an den Dateianfang.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_settings(env_file: Path = PIPELINE_DIR / ".env") -> Settings:
    file_values = read_env_file(env_file)

    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or file_values.get(key) or default

    api_key = get("RIOT_API_KEY")
    if not api_key or api_key.startswith("RGAPI-xxxx"):
        raise SystemExit(
            "RIOT_API_KEY fehlt. Kopiere pipeline/.env.example nach pipeline/.env und trag deinen Key ein."
        )
    platform = (get("RIOT_PLATFORM", "euw1") or "euw1").lower()
    if platform not in PLATFORM_TO_REGION:
        allowed = ", ".join(sorted(PLATFORM_TO_REGION))
        raise SystemExit(f"Unbekannter Server {platform!r}. Erlaubt: {allowed}")
    return Settings(api_key=api_key, platform=platform, region=PLATFORM_TO_REGION[platform])
