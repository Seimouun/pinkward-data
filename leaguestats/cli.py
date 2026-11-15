"""Kommandozeile:  python -m leaguestats {crawl,export,prune,status,snapshot,restore}"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from .config import DEFAULT_DB, DEFAULT_EXPORT, load_settings
from .crawl import crawl, parse_tiers
from .export import DEFAULT_MIN_MINUTES, build_export, write_export
from .riot_api import RiotApi, RiotApiError
from .store import Store

# Engpass ist das App-Limit des Development/Personal Keys: 100 Anfragen pro 2 Minuten.
REQUESTS_PER_SECOND = 100 / 120


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="leaguestats", description="Matchup-Statistiken aus der Riot API für LeagueAnalyser"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"DuckDB-Datei (Standard: {DEFAULT_DB})")
    commands = parser.add_subparsers(dest="command", required=True)

    crawl_cmd = commands.add_parser("crawl", help="Ranked-Matches von Spielern aus der Rangliste laden")
    crawl_cmd.add_argument("--matches", type=int, default=1000, help="neue Matches, die geladen werden (Standard: 1000)")
    crawl_cmd.add_argument("--days", type=int, default=14, help="nur Matches der letzten N Tage (Standard: 14)")
    crawl_cmd.add_argument(
        "--tiers",
        default="challenger,grandmaster,master",
        help="kommagetrennt, z. B. master,diamond (Standard: challenger,grandmaster,master)",
    )
    crawl_cmd.add_argument(
        "--timelines", action="store_true", help="auch Gold bei Minute 15 laden (doppelt so viele Anfragen)"
    )
    crawl_cmd.add_argument(
        "--export-every",
        type=int,
        default=500,
        help="alle N neuen Matches und am Ende nach app/src/data/matchups.json exportieren; 0 = nur am Ende (Standard: 500)",
    )
    crawl_cmd.add_argument("--no-export", action="store_true", help="gar nicht automatisch exportieren")
    crawl_cmd.add_argument(
        "--max-minutes", type=float, default=None, help="spätestens nach N Minuten aufhören (z. B. für GitHub Actions)"
    )

    prune_cmd = commands.add_parser("prune", help="Matches älterer Patches aus der Datenbank löschen")
    prune_cmd.add_argument("--keep-patches", type=int, default=3, help="die neuesten N Patches behalten (Standard: 3)")

    export_cmd = commands.add_parser("export", help="Matchups für die App nach app/src/data/matchups.json schreiben")
    export_cmd.add_argument("--patches", type=int, default=2, help="die neuesten N Patches einbeziehen (Standard: 2)")
    export_cmd.add_argument("--min-games", type=int, default=20, help="Matchups mit weniger Spielen weglassen (Standard: 20)")
    export_cmd.add_argument(
        "--min-minutes",
        type=int,
        default=DEFAULT_MIN_MINUTES,
        help=f"kürzere Spiele nicht mitzählen (Standard: {DEFAULT_MIN_MINUTES})",
    )
    export_cmd.add_argument("--out", type=Path, default=DEFAULT_EXPORT)

    commands.add_parser("status", help="zeigen, was in der Datenbank liegt")

    snapshot_cmd = commands.add_parser("snapshot", help="alle Tabellen als Parquet-Dateien in einen Ordner schreiben")
    snapshot_cmd.add_argument("directory", type=Path)
    restore_cmd = commands.add_parser("restore", help="eine leere Datenbank aus Parquet-Dateien befüllen")
    restore_cmd.add_argument("directory", type=Path)

    args = parser.parse_args(argv)
    if args.command == "crawl":
        return run_crawl(args)
    if args.command == "export":
        return run_export(args)
    if args.command == "prune":
        return run_prune(args)
    if args.command == "snapshot":
        return run_snapshot(args)
    if args.command == "restore":
        return run_restore(args)
    return run_status(args)


def run_snapshot(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        files = store.snapshot(args.directory)
    finally:
        store.close()
    total = sum(path.stat().st_size for path in files)
    largest = max(files, key=lambda path: path.stat().st_size)
    print(f"Snapshot: {len(files)} Dateien, zusammen {total / 1e6:.1f} MB, größte {largest.name} "
          f"{largest.stat().st_size / 1e6:.1f} MB → {args.directory}")
    return 0


def run_restore(args: argparse.Namespace) -> int:
    if not any(args.directory.glob("*.parquet")):
        print(f"Keine Parquet-Dateien in {args.directory}; Datenbank bleibt leer.")
        return 0
    store = Store(args.db)
    try:
        matches = store.restore(args.directory)
    except ValueError as err:
        print(err)
        return 1
    finally:
        store.close()
    print(f"Wiederhergestellt: {matches} Matches aus {args.directory}")
    return 0


def run_crawl(args: argparse.Namespace) -> int:
    try:
        tiers = parse_tiers(args.tiers)
    except ValueError as err:
        print(err)
        return 2
    settings = load_settings()
    api = RiotApi(settings.api_key, settings.platform, settings.region)

    per_match = 2 if args.timelines else 1
    minutes = args.matches * per_match * 1.1 / REQUESTS_PER_SECOND / 60
    if args.max_minutes is not None:
        minutes = min(minutes, args.max_minutes)
    print(f"Server {settings.platform}: lade bis zu {args.matches} Matches, höchstens etwa {minutes:.0f} Minuten.")
    deadline = None if args.max_minutes is None else time.monotonic() + args.max_minutes * 60
    print("Abbrechen mit Strg+C ist jederzeit möglich; bereits geladene Matches bleiben gespeichert.")

    store = Store(args.db)
    auto_export = not args.no_export

    def on_saved(saved: int) -> None:
        if auto_export and args.export_every > 0 and saved % args.export_every == 0:
            export_to_app(store, DEFAULT_EXPORT)

    exit_code = 0
    result = None
    try:
        result = crawl(
            api,
            store,
            tiers=tiers,
            max_matches=args.matches,
            days=args.days,
            timelines=args.timelines,
            on_saved=on_saved,
            should_stop=lambda: deadline is not None and time.monotonic() >= deadline,
        )
    except KeyboardInterrupt:
        print("\nAbgebrochen. Bisher geladene Matches sind gespeichert.")
        exit_code = 130
    except RiotApiError as err:
        print(f"\n{err}")
        exit_code = 1

    try:
        if result is not None:
            print(
                f"Fertig: {result.saved} neue Matches, {result.skipped} übersprungen "
                f"(Remakes u. Ä.), {result.players_visited} von {result.players} Spielern abgefragt, "
                f"{api.requests} API-Anfragen."
            )
        if auto_export:
            export_to_app(store, DEFAULT_EXPORT)
    finally:
        store.close()
    return exit_code


def export_to_app(
    store: Store, out: Path, *, patches: int = 2, min_games: int = 20, min_minutes: int = DEFAULT_MIN_MINUTES
) -> bool:
    data = build_export(store, patches=patches, min_games=min_games, min_minutes=min_minutes)
    if not data["patches"]:
        print("Keine Matches in der Datenbank. Erst  python -m leaguestats crawl  ausführen.")
        return False
    write_export(data, out)
    print(
        f"Export: {len(data['matchups'])} Matchups (mind. {min_games} Spiele) aus {data['matches']} Matches "
        f"der Patches {', '.join(data['patches'])} → {out}"
    )
    return True


def run_export(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        ok = export_to_app(
            store, args.out, patches=args.patches, min_games=args.min_games, min_minutes=args.min_minutes
        )
    finally:
        store.close()
    return 0 if ok else 1


def run_prune(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        dropped = store.prune(args.keep_patches)
    finally:
        store.close()
    print(f"Gelöscht: Patches {', '.join(dropped)}" if dropped else "Nichts zu löschen.")
    return 0


def run_status(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"Noch keine Datenbank unter {args.db}.")
        return 0
    store = Store(args.db)
    try:
        summary = store.patch_summary()
        skipped = store.skipped_count()
    finally:
        store.close()
    if not summary:
        print("Datenbank ist leer.")
    for patch, matches, with_timeline in summary:
        print(f"Patch {patch}: {matches} Matches ({with_timeline} mit Gold bei Minute 15)")
    print(f"Übersprungen: {skipped}")
    return 0
