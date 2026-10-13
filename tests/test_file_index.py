import threading

from fastapi.testclient import TestClient

from app.file_index import FileIndex
from app.jobs import JobStore
from app.models import AnalysisRequest


def fixture():
    index = FileIndex()
    rows = [{"path": f"/volume1/photos/照片{i:03d}.jpg", "size_bytes": i, "link_count": 1} for i in range(120)]
    for row in rows:
        index.add(row)
    result = {"scanned_files": 120, "tree_directories": [
        {"path": "/volume1/photos", "size_bytes": sum(range(120)) + 200, "file_count": 121},
        {"path": "/volume1/photos/nested", "size_bytes": 200, "file_count": 1},
    ]}
    return index, result


def test_file_index_pages_all_small_files_and_zero_bytes():
    index, result = fixture()
    seen = []
    for offset in range(0, 121, 30):
        page = index.browse(result, "/volume1/photos", offset, 30)
        assert page["total"] == 121
        assert len(page["map_items"]) == 80
        seen.extend(page["items"])
    assert len({row["path"] for row in seen}) == 121
    assert seen[0]["type"] == "folder"
    assert seen[-1]["size_bytes"] == 0
    found = index.browse(result, "/volume1/photos", query="照片001")
    assert found["total"] == 1
    assert found["items"][0]["size_bytes"] == 1
    assert len(found["map_items"]) == 80  # Search does not change area.
    assert index.browse(result, "/volume1/photos", offset=999999)["offset"] == 120


def test_snapshot_boundary_excludes_unpublished_records():
    index, result = fixture()
    index.add({"path": "/volume1/photos/new.jpg", "size_bytes": 9999, "link_count": 2})
    page = index.browse(result, "/volume1/photos")
    assert page["total"] == 121
    assert not any(row["path"].endswith("new.jpg") for row in page["map_items"])


def test_tree_endpoint_validation_and_cancelled_result(monkeypatch):
    import app.main as main
    index, result = fixture()
    store = JobStore(1)
    store._jobs["test"] = {"result": result, "status": "cancelled"}
    store._file_indexes["test"] = index
    store._cancel_events["test"] = threading.Event()
    monkeypatch.setattr(main, "jobs", store)
    with TestClient(main.app) as client:
        assert client.get("/api/jobs/test/tree", params={"path": "/volume1/photos"}).json()["total"] == 121
        assert client.get("/api/jobs/test/tree", params={"path": "/etc"}).status_code == 400
        assert client.get("/api/jobs/absent/tree", params={"path": "/volume1"}).status_code == 404
        assert client.get("/api/jobs/test/tree", params={"path": "/volume1/photos", "limit": 10000}).status_code == 422


def test_job_retains_index_on_cancel(monkeypatch):
    import app.jobs as module
    ready = threading.Event()
    def scan(req, progress, publish, cancel_event, on_file):
        on_file({"path": "/volume1/tiny.txt", "size_bytes": 1, "link_count": 1})
        publish({"scanned_files": 1, "tree_directories": [{"path": "/volume1", "size_bytes": 1, "file_count": 1}]})
        ready.set()
        assert cancel_event.wait(5)
        from app.synology import ScanCancelled
        raise ScanCancelled()
    monkeypatch.setattr(module, "analyze", scan)
    store = JobStore(1)
    job_id = store.create(AnalysisRequest(address="test.invalid", username="test", password="test", volumes=["/volume1"]))
    try:
        assert ready.wait(5)
        assert store.browse(job_id, "/volume1", 0, 30, "")["items"][0]["size_bytes"] == 1
    finally:
        store.cancel(job_id)
        store._executor.shutdown(wait=True)
    assert store.get(job_id)["status"] == "cancelled"
    assert store.browse(job_id, "/volume1", 0, 30, "")["total"] == 1
