"""Typed configuration loaded from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_core.core_schema import ValidationInfo


class InstanceConfig(BaseModel):
    name: str
    url: str
    username: str
    password: SecretStr
    # Map container-side absolute paths (as seen by qB) to host-side absolute
    # paths (as seen by the mover). Longest-prefix match wins. The mover's
    # bind-mount layout must allow the host-side paths to be resolved.
    path_map: dict[str, str] = {}


class HotnessConfig(BaseModel):
    window_days: int = Field(14, gt=0)
    promote_min_upload_mb: float = Field(50, ge=0)
    demote_max_upload_mb: float = Field(5, ge=0)
    min_hot_minutes: int = Field(60, ge=0)
    min_cold_minutes: int = Field(120, ge=0)

    @field_validator("demote_max_upload_mb")
    @classmethod
    def _check_thresholds(cls, v: float, info: ValidationInfo) -> float:
        promote = info.data.get("promote_min_upload_mb")
        if promote is not None and v >= promote:
            raise ValueError("demote_max_upload_mb must be < promote_min_upload_mb (asymmetric)")
        return v


class Config(BaseModel):
    ssd_cache_dir: Path
    quota_gb: float = Field(100, gt=0)
    min_free_gb: float = Field(10, ge=0)
    # Per-torrent size cap. Torrents above this size are never promoted, even
    # if they're the hottest candidate. Useful for huge multi-file torrents
    # (whole TV series, filmographies) that would dominate the SSD quota.
    # None = no cap.
    max_torrent_size_gb: float | None = Field(None, gt=0)

    bulk_root: Path
    managed_paths: list[Path]

    instances: list[InstanceConfig]

    hotness: HotnessConfig = Field(default_factory=lambda: HotnessConfig())

    poll_interval_sec: int = Field(300, gt=0)
    state_db: Path = Path("/var/lib/seed-cache/state.db")

    log_format: Literal["json", "console"] = "json"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    dry_run: bool = False
    max_concurrent_promotions: int = Field(1, gt=0)

    @field_validator("instances")
    @classmethod
    def _unique_instance_names(cls, v: list[InstanceConfig]) -> list[InstanceConfig]:
        names = [i.name for i in v]
        if len(names) != len(set(names)):
            raise ValueError("instance names must be unique")
        return v


def load_config(path: Path) -> Config:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
