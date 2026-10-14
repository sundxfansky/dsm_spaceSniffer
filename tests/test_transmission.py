from datetime import UTC, datetime, timedelta

from app.models import AnalysisRequest, TransmissionServer
from app.synology import cleanup_candidates
from app.transmission import find_candidates


def test_transmission_candidates_require_old_small_single_file_and_television_folder(monkeypatch):
    old = int((datetime.now(UTC) - timedelta(days=31)).timestamp())
    server = TransmissionServer(name="客厅", address="tr.local", mount_path="/downloads", local_path="/volume1/downloads")

    class Client:
        def __init__(self, _server):
            self.url = "http://tr.local:9091/transmission/rpc"

        def torrents(self):
            return [
                {"id": 1, "name": "old", "downloadDir": "/downloads", "doneDate": old, "percentDone": 1,
                 "files": [{"name": "old.mkv", "length": 100}]},
                {"id": 2, "name": "large", "downloadDir": "/downloads", "doneDate": old, "percentDone": 1,
                 "files": [{"name": "large.mkv", "length": 2_000_000}]},
                {"id": 3, "name": "folder", "downloadDir": "/downloads", "doneDate": old, "percentDone": 1,
                 "files": [{"name": "dir/a.mkv", "length": 100}, {"name": "dir/b.srt", "length": 20}]},
            ]

    monkeypatch.setattr("app.transmission.TransmissionClient", Client)
    groups = [{"inode": "9", "link_count": 2, "size_bytes": 100,
               "paths": ["/volume1/downloads/old.mkv", "/volume1/电视剧/old.mkv"]}]
    _, candidates, warnings = find_candidates([server], groups, max_file_mb=1, age_days=30)
    assert not warnings
    assert len(candidates) == 1
    assert candidates[0]["dsm_path"] == "/volume1/downloads/old.mkv"


def test_cleanup_rejects_paths_outside_selected_volumes():
    request = AnalysisRequest(address="nas", username="u", password="p", volumes=["/volume1"],
                              transmission_servers=[TransmissionServer(address="tr", mount_path="/d", local_path="/volume1/d")])
    result = {"transmission_candidates": [{"id": "x", "dsm_path": "/etc/passwd", "hardlink_paths": ["/etc/passwd"],
                                           "server_index": 0, "task_id": 1, "task_name": "x"}]}
    try:
        cleanup_candidates(request, result, ["x"])
    except ValueError as exc:
        assert "存储空间" in str(exc)
    else:
        raise AssertionError("unsafe path must be rejected")
