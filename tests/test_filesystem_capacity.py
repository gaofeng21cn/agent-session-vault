from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_session_vault import storage


def test_native_darwin_capacity_keeps_large_smb_counters_and_spaces(monkeypatch):
    monkeypatch.setattr(storage.sys, "platform", "darwin")
    output = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
              "//temporary-private-login@nas/Personal Drive 58569895936 10602223360 47967672576 19% /Volumes/Personal Drive\n")
    monkeypatch.setattr(storage.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output))
    result = storage.filesystem_capacity(Path('/Volumes/Personal Drive'))
    assert result == {"method": "df-kP", "total_bytes": 59975573438464,
                      "used_bytes": 10856676720640, "available_bytes": 49118896717824}
    assert 'private-login' not in str(result)


@pytest.mark.parametrize('code,output', [(1, '//secret@server'), (0, '//secret@server invalid')])
def test_native_capacity_errors_do_not_disclose_mount_credentials(monkeypatch, code, output):
    monkeypatch.setattr(storage.sys, "platform", "darwin")
    monkeypatch.setattr(storage.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=output))
    with pytest.raises(ValueError) as error:
        storage.filesystem_capacity(Path('/Volumes/Drive'))
    assert 'secret' not in str(error.value)


def test_other_platform_uses_native_python_counters(monkeypatch):
    monkeypatch.setattr(storage.sys, "platform", "linux")
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(total=10, used=3, free=7))
    assert storage.filesystem_capacity(Path('/somewhere')) == {
        'method': 'disk_usage', 'total_bytes': 10, 'used_bytes': 3, 'available_bytes': 7}
