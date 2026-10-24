import threading

import pytest

from app import synology
from app.models import AnalysisRequest, VolumeInfo
from app.jobs import JobStore


def request():
    return AnalysisRequest(address="test.invalid", username="test", password="test", volumes=["/volume1"], large_file_mb=1)


def test_analysis_discovers_transmission_download_paths_from_scan(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.models import TransmissionServer

    volume = VolumeInfo(path="/volume1", filesystem="test", total_bytes=100, used_bytes=50, available_bytes=50, usage_percent=50)
    old = int((datetime.now(UTC) - timedelta(days=40)).timestamp())
    torrents = [{"id": 7, "name": "ep", "downloadDir": "/downloads/tv", "doneDate": old, "percentDone": 1,
                 "files": [{"name": "ep1.mkv", "length": 100}]}]

    class Client:
        def __init__(self, _server):
            self.url = "http://tr:9091/transmission/rpc"

        def torrents(self):
            return torrents

    class Session:
        host = "nas"
        fingerprint = "test"
        def close(self):
            pass
        def capture(self, command, on_chunk=None):
            if on_chunk is None:
                return "1024\t/volume1/share\n", "", 0
            on_chunk("1\t5\t2\t100\t123\t/volume1/share/tv/ep1.mkv\0".encode())
            on_chunk("1\t5\t2\t100\t123\t/volume1/电视剧/Show/ep1.mkv\0".encode())
            return "", "", 0

    monkeypatch.setattr("app.transmission.TransmissionClient", Client)
    monkeypatch.setattr(synology, "discover", lambda _: (Session(), [volume]))
    monkeypatch.setattr(synology, "file_scan_command", lambda *_: "scan")
    req = AnalysisRequest(address="nas", username="u", password="p", volumes=["/volume1"],
                          transmission_servers=[TransmissionServer(address="tr")])
    published = []
    clock = iter(range(0, 10000, 3))
    monkeypatch.setattr(synology.time, "monotonic", lambda: next(clock))
    result = synology.analyze(req, lambda *_: None, published.append)
    connection = result["transmission_connections"][0]
    assert connection["path_mappings"] == [{"tr_path": "/downloads/tv", "dsm_path": "/volume1/share/tv", "matched_files": 1}]
    assert connection["mappings_final"] and connection["download_root"] == "/downloads"
    live = [snap["transmission_connections"][0] for snap in published if snap["transmission_connections"]]
    assert live[0]["path_mappings"][0]["dsm_path"] is None and not live[0]["mappings_final"]
    assert any(row["path_mappings"][0]["dsm_path"] == "/volume1/share/tv" and not row["mappings_final"] for row in live)
    assert [row["dsm_path"] for row in result["transmission_candidates"]] == ["/volume1/share/tv/ep1.mkv"]
    assert result["complete"]


def test_live_nested_folders_small_files_split_utf8_and_immutable_snapshots(monkeypatch):
    published = []
    volume = VolumeInfo(path="/volume1", filesystem="test", total_bytes=100, used_bytes=50, available_bytes=50, usage_percent=50)
    clock = iter(range(0, 10000, 3))
    monkeypatch.setattr(synology.time, "monotonic", lambda: next(clock))

    class Session:
        host = "nas"
        fingerprint = "test"
        def close(self):
            pass
        def capture(self, command, on_chunk=None):
            if on_chunk is None:
                return "1024\t/volume1/share\n", "", 0
            first = "1\t1\t1\t2097152\t123\t/volume1/share/中文\nfile\0".encode()
            cut = first.index("中".encode()) + 1
            on_chunk(first[:cut])
            assert not published[-1]["large_files"]
            on_chunk(first[cut:])
            assert published[-1]["large_files"][0]["path"].endswith("中文\nfile")
            assert not published[-1]["scan_finished"]
            early = published[-1]
            for i in (2, 3):
                on_chunk(f"1\t{i}\t1\t524288\t123\t/volume1/share/nested/{i}\0".encode())
            folders = {r["path"]: r for r in published[-1]["large_directories"]}
            assert folders["/volume1/share/nested"]["size_bytes"] == 1048576
            assert folders["/volume1/share"]["size_bytes"] == 3145728
            assert len(published[-1]["large_files"]) == 1
            assert early["large_directories"][0]["size_bytes"] == 2097152
            assert early["tree_directories"][0]["size_bytes"] == 2097152
            return "", "", 0

    monkeypatch.setattr(synology, "discover", lambda _: (Session(), [volume]))
    monkeypatch.setattr(synology, "file_scan_command", lambda *_: "scan")
    indexed = []
    result = synology.analyze(request(), lambda *_: None, published.append, on_file=indexed.append)
    assert result["scanned_files"] == 3
    assert result["scan_finished"] and result["complete"]
    assert result["large_directories_total"] == 2
    tree = {row["path"]: row for row in result["tree_directories"]}
    assert tree["/volume1"]["size_bytes"] == 3145728
    assert tree["/volume1/share"]["direct_size_bytes"] == 2097152
    assert tree["/volume1/share/nested"]["direct_size_bytes"] == 1048576
    assert tree["/volume1/share/nested"]["direct_file_count"] == 2
    assert len(result["tree_file_previews"]) == 3
    assert len(indexed) == 3
    assert result["tree_file_index"] is True
    assert len(result["large_files"]) == 1
    assert any(row["size_bytes"] == 524288 for row in result["tree_file_previews"])


def test_job_exposes_partial_results_before_completion_and_keeps_them_on_failure(monkeypatch):
    import app.jobs as module
    published = threading.Event()
    release = threading.Event()
    def scanner(req, progress, publish, cancel_event=None, on_file=None):
        publish({"scanned_files": 1, "large_files": [{"path": "/volume1/file"}], "scan_finished": False})
        published.set()
        assert release.wait(5)
        raise synology.SynologyError("Permission denied")
    monkeypatch.setattr(module, "analyze", scanner)
    store = JobStore(max_workers=1)
    job_id = store.create(request())
    try:
        assert published.wait(5)
        live = store.get(job_id)
        assert live["status"] == "running"
        assert live["result"]["scanned_files"] == 1
    finally:
        release.set()
        store._executor.shutdown(wait=True)
    failed = store.get(job_id)
    assert failed["status"] == "failed"
    assert failed["result"] == live["result"]


def test_capture_delivers_chunks_before_command_exit():
    class Channel:
        chunks = [b"first\0", b"second\0"]
        delivered = []
        closed = False
        def recv_ready(self): return bool(self.chunks)
        def recv(self, size): return self.chunks.pop(0)
        def recv_stderr_ready(self): return False
        def exit_status_ready(self): return not self.chunks
        def recv_exit_status(self):
            assert self.delivered == [b"first\0", b"second\0"]
            return 0
        def close(self): self.closed = True
    channel = Channel()
    class Client:
        def exec_command(self, command, timeout):
            class Stdout: pass
            stdout = Stdout()
            stdout.channel = channel
            return None, stdout, None
    session = synology.SSHSession(Client(), "test", "test")
    assert session.capture("read-only", on_chunk=channel.delivered.append) == ("", "", 0)
    assert channel.closed
