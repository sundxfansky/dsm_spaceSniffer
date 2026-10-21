from datetime import UTC, datetime, timedelta

from app.models import AnalysisRequest, TransmissionServer
from app.synology import cleanup_candidates
from app.transmission import PathLocator, find_candidates, load_torrents


def _locate(torrents, dsm_files, root="/downloads"):
    locator = PathLocator([torrents], [root])
    for path, size in dsm_files:
        locator.add(path, size)
    return locator


def test_transmission_candidates_require_old_small_single_file_and_television_folder():
    old = int((datetime.now(UTC) - timedelta(days=31)).timestamp())
    server = TransmissionServer(name="客厅", address="tr.local")
    torrents = [
        {"id": 1, "name": "old", "downloadDir": "/downloads/tv", "doneDate": old, "percentDone": 1,
         "files": [{"name": "old.mkv", "length": 100}]},
        {"id": 2, "name": "large", "downloadDir": "/downloads/tv", "doneDate": old, "percentDone": 1,
         "files": [{"name": "large.mkv", "length": 2_000_000}]},
        {"id": 3, "name": "folder", "downloadDir": "/downloads/tv", "doneDate": old, "percentDone": 1,
         "files": [{"name": "dir/a.mkv", "length": 100}, {"name": "dir/b.srt", "length": 20}]},
    ]
    locator = _locate(torrents, [
        ("/volume1/downloads/tv/old.mkv", 100),
        ("/volume1/downloads/tv/large.mkv", 2_000_000),
        ("/volume1/电视剧/old.mkv", 100),
    ])
    groups = [{"inode": "9", "link_count": 2, "size_bytes": 100,
               "paths": ["/volume1/downloads/tv/old.mkv", "/volume1/电视剧/old.mkv"]}]
    mappings, candidates, warnings = find_candidates([server], [torrents], locator, groups, max_file_mb=1, age_days=30)
    assert not warnings
    assert mappings == [[{"tr_path": "/downloads/tv", "dsm_path": "/volume1/downloads/tv", "matched_files": 2}]]
    assert len(candidates) == 1
    assert candidates[0]["dsm_path"] == "/volume1/downloads/tv/old.mkv"


def test_each_first_level_download_entry_is_located_independently():
    torrents = [
        {"id": 1, "downloadDir": "/downloads/movies", "files": [{"name": "a.mkv", "length": 10}]},
        {"id": 2, "downloadDir": "/downloads/movies", "files": [{"name": "Show/b.mkv", "length": 20}]},
        {"id": 3, "downloadDir": "/downloads", "files": [{"name": "series/S01/c.mkv", "length": 30}]},
        {"id": 4, "downloadDir": "/downloads", "files": [{"name": "loose.iso", "length": 40}]},
        {"id": 5, "downloadDir": "/other", "files": [{"name": "ignored.mkv", "length": 50}]},
    ]
    locator = _locate(torrents, [
        ("/volume1/video/movies/a.mkv", 10),
        ("/volume1/video/movies/Show/b.mkv", 20),
        ("/volume2/tv-share/series/S01/c.mkv", 30),
        ("/volume1/dl/loose.iso", 40),
        ("/volume1/other/ignored.mkv", 50),
        # Same name, different size: must not vote.
        ("/volume2/backup/movies/a.mkv", 11),
    ])
    located = locator.located(0)
    assert located == {
        "movies": {"dsm_path": "/volume1/video/movies", "matched_files": 2},
        "series": {"dsm_path": "/volume2/tv-share/series", "matched_files": 1},
        "": {"dsm_path": "/volume1/dl", "matched_files": 1},
    }


def test_majority_of_matching_files_decides_location():
    torrents = [{"id": n, "downloadDir": "/downloads/tv", "files": [{"name": f"e{n}.mkv", "length": n}]} for n in range(1, 4)]
    locator = _locate(torrents, [
        ("/volume1/电视剧/Show/e1.mkv", 1),
        ("/volume1/downloads/tv/e1.mkv", 1),
        ("/volume1/downloads/tv/e2.mkv", 2),
        ("/volume1/downloads/tv/e3.mkv", 3),
    ])
    assert locator.located(0)["tv"]["dsm_path"] == "/volume1/downloads/tv"


def test_tie_prefers_folder_named_like_entry_outside_television_library():
    torrents = [{"id": 1, "downloadDir": "/downloads/tv", "files": [{"name": "e1.mkv", "length": 1}]}]
    locator = _locate(torrents, [("/volume1/电视剧/e1.mkv", 1), ("/volume1/downloads/tv/e1.mkv", 1)])
    assert locator.located(0)["tv"]["dsm_path"] == "/volume1/downloads/tv"


def test_unlocated_entries_are_reported_and_skipped():
    old = int((datetime.now(UTC) - timedelta(days=31)).timestamp())
    server = TransmissionServer(name="客厅", address="tr.local")
    torrents = [{"id": 1, "downloadDir": "/downloads/missing", "doneDate": old, "percentDone": 1,
                 "files": [{"name": "x.mkv", "length": 5}]}]
    locator = _locate(torrents, [])
    mappings, candidates, warnings = find_candidates([server], [torrents], locator, [], max_file_mb=1, age_days=30)
    assert mappings == [[{"tr_path": "/downloads/missing", "dsm_path": None, "matched_files": 0}]]
    assert not candidates
    assert "/downloads/missing" in warnings[0]


def test_load_torrents_reports_offline_servers(monkeypatch):
    class Client:
        def __init__(self, server):
            if server.address == "down":
                from app.transmission import TransmissionError
                raise TransmissionError("offline")
            self.url = "http://up:9091/transmission/rpc"

        def torrents(self):
            return [{"id": 1}]

    monkeypatch.setattr("app.transmission.TransmissionClient", Client)
    connections, lists, warnings = load_torrents([TransmissionServer(address="up"), TransmissionServer(address="down")])
    assert [row["connected"] for row in connections] == [True, False]
    assert lists == [[{"id": 1}], None]
    assert len(warnings) == 1


def test_torrents_are_fetched_in_batches_and_trimmed(monkeypatch):
    from app import transmission

    calls = []

    def call(self, method, arguments=None):
        calls.append(arguments)
        if arguments["fields"] == ["id"]:
            return {"torrents": [{"id": n} for n in range(5)]}
        return {"torrents": [{"id": n, "files": [{"name": f"{n}.mkv", "length": n, "bytesCompleted": n}]}
                             for n in arguments["ids"]]}

    monkeypatch.setattr(transmission, "TORRENT_BATCH_SIZE", 2)
    monkeypatch.setattr(transmission.TransmissionClient, "call", call)
    torrents = transmission.TransmissionClient(TransmissionServer(address="tr")).torrents()
    assert [row["id"] for row in torrents] == [0, 1, 2, 3, 4]
    assert [args.get("ids") for args in calls[1:]] == [[0, 1], [2, 3], [4]]
    assert torrents[0]["files"] == [{"name": "0.mkv", "length": 0}]


def test_oversized_response_is_reported_instead_of_invalid_json(monkeypatch):
    from app import transmission

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, limit):
            return b"{" * limit

    monkeypatch.setattr(transmission, "MAX_RESPONSE_BYTES", 8)
    monkeypatch.setattr(transmission.urllib.request, "urlopen", lambda *_, **__: Response())
    try:
        transmission.TransmissionClient(TransmissionServer(address="tr")).call("torrent-get")
    except transmission.TransmissionError as exc:
        assert "超过" in str(exc)
    else:
        raise AssertionError("oversized response must be rejected")


def test_dsm_directory_is_mapped_to_only_the_entry_with_most_matches():
    torrents = [{"id": n, "downloadDir": "/downloads/11TB", "files": [{"name": f"a{n}.mkv", "length": n}]} for n in range(1, 4)]
    torrents += [{"id": 10, "downloadDir": "/downloads/7TB", "files": [{"name": "a1.mkv", "length": 1}]},
                 {"id": 11, "downloadDir": "/downloads/13TB", "files": [{"name": "a2.mkv", "length": 2}]}]
    locator = _locate(torrents, [("/volume3/11TB/a1.mkv", 1), ("/volume3/11TB/a2.mkv", 2), ("/volume3/11TB/a3.mkv", 3)])
    assert locator.mappings(0) == [
        {"tr_path": "/downloads/11TB", "dsm_path": "/volume3/11TB", "matched_files": 3},
        {"tr_path": "/downloads/13TB", "dsm_path": None, "matched_files": 0},
        {"tr_path": "/downloads/7TB", "dsm_path": None, "matched_files": 0},
    ]


def test_entry_that_loses_a_directory_falls_back_to_next_choice():
    torrents = [{"id": n, "downloadDir": "/downloads/11TB", "files": [{"name": f"a{n}.mkv", "length": n}]} for n in range(1, 4)]
    torrents += [{"id": 10, "downloadDir": "/downloads/7TB", "files": [{"name": "a1.mkv", "length": 1}]},
                 {"id": 11, "downloadDir": "/downloads/7TB", "files": [{"name": "b.mkv", "length": 9}]}]
    locator = _locate(torrents, [("/volume3/11TB/a1.mkv", 1), ("/volume3/11TB/a2.mkv", 2), ("/volume3/11TB/a3.mkv", 3),
                                 ("/volume1/7TB/b.mkv", 9)])
    located = locator.located(0)
    assert located["11TB"]["dsm_path"] == "/volume3/11TB"
    assert located["7TB"] == {"dsm_path": "/volume1/7TB", "matched_files": 1}


def test_custom_download_root_per_server():
    torrents = [
        {"id": 1, "downloadDir": "/data/tr/movies", "files": [{"name": "a.mkv", "length": 10}]},
        {"id": 2, "downloadDir": "/downloads/tv", "files": [{"name": "b.mkv", "length": 20}]},
    ]
    locator = _locate(torrents, [("/volume1/m/movies/a.mkv", 10), ("/volume1/t/tv/b.mkv", 20)], root="/data/tr")
    assert locator.mappings(0) == [{"tr_path": "/data/tr/movies", "dsm_path": "/volume1/m/movies", "matched_files": 1}]


def test_download_root_defaults_and_validation():
    assert TransmissionServer(address="tr").download_root == "/downloads"
    assert TransmissionServer(address="tr", download_root=" /data/tr/ ").download_root == "/data/tr"
    assert TransmissionServer(address="tr", download_root="").download_root == "/downloads"
    try:
        TransmissionServer(address="tr", download_root="data")
    except ValueError:
        pass
    else:
        raise AssertionError("relative root must be rejected")


def test_mappings_are_available_before_scan_finishes():
    torrents = [{"id": 1, "downloadDir": "/downloads/a", "files": [{"name": "x.mkv", "length": 1}]},
                {"id": 2, "downloadDir": "/downloads/b", "files": [{"name": "y.mkv", "length": 2}]}]
    locator = PathLocator([torrents], ["/downloads"])
    assert [row["dsm_path"] for row in locator.mappings(0)] == [None, None]
    locator.add("/volume1/s/a/x.mkv", 1)
    assert [row["dsm_path"] for row in locator.mappings(0)] == ["/volume1/s/a", None]


def test_legacy_mapping_fields_are_ignored():
    server = TransmissionServer.model_validate({"address": "tr", "mount_path": "/d", "local_path": "/volume1/d"})
    assert not hasattr(server, "mount_path")


def test_cleanup_rejects_paths_outside_selected_volumes():
    request = AnalysisRequest(address="nas", username="u", password="p", volumes=["/volume1"],
                              transmission_servers=[TransmissionServer(address="tr")])
    result = {"transmission_candidates": [{"id": "x", "dsm_path": "/etc/passwd", "hardlink_paths": ["/etc/passwd"],
                                           "server_index": 0, "task_id": 1, "task_name": "x"}]}
    try:
        cleanup_candidates(request, result, ["x"])
    except ValueError as exc:
        assert "存储空间" in str(exc)
    else:
        raise AssertionError("unsafe path must be rejected")
