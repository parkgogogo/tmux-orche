from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path

import pytest

import self_update


def _write_release_archive(tmp_path: Path, *, version: str, target: str) -> Path:
    source_root = tmp_path / "payload" / "orche"
    source_root.mkdir(parents=True)
    executable = source_root / "orche"
    executable.write_text("#!/bin/sh\necho orche\n", encoding="utf-8")
    executable.chmod(0o755)
    internal_dir = source_root / "_internal"
    internal_dir.mkdir()
    (internal_dir / "manifest.txt").write_text(f"{version}:{target}\n", encoding="utf-8")

    archive_path = tmp_path / f"orche-{version}-{target}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source_root, arcname="orche")
    return archive_path


def _write_legacy_release_archive(tmp_path: Path, *, version: str, target: str) -> Path:
    source_root = tmp_path / "legacy"
    source_root.mkdir(parents=True, exist_ok=True)
    executable = source_root / "orche"
    executable.write_text("#!/bin/sh\necho legacy\n", encoding="utf-8")
    executable.chmod(0o755)

    archive_path = tmp_path / f"orche-{version}-{target}-legacy.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(executable, arcname="orche")
    return archive_path


def test_perform_self_update_requires_install_metadata(xdg_runtime):
    with pytest.raises(self_update.SelfUpdateError, match="install.sh"):
        self_update.perform_self_update()


def test_perform_self_update_installs_archive_and_updates_symlink(xdg_runtime, tmp_path, monkeypatch):
    prefix = tmp_path / "bin"
    install_root = tmp_path / "releases"
    current_dir = install_root / "v0.4.35" / "linux-x64"
    current_dir.mkdir(parents=True)
    current_executable = current_dir / "orche"
    current_executable.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    current_executable.chmod(0o755)
    prefix.mkdir()
    os.symlink(current_executable, prefix / "orche")

    self_update.save_install_metadata(
        {
            "channel": self_update.INSTALL_CHANNEL,
            "repo": self_update.DEFAULT_RELEASE_REPO,
            "version": "v0.4.35",
            "target": "linux-x64",
            "prefix": str(prefix),
            "link_path": str(prefix / "orche"),
            "install_root": str(install_root),
            "executable_path": str(current_executable),
        }
    )

    archive_path = _write_release_archive(tmp_path, version="v0.4.36", target="linux-x64")

    def fake_download(*, destination: Path, **_: object) -> None:
        shutil.copyfile(archive_path, destination)

    monkeypatch.setattr(self_update, "download_release_archive", fake_download)

    result = self_update.perform_self_update(requested_version="v0.4.36")

    expected_executable = install_root / "v0.4.36" / "linux-x64" / "orche"
    assert result.version == "v0.4.36"
    assert result.updated is True
    assert result.link_path == prefix / "orche"
    assert (prefix / "orche").is_symlink()
    assert (prefix / "orche").resolve() == expected_executable
    assert expected_executable.exists()
    metadata = self_update.load_install_metadata()
    assert metadata is not None
    assert metadata["version"] == "v0.4.36"
    assert metadata["target"] == "linux-x64"


def test_perform_self_update_noops_when_requested_version_is_already_active(xdg_runtime, tmp_path):
    prefix = tmp_path / "bin"
    install_root = tmp_path / "releases"
    executable = install_root / "v0.4.35" / "linux-x64" / "orche"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho current\n", encoding="utf-8")
    executable.chmod(0o755)
    prefix.mkdir()
    os.symlink(executable, prefix / "orche")

    self_update.save_install_metadata(
        {
            "channel": self_update.INSTALL_CHANNEL,
            "repo": self_update.DEFAULT_RELEASE_REPO,
            "version": "v0.4.35",
            "target": "linux-x64",
            "prefix": str(prefix),
            "link_path": str(prefix / "orche"),
            "install_root": str(install_root),
            "executable_path": str(executable),
        }
    )

    result = self_update.perform_self_update(requested_version="v0.4.35")

    assert result.updated is False
    assert result.version == "v0.4.35"


def test_perform_self_update_repairs_stale_install_metadata(xdg_runtime, tmp_path, monkeypatch):
    prefix = tmp_path / "bin"
    install_root = tmp_path / "releases"
    current_executable = install_root / "v0.4.38" / "darwin-arm64" / "orche"
    current_executable.parent.mkdir(parents=True)
    current_executable.write_text("#!/bin/sh\necho current\n", encoding="utf-8")
    current_executable.chmod(0o755)
    prefix.mkdir()
    os.symlink(current_executable, prefix / "orche")

    self_update.save_install_metadata(
        {
            "channel": self_update.INSTALL_CHANNEL,
            "repo": self_update.DEFAULT_RELEASE_REPO,
            "version": "v0.4.38",
            "target": "linux-x64",
            "prefix": str(tmp_path / "old-bin"),
            "link_path": str(tmp_path / "old-bin" / "orche"),
            "install_root": str(tmp_path / "old-releases"),
            "executable_path": str(tmp_path / "old-releases" / "v0.4.38" / "linux-x64" / "orche"),
        }
    )
    monkeypatch.setattr(self_update, "detect_target", lambda: "darwin-arm64")
    monkeypatch.setattr(self_update.sys, "argv", [str(prefix / "orche")])
    monkeypatch.setattr(self_update, "resolve_version", lambda repo, requested_version: requested_version or "v0.4.38")

    result = self_update.perform_self_update()

    metadata = self_update.load_install_metadata()
    assert result.updated is False
    assert result.version == "v0.4.38"
    assert metadata is not None
    assert metadata["target"] == "darwin-arm64"
    assert metadata["prefix"] == str(prefix.resolve())
    assert metadata["install_root"] == str(install_root.resolve())
    assert metadata["executable_path"] == str(current_executable.resolve())


def test_install_release_archive_supports_legacy_single_binary_layout(xdg_runtime, tmp_path):
    archive_path = _write_legacy_release_archive(
        tmp_path,
        version="v0.4.36",
        target="linux-x64",
    )
    prefix = tmp_path / "bin"
    install_root = tmp_path / "releases"

    result = self_update.install_release_archive(
        archive_path=archive_path,
        version="v0.4.36",
        target="linux-x64",
        repo=self_update.DEFAULT_RELEASE_REPO,
        prefix=prefix,
        install_root=install_root,
    )

    executable = install_root / "v0.4.36" / "linux-x64" / "orche"
    assert result.updated is True
    assert executable.exists()
    assert executable.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert (prefix / "orche").is_symlink()
    assert (prefix / "orche").resolve() == executable


def test_install_release_archive_rejects_symlink_members(xdg_runtime, tmp_path):
    archive_path = tmp_path / "orche-v0.4.36-linux-x64.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("orche/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../outside"
        archive.addfile(member)

    with pytest.raises(self_update.SelfUpdateError, match="unsupported archive member type"):
        self_update.install_release_archive(
            archive_path=archive_path,
            version="v0.4.36",
            target="linux-x64",
            repo=self_update.DEFAULT_RELEASE_REPO,
            prefix=tmp_path / "bin",
            install_root=tmp_path / "releases",
        )
