#!/bin/sh
# Dauerschleife des Crawlers auf dem Server: crawlen → aufräumen → exportieren → ins Release hochladen → von vorn.
# Die Datenbank bleibt im Volume /data liegen; nur beim allerersten Start wird sie aus dem Release aufgebaut.
# Einstellungen kommen aus deploy/.env (RIOT_API_KEY, RIOT_PLATFORM, GITHUB_TOKEN, GITHUB_REPO, …).
set -u

DB=/data/stats.duckdb
DIST=/data/dist
CRAWL_MINUTES="${CRAWL_MINUTES:-90}"
PAUSE_SECONDS="${PAUSE_SECONDS:-30}"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] $*"; }

if [ ! -f "$DB" ]; then
  log "Erster Start: Datenbank aus dem Release aufbauen"
  rm -rf /data/snapshot
  # Ohne die alten Daten nicht weitermachen: sonst würde der erste Upload das Release mit fast nichts überschreiben.
  if ! python -m leaguestats.release download /data/snapshot; then
    log "Release nicht erreichbar, neuer Versuch in 5 Minuten"
    sleep 300
    exit 1
  fi
  if ! python -m leaguestats --db "$DB.neu" restore /data/snapshot; then
    log "Wiederherstellen fehlgeschlagen"
    rm -f "$DB.neu"
    exit 1
  fi
  mv "$DB.neu" "$DB"
fi

while true; do
  log "Crawl startet ($CRAWL_MINUTES min, Server: ${RIOT_PLATFORM:-euw1})"
  python -m leaguestats --db "$DB" crawl --timelines --matches 1000000 --no-export --max-minutes "$CRAWL_MINUTES" \
    || log "Crawl mit Fehler beendet (Key abgelaufen? Riot down?), Daten werden trotzdem hochgeladen"

  log "Aufräumen und exportieren"
  python -m leaguestats --db "$DB" prune --keep-patches 3
  rm -rf "$DIST" && mkdir -p "$DIST"
  if python -m leaguestats --db "$DB" export --out "$DIST/matchups.json" \
    && python -m leaguestats --db "$DB" snapshot "$DIST/snapshot"; then
    python -m leaguestats --db "$DB" status
    if python -m leaguestats.release upload "$DIST"/snapshot/*.parquet "$DIST/matchups.json"; then
      log "Hochgeladen"
    else
      log "Hochladen fehlgeschlagen, nächster Versuch nach dem nächsten Crawl"
    fi
  else
    log "Export fehlgeschlagen"
  fi
  sleep "$PAUSE_SECONDS"
done
