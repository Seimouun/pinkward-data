"""Match-V5-Antworten (Match + Timeline) in Tabellenzeilen übersetzen.

Gespeichert wird bewusst breit, was später für Analysen nützlich sein kann, aber nichts, was einen
Spieler identifiziert: keine Namen, keine PUUIDs, keine Summoner-IDs. Spieler erscheinen nur als
(Team, Rolle) innerhalb eines Matches.

Wird hier etwas ergänzt: PARSER_VERSION erhöhen und in store.py eine Migration für neue Spalten
anlegen. Alte Matches behalten dann NULL in den neuen Spalten; über `matches.parser_version`
lässt sich erkennen, welche Matches welche Felder haben. Fehlendes lässt sich nachladen, weil die
Match-IDs gespeichert sind (Riot hält Matches etwa zwei Jahre vor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PARSER_VERSION = 2

ROLES = ("top", "jungle", "middle", "bottom", "utility")
MIN_DURATION_S = 300
# Minuten, zu denen Gold, XP, CS und Schaden je Spieler festgehalten werden (Lane-Phase bis Midgame).
FRAME_MINUTES = (5, 10, 15, 20, 25, 30)
# Kills nur aus den ersten 20 Minuten: Ganks, Lane-Kills, frühe Skirmishes. Spätere Teamfights
# sagen wenig über Matchups und würden die Datenbank stark aufblähen.
KILL_WINDOW_S = 20 * 60
LANES = {"TOP_LANE": "top", "MID_LANE": "middle", "BOT_LANE": "bottom"}

# Zahlenwerte je Teilnehmer: Spaltenname → Pfad in der Match-V5-Antwort.
# Werte unter "challenges." sind Riots eigene Kennzahlen, darunter Lane-Vergleiche mit dem direkten Gegner.
PARTICIPANT_STATS: dict[str, str] = {
    "champ_level": "champLevel",
    "kills": "kills",
    "deaths": "deaths",
    "assists": "assists",
    "gold_earned": "goldEarned",
    "cs_lane": "totalMinionsKilled",
    "cs_jungle": "neutralMinionsKilled",
    "damage_to_champions": "totalDamageDealtToChampions",
    "physical_to_champions": "physicalDamageDealtToChampions",
    "magic_to_champions": "magicDamageDealtToChampions",
    "true_to_champions": "trueDamageDealtToChampions",
    "damage_taken": "totalDamageTaken",
    "damage_mitigated": "damageSelfMitigated",
    "heal_on_allies": "totalHealsOnTeammates",
    "shield_on_allies": "totalDamageShieldedOnTeammates",
    "cc_score": "timeCCingOthers",
    "cc_time_dealt": "totalTimeCCDealt",
    "damage_to_buildings": "damageDealtToBuildings",
    "damage_to_objectives": "damageDealtToObjectives",
    "damage_to_epic_monsters": "damageDealtToEpicMonsters",
    "vision_score": "visionScore",
    "wards_placed": "wardsPlaced",
    "wards_killed": "wardsKilled",
    "control_wards": "visionWardsBoughtInGame",
    "turret_takedowns": "turretTakedowns",
    "time_dead_s": "totalTimeSpentDead",
    "role_bound_item": "roleBoundItem",
    "immobilizations": "challenges.enemyChampionImmobilizations",
    "turret_plates": "challenges.turretPlatesTaken",
    "solo_kills": "challenges.soloKills",
    "kill_participation": "challenges.killParticipation",
    "team_damage_share": "challenges.teamDamagePercentage",
    "damage_taken_share": "challenges.damageTakenOnTeamPercentage",
    "early_lane_advantage": "challenges.earlyLaningPhaseGoldExpAdvantage",
    "lane_advantage": "challenges.laningPhaseGoldExpAdvantage",
    "max_cs_advantage": "challenges.maxCsAdvantageOnLaneOpponent",
    "max_level_lead": "challenges.maxLevelLeadLaneOpponent",
    "lane_cs_10": "challenges.laneMinionsFirst10Minutes",
    "jungle_cs_10": "challenges.jungleCsBefore10Minutes",
    "vision_advantage": "challenges.visionScoreAdvantageLaneOpponent",
    "takedowns_early": "challenges.takedownsFirstXMinutes",
    "kills_near_enemy_turret": "challenges.killsNearEnemyTurret",
    "kills_under_own_turret": "challenges.killsUnderOwnTurret",
    "skillshots_hit": "challenges.skillshotsHit",
    "skillshots_dodged": "challenges.skillshotsDodged",
    "played_champ_select_position": "challenges.playedChampSelectPosition",
}

# Alle Spalten, die der Parser für participants liefert, mit SQL-Typ (store.py legt sie an).
PARTICIPANT_COLUMNS: dict[str, str] = {
    "match_id": "VARCHAR",
    "team_id": "INTEGER",
    "role": "VARCHAR",
    "champion_id": "INTEGER",
    "win": "BOOLEAN",
    "gold_15": "INTEGER",
    # Rollenvergabe: z. B. "TOP" und "TOP.PRIMARY.TOP.MIDDLE"; zeigt u. a. Autofill.
    "assigned_role": "VARCHAR",
    "role_preferences": "VARCHAR",
    "summoner1": "INTEGER",
    "summoner2": "INTEGER",
    "keystone": "INTEGER",
    "primary_style": "INTEGER",
    "sub_style": "INTEGER",
    "perks": "INTEGER[]",
    "stat_perks": "INTEGER[]",
    "items": "INTEGER[]",
    "first_blood": "BOOLEAN",
    "first_tower": "BOOLEAN",
    **{
        name: ("DOUBLE" if path.startswith("challenges.") else "INTEGER")
        for name, path in PARTICIPANT_STATS.items()
    },
}


class SkipMatch(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ParsedMatch:
    match: dict[str, Any]
    participants: list[dict[str, Any]]
    frames: list[dict[str, Any]] = field(default_factory=list)
    kills: list[dict[str, Any]] = field(default_factory=list)
    objective_events: list[dict[str, Any]] = field(default_factory=list)
    team_objectives: list[dict[str, Any]] = field(default_factory=list)
    bans: list[dict[str, Any]] = field(default_factory=list)


def patch_of(game_version: str) -> str:
    """`"16.22.712.3456"` → `"16.22"`"""
    parts = game_version.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise SkipMatch("version")
    return f"{int(parts[0])}.{int(parts[1])}"


def number_at(obj: Any, path: str) -> int | float | None:
    for key in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    if isinstance(obj, bool):
        return int(obj)
    return obj if isinstance(obj, (int, float)) else None


def parse_match(
    match: dict[str, Any], timeline: dict | None = None, *, seed_tier: str | None = None
) -> ParsedMatch:
    match_id = match["metadata"]["matchId"]
    info = match["info"]
    participants = info.get("participants", [])

    duration = int(info.get("gameDuration", 0))
    if duration < MIN_DURATION_S or any(p.get("gameEndedInEarlySurrender") for p in participants):
        raise SkipMatch("remake")
    if len(participants) != 10:
        raise SkipMatch("participants")

    # participantId (1–10) → (Team, Rolle); damit werden Timeline-Ereignisse zugeordnet.
    seats: dict[int, tuple[int, str]] = {}
    for p in participants:
        role = str(p.get("teamPosition") or "").lower()
        if role not in ROLES:
            raise SkipMatch("positions")
        seats[int(p.get("participantId", 0))] = (int(p["teamId"]), role)
    if len(set(seats.values())) != 10:
        raise SkipMatch("positions")

    frames = timeline_frames(timeline)
    gold_15 = {seat: row["total_gold"] for seat, row in _frame_rows(frames, seats, [15])}

    participant_rows = [_participant_row(match_id, p, gold_15) for p in participants]

    start_ms = info.get("gameStartTimestamp")
    match_row = {
        "match_id": match_id,
        "patch": patch_of(str(info.get("gameVersion", ""))),
        "game_start": (
            datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(tzinfo=None) if start_ms else None
        ),
        "duration_s": duration,
        "queue_id": int(info.get("queueId", 0)),
        "has_timeline": bool(frames),
        "platform": info.get("platformId"),
        "seed_tier": seed_tier,
        "ended_in_surrender": any(p.get("gameEndedInSurrender") for p in participants),
        "end_result": info.get("endOfGameResult"),
        "parser_version": PARSER_VERSION,
    }

    parsed = ParsedMatch(match=match_row, participants=participant_rows)
    parsed.team_objectives, parsed.bans = _team_rows(match_id, info.get("teams", []))
    if frames:
        parsed.frames = [
            {"match_id": match_id, "team_id": team, "role": role, **row}
            for (team, role), row in _frame_rows(frames, seats, FRAME_MINUTES, with_minute=True)
        ]
        parsed.kills, parsed.objective_events = _events(match_id, frames, seats)
    return parsed


def timeline_frames(timeline: dict | None) -> list[dict]:
    if not timeline:
        return []
    return timeline.get("info", {}).get("frames", []) or []


def _participant_row(match_id: str, p: dict, gold_15: dict) -> dict[str, Any]:
    team, role = int(p["teamId"]), str(p["teamPosition"]).lower()
    perks = p.get("perks") or {}
    styles = {style.get("description"): style for style in perks.get("styles", [])}
    primary, sub = styles.get("primaryStyle", {}), styles.get("subStyle", {})
    selections = [s.get("perk") for style in (primary, sub) for s in style.get("selections", [])]
    stat = perks.get("statPerks") or {}
    assigned = p.get("positionAssignedByMatchmaking")

    row: dict[str, Any] = {
        "match_id": match_id,
        "team_id": team,
        "role": role,
        "champion_id": int(p["championId"]),
        "win": bool(p["win"]),
        "gold_15": gold_15.get((team, role)),
        "assigned_role": assigned.lower() if isinstance(assigned, str) and assigned else None,
        "role_preferences": p.get("selectedRolePreferences") or None,
        "summoner1": p.get("summoner1Id"),
        "summoner2": p.get("summoner2Id"),
        "keystone": selections[0] if primary.get("selections") else None,
        "primary_style": primary.get("style"),
        "sub_style": sub.get("style"),
        "perks": [s for s in selections if s is not None] or None,
        "stat_perks": [stat.get(k) for k in ("offense", "flex", "defense")] if stat else None,
        "items": [int(p.get(f"item{i}", 0) or 0) for i in range(7)],
        "first_blood": bool(p.get("firstBloodKill") or p.get("firstBloodAssist")),
        "first_tower": bool(p.get("firstTowerKill") or p.get("firstTowerAssist")),
    }
    for name, path in PARTICIPANT_STATS.items():
        row[name] = number_at(p, path)
    return row


def _frame_rows(frames: list[dict], seats: dict[int, tuple[int, str]], minutes, *, with_minute: bool = False):
    """(Team, Rolle), Zeile je gewünschter Minute; Frames kommen im Minutentakt (Frame 0 = 0:00)."""
    for minute in minutes:
        if len(frames) <= minute:
            continue
        for participant_id, pf in (frames[minute].get("participantFrames") or {}).items():
            seat = seats.get(int(participant_id))
            if seat is None:
                continue
            damage = pf.get("damageStats") or {}
            row = {
                "total_gold": pf.get("totalGold"),
                "xp": pf.get("xp"),
                "level": pf.get("level"),
                "cs": (pf.get("minionsKilled") or 0) + (pf.get("jungleMinionsKilled") or 0),
                "damage_to_champions": damage.get("totalDamageDoneToChampions"),
                "damage_taken": damage.get("totalDamageTaken"),
            }
            if with_minute:
                row = {"minute": minute, **row}
            yield seat, row


def _events(match_id: str, frames: list[dict], seats: dict[int, tuple[int, str]]):
    def seat(participant_id) -> tuple[int | None, str | None]:
        return seats.get(int(participant_id or 0), (None, None))

    kills, objectives = [], []
    for frame in frames:
        for event in frame.get("events") or []:
            kind = event.get("type")
            at_s = int(event.get("timestamp", 0)) // 1000
            position = event.get("position") or {}

            if kind == "CHAMPION_KILL" and at_s <= KILL_WINDOW_S:
                killer_team, killer_role = seat(event.get("killerId"))
                victim_team, victim_role = seat(event.get("victimId"))
                if victim_team is None:
                    continue
                kills.append(
                    {
                        "match_id": match_id,
                        "at_s": at_s,
                        "killer_team": killer_team,
                        "killer_role": killer_role,
                        "victim_team": victim_team,
                        "victim_role": victim_role,
                        "assist_roles": [r for r in (seat(a)[1] for a in event.get("assistingParticipantIds") or []) if r],
                        "x": position.get("x"),
                        "y": position.get("y"),
                        "bounty": event.get("bounty"),
                        "shutdown_bounty": event.get("shutdownBounty"),
                    }
                )
            elif kind == "ELITE_MONSTER_KILL":
                objectives.append(
                    {
                        "match_id": match_id,
                        "at_s": at_s,
                        "kind": event.get("monsterType"),
                        "subtype": event.get("monsterSubType"),
                        "team_id": event.get("killerTeamId"),
                        "lane": None,
                        "killer_role": seat(event.get("killerId"))[1],
                    }
                )
            elif kind in ("BUILDING_KILL", "TURRET_PLATE_DESTROYED"):
                building_team = event.get("teamId")
                if kind == "TURRET_PLATE_DESTROYED":
                    label, subtype = "PLATE", None
                elif event.get("buildingType") == "TOWER_BUILDING":
                    label, subtype = "TOWER", event.get("towerType")
                else:
                    label, subtype = "INHIBITOR", None
                objectives.append(
                    {
                        "match_id": match_id,
                        "at_s": at_s,
                        "kind": label,
                        "subtype": subtype,
                        # Eingetragen wird das Team, das das Gebäude zerstört hat, nicht dessen Besitzer.
                        "team_id": 300 - building_team if building_team in (100, 200) else None,
                        "lane": LANES.get(event.get("laneType")),
                        "killer_role": seat(event.get("killerId"))[1],
                    }
                )
    return kills, objectives


def _team_rows(match_id: str, teams: list[dict]):
    objectives, bans = [], []
    for team in teams:
        team_id = int(team.get("teamId", 0))
        for name, values in (team.get("objectives") or {}).items():
            objectives.append(
                {
                    "match_id": match_id,
                    "team_id": team_id,
                    "objective": name,
                    "is_first": bool(values.get("first")),
                    "kills": values.get("kills"),
                }
            )
        for ban in team.get("bans") or []:
            champion_id = ban.get("championId")
            if isinstance(champion_id, int) and champion_id > 0:
                bans.append(
                    {"match_id": match_id, "team_id": team_id, "pick_turn": ban.get("pickTurn"), "champion_id": champion_id}
                )
    return objectives, bans
