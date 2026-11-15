"""Matches zu Matchup-Statistiken verdichten und für die App als JSON schreiben.

Format (gelesen von app/src/analysis/matchups.ts):
  matchups["<rolle>:<a>:<b>"] = {games, wins[, goldDiff10, goldDiff15, xpDiff15, csDiff15]}
                                mit a < b; Siege und Differenzen aus Sicht von a
  roles["<championId>"]       = {<rolle>: Spiele}

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
WHERE list_contains(?, m.patch) AND a.champion_id < b.champion_id
GROUP BY a.role, a.champion_id, b.champion_id
HAVING count(*) >= ?
ORDER BY 1, 2, 3
"""

LANE_FIELDS = (("goldDiff10", 6), ("goldDiff15", 7), ("xpDiff15", 8), ("csDiff15", 9))

ROLE_SQL = """
SELECT p.champion_id, p.role, count(*)
FROM participants p
JOIN matches m ON m.match_id = p.match_id
WHERE list_contains(?, m.patch)
GROUP BY p.champion_id, p.role
ORDER BY 1, 2
"""


def latest_patches(store: Store, count: int) -> list[str]:
    rows = store.con.execute("SELECT DISTINCT patch FROM matches").fetchall()
    return sorted((row[0] for row in rows), key=version_key, reverse=True)[:count]


def build_export(
    store: Store, *, patches: int = 2, min_games: int = 20, now: datetime | None = None
) -> dict[str, Any]:
    selected = latest_patches(store, patches)
    data: dict[str, Any] = {
        "patches": selected,
        "generatedAt": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "matches": 0,
        "minGames": min_games,
        "matchups": {},
        "roles": {},
    }
    if not selected:
        return data

    con = store.con
    data["matches"] = con.execute(
        "SELECT count(*) FROM matches WHERE list_contains(?, patch)", [selected]
    ).fetchone()[0]

    for row in con.execute(MATCHUP_SQL, [selected, min_games]).fetchall():
        role, a, b, games, wins, timeline_games = row[:6]
        record: dict[str, int] = {"games": int(games), "wins": int(wins)}
        if timeline_games >= min_games:
            for name, index in LANE_FIELDS:
                if row[index] is not None:
                    record[name] = round(row[index])
        data["matchups"][f"{role}:{a}:{b}"] = record

    for champion_id, role, games in con.execute(ROLE_SQL, [selected]).fetchall():
        data["roles"].setdefault(str(champion_id), {})[role] = int(games)

    return data


def write_export(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=0, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8")
