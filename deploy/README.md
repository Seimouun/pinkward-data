# Dauer-Crawler auf dem Server

Der Crawler läuft auf dem eigenen Server (netcup VPS) rund um die Uhr in Docker: crawlen (90 min) → aufräumen →
exportieren → ins Release `data` hochladen → von vorn. Die App lädt `matchups.json` weiter von derselben
GitHub-Adresse, für sie ändert sich nichts. Die Datenbank bleibt im Docker-Volume `crawler-data` liegen; nur beim
allerersten Start wird sie aus dem Release aufgebaut.

Passwörter, Riot-Key und GitHub-Token gibst du nur auf dem Server selbst ein. Sie gehören in keinen Chat und in
keine Datei im Repo.

## 1. SSH-Schlüssel am Mac

```bash
ssh-keygen -t ed25519 -C "pinkward-server"
```

Enter für den Standardpfad, dann eine Passphrase (z. B. in der Passwörter-App speichern). Den Schlüssel auf den
Server kopieren; dabei fragt der Server einmal nach dem Root-Passwort aus der netcup-Mail:

```bash
ssh-copy-id root@SERVER-IP
```

## 2. Grundeinrichtung (als root)

```bash
ssh root@SERVER-IP
apt update && apt upgrade -y
apt install -y ufw unattended-upgrades git curl
```

Eigener Benutzer statt root (das Passwort brauchst du nur für `sudo`):

```bash
adduser pinkward
usermod -aG sudo pinkward
mkdir -p /home/pinkward/.ssh
cp /root/.ssh/authorized_keys /home/pinkward/.ssh/
chown -R pinkward:pinkward /home/pinkward/.ssh
chmod 700 /home/pinkward/.ssh && chmod 600 /home/pinkward/.ssh/authorized_keys
```

Firewall: nur SSH und (später, für die API) HTTP/HTTPS:

```bash
ufw allow OpenSSH
ufw allow 80,443/tcp
ufw --force enable
```

Sicherheitsupdates automatisch:

```bash
dpkg-reconfigure -plow unattended-upgrades
```

**Erst in einem zweiten Terminal am Mac prüfen**, dass `ssh pinkward@SERVER-IP` klappt. Danach Login mit Passwort und
als root abschalten:

```bash
printf 'PasswordAuthentication no\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/90-pinkward.conf
systemctl reload ssh
```

## 3. Docker

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker pinkward
```

Ab hier als `pinkward` arbeiten (`exit`, dann `ssh pinkward@SERVER-IP`).

## 4. GitHub-Token

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token:

- Repository access: Only select repositories → `pinkward-data`
- Permissions: Repository → Contents → **Read and write**
- Ablaufdatum notieren; vor Ablauf einen neuen Token in `deploy/.env` eintragen

## 5. GitHub-Crawl abschalten

In pinkward-data → Settings → Secrets and variables → Actions → Variables: `CRAWL_ON_GITHUB` = `false`.
Läuft gerade „Matches sammeln“, das Ende abwarten. Sonst laden beide Crawler hoch und überschreiben sich.
Manuell starten lässt sich der Workflow weiterhin (Notfall, wenn der Server ausfällt).

## 6. Crawler starten

```bash
git clone https://github.com/Seimouun/pinkward-data.git
cd pinkward-data
cp deploy/env.example deploy/.env
nano deploy/.env        # RIOT_API_KEY und GITHUB_TOKEN eintragen
chmod 600 deploy/.env
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

Im Log steht zuerst „Erster Start: Datenbank aus dem Release aufbauen“, dann laufen die Zeilen des Crawls mit
`[euw1]` und `[na1]` live mit. Nach 90 Minuten folgt „Hochgeladen“. `Strg+C` beendet nur die Anzeige, der Crawler
läuft weiter.

## Alltag

```bash
cd ~/pinkward-data
docker compose -f deploy/docker-compose.yml logs --tail 50     # was gerade passiert
docker compose -f deploy/docker-compose.yml exec crawler python -m leaguestats --db /data/stats.duckdb status
git pull && docker compose -f deploy/docker-compose.yml up -d --build   # neue Version des Crawlers
docker compose -f deploy/docker-compose.yml restart             # nach Änderungen an deploy/.env
```

- Neuer Riot-Key (z. B. Production Key): in `deploy/.env` ersetzen, dann `restart`.
- Snapshots des Servers im netcup Server Control Panel anlegen, vor allem vor größeren Änderungen.
- Fällt der Server aus: `CRAWL_ON_GITHUB` wieder auf `true` setzen, dann crawlt GitHub wie früher.
