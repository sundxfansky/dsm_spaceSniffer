import pytest

from app.synology import remote_identity, scan_issue, SynologyError


@pytest.mark.parametrize("uid,name", [(0, "root"), (1026, "admin")])
def test_identity_is_remote_uid_not_account_name(uid, name):
    class Session:
        def run(self, command, timeout):
            assert command == "id -u; id -un"
            return f"{uid}\n{name}\n"
    assert remote_identity(Session()) == {"uid": uid, "username": name}


def test_root_denial_and_missing_file_are_not_mislabelled():
    root = {"uid": 0, "username": "root"}
    assert "UID=0" in scan_issue("scan", "Permission denied", 1, root)
    assert "root_squash" in scan_issue("scan", "Permission denied", 1, root)
    missing = scan_issue("scan", "No such file or directory", 1, root)
    assert "可能已移动" in missing
    assert "权限" not in missing
    assert "未确认为 root" in scan_issue("scan", "Permission denied", 1, {"uid": 1026})


def test_invalid_identity_is_not_assumed_root():
    class Session:
        def run(self, command, timeout):
            return "welcome\nroot\n"
    with pytest.raises(SynologyError):
        remote_identity(Session())
