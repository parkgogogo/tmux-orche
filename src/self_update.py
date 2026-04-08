from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from paths import data_dir
from tls import urlopen


DEFAULT_RELEASE_REPO = "parkgogogo/tmux-orche"
INSTALL_CHANNEL = "prebuilt-binary"
INSTALL_METADATA_FILE = "install.json"
BIN_NAME = "orche"


class SelfUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateResult:
    version: str
    target: str
    link_path: Path
    install_root: Path
    updated: bool


@dataclass(frozen=True)
class InstallContext:
    repo: str
    version: str
    target: str
    prefix: Path
    link_path: Path
    install_root: Path
    executable_path: Path


def install_metadata_path() -> Path:
    return data_dir() / INSTALL_METADATA_FILE


def load_install_metadata() -> Optional[dict[str, Any]]:
    path = install_metadata_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SelfUpdateError(f"Invalid install metadata: {path}") from exc
    if not isinstance(payload, dict):
        raise SelfUpdateError(f"Invalid install metadata: {path}")
    return payload


def save_install_metadata(payload: dict[str, Any]) -> None:
    path = install_metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def detect_target() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        os_name = "darwin"
    elif system == "linux":
        os_name = "linux"
    else:
        raise SelfUpdateError(f"Unsupported operating system: {system}")

    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine in {"x86_64", "amd64"}:
        arch = "x64"
    else:
        raise SelfUpdateError(f"Unsupported architecture: {machine}")

    target = f"{os_name}-{arch}"
    if target not in {"darwin-arm64", "darwin-x64", "linux-x64"}:
        raise SelfUpdateError(f"No prebuilt binary is published for {target}")
    return target


def runtime_link_path() -> Optional[Path]:
    argv0 = str(sys.argv[0] or "").strip()
    if argv0:
        candidate = Path(argv0).expanduser()
        if candidate.name == BIN_NAME:
            return candidate if candidate.is_absolute() else candidate.resolve()
    if getattr(sys, "frozen", False):
        resolved = shutil.which(BIN_NAME)
        if resolved:
            return Path(resolved).expanduser().resolve()
    return None


def runtime_executable_path() -> Optional[Path]:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).expanduser().resolve()
    link_path = runtime_link_path()
    if link_path is None:
        return None
    return link_path.resolve()


def infer_install_context(
    metadata: Optional[dict[str, Any]],
    *,
    repo: Optional[str] = None,
) -> InstallContext:
    link_path = runtime_link_path()
    executable_path = runtime_executable_path()
    metadata_target = str((metadata or {}).get("target") or "").strip()
    target = detect_target() if link_path is not None else (metadata_target or detect_target())

    if link_path is None:
        link_path_value = str((metadata or {}).get("link_path") or "").strip()
        if not link_path_value:
            raise SelfUpdateError("Install metadata is missing link_path")
        link_path = Path(link_path_value).expanduser().resolve()
    if executable_path is None:
        executable_value = str((metadata or {}).get("executable_path") or "").strip()
        if executable_value:
            executable_path = Path(executable_value).expanduser().resolve()
        else:
            executable_path = link_path.resolve()

    resolved_repo = str(repo or (metadata or {}).get("repo") or DEFAULT_RELEASE_REPO).strip() or DEFAULT_RELEASE_REPO
    version = str((metadata or {}).get("version") or "").strip()
    install_root_value = str((metadata or {}).get("install_root") or "").strip()
    prefix_value = str((metadata or {}).get("prefix") or "").strip()

    if executable_path.parent.name == target:
        inferred_version = executable_path.parent.parent.name
        inferred_install_root = executable_path.parent.parent.parent
        if inferred_version:
            version = inferred_version
        install_root = inferred_install_root
    else:
        install_root = Path(install_root_value).expanduser().resolve() if install_root_value else executable_path.parent

    prefix = link_path.parent.resolve() if runtime_link_path() is not None else (
        Path(prefix_value).expanduser().resolve() if prefix_value else link_path.parent.resolve()
    )
    if not version:
        version = str((metadata or {}).get("version") or "").strip()

    return InstallContext(
        repo=resolved_repo,
        version=version,
        target=target,
        prefix=prefix,
        link_path=link_path.resolve() if link_path.is_absolute() else link_path,
        install_root=install_root,
        executable_path=executable_path,
    )


def metadata_matches_context(metadata: Optional[dict[str, Any]], context: InstallContext) -> bool:
    if not metadata:
        return False
    if str(metadata.get("channel") or "").strip() != INSTALL_CHANNEL:
        return False
    if str(metadata.get("target") or "").strip() != context.target:
        return False
    if str(metadata.get("repo") or "").strip() not in {"", context.repo}:
        return False
    link_path_value = str(metadata.get("link_path") or "").strip()
    executable_value = str(metadata.get("executable_path") or "").strip()
    install_root_value = str(metadata.get("install_root") or "").strip()
    prefix_value = str(metadata.get("prefix") or "").strip()
    if not link_path_value or not executable_value or not install_root_value or not prefix_value:
        return False
    try:
        metadata_link_path = Path(link_path_value).expanduser().resolve()
        metadata_executable_path = Path(executable_value).expanduser().resolve()
        metadata_install_root = Path(install_root_value).expanduser().resolve()
        metadata_prefix = Path(prefix_value).expanduser().resolve()
    except OSError:
        return False
    return (
        metadata_link_path == context.link_path.resolve()
        and metadata_executable_path == context.executable_path.resolve()
        and metadata_install_root == context.install_root.resolve()
        and metadata_prefix == context.prefix.resolve()
    )


def save_install_context(context: InstallContext) -> None:
    save_install_metadata(
        {
            "channel": INSTALL_CHANNEL,
            "repo": context.repo,
            "version": context.version,
            "target": context.target,
            "prefix": str(context.prefix),
            "link_path": str(context.link_path),
            "install_root": str(context.install_root),
            "executable_path": str(context.executable_path),
        }
    )


def resolve_version(repo: str, requested_version: Optional[str]) -> str:
    if requested_version:
        return requested_version
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "orche-self-update",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.URLError as exc:
        raise SelfUpdateError(f"Failed to resolve latest release from {repo}") from exc
    version = str(payload.get("tag_name") or "").strip()
    if not version:
        raise SelfUpdateError(f"Failed to resolve latest release from {repo}")
    return version


def release_archive_name(version: str, target: str) -> str:
    return f"{BIN_NAME}-{version}-{target}.tar.gz"


def release_archive_url(repo: str, version: str, target: str) -> str:
    return f"https://github.com/{repo}/releases/download/{version}/{release_archive_name(version, target)}"


def download_release_archive(*, repo: str, version: str, target: str, destination: Path) -> None:
    request = urllib.request.Request(
        release_archive_url(repo, version, target),
        headers={"User-Agent": "orche-self-update"},
    )
    try:
        with urlopen(request, timeout=60) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except urllib.error.URLError as exc:
        raise SelfUpdateError(
            f"Failed to download {release_archive_name(version, target)}"
        ) from exc


def _replace_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)


def _resolve_extracted_source(temp_path: Path) -> Path:
    source_root = temp_path / BIN_NAME
    nested_executable = source_root / BIN_NAME
    if nested_executable.exists():
        return source_root

    if source_root.is_file():
        legacy_root = temp_path / ".orche-legacy"
        legacy_root.mkdir()
        legacy_executable = legacy_root / BIN_NAME
        shutil.copy2(source_root, legacy_executable)
        legacy_executable.chmod(0o755)
        return legacy_root

    raise SelfUpdateError(
        f"Downloaded archive did not contain {BIN_NAME} or {BIN_NAME}/{BIN_NAME}"
    )


def _safe_extract_archive(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        member_path = (destination / member.name).resolve()
        if member_path != root and root not in member_path.parents:
            raise SelfUpdateError(f"Refusing to extract archive member outside target directory: {member.name}")
    archive.extractall(destination)


def install_release_archive(
    *,
    archive_path: Path,
    version: str,
    target: str,
    repo: str,
    prefix: Path,
    install_root: Path,
) -> UpdateResult:
    with tempfile.TemporaryDirectory(prefix="orche-update-") as temp_dir:
        temp_path = Path(temp_dir)
        with tarfile.open(archive_path, "r:gz") as archive:
            _safe_extract_archive(archive, temp_path)

        source_root = _resolve_extracted_source(temp_path)

        install_root = install_root.expanduser().resolve()
        prefix = prefix.expanduser().resolve()
        release_dir = install_root / version / target
        executable_path = release_dir / BIN_NAME
        _replace_tree(source_root, release_dir)

        link_path = prefix / BIN_NAME
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.exists() and link_path.is_dir() and not link_path.is_symlink():
            raise SelfUpdateError(f"Refusing to replace directory: {link_path}")
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        os.symlink(executable_path, link_path)

        save_install_metadata(
            {
                "channel": INSTALL_CHANNEL,
                "repo": repo,
                "version": version,
                "target": target,
                "prefix": str(prefix),
                "link_path": str(link_path),
                "install_root": str(install_root),
                "executable_path": str(executable_path),
            }
        )

    return UpdateResult(
        version=version,
        target=target,
        link_path=link_path,
        install_root=install_root,
        updated=True,
    )


def perform_self_update(
    *,
    requested_version: Optional[str] = None,
    repo: Optional[str] = None,
) -> UpdateResult:
    metadata = load_install_metadata()
    if not metadata or str(metadata.get("channel") or "").strip() != INSTALL_CHANNEL:
        raise SelfUpdateError(
            "orche update is only supported for prebuilt binary installs managed by install.sh"
        )

    install_context = infer_install_context(metadata, repo=repo)
    if not metadata_matches_context(metadata, install_context):
        save_install_context(install_context)

    resolved_repo = install_context.repo
    target = install_context.target
    prefix = install_context.prefix
    install_root = install_context.install_root

    version = resolve_version(resolved_repo, requested_version)
    expected_link = prefix.resolve() / BIN_NAME
    expected_executable = install_root.resolve() / version / target / BIN_NAME
    current_version = install_context.version
    if current_version == version and expected_link.is_symlink():
        try:
            if expected_link.resolve() == expected_executable:
                return UpdateResult(
                    version=version,
                    target=target,
                    link_path=expected_link,
                    install_root=install_root.resolve(),
                    updated=False,
                )
        except OSError:
            pass

    with tempfile.TemporaryDirectory(prefix="orche-download-") as temp_dir:
        archive_path = Path(temp_dir) / release_archive_name(version, target)
        download_release_archive(
            repo=resolved_repo,
            version=version,
            target=target,
            destination=archive_path,
        )
        return install_release_archive(
            archive_path=archive_path,
            version=version,
            target=target,
            repo=resolved_repo,
            prefix=prefix,
            install_root=install_root,
        )
