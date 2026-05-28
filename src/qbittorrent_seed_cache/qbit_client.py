"""Thin async client for the qBittorrent Web API (v2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TorrentInfo:
    """Subset of /api/v2/torrents/info we care about."""

    hash: str
    name: str
    save_path: str           # absolute path inside the qB container
    content_path: str        # primary file/folder
    size: int                # bytes
    upspeed: int             # B/s
    uploaded_session: int    # bytes since qB session start
    last_activity: int       # unix ts of last upload/download progress
    state: str

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "TorrentInfo":
        return cls(
            hash=d["hash"],
            name=d["name"],
            save_path=d["save_path"],
            content_path=d["content_path"],
            size=int(d["size"]),
            upspeed=int(d["upspeed"]),
            uploaded_session=int(d["uploaded_session"]),
            last_activity=int(d["last_activity"]),
            state=d["state"],
        )


class QbitClient:
    """One-shot async client. Re-authenticates on 403."""

    def __init__(self, *, name: str, url: str, username: str, password: str) -> None:
        self.name = name
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "QbitClient":
        self._client = httpx.AsyncClient(base_url=self._url, timeout=30.0)
        await self._login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _login(self) -> None:
        assert self._client is not None
        r = await self._client.post(
            "/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
            headers={"Referer": self._url},
        )
        r.raise_for_status()
        if r.text.strip() != "Ok.":
            raise RuntimeError(f"qB login failed for instance {self.name!r}")
        log.info("qbit.login.ok", instance=self.name)

    async def torrents(self) -> list[TorrentInfo]:
        assert self._client is not None
        r = await self._client.get("/api/v2/torrents/info")
        r.raise_for_status()
        return [TorrentInfo.from_api(d) for d in r.json()]

    async def torrent_files(self, infohash: str) -> list[dict[str, Any]]:
        assert self._client is not None
        r = await self._client.get(
            "/api/v2/torrents/files",
            params={"hash": infohash},
        )
        r.raise_for_status()
        return list(r.json())
