"""Baut Match-V5-Antworten im Format der Riot API für Tests (Feldnamen wie in echten Antworten)."""

from __future__ import annotations

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def make_match(
    match_id: str = "EUW1_1",
    *,
    blue: tuple[int, ...] = (1, 2, 3, 4, 5),
    red: tuple[int, ...] = (6, 7, 8, 9, 10),
    blue_wins: bool = True,
    version: str = "16.22.712.3456",
    duration: int = 1800,
    positions: tuple[str, ...] = POSITIONS,
    remake: bool = False,
) -> dict:
    participants = []
    for team_id, champions, win, offset in ((100, blue, blue_wins, 0), (200, red, not blue_wins, 5)):
        for index, (champion_id, position) in enumerate(zip(champions, positions)):
            participants.append(
                {
                    "participantId": offset + index + 1,
                    # Identitätsfelder wie in echten Antworten; der Parser darf sie nie übernehmen.
                    "puuid": f"puuid-{offset + index}",
                    "riotIdGameName": f"Spieler{offset + index}",
                    "riotIdTagline": "EUW",
                    "teamId": team_id,
                    "teamPosition": position,
                    "positionAssignedByMatchmaking": position,
                    "selectedRolePreferences": f"{position}.PRIMARY.{position}.MIDDLE",
                    "championId": champion_id,
                    "win": win,
                    "gameEndedInEarlySurrender": remake,
                    "gameEndedInSurrender": False,
                    "summoner1Id": 4,
                    "summoner2Id": 12,
                    "perks": {
                        "statPerks": {"defense": 5001, "flex": 5008, "offense": 5005},
                        "styles": [
                            {
                                "description": "primaryStyle",
                                "style": 8000,
                                "selections": [{"perk": 8010}, {"perk": 9111}, {"perk": 9104}, {"perk": 8299}],
                            },
                            {"description": "subStyle", "style": 8400, "selections": [{"perk": 8444}, {"perk": 8242}]},
                        ],
                    },
                    **{f"item{i}": 3000 + i for i in range(6)},
                    "item6": 3340,
                    # Bot-Laner: Stiefel im eigenen Slot (Rollen-Quest), sonst ein Quest-Platzhalter
                    "roleBoundItem": 3006 if position == "BOTTOM" else 1220,
                    "champLevel": 16,
                    "kills": 5,
                    "deaths": 3,
                    "assists": 7,
                    "goldEarned": 12000,
                    "totalMinionsKilled": 200,
                    "neutralMinionsKilled": 12,
                    "totalDamageDealtToChampions": 20000,
                    "firstBloodKill": offset + index == 0,
                    "firstTowerAssist": offset + index == 2,
                    "visionScore": 25,
                    "challenges": {
                        "soloKills": 1,
                        "turretPlatesTaken": 2,
                        "killParticipation": 0.5,
                        "laningPhaseGoldExpAdvantage": 1 if team_id == 100 else 0,
                        "maxCsAdvantageOnLaneOpponent": 13.000000029,
                        "playedChampSelectPosition": 1,
                    },
                }
            )
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameVersion": version,
            "gameDuration": duration,
            "gameStartTimestamp": 1794600000000,
            "queueId": 420,
            "platformId": "EUW1",
            "endOfGameResult": "GameComplete",
            "participants": participants,
            "teams": [
                {
                    "teamId": 100,
                    "win": blue_wins,
                    "bans": [{"championId": 157, "pickTurn": 1}, {"championId": -1, "pickTurn": 2}],
                    "objectives": {"dragon": {"first": True, "kills": 3}, "voidGator": {"first": False, "kills": 0}},
                },
                {
                    "teamId": 200,
                    "win": not blue_wins,
                    "bans": [{"championId": 238, "pickTurn": 6}],
                    "objectives": {"dragon": {"first": False, "kills": 1}, "voidGator": {"first": True, "kills": 1}},
                },
            ],
        },
    }


def make_timeline(gold_by_participant: dict[int, int], minute: int = 15) -> dict:
    """Timeline, deren einziger gefüllter Frame die gegebene Minute ist."""
    frames = [{"participantFrames": {}, "events": []} for _ in range(minute)]
    frames.append(
        {
            "participantFrames": {
                str(pid): {"totalGold": gold, "xp": 0, "level": 1, "minionsKilled": 0, "jungleMinionsKilled": 0}
                for pid, gold in gold_by_participant.items()
            },
            "events": [],
        }
    )
    return {"info": {"frameInterval": 60000, "frames": frames}}


def make_full_timeline(minutes: int = 31, *, gold_lead: dict[int, int] | None = None, events: list[dict] | None = None) -> dict:
    """Frames für alle 10 Teilnehmer; `gold_lead` addiert pro participantId Gold je Minute."""
    gold_lead = gold_lead or {}
    frames = []
    for minute in range(minutes):
        participant_frames = {
            str(pid): {
                "participantId": pid,
                "totalGold": 500 + minute * (400 + gold_lead.get(pid, 0)),
                "xp": minute * 300,
                "level": min(18, 1 + minute // 2),
                "minionsKilled": minute * 7,
                "jungleMinionsKilled": minute,
                "damageStats": {"totalDamageDoneToChampions": minute * 500, "totalDamageTaken": minute * 450},
                "position": {"x": 1000, "y": 1000},
            }
            for pid in range(1, 11)
        }
        frames.append({"timestamp": minute * 60000, "participantFrames": participant_frames, "events": []})
    for event in events or []:
        frames[min(len(frames) - 1, event["timestamp"] // 60000)]["events"].append(event)
    return {"info": {"frameInterval": 60000, "frames": frames}}


def kill(at_s: int, killer: int, victim: int, assists: tuple[int, ...] = ()) -> dict:
    return {
        "type": "CHAMPION_KILL",
        "timestamp": at_s * 1000,
        "killerId": killer,
        "victimId": victim,
        "assistingParticipantIds": list(assists),
        "position": {"x": 5000, "y": 6000},
        "bounty": 300,
        "shutdownBounty": 0,
    }


def buy(at_s: int, participant: int, item: int) -> dict:
    return {"type": "ITEM_PURCHASED", "timestamp": at_s * 1000, "participantId": participant, "itemId": item}


def skill_ups(participant: int, order: str, *, start_s: int = 60, every_s: int = 90) -> list[dict]:
    """SKILL_LEVEL_UP-Ereignisse in der gegebenen Reihenfolge, z. B. "QWEQQRQ…"."""
    slots = {"Q": 1, "W": 2, "E": 3, "R": 4}
    return [
        {
            "type": "SKILL_LEVEL_UP",
            "timestamp": (start_s + index * every_s) * 1000,
            "participantId": participant,
            "skillSlot": slots[letter],
            "levelUpType": "NORMAL",
        }
        for index, letter in enumerate(order)
    ]
