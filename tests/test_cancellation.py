import threading

import pytest
from fastapi.testclient import TestClient

from app import jobs, synology
from app.jobs import JobStore
from app.models import AnalysisRequest, VolumeInfo


def request():
    return AnalysisRequest(address="test.invalid", username="test", password="test", volumes=["/volume1"], large_file_mb=0)


@pytest.mark.parametrize("finish", ["cancel", "success", "error"])
def test_running_cancel_preserves_results_and_wins_completion_race(monkeypatch, finish):
    started, release = threading.Event(), threading.Event()
    def scan(req, progress, publish, cancel_event, on_file=None):
        publish({"scanned_files": 3, "complete": False})
        started.set()
        assert release.wait(5)
        assert cancel_event.is_set()
        progress(90, "must not replace stopping stage")
        if finish == "cancel":
            raise synology.ScanCancelled()
        if finish == "error":
            raise OSError("SSH closed")
        return {"scanned_files": 4, "complete": True, "scan_finished": True}
    monkeypatch.setattr(jobs, "analyze", scan)
    store = JobStore(1)
    job_id = store.create(request())
    try:
        assert started.wait(5)
        assert store.cancel(job_id)["status"] == "cancelling"
        assert store.cancel(job_id)["status"] == "cancelling"
    finally:
        release.set()
        store._executor.shutdown(wait=True)
    result = store.get(job_id)
    assert result["status"] == "cancelled"
    assert result["error"] is None
    assert result["result"]["scanned_files"] >= 3
    assert not result["result"]["complete"]
    assert not result["result"]["scan_finished"]
    assert store.cancel(job_id) == result
    assert store.cancel("missing") is None


def test_queued_cancel_never_connects_and_completed_cancel_is_noop(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def scan(req, progress, publish, cancel_event, on_file=None):
        calls.append(req)
        started.set()
        assert release.wait(5)
        return {"complete": True}
    monkeypatch.setattr(jobs, "analyze", scan)
    store = JobStore(1)
    first = store.create(request())
    try:
        assert started.wait(5)
        second = store.create(request())
        assert store.cancel(second)["status"] == "cancelled"
    finally:
        release.set()
        store._executor.shutdown(wait=True)
    assert len(calls) == 1
    assert store.get(second)["status"] == "cancelled"
    completed = store.get(first)
    assert completed["status"] == "completed"
    assert store.cancel(first) == completed


@pytest.mark.parametrize("command", ["find", "du", "df"])
def test_capture_cancels_even_without_stdout_and_closes_channel(command):
    event = threading.Event()
    class Channel:
        closed = False
        def recv_ready(self):
            event.set()
            return False
        def recv_stderr_ready(self): return False
        def exit_status_ready(self): return False
        def close(self): self.closed = True
    channel = Channel()
    class Client:
        def exec_command(self, command, timeout):
            return None, type("Stdout", (), {"channel": channel})(), None
    session = synology.SSHSession(Client(), "test", "test", cancel_event=event)
    with pytest.raises(synology.ScanCancelled):
        session.capture(command)
    assert channel.closed


def test_cancel_flushes_unpublished_file_results_and_closes_session(monkeypatch):
    event = threading.Event()
    snapshots = []
    class Session:
        host, fingerprint, closed = "test", "test", False
        def close(self): self.closed = True
        def capture(self, command, on_chunk=None):
            on_chunk(b"1\t1\t1\t100\t123\t/volume1/folder/file\0")
            event.set()
            on_chunk(b"1\t2\t1\t100\t123\t/volume1/folder/other\0")
            pytest.fail("scan must stop")
    session = Session()
    volume = VolumeInfo(path="/volume1", filesystem="test", total_bytes=100, used_bytes=50, available_bytes=50, usage_percent=50)
    monkeypatch.setattr(synology, "discover", lambda *_: (session, [volume]))
    monkeypatch.setattr(synology, "file_scan_command", lambda *_: "scan")
    with pytest.raises(synology.ScanCancelled):
        synology.analyze(request(), lambda *_: None, snapshots.append, event)
    assert session.closed
    assert snapshots[-1]["scanned_files"] == 1
    assert not snapshots[-1]["scan_finished"]


def test_cancel_endpoint(monkeypatch):
    import app.main as main
    store = JobStore(1)
    monkeypatch.setattr(main, "jobs", store)
    # A queued task without submitting a worker allows deterministic API assertions.
    store._cancel_events["queued"] = threading.Event()
    store._jobs["queued"] = dict(id="queued", status="queued", progress=0, stage="等待扫描",
                                created_at=store._now(), updated_at=store._now(), error=None, result=None)
    with TestClient(main.app) as client:
        assert client.post("/api/jobs/missing/cancel").status_code == 404
        response = client.post("/api/jobs/queued/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert client.get("/api/jobs/queued").json()["status"] == "cancelled"
