# pinkward-data

Matchup-Statistiken für **Pinkward**, einen persönlichen Champ-Select-Assistenten für League of Legends.

Ein GitHub-Actions-Workflow lädt alle drei Stunden Ranked-Solo-Matches samt Timeline von Spielern ab Master über die Riot API und veröffentlicht im Release `data`:

- `matchups.json`: das, was die App lädt (Matchup-Winrates, Lane-Differenzen, Rollenverteilung)
- `*.parquet`: die Rohdaten, eine Datei je Tabelle; Grundlage für den nächsten Lauf und für spätere Auswertungen

## Was gespeichert wird

Bewusst breit, damit spätere Auswertungen nicht an fehlenden Daten scheitern, aber **ohne Spieleridentität**: keine Namen, keine PUUIDs, keine Summoner-IDs. Spieler erscheinen nur als Team und Rolle innerhalb eines Matches.

| Tabelle | Inhalt |
|---|---|
| `matches` | Patch, Server (`platform`, z. B. `EUW1`, `NA1`; fehlt er, kommt er aus der Match-ID), Dauer, Startzeit, Aufgabe/Surrender, Tier, in dem das Match gefunden wurde, Parser-Version |
| `participants` | Champion (bei Kayn mit Form), Rolle, Sieg; Rollenvergabe (Wunschrolle/Autofill); Tier und LP aus der Rangliste (leer bei Master mit wenig LP oder darunter); Summoner Spells, Runen, Items; KDA, Gold, CS, Schaden nach Art, Tanken, Heilung/Schilde, CC, Vision, Türme; Riots Lane-Kennzahlen (Gold/XP-Vorsprung in der Lane-Phase, max. CS- und Level-Vorsprung, Solo-Kills, Platings, CS nach 10 Minuten) |
| `participant_frames` | Gold, XP, Level, CS, Schaden ausgeteilt/erhalten zu Minute 5, 10, 15, 20, 25, 30 |
| `kills` | alle Kills der ersten 20 Minuten: Zeit, Killer, Opfer, Assists (je Team und Rolle), Position, Bounty |
| `objective_events` | Drachen (mit Element), Grubs, Herald, Baron, weitere Monster, Türme, Inhibitoren, Platings: Zeit, Team, Lane |
| `team_objectives` | Riots Zusammenfassung je Team und Objective (erstes, Anzahl) |
| `bans` | Bans je Team |

Neue Felder kommen über eine neue Parser-Version und eine Migration hinzu. Alte Matches behalten dann leere Werte; weil die Match-IDs gespeichert sind, lassen sie sich bei Bedarf nachladen (Riot hält Matches etwa zwei Jahre vor).

Platzbedarf: etwa 2,5 KB je Match als Parquet; bei drei behaltenen Patches (rund sechs Wochen) etwa 1 GB.

## Format von matchups.json

```json
{
  "patches": ["16.22", "16.21"],
  "generatedAt": "2026-11-15T12:00:00+00:00",
  "matches": 12345,
  "minGames": 20,
  "minMinutes": 15,
  "matchups": { "top:86:122": { "games": 80, "wins": 47, "goldDiff10": 85, "goldDiff15": 143, "xpDiff15": 60, "csDiff15": 4 } },
  "roles": { "86": { "top": 1200, "middle": 12 } },
  "roleWins": { "86": { "top": 618, "middle": 5 } },
  "builds": {
    "top:86": {
      "runes": { "primaryStyle": 8000, "subStyle": 8400, "perks": [8010, 9111, 9104, 8299, 8242, 8444], "statPerks": [5005, 5008, 5001], "games": 310, "wins": 162 },
      "spells": { "ids": [4, 12], "games": 900, "wins": 470 },
      "items": [[3078, 850, 450, 14.2], [3047, 700, 370, 9.8]],
      "starters": { "ids": [1055, 2003], "games": 640, "wins": 330 },
      "skills": { "max": "QEW", "start": "QEW", "games": 410, "wins": 214 }
    }
  },
  "matchupItems": { "top:86:122": [[3071, 60, 33], [3047, 55, 30]] },
  "patchStats": {
    "16.23": {
      "matches": 9800,
      "champions": { "86": { "top": [410, 212] } },
      "bans": { "86": 120 },
      "duos": { "22:412": [95, 51] }
    }
  }
}
```

Spiele unter 15 Minuten (Remakes, AFK, vorzeitige Aufgabe) zählen im Export nicht mit; gespeichert bleiben sie. Schlüssel `rolle:a:b` mit Champion-ID `a < b`; Siege und Differenzen aus Sicht von `a`. Die Lane-Differenzen erscheinen nur, wenn genug Spiele des Matchups eine Timeline haben. `builds` enthält je Champion und Rolle die häufigste Runenseite (Nebenrunen sortiert, Werte-Runen getrennt gezählt) und das häufigste Spell-Paar (ohne Reihenfolge), jeweils ab `minGames` Spielen. `items` sind die häufigsten Items im End-Inventar als `[itemId, Spiele, Siege, Kaufminute]`; die mittlere Minute des ersten Kaufs und die Start-Items (`starters`, alles aus den ersten 90 Sekunden) gibt es nur für Matches mit Kauf-Daten (ab Parser-Version 4). `skills` ist die häufigste Max-Reihenfolge der Grundfähigkeiten (`max`, z. B. `"QEW"`: zuerst wer als Erstes 5 Punkte hat) und die häufigsten drei ersten Punkte (`start`), aus den `SKILL_LEVEL_UP`-Ereignissen der Timeline (ab Parser-Version 5, füllt sich mit neuen Spielen). `matchupItems` zeigt dasselbe gegen einen bestimmten Lane-Gegner (`rolle:champion:gegner`, beide Richtungen). `patchStats` (seit Export v3) hat dieselben Patches noch einmal einzeln, damit die App nach Patch filtern kann: Spiele und Siege je Champion und Rolle, Bans (Spiele, in denen der Champion gebannt war) und Bot-Lane-Duos (Bot und Support im selben Team, je Patch ab 3 Spielen).

## Auf dem eigenen Server

Seit November 2026 crawlt ein Docker-Container auf dem eigenen Server rund um die Uhr und lädt alle 90 Minuten ins
Release `data` hoch. Einrichtung und Alltag: [deploy/README.md](deploy/README.md). Solange der Server läuft, steht die
Repo-Variable `CRAWL_ON_GITHUB` auf `false`, und der Workflow unten crawlt nur noch, wenn man ihn von Hand startet.

## Einrichten

1. Unter *Settings → Secrets and variables → Actions* das Secret `RIOT_API_KEY` anlegen (Key von <https://developer.riotgames.com>).
2. Optional die Variable `RIOT_PLATFORM` setzen (Standard `euw1`). Mehrere Server kommagetrennt, z. B. `euw1,na1`:
   Sie crawlen parallel, jeder mit dem Rate-Limit seiner Region. Je Region nur ein Server, denn EUW und EUNE teilen
   sich `europe` und damit das Limit.
3. Unter *Actions* den Workflow „Matches sammeln“ einmal von Hand starten. Danach läuft er alle drei Stunden.

Ein Development Key läuft nach 24 Stunden ab; für den Dauerbetrieb braucht es einen Personal API Key.

## Lokal

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m leaguestats crawl --matches 500 --timelines
.venv\Scripts\python -m leaguestats export
.venv\Scripts\python -m pytest
```

Mehrere Server auf einmal: `crawl --platforms euw1,na1` (überschreibt `RIOT_PLATFORM`); `--matches` gilt je Server.

Weitere Befehle: `status` (auch Matches je Server), `prune --keep-patches 3`, `snapshot ORDNER` (alle Tabellen als Parquet) und `restore ORDNER` (leere Datenbank aus Parquet befüllen; so baut der Workflow seine Arbeitsdatenbank).

Den Key dafür in `.env` eintragen (Vorlage: `.env.example`).

---

Pinkward isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
