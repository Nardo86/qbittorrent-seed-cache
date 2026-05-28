# qbittorrent-seed-cache

Hot/cold tiering daemon for qBittorrent seeds: keep an SSD copy of the most actively-uploading torrents while the canonical files live on slow bulk storage (HDD / NAS / NFS). Aimed at home servers where a NAS HDD is kept spinning 24/7 by a long tail of low-rate seeds.

> **Status:** scaffold. The promotion/demotion logic and hotness scoring are in active development.

## What it does

Given:

- a **bulk** filesystem where the real media files live (`Films/...`, `SerieTV/...`) — typically a NAS over NFS,
- a **torrents** directory used by qBittorrent as the download/seed path, where every entry is a symlink to the bulk file (set up via the included [`tools/post-import.sh`](tools/post-import.sh)),
- a small **SSD** cache directory with the *same absolute path* on host and inside every relevant container (`/var/lib/seed-cache` by default),

the daemon:

1. Polls qBittorrent Web API for upload metrics (across one or more instances).
2. Maintains a rolling window of upload activity per torrent in a small SQLite store.
3. Promotes "hot" torrents to the SSD: `cp` the bulk file → SSD, then **atomically retarget** the symlink in `torrents/` to the SSD copy. One read from the HDD; after that all seeding I/O hits the SSD.
4. Demotes "cold" torrents: retarget the symlink back to the bulk file, delete the SSD copy. Zero writes to the HDD.
5. Respects a soft quota (default 100 GB) using a "young + sticky" policy to avoid flapping.

The real file never moves. If the SSD dies, you regenerate the symlinks from the bulk filesystem and you are back to a fully-cold state.

## Why

A small NAS (e.g. Synology DS214play) with no SSD cache slot will keep its HDDs spinning forever if you seed long-tail torrents from it. Moving the seed I/O to a host-local SSD lets the bulk disk spin down most of the time, without breaking the media layout that Sonarr/Radarr/Emby expect.

## Architecture

```
        bulk fs (HDD/NFS)                   SSD (host-local)
        ┌──────────────────┐               ┌────────────────────────┐
        │ Films/X.mkv      │◀──── cp ──────│ seed-cache/<hash>/X.mkv │
        └─────────▲────────┘               └────────────▲───────────┘
                  │ symlink                              │ symlink
                  │ (cold)                               │ (hot)
                  │                                      │
        ┌─────────┴──────────────────────────────────────┘
        │ torrents/<release>/X.mkv (symlink, retargeted by mover)
        │       │
        │       ▼
        │   qBittorrent reads from here, never knows the difference
        └────────────────────────────────────────────────────
```

See [docs/architecture.md](docs/architecture.md) for the full design (atomic transitions, multi-qB handling, failure modes).

## Quick start

```bash
docker run -d --name seed-cache \
  -v /var/lib/seed-cache:/var/lib/seed-cache \
  -v /mnt/media:/mnt/media \
  -v /path/to/config.yaml:/etc/qbittorrent-seed-cache/config.yaml:ro \
  ghcr.io/<your-user>/qbittorrent-seed-cache:latest
```

Full example with bind-mount layout for qB / Sonarr / Radarr: [`docker-compose.example.yaml`](docker-compose.example.yaml).

## Configuration

See [`config.example.yaml`](config.example.yaml) for the annotated reference. Minimal:

```yaml
ssd_cache_dir: /var/lib/seed-cache
quota_gb: 100

instances:
  - name: qbittorrent
    url: http://qbittorrent:8080
    username: admin
    password: adminadmin

hotness:
  window_days: 14
  promote_min_upload_mb: 50
  demote_max_upload_mb: 5

poll_interval_sec: 300
```

## Setup helpers

The [`tools/`](tools/) directory bundles one-shot helpers for the surrounding setup:

- [`post-import.sh`](tools/post-import.sh) — Sonarr/Radarr `Custom Script`: after each import, replaces the file in `torrents/` with a relative symlink to the bulk-fs file. Works with hardlinks enabled (converts them) or with pure Move.
- [`register-custom-script.sh`](tools/register-custom-script.sh) — registers `post-import.sh` in Sonarr+Radarr through the REST API (custom-script config lives in the app's SQLite, not in YAML, so it needs a one-shot HTTP call).
- [`migrate-hardlinks-to-symlinks.py`](tools/migrate-hardlinks-to-symlinks.py) — one-shot migration: converts existing hardlinks in `torrents/` to relative symlinks. Run once on the backlog before starting the mover.

## Status

| Component | State |
|---|---|
| Setup helpers (`tools/`)        | ✅ used in production on the author's setup |
| qB Web API client               | 🚧 scaffold |
| Hotness rolling window          | 🚧 scaffold |
| Atomic promote/demote           | 🚧 scaffold |
| Quota & candidate selection     | 🚧 scaffold |
| Daemon loop                     | 🚧 scaffold |
| Tests                           | 🚧 smoke only |
| CI / image publishing           | 🚧 workflow drafted |

## License

MIT — see [LICENSE](LICENSE).
