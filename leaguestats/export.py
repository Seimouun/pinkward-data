"""Matches zu Matchup-Statistiken verdichten und für die App als JSON schreiben.

Format (gelesen von app/src/analysis/matchups.ts):
  matchups["<rolle>:<a>:<b>"] = {games, wins[, goldDiff10, goldDiff15, xpDiff15, csDiff15]}
                                mit a < b; Siege und Differenzen aus Sicht von a
  roles["<championId>"]       = {<rolle>: Spiele}
  roleWins["<championId>"]    = {<rolle>: Siege}
  builds["<rolle>:<championId>"] = {
      runes:  {primaryStyle, subStyle, perks: [4 Haupt-, 2 Nebenrunen], statPerks: [3], games, wins},
      spells: {ids: [a, b], games, wins},
      items:  [[itemId, Spiele, Siege, Kaufminute?], …],  häufigste Items im End-Inventar
      starters: {ids: [...], games, wins}                  häufigste Käufe der ersten 90 s
  }                             jeweils ab `min_games` Spielen; Kaufminute und Start-Items nur mit Kauf-Daten
  matchupItems["<rolle>:<champion>:<gegner>"] = [[itemId, Spiele, Siege], …]
                                Items im End-Inventar gegen genau diesen Lane-Gegner (beide Richtungen)

Die Lane-Differenzen stammen aus den Timeline-Schnappschüssen und erscheinen nur, wenn mindestens
`min_games` Spiele des Matchups eine Timeline haben.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import Store, version_key

MATCHUP_SQL = """
WITH lane AS (
    SELECT match_id, team_id, role,
           max(total_gold) FILTER (WHERE minute = 10) AS gold_10,
           max(xp)         FILTER (WHERE minute = 15) AS xp_15,
           max(cs)         FILTER (WHERE minute = 15) AS cs_15
    FROM participant_frames
    WHERE minute IN (10, 15)
    GROUP BY match_id, team_id, role
)
SELECT a.role,
       a.champion_id,
       b.champion_id,
       count(*)                                  AS games,
       sum(CASE WHEN a.win THEN 1 ELSE 0 END)    AS wins,
       count(a.gold_15 - b.gold_15)              AS timeline_games,
       avg(la.gold_10 - lb.gold_10)              AS gold_diff_10,
       avg(a.gold_15 - b.gold_15)                AS gold_diff_15,
       avg(la.xp_15 - lb.xp_15)                  AS xp_diff_15,
       avg(la.cs_15 - lb.cs_15)                  AS cs_diff_15
FROM participants a
JOIN participants b
  ON a.match_id = b.match_id AND a.role = b.role AND a.team_id <> b.team_id
JOIN matches m ON m.match_id = a.match_id
LEFT JOIN lane la ON la.match_id = a.match_id AND la.team_id = a.team_id AND la.role = a.role
LEFT JOIN lane lb ON lb.match_id = b.match_id AND lb.team_id = b.team_id AND lb.role = b.role
WHERE list_contains(?, m.patch) AND m.duration_s >= ? AND a.champion_id < b.champion_id
GROUP BY a.role, a.champion_id, b.champion_id
HAVING count(*) >= ?
ORDER BY 1, 2, 3
"""

LANE_FIELDS = (("goldDiff10", 6), ("goldDiff15", 7), ("xpDiff15", 8), ("csDiff15", 9))

# Vor Minute 15 endet ein Ranked-Spiel nur durch Remake, AFK oder vorzeitige Aufgabe.
DEFAULT_MIN_MINUTES = 15

ROLE_SQL = """
SELECT p.champion_id, p.role, count(*), sum(CASE WHEN p.win THEN 1 ELSE 0 END)
FROM participants p
JOIN matches m ON m.match_id = p.match_id
WHERE list_contains(?, m.patch) AND m.duration_s >= ?
GROUP BY p.champion_id, p.role
ORDER BY 1, 2
"""

# Häufigste Runenseite je Champion und Rolle. Die zwei Nebenrunen sind sortiert, weil Riot sie in der
# gewählten Reihenfolge liefert; die Werte-Runen (Shards) zählen getrennt, sonst zerfällt die Seite in
# zu viele Varianten.
RUNES_SQL = """
WITH pages AS (
    SELECT p.champion_id, p.role, p.primary_style, p.sub_style,
           p.perks[1:4] || list_sort(p.perks[5:6]) AS perks,
           count(*) AS games,
           sum(CASE WHEN p.win THEN 1 ELSE 0 END) AS wins
    FROM participants p
    JOIN matches m ON m.match_id = p.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ? AND len(p.perks) = 6
    GROUP BY ALL
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY champion_id, role ORDER BY games DESC, wins DESC) AS rank
    FROM pages
)
SELECT champion_id, role, primary_style, sub_style, perks, games, wins
FROM ranked
WHERE rank = 1 AND games >= ?
"""

SHARDS_SQL = """
WITH shards AS (
    SELECT p.champion_id, p.role, p.stat_perks, count(*) AS games
    FROM participants p
    JOIN matches m ON m.match_id = p.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ? AND len(p.stat_perks) = 3
    GROUP BY ALL
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY champion_id, role ORDER BY games DESC) AS rank FROM shards
)
SELECT champion_id, role, stat_perks FROM ranked WHERE rank = 1
"""

# Items im End-Inventar (ohne Trinket-Slot), je Spiel jedes Item einmal; dazu die mittlere Minute des
# ersten Kaufs, soweit Kauf-Daten da sind (ab Parser v4). Daraus sortiert die App die Kaufreihenfolge.
ITEMS_SQL = """
WITH inventory AS (
    SELECT p.match_id, p.team_id, p.role, p.champion_id, p.win,
           unnest(list_distinct(list_filter(p.items[1:6], x -> x > 0))) AS item_id
    FROM participants p
    JOIN matches m ON m.match_id = p.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ?
), first_buys AS (
    SELECT match_id, team_id, role, item_id, min(at_s) AS first_s
    FROM item_purchases
    GROUP BY ALL
), counted AS (
    SELECT i.champion_id, i.role, i.item_id,
           count(*) AS games,
           sum(CASE WHEN i.win THEN 1 ELSE 0 END) AS wins,
           avg(f.first_s) / 60 AS minute
    FROM inventory i
    LEFT JOIN first_buys f USING (match_id, team_id, role, item_id)
    GROUP BY ALL
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY champion_id, role ORDER BY games DESC, item_id) AS rank FROM counted
)
SELECT champion_id, role, item_id, games, wins, minute FROM ranked WHERE rank <= ? ORDER BY champion_id, role, rank
"""

# Dasselbe gegen einen bestimmten Lane-Gegner, nur für Paare mit genug Spielen.
MATCHUP_ITEMS_SQL = """
WITH pairs AS (
    SELECT a.match_id, a.team_id, a.role, a.champion_id, b.champion_id AS enemy_id, a.win, a.items
    FROM participants a
    JOIN participants b ON a.match_id = b.match_id AND a.role = b.role AND a.team_id <> b.team_id
    JOIN matches m ON m.match_id = a.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ?
), enough AS (
    SELECT role, champion_id, enemy_id FROM pairs GROUP BY ALL HAVING count(*) >= ?
), inventory AS (
    SELECT p.role, p.champion_id, p.enemy_id, p.win,
           unnest(list_distinct(list_filter(p.items[1:6], x -> x > 0))) AS item_id
    FROM pairs p
    JOIN enough USING (role, champion_id, enemy_id)
), counted AS (
    SELECT role, champion_id, enemy_id, item_id, count(*) AS games, sum(CASE WHEN win THEN 1 ELSE 0 END) AS wins
    FROM inventory
    GROUP BY ALL
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY role, champion_id, enemy_id ORDER BY games DESC, item_id) AS rank
    FROM counted
)
SELECT role, champion_id, enemy_id, item_id, games, wins FROM ranked WHERE rank <= ? ORDER BY 1, 2, 3, rank
"""

# Start-Items: alles, was in den ersten 90 Sekunden gekauft wurde, als sortierte Liste.
STARTERS_SQL = """
WITH starts AS (
    SELECT match_id, team_id, role, list_sort(list(item_id)) AS items
    FROM item_purchases
    WHERE at_s < 90
    GROUP BY ALL
), counted AS (
    SELECT p.champion_id, p.role, s.items, count(*) AS games, sum(CASE WHEN p.win THEN 1 ELSE 0 END) AS wins
    FROM starts s
    JOIN participants p USING (match_id, team_id, role)
    JOIN matches m ON m.match_id = p.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ?
    GROUP BY ALL
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY champion_id, role ORDER BY games DESC, wins DESC) AS rank FROM counted
)
SELECT champion_id, role, items, games, wins FROM ranked WHERE rank = 1 AND games >= ?
"""

# Wie viele Items je Champion und Rolle bzw. je Matchup exportiert werden (Komponenten, Tränke und
# Kontrollwards stehen mit drin; die App filtert mit den Item-Daten aus Data Dragon).
TOP_ITEMS = 20
TOP_MATCHUP_ITEMS = 12

# Spell-Paar ohne Reihenfolge (Flash auf D oder F ist Geschmack).
SPELLS_SQL = """
WITH pairs AS (
    SELECT p.champion_id, p.role,
           least(p.summoner1, p.summoner2) AS a, greatest(p.summoner1, p.summoner2) AS b,
           count(*) AS games,
           sum(CASE WHEN p.win THEN 1 ELSE 0 END) AS wins
    FROM participants p
    JOIN matches m ON m.match_id = p.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ? AND p.summoner1 IS NOT NULL AND p.summoner2 IS NOT NULL
    GROUP BY ALL
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY champion_id, role ORDER BY games DESC, wins DESC) AS rank FROM pairs
)
SELECT champion_id, role, a, b, games, wins FROM ranked WHERE rank = 1 AND games >= ?
"""


def latest_patches(store: Store, count: int) -> list[str]:
    rows = store.con.execute("SELECT DISTINCT patch FROM matches").fetchall()
    return sorted((row[0] for row in rows), key=version_key, reverse=True)[:count]


def build_export(
    store: Store,
    *,
    patches: int = 2,
    min_games: int = 20,
    min_minutes: int = DEFAULT_MIN_MINUTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """`min_minutes`: kürzere Spiele (Abbrüche, AFK, Aufgaben vor Minute 15) zählen nicht mit.
    Gespeichert bleiben sie trotzdem; der Filter greift nur hier."""
    selected = latest_patches(store, patches)
    data: dict[str, Any] = {
        "patches": selected,
        "generatedAt": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "matches": 0,
        "minGames": min_games,
        "minMinutes": min_minutes,
        "matchups": {},
        "roles": {},
        "roleWins": {},
        "builds": {},
        "matchupItems": {},
    }
    if not selected:
        return data

    con = store.con
    min_duration_s = min_minutes * 60
    data["matches"] = con.execute(
        "SELECT count(*) FROM matches WHERE list_contains(?, patch) AND duration_s >= ?", [selected, min_duration_s]
    ).fetchone()[0]

    for row in con.execute(MATCHUP_SQL, [selected, min_duration_s, min_games]).fetchall():
        role, a, b, games, wins, timeline_games = row[:6]
        record: dict[str, int] = {"games": int(games), "wins": int(wins)}
        if timeline_games >= min_games:
            for name, index in LANE_FIELDS:
                if row[index] is not None:
                    record[name] = round(row[index])
        data["matchups"][f"{role}:{a}:{b}"] = record

    for champion_id, role, games, wins in con.execute(ROLE_SQL, [selected, min_duration_s]).fetchall():
        data["roles"].setdefault(str(champion_id), {})[role] = int(games)
        data["roleWins"].setdefault(str(champion_id), {})[role] = int(wins)

    builds: dict[str, dict[str, Any]] = data["builds"]
    shards = {
        f"{role}:{champion_id}": [int(x) for x in stat_perks]
        for champion_id, role, stat_perks in con.execute(SHARDS_SQL, [selected, min_duration_s]).fetchall()
    }
    for champion_id, role, primary, sub, perks, games, wins in con.execute(
        RUNES_SQL, [selected, min_duration_s, min_games]
    ).fetchall():
        key = f"{role}:{champion_id}"
        builds.setdefault(key, {})["runes"] = {
            "primaryStyle": int(primary),
            "subStyle": int(sub),
            "perks": [int(x) for x in perks],
            "statPerks": shards.get(key, []),
            "games": int(games),
            "wins": int(wins),
        }
    for champion_id, role, a, b, games, wins in con.execute(SPELLS_SQL, [selected, min_duration_s, min_games]).fetchall():
        builds.setdefault(f"{role}:{champion_id}", {})["spells"] = {
            "ids": [int(a), int(b)],
            "games": int(games),
            "wins": int(wins),
        }

    params = [selected, min_duration_s]
    for champion_id, role, item_id, games, wins, minute in con.execute(ITEMS_SQL, [*params, TOP_ITEMS]).fetchall():
        if data["roles"].get(str(champion_id), {}).get(role, 0) < min_games:
            continue
        entry = [int(item_id), int(games), int(wins)]
        if minute is not None:
            entry.append(round(minute, 1))
        builds.setdefault(f"{role}:{champion_id}", {}).setdefault("items", []).append(entry)
    for champion_id, role, items, games, wins in con.execute(STARTERS_SQL, [*params, min_games]).fetchall():
        builds.setdefault(f"{role}:{champion_id}", {})["starters"] = {
            "ids": [int(x) for x in items],
            "games": int(games),
            "wins": int(wins),
        }
    for role, champion_id, enemy_id, item_id, games, wins in con.execute(
        MATCHUP_ITEMS_SQL, [*params, min_games, TOP_MATCHUP_ITEMS]
    ).fetchall():
        data["matchupItems"].setdefault(f"{role}:{champion_id}:{enemy_id}", []).append([int(item_id), int(games), int(wins)])

    return data


def write_export(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=0, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8")
