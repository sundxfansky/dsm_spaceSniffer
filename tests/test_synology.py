import pytest

from app.synology import group_hardlinks, parse_df, parse_du, parse_host, parse_large_files


def test_nul_records_preserve_newlines_tabs_and_unicode():
    path = "/volume1/中文\nwith\ttab.mov"
    rows = parse_large_files(f"2049\t42\t1\t100\t123.456\t{path}\0")
    assert rows[0]["path"] == path


def test_invalid_metadata_is_not_silently_dropped():
    from app.synology import SynologyError
    with pytest.raises(SynologyError):
        parse_large_files("unsupported stat output\0")


def test_hardlink_with_one_visible_path_is_still_reported():
    groups = group_hardlinks("2049\t42\t3\t100\t123\t/volume1/visible\0", "/volume1")
    assert len(groups) == 1
    assert groups[0]["complete"] is False
    assert groups[0]["link_count"] == 3


def test_scan_retains_over_5000_files_and_threshold_boundary(monkeypatch):
    from app import synology
    from app.models import AnalysisRequest, VolumeInfo

    class Session:
        host = "nas"
        fingerprint = "test"
        closed = False

        def capture(self, command, on_chunk=None):
            if "du --help" in command:
                return "10\t/volume1/share\n10\t/volume1\n", "", 0
            on_chunk("".join(f"2049\t{i}\t1\t1048576\t123\t/volume1/share/{i}\0" for i in range(5010)).encode())
            return "", "", 0

        def close(self):
            self.closed = True

    session = Session()
    volume = VolumeInfo(path="/volume1", filesystem="test", total_bytes=100, used_bytes=50, available_bytes=50, usage_percent=50)
    monkeypatch.setattr(synology, "discover", lambda request: (session, [volume]))
    monkeypatch.setattr(synology, "file_scan_command", lambda session, root: "scan")
    request = AnalysisRequest(address="nas", username="user", password="placeholder", volumes=["/volume1"], large_file_mb=1, max_large_files=10)
    result = synology.analyze(request, lambda *_: None)
    assert len(result["large_files"]) == 5010
    assert result["scanned_files"] == 5010
    assert result["complete"] is True
    assert session.closed


@pytest.mark.parametrize("partial", [True, False])
def test_permission_errors_never_report_complete_success(monkeypatch, partial):
    from app import synology
    from app.models import AnalysisRequest, VolumeInfo

    class Session:
        host = "nas"
        fingerprint = "test"
        def capture(self, command, on_chunk=None):
            if "du --help" in command:
                return "10\t/volume1/share\n", "", 0
            if partial:
                on_chunk(b"2049\t42\t2\t100\t123\t/volume1/visible\0")
            return "", "find: /volume1/private: Permission denied", 1
        def close(self):
            pass

    volume = VolumeInfo(path="/volume1", filesystem="test", total_bytes=100, used_bytes=50, available_bytes=50, usage_percent=50)
    monkeypatch.setattr(synology, "discover", lambda request: (Session(), [volume]))
    monkeypatch.setattr(synology, "file_scan_command", lambda session, root: "scan")
    request = AnalysisRequest(address="nas", username="user", password="placeholder", volumes=["/volume1"], large_file_mb=0)
    if partial:
        result = synology.analyze(request, lambda *_: None)
        assert result["complete"] is False
        assert "Permission denied" in result["warnings"][0]
        assert len(result["large_files"]) == 1
    else:
        with pytest.raises(synology.SynologyError, match="Permission denied"):
            synology.analyze(request, lambda *_: None)


def test_parse_host_accepts_dsm_url_and_hostname():
    assert parse_host("https://nas.local:5001") == "nas.local"
    assert parse_host("192.168.1.20") == "192.168.1.20"


def test_parse_host_rejects_unknown_scheme():
    with pytest.raises(ValueError):
        parse_host("ftp://nas.local")


def test_parse_df_returns_only_synology_volumes():
    output = """Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/md0 2000000 1000000 1000000 50% /
/dev/vg1/volume_1 10000000 4000000 6000000 40% /volume1
/dev/vg2/volume_2 20000000 18000000 2000000 90% /volume2
"""
    volumes = parse_df(output)
    assert [volume.path for volume in volumes] == ["/volume1", "/volume2"]
    assert volumes[0].total_bytes == 10_000_000 * 1024
    assert volumes[1].usage_percent == 90


def test_parse_du_sorts_descending_and_omits_root():
    output = "100\t/volume1/a\n500\t/volume1/b\n600\t/volume1\n"
    assert parse_du(output, "/volume1") == [
        {"path": "/volume1/b", "size_bytes": 500 * 1024},
        {"path": "/volume1/a", "size_bytes": 100 * 1024},
    ]


def test_large_file_and_hardlink_parsing():
    output = (
        "2049\t42\t2\t1000\t1728000000\t/volume1/a.mov\n"
        "2049\t42\t2\t1000\t1728000000\t/volume1/archive/a.mov\n"
        "2049\t90\t1\t2000\t1728000001\t/volume1/b.mov\n"
    )
    rows = parse_large_files(output)
    assert rows[0]["path"] == "/volume1/a.mov"
    assert rows[0]["size_bytes"] == 1000
    groups = group_hardlinks(output, "/volume1")
    assert len(groups) == 1
    assert groups[0]["inode"] == "42"
    assert groups[0]["shared_bytes"] == 1000
