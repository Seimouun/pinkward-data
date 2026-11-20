from leaguestats.release import Release


class FakeHttp:
    """Merkt sich die Aufrufe und spielt ein Release mit zwei Dateien vor."""

    def __init__(self):
        self.calls = []
        self.assets = [
            {"id": 1, "name": "matchups.json"},
            {"id": 2, "name": "matches.parquet"},
            {"id": 3, "name": "matches.parquet.neu"},  # Rest eines abgebrochenen Laufs
        ]

    def json(self, method, url, body=None):
        self.calls.append((method, url.split("/repos/x/y")[-1], body))
        if method == "GET":
            return {"id": 99, "assets": self.assets}
        return None

    def upload(self, url, data, content_type):
        self.calls.append(("UPLOAD", url.split("/repos/x/y")[-1], content_type))
        return {"id": 100 + len(self.calls)}

    def download(self, url):
        self.calls.append(("DOWNLOAD", url.split("/repos/x/y")[-1], None))
        return b"parquet"


def test_upload_replaces_files_without_a_gap(tmp_path):
    http = FakeHttp()
    (tmp_path / "matches.parquet").write_bytes(b"neu")
    (tmp_path / "matchups.json").write_text("{}")
    Release(http, "x/y", "data").upload([tmp_path / "matches.parquet", tmp_path / "matchups.json"])

    steps = [(method, path) for method, path, _ in http.calls]
    assert steps == [
        ("GET", "/releases/tags/data"),
        ("DELETE", "/releases/assets/3"),  # alter Rest weg
        ("UPLOAD", "/releases/99/assets?name=matches.parquet.neu"),
        ("DELETE", "/releases/assets/2"),  # erst danach die alte Datei löschen …
        ("PATCH", "/releases/assets/103"),  # … und die neue umbenennen (Id aus FakeHttp.upload)
        ("UPLOAD", "/releases/99/assets?name=matchups.json.neu"),
        ("DELETE", "/releases/assets/1"),
        ("PATCH", "/releases/assets/106"),
    ]
    renames = [body for method, _, body in http.calls if method == "PATCH"]
    assert renames == [{"name": "matches.parquet"}, {"name": "matchups.json"}]
    assert ("UPLOAD", "/releases/99/assets?name=matchups.json.neu", "application/json") in http.calls


def test_download_fetches_only_parquet_files(tmp_path):
    http = FakeHttp()
    files = Release(http, "x/y", "data").download(tmp_path / "snap")
    assert [f.name for f in files] == ["matches.parquet"]
    assert (tmp_path / "snap" / "matches.parquet").read_bytes() == b"parquet"
