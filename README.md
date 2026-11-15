# pinkward-data

Matchup-Statistiken für **Pinkward**, einen persönlichen Champ-Select-Assistenten für League of Legends.

Ein GitHub-Actions-Workflow lädt alle drei Stunden Ranked-Solo-Matches samt Timeline von Spielern ab Master über die Riot API und veröffentlicht im Release `data`:

- `matchups.json`: das, was die App lädt (Matchup-Winrates, Lane-Differenzen, Rollenverteilung)
- `*.parquet`: die Rohdaten, eine Datei je Tabelle; Grundlage für den nächsten Lauf und für spätere Auswertungen

## Was gespeichert wird

Bewusst breit, damit spätere Auswertungen nicht an fehlenden Daten scheitern, aber **ohne Spieleridentität**: keine Namen, keine PUUIDs, keine Summoner-IDs. Spieler erscheinen nur als Team und Rolle innerhalb eines Matches.

| Tabelle | Inhalt |
|---|---|
| `matches` | Patch, Server, Dauer, Startzeit, Aufgabe/Surrender, Tier, in dem das Match gefunden wurde, Parser-Version |
| `participants` | Champion, Rolle, Sieg; Rollenvergabe (Wunschrolle/Autofill); Summoner Spells, Runen, Items; KDA, Gold, CS, Schaden nach Art, Tanken, Heilung/Schilde, CC, Vision, Türme; Riots Lane-Kennzahlen (Gold/XP-Vorsprung in der Lane-Phase, max. CS- und Level-Vorsprung, Solo-Kills, Platings, CS nach 10 Minuten) |
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
  "matchups": { "top:86:122": { "games": 80, "wins": 47, "goldDiff10": 85, "goldDiff15": 143, "xpDiff15": 60, "csDiff15": 4 } },
  "roles": { "86": { "top": 1200, "middle": 12 } }
}
```

Schlüssel `rolle:a:b` mit Champion-ID `a < b`; Siege und Differenzen aus Sicht von `a`. Die Lane-Differenzen erscheinen nur, wenn genug Spiele des Matchups eine Timeline haben.

## Einrichten

1. Unter *Settings → Secrets and variables → Actions* das Secret `RIOT_API_KEY` anlegen (Key von <https://developer.riotgames.com>).
2. Optional die Variable `RIOT_PLATFORM` setzen (Standard `euw1`).
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

Weitere Befehle: `status`, `prune --keep-patches 3`, `snapshot ORDNER` (alle Tabellen als Parquet) und `restore ORDNER` (leere Datenbank aus Parquet befüllen; so baut der Workflow seine Arbeitsdatenbank).

Den Key dafür in `.env` eintragen (Vorlage: `.env.example`).

---

Pinkward isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
