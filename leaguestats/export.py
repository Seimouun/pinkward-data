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
  builds["<rolle>:<championId>"].skills = {max: "QEW", start: "QWE", games, wins}
  builds["<rolle>:<championId>"].variants = [{name: "AP" | "AD", games, wins, runes, spells, items, …}, …]
                                nur bei Champions mit zwei Spielweisen (nach Anteil magischen Schadens)
                                häufigste Max-Reihenfolge und die häufigsten drei ersten Punkte (seit Parser v5)
  patchStats["<patch>"] = {     je Patch einzeln (seit Export v3), damit die App nach Patch filtern kann
      matches: Spiele,
      champions: {"<championId>": {<rolle>: [Spiele, Siege]}},
      bans: {"<championId>": Spiele, in denen er gebannt wurde},
      duos: {"<botId>:<supportId>": [Spiele, Siege]}   Bot-Lane-Duos ab `DUO_MIN_GAMES` Spielen je Patch
  }

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

# Items im End-Inventar ohne Trinket-Slot. Bei Bot-Lanern stehen die Stiefel seit der Rollen-Quest nicht im
# Inventar, sondern im eigenen Slot `roleBoundItem`; bei den anderen Rollen steht dort nur ein Quest-Platzhalter
# (z. B. 1206), darum zählt der Slot nur für `bottom`.
INVENTORY = """list_distinct(list_filter(
    p.items[1:6] || CASE WHEN p.role = 'bottom' THEN [p.role_bound_item] ELSE []::INTEGER[] END,
    x -> x > 0))"""

# Je Spiel jedes Item einmal; dazu die mittlere Minute des ersten Kaufs, soweit Kauf-Daten da sind (ab
# Parser v4). Daraus sortiert die App die Kaufreihenfolge.
ITEMS_SQL = f"""
WITH inventory AS (
    SELECT p.match_id, p.team_id, p.role, p.champion_id, p.win,
           unnest({INVENTORY}) AS item_id
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
MATCHUP_ITEMS_SQL = f"""
WITH pairs AS (
    SELECT a.match_id, a.team_id, a.role, a.champion_id, b.champion_id AS enemy_id, a.win, a.items, a.role_bound_item
    FROM participants a
    JOIN participants b ON a.match_id = b.match_id AND a.role = b.role AND a.team_id <> b.team_id
    JOIN matches m ON m.match_id = a.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ?
), enough AS (
    SELECT role, champion_id, enemy_id FROM pairs GROUP BY ALL HAVING count(*) >= ?
), inventory AS (
    SELECT p.role, p.champion_id, p.enemy_id, p.win,
           unnest({INVENTORY}) AS item_id
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

# Skill-Reihenfolge: häufigste Max-Reihenfolge der Grundfähigkeiten und, getrennt gezählt, die häufigsten
# ersten drei Punkte (Level 1–3). Nur Spiele mit Skill-Daten (Parser v5).
SKILLS_SQL = """
WITH counted AS (
    SELECT p.champion_id, p.role, p.skill_max,
           count(*) AS games,
           sum(CASE WHEN p.win THEN 1 ELSE 0 END) AS wins
    FROM participants p
    JOIN matches m ON m.match_id = p.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ? AND p.skill_max IS NOT NULL
    GROUP BY ALL
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY champion_id, role ORDER BY games DESC, wins DESC) AS rank FROM counted
)
SELECT champion_id, role, skill_max, games, wins FROM ranked WHERE rank = 1 AND games >= ?
"""

SKILL_START_SQL = """
WITH counted AS (
    SELECT p.champion_id, p.role, p.skill_order[1:3] AS start, count(*) AS games
    FROM participants p
    JOIN matches m ON m.match_id = p.match_id
    WHERE list_contains(?, m.patch) AND m.duration_s >= ? AND length(p.skill_order) >= 3
    GROUP BY ALL
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY champion_id, role ORDER BY games DESC) AS rank FROM counted
)
SELECT champion_id, role, start FROM ranked WHERE rank = 1
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


# Je Patch getrennt: Spiele und Siege je Champion und Rolle, Bans und Bot-Lane-Duos.
PATCH_MATCHES_SQL = """
SELECT patch, count(*) FROM matches WHERE list_contains(?, patch) AND duration_s >= ? GROUP BY 1
"""

PATCH_ROLES_SQL = """
SELECT m.patch, p.champion_id, p.role, count(*), sum(CASE WHEN p.win THEN 1 ELSE 0 END)
FROM participants p
JOIN matches m ON m.match_id = p.match_id
WHERE list_contains(?, m.patch) AND m.duration_s >= ?
GROUP BY ALL
ORDER BY 1, 2, 3
"""

# Spiele, in denen der Champion gebannt war (zählt einmal, auch wenn beide Teams ihn bannen).
BANS_SQL = """
SELECT m.patch, b.champion_id, count(DISTINCT b.match_id)
FROM bans b
JOIN matches m ON m.match_id = b.match_id
WHERE list_contains(?, m.patch) AND m.duration_s >= ? AND b.champion_id > 0
GROUP BY ALL
ORDER BY 1, 2
"""

DUOS_SQL = """
SELECT m.patch, a.champion_id, s.champion_id, count(*), sum(CASE WHEN a.win THEN 1 ELSE 0 END)
FROM participants a
JOIN participants s ON s.match_id = a.match_id AND s.team_id = a.team_id AND s.role = 'utility'
JOIN matches m ON m.match_id = a.match_id
WHERE a.role = 'bottom' AND list_contains(?, m.patch) AND m.duration_s >= ?
GROUP BY ALL
HAVING count(*) >= ?
ORDER BY 1, 2, 3
"""

# Seltene Duos fallen je Patch weg, damit die Datei klein bleibt; die App verlangt ohnehin mehr Spiele.
DUO_MIN_GAMES = 3


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
        "patchStats": {},
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

    params = [selected, min_duration_s]
    role_games = {
        (int(champion_id), role): games
        for champion_id, by_role in data["roles"].items()
        for role, games in by_role.items()
    }
    data["builds"] = _builds(con, "participants", params, min_games, role_games)
    for role, champion_id, enemy_id, item_id, games, wins in con.execute(
        MATCHUP_ITEMS_SQL, [*params, min_games, TOP_MATCHUP_ITEMS]
    ).fetchall():
        data["matchupItems"].setdefault(f"{role}:{champion_id}:{enemy_id}", []).append([int(item_id), int(games), int(wins)])

    _add_variants(con, data["builds"], params, min_games)

    patch_stats: dict[str, dict[str, Any]] = data["patchStats"]
    for patch in selected:
        patch_stats[patch] = {"matches": 0, "champions": {}, "bans": {}, "duos": {}}
    for patch, matches in con.execute(PATCH_MATCHES_SQL, params).fetchall():
        patch_stats[patch]["matches"] = int(matches)
    for patch, champion_id, role, games, wins in con.execute(PATCH_ROLES_SQL, params).fetchall():
        patch_stats[patch]["champions"].setdefault(str(champion_id), {})[role] = [int(games), int(wins)]
    for patch, champion_id, banned in con.execute(BANS_SQL, params).fetchall():
        patch_stats[patch]["bans"][str(champion_id)] = int(banned)
    for patch, bottom, support, games, wins in con.execute(DUOS_SQL, [*params, DUO_MIN_GAMES]).fetchall():
        patch_stats[patch]["duos"][f"{bottom}:{support}"] = [int(games), int(wins)]

    return data


def _source(sql: str, table: str) -> str:
    """Dieselbe Abfrage auf einer anderen Teilnehmer-Tabelle (für Build-Varianten)."""
    return sql.replace("participants p", f"{table} p")


def _builds(con, table: str, params: list, min_games: int, role_games: dict[tuple[int, str], int]) -> dict[str, dict[str, Any]]:
    """Runen, Spells, Items, Start-Items und Skills je `rolle:champion` aus `table`."""
    builds: dict[str, dict[str, Any]] = {}
    shards = {
        f"{role}:{champion_id}": [int(x) for x in stat_perks]
        for champion_id, role, stat_perks in con.execute(_source(SHARDS_SQL, table), params).fetchall()
    }
    for champion_id, role, primary, sub, perks, games, wins in con.execute(
        _source(RUNES_SQL, table), [*params, min_games]
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
    for champion_id, role, a, b, games, wins in con.execute(_source(SPELLS_SQL, table), [*params, min_games]).fetchall():
        builds.setdefault(f"{role}:{champion_id}", {})["spells"] = {
            "ids": [int(a), int(b)],
            "games": int(games),
            "wins": int(wins),
        }
    for champion_id, role, item_id, games, wins, minute in con.execute(
        _source(ITEMS_SQL, table), [*params, TOP_ITEMS]
    ).fetchall():
        if role_games.get((int(champion_id), role), 0) < min_games:
            continue
        entry = [int(item_id), int(games), int(wins)]
        if minute is not None:
            entry.append(round(minute, 1))
        builds.setdefault(f"{role}:{champion_id}", {}).setdefault("items", []).append(entry)
    for champion_id, role, items, games, wins in con.execute(_source(STARTERS_SQL, table), [*params, min_games]).fetchall():
        builds.setdefault(f"{role}:{champion_id}", {})["starters"] = {
            "ids": [int(x) for x in items],
            "games": int(games),
            "wins": int(wins),
        }
    starts = {
        f"{role}:{champion_id}": start
        for champion_id, role, start in con.execute(_source(SKILL_START_SQL, table), params).fetchall()
    }
    for champion_id, role, maxed, games, wins in con.execute(_source(SKILLS_SQL, table), [*params, min_games]).fetchall():
        key = f"{role}:{champion_id}"
        skills = {"max": maxed, "games": int(games), "wins": int(wins)}
        if key in starts:
            skills["start"] = starts[key]
        builds.setdefault(key, {})["skills"] = skills
    return builds


# Build-Varianten: Manche Champions werden auf zwei Arten gespielt (Katarina AD mit Conqueror oder AP mit
# Electrocute). Getrennt wird je Champion und Rolle nach dem Anteil magischen Schadens an Champions: Zerfallen
# die Spiele in zwei deutlich getrennte Gruppen, bekommt jede ihren eigenen Build.
DAMAGE_SHARES_SQL = """
SELECT p.champion_id, p.role,
       list(p.magic_to_champions / (p.physical_to_champions + p.magic_to_champions + p.true_to_champions))
FROM participants p
JOIN matches m ON m.match_id = p.match_id
WHERE list_contains(?, m.patch) AND m.duration_s >= ?
  AND p.physical_to_champions + p.magic_to_champions + p.true_to_champions > 0
GROUP BY ALL
"""

VARIANTS_TABLE_SQL = """
CREATE OR REPLACE TEMP TABLE build_variants AS
SELECT p.match_id, p.team_id, p.role,
       p.champion_id * 10 + CASE WHEN p.magic_to_champions
            / (p.physical_to_champions + p.magic_to_champions + p.true_to_champions) >= s.threshold THEN 2 ELSE 1 END
           AS champion_id
FROM participants p
JOIN matches m ON m.match_id = p.match_id
JOIN variant_splits s ON s.champion_id = p.champion_id AND s.role = p.role
WHERE list_contains(?, m.patch) AND m.duration_s >= ?
  AND p.physical_to_champions + p.magic_to_champions + p.true_to_champions > 0
"""

# Code der Variante in der letzten Stelle der Champion-ID (×10): 1 = weniger, 2 = mehr magischer Schaden.
VARIANT_NAMES = {1: "AD", 2: "AP"}
# Ab diesem Abstand der Gruppen-Mittel (Anteil magischer Schaden) gilt ein Champion als zweigeteilt.
VARIANT_MIN_GAP = 0.3
# Die kleinere Gruppe braucht mindestens diesen Anteil der Spiele (und min_games).
VARIANT_MIN_SHARE = 0.12


def split_threshold(shares: list[float], min_games: int) -> float | None:
    """Grenze zwischen zwei Gruppen (beste Zweiteilung nach kleinster Streuung innerhalb), oder None, wenn
    die Spiele nicht deutlich in zwei Spielweisen zerfallen."""
    values = sorted(shares)
    n = len(values)
    if n < 2 * min_games:
        return None
    prefix = [0.0]
    prefix_sq = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
        prefix_sq.append(prefix_sq[-1] + value * value)

    def spread(i: int, j: int) -> float:
        count = j - i
        total = prefix[j] - prefix[i]
        return (prefix_sq[j] - prefix_sq[i]) - total * total / count

    smallest = max(min_games, int(n * VARIANT_MIN_SHARE))
    best: tuple[float, int] | None = None
    for cut in range(smallest, n - smallest + 1):
        cost = spread(0, cut) + spread(cut, n)
        if best is None or cost < best[0]:
            best = (cost, cut)
    if best is None:
        return None
    cut = best[1]
    low = (prefix[cut] - prefix[0]) / cut
    high = (prefix[n] - prefix[cut]) / (n - cut)
    if high - low < VARIANT_MIN_GAP:
        return None
    return (values[cut - 1] + values[cut]) / 2


def _add_variants(con, builds: dict[str, dict[str, Any]], params: list, min_games: int) -> None:
    """Hängt bei zweigeteilten Champions `variants` an den Build: je Spielweise Name, Spiele, Siege und ein
    eigener Build (gleiches Format), häufigste Variante zuerst."""
    splits = []
    for champion_id, role, shares in con.execute(DAMAGE_SHARES_SQL, params).fetchall():
        threshold = split_threshold([float(x) for x in shares if x is not None], min_games)
        if threshold is not None:
            splits.append((int(champion_id), role, threshold))
    if not splits:
        return
    con.execute("CREATE OR REPLACE TEMP TABLE variant_splits (champion_id INTEGER, role VARCHAR, threshold DOUBLE)")
    con.executemany("INSERT INTO variant_splits VALUES (?, ?, ?)", splits)
    con.execute(VARIANTS_TABLE_SQL, params)
    con.execute(
        """
        CREATE OR REPLACE TEMP VIEW variant_participants AS
        SELECT p.* REPLACE (v.champion_id AS champion_id)
        FROM participants p
        JOIN build_variants v USING (match_id, team_id, role)
        """
    )
    totals = con.execute(
        """
        SELECT p.champion_id, p.role, count(*), sum(CASE WHEN p.win THEN 1 ELSE 0 END)
        FROM variant_participants p GROUP BY ALL
        """
    ).fetchall()
    role_games = {(int(champion_id), role): int(games) for champion_id, role, games, _ in totals}
    variant_builds = _builds(con, "variant_participants", params, min_games, role_games)
    for champion_id, role, games, wins in sorted(totals, key=lambda row: -row[2]):
        if games < min_games:
            continue
        base, code = divmod(int(champion_id), 10)
        variant = {"name": VARIANT_NAMES[code], "games": int(games), "wins": int(wins)}
        variant.update(variant_builds.get(f"{role}:{champion_id}", {}))
        builds.setdefault(f"{role}:{base}", {}).setdefault("variants", []).append(variant)
    for build in builds.values():
        if len(build.get("variants", [])) < 2:
            build.pop("variants", None)


def write_export(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=0, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8")
