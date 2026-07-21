"""Unit tests for :mod:`docker_disk_toolkit.system_info`."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from docker_disk_toolkit import system_info

_Part = namedtuple("_Part", ["device", "mountpoint", "fstype", "opts"])
_Usage = namedtuple("_Usage", ["total", "used", "free", "percent"])


@pytest.fixture
def fake_disks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch psutil so disk collection is deterministic and cross-platform."""
    partitions = [
        _Part("/dev/sda1", "/", "ext4", "rw"),
        _Part("/dev/sda2", "/var/lib/docker", "ext4", "rw"),
    ]
    usages = {
        "/": _Usage(1000, 800, 200, 80.0),
        "/var/lib/docker": _Usage(2000, 1500, 500, 75.0),
    }
    monkeypatch.setattr(system_info.psutil, "disk_partitions", lambda all=False: partitions)

    def fake_usage(path: str) -> _Usage:
        return usages.get(str(path), _Usage(500, 100, 400, 20.0))

    monkeypatch.setattr(system_info.psutil, "disk_usage", fake_usage)
    # Force inode path to be exercised deterministically.
    monkeypatch.setattr(system_info, "get_inode_usage", lambda mp: (100, 40, 60, 40.0))


class TestPlatformBasics:
    def test_hostname_and_os(self) -> None:
        assert isinstance(system_info.hostname(), str)
        assert system_info.hostname()
        assert isinstance(system_info.os_description(), str)

    def test_platform_predicates_consistent(self) -> None:
        flags = [system_info.is_windows(), system_info.is_macos(), system_info.is_linux()]
        assert sum(bool(f) for f in flags) <= 1


class TestCollectDisks:
    def test_flags_docker_root(self, fake_disks: None) -> None:
        disks = system_info.collect_disks(docker_root_dir="/var/lib/docker/overlay2")
        by_mp = {d.mountpoint: d for d in disks}
        assert by_mp["/var/lib/docker"].is_docker_root is True
        assert by_mp["/"].is_docker_root is False
        assert by_mp["/"].inodes_percent == 40.0

    def test_percent_and_bytes(self, fake_disks: None) -> None:
        disks = system_info.collect_disks()
        root = next(d for d in disks if d.mountpoint == "/")
        assert root.total_bytes == 1000 and root.free_bytes == 200
        assert root.percent_used == 80.0

    def test_skips_inaccessible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            system_info.psutil,
            "disk_partitions",
            lambda all=False: [_Part("z:", "Z:\\", "cdfs", "ro")],
        )

        def boom(path: str) -> _Usage:
            raise PermissionError("empty drive")

        monkeypatch.setattr(system_info.psutil, "disk_usage", boom)
        assert system_info.collect_disks() == []

    def test_disk_for_path(self, fake_disks: None) -> None:
        disk = system_info.disk_for_path("/var/lib/docker/volumes")
        assert disk is not None and disk.mountpoint == "/var/lib/docker"


class TestInodeUsage:
    def test_no_statvfs_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(system_info.os, "statvfs", raising=False)
        assert system_info.get_inode_usage("/") == (None, None, None, None)


class TestVhdxDiscovery:
    def test_finds_and_sorts_vhdx(self, tmp_path: Path) -> None:
        root = tmp_path / "Docker" / "wsl"
        (root / "data").mkdir(parents=True)
        (root / "main").mkdir(parents=True)
        small = root / "main" / "ext4.vhdx"
        big = root / "data" / "ext4.vhdx"
        small.write_bytes(b"x" * 100)
        big.write_bytes(b"y" * 5000)

        found = system_info.find_vhdx_files(search_roots=[tmp_path / "Docker"])
        assert [v.size_bytes for v in found] == [5000, 100]  # largest first
        assert found[0].label == "docker-desktop-data"

    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        assert system_info.find_vhdx_files(search_roots=[tmp_path / "nope"]) == []

    def test_default_roots_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "Docker"
        root.mkdir()
        (root / "ext4.vhdx").write_bytes(b"z" * 42)
        monkeypatch.setattr(system_info, "_localappdata_roots", lambda: [root])
        found = system_info.find_vhdx_files()
        assert len(found) == 1 and found[0].size_bytes == 42


class TestInodeAndPlatformBranches:
    def test_inode_usage_with_fake_statvfs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Stat:
            f_files = 100
            f_ffree = 60

        monkeypatch.setattr(system_info.os, "statvfs", lambda p: _Stat(), raising=False)
        assert system_info.get_inode_usage("/") == (100, 40, 60, 40.0)

    def test_inode_usage_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(path: str) -> object:
            raise OSError("nope")

        monkeypatch.setattr(system_info.os, "statvfs", boom, raising=False)
        assert system_info.get_inode_usage("/") == (None, None, None, None)

    def test_os_description_wsl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(system_info, "is_wsl", lambda: True)
        assert "WSL2" in system_info.os_description()

    def test_localappdata_roots_native(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(system_info, "is_wsl", lambda: False)
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
        roots = system_info._localappdata_roots()
        assert any("Docker" in str(r) for r in roots)
