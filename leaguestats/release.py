"""Daten mit dem GitHub-Release austauschen, ohne `gh`: für den Dauer-Crawler auf dem Server (deploy/).

    python -m leaguestats.release download <ordner>      Parquet-Dateien aus dem Release laden
    python -m leaguestats.release upload <datei> …       Dateien ins Release hochladen (ersetzt gleichnamige)

Braucht GITHUB_TOKEN (fine-grained, nur dieses Repo, Contents: Read and write) und GITHUB_REPO
(z. B. Seimouun/pinkward-data); RELEASE_TAG ist standardmäßig „data“. Hochladen geht ohne Lücke: Erst kommt die
neue Datei unter einem vorläufigen Namen hoch, dann wird die alte gelöscht und die neue umbenannt. So findet die App
`matchups.json` in jedem Moment.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
TEMP_SUFFIX = ".neu"


class Http(Protocol):
    def json(self, method: str, url: str, body: dict | None = None) -> Any: ...
    def upload(self, url: str, data: bytes, content_type: str) -> Any: ...
    def download(self, url: str) -> bytes: ...


class GitHubHttp:
    def __init__(self, token: str):
        self.token = token

    def _request(self, method: str, url: str, data: bytes | None, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pinkward-data-crawler",
                **headers,
            },
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.read()

    def json(self, method: str, url: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        raw = self._request(method, url, data, {"Content-Type": "application/json"} if data else {})
        return json.loads(raw) if raw else None

    def upload(self, url: str, data: bytes, content_type: str) -> Any:
        return json.loads(self._request("POST", url, data, {"Content-Type": content_type}))

    def download(self, url: str) -> bytes:
        return self._request("GET", url, None, {"Accept": "application/octet-stream"})


class Release:
    def __init__(self, http: Http, repo: str, tag: str):
        self.http, self.repo, self.tag = http, repo, tag

    def info(self) -> dict:
        return self.http.json("GET", f"{API}/repos/{self.repo}/releases/tags/{self.tag}")

    def download(self, directory: Path, suffix: str = ".parquet") -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        saved = []
        for asset in self.info()["assets"]:
            if not asset["name"].endswith(suffix):
                continue
            path = directory / asset["name"]
            path.write_bytes(self.http.download(f"{API}/repos/{self.repo}/releases/assets/{asset['id']}"))
            saved.append(path)
        return saved

    def upload(self, files: list[Path]) -> None:
        release = self.info()
        existing = {asset["name"]: asset["id"] for asset in release["assets"]}
        for path in files:
            temp = path.name + TEMP_SUFFIX
            # Reste eines abgebrochenen Laufs zuerst wegräumen.
            if temp in existing:
                self.http.json("DELETE", f"{API}/repos/{self.repo}/releases/assets/{existing.pop(temp)}")
            content_type = "application/json" if path.suffix == ".json" else "application/octet-stream"
            new = self.http.upload(
                f"{UPLOADS}/repos/{self.repo}/releases/{release['id']}/assets?name={temp}",
                path.read_bytes(),
                content_type,
            )
            if path.name in existing:
                self.http.json("DELETE", f"{API}/repos/{self.repo}/releases/assets/{existing[path.name]}")
            self.http.json("PATCH", f"{API}/repos/{self.repo}/releases/assets/{new['id']}", {"name": path.name})


def from_env() -> Release:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        raise SystemExit("GITHUB_TOKEN und GITHUB_REPO müssen gesetzt sein (siehe deploy/README.md).")
    return Release(GitHubHttp(token), repo, os.environ.get("RELEASE_TAG", "data"))


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "download":
        files = from_env().download(Path(argv[1]))
        print(f"{len(files)} Dateien aus dem Release geladen")
        return 0
    if len(argv) >= 2 and argv[0] == "upload":
        files = [Path(p) for p in argv[1:]]
        try:
            from_env().upload(files)
        except urllib.error.HTTPError as err:
            print(f"Hochladen fehlgeschlagen: HTTP {err.code}", file=sys.stderr)
            return 1
        print(f"{len(files)} Dateien ins Release hochgeladen")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
