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
    # Ein oder mehrere Server, z. B. ("euw1", "na1"); jeder crawlt mit eigenem Rate-Limit seiner Region.
    platforms: tuple[str, ...]

    @property
    def platform(self) -> str:
        return self.platforms[0]

    @property
    def region(self) -> str:
        return PLATFORM_TO_REGION[self.platform]


def parse_platforms(value: str) -> tuple[str, ...]:
    """`"euw1, NA1"` → `("euw1", "na1")`. Server, die sich eine Region teilen (euw1 und eun1), lehnen wir ab:
    Riot zählt die Match-Anfragen je Region, beide zusammen wären nicht schneller, sondern stritten ums Limit."""
    platforms = tuple(dict.fromkeys(p.strip().lower() for p in value.split(",") if p.strip()))
    unknown = [p for p in platforms if p not in PLATFORM_TO_REGION]
    if unknown or not platforms:
        allowed = ", ".join(sorted(PLATFORM_TO_REGION))
        raise ValueError(f"Unbekannter Server {', '.join(unknown) or '(leer)'}. Erlaubt: {allowed}")
    regions = [PLATFORM_TO_REGION[p] for p in platforms]
    shared = sorted({r for r in regions if regions.count(r) > 1})
    if shared:
        raise ValueError(
            f"Mehrere Server in derselben Region ({', '.join(shared)}) teilen sich das Rate-Limit; "
            "je Region nur einen Server angeben (z. B. euw1,na1)."
        )
    return platforms


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
    try:
        platforms = parse_platforms(get("RIOT_PLATFORM", "euw1") or "euw1")
    except ValueError as err:
        raise SystemExit(str(err)) from err
    return Settings(api_key=api_key, platforms=platforms)
