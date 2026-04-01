from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "orche"
CONFIG_FILE_NAME = "config.json"
HISTORY_DIR_NAME = "history"
META_DIR_NAME = "meta"
LOCKS_DIR_NAME = "locks"
BRIDGES_DIR_NAME = "bridges"
LOGS_DIR_NAME = "logs"


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()


def config_dir() -> Path:
    return xdg_config_home() / APP_NAME


def config_path() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def data_dir() -> Path:
    return xdg_data_home() / APP_NAME


def history_dir() -> Path:
    return data_dir() / HISTORY_DIR_NAME


def meta_dir() -> Path:
    return data_dir() / META_DIR_NAME


def locks_dir() -> Path:
    return data_dir() / LOCKS_DIR_NAME


def bridges_dir() -> Path:
    return data_dir() / BRIDGES_DIR_NAME


def logs_dir() -> Path:
    return data_dir() / LOGS_DIR_NAME


def orch_log_path() -> Path:
    return logs_dir() / "orche.log"


def ensure_directories() -> None:
    for path in (
        config_dir(),
        data_dir(),
        history_dir(),
        meta_dir(),
        locks_dir(),
        bridges_dir(),
        logs_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
