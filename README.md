# qbittorrent-seed-cache

Hot/cold tiering daemon for qBittorrent seeds: keep an SSD copy of the most actively-uploading torrents while the canonical files live on slow bulk storage (HDD / NAS / NFS). Aimed at home servers where a NAS HDD is kept spinning 24/7 by a long tail of low-rate seeds.

> **Status:** community-maintained, no active testing. In production on the
> author's home server (two qBittorrent instances, ~300 torrents, NFS bulk over
> a Synology DS214play). Issues and PRs welcome.

## What it does

Given:

- a **bulk** filesystem where the real media files live (`Movies/...`, `TV/...`) — typically a NAS over NFS,
- a **torrents** directory used by qBittorrent as the download/seed path, where every entry is a symlink to the bulk file (set up via the included [`tools/post-import.sh`](tools/post-import.sh)),
- a small **SSD** cache directory with the *same absolute path* on host and inside every relevant container (`/var/lib/seed-cache` by default),

the daemon:

1. Polls qBittorrent Web API for upload metrics (across one or more instances).
2. Maintains a rolling window of upload activity per torrent in a small SQLite store.
3. Promotes "hot" torrents to the SSD: `cp` the bulk file → SSD, then **atomically retarget** the symlink in `torrents/` to the SSD copy. One read from the HDD; after that all seeding I/O hits the SSD.
4. Demotes "cold" torrents: retarget the symlink back to the bulk file, delete the SSD copy. Zero writes to the HDD.
5. Respects a soft quota (default 100 GB) using a "young + sticky" policy to avoid flapping.
6. When the quota is full of lukewarm-but-not-cold torrents, **evicts the least *dense* hot torrents to make room for denser cold ones** (displacement). "Density" is upload/day per byte of SSD occupied, so a huge low-density pack can't push out several small high-density releases that together serve far more. A cold torrent only displaces a hot one when its density is at least `displacement_factor`× the victim's — hysteresis that keeps similar-density torrents from swapping SSD slots every tick.

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

### Durability & recovery

The SSD cache is disposable, but the `link → bulk file` mapping is not. It is persisted both in the state DB and in a per-torrent sidecar (`<ssd_cache_dir>/<infohash>/.qbsc-meta.json`), so the cache is **self-describing**. At startup the daemon reconciles the two:

- **DB lost or replaced** → hot tier rows are rebuilt from the sidecars (no over-promoting on top of an SSD it forgot about).
- **SSD dir deleted** → the affected symlinks are retargeted back to bulk and the tier row dropped.
- **Both lost** (a live symlink into the SSD whose mapping is in neither place) → the affected torrents are **quarantined** (never promoted/demoted), an anomaly marker lists the links, and an error is logged when the set changes and then hourly. Repair them with [`repair-dangling`](#repairing-lost-mappings).
- **Restart mid-promotion** → the partial copy is resumed, not thrown away (same source, same boot).

See [State durability & recovery](docs/architecture.md#state-durability--recovery).

### Healthcheck

The image's healthcheck is a **liveness** check: it fails only if the SSD dir is missing/unwritable or the daemon's heartbeat (`.qbsc-heartbeat`, refreshed every tick and during long copies) is stale. A data anomaly is printed in the health output (`docker inspect --format '{{json .State.Health}}' seed-cache`) but does **not** make the container unhealthy, because a restart cannot fix it — with an `autoheal`-style sidecar it used to cause a kill/restart loop that also killed every in-flight promotion. Set `QBSC_HEALTHCHECK_FAIL_ON_ANOMALY=1` to fail on anomalies anyway (only if nothing auto-restarts on unhealthy).

### Repairing lost mappings

```bash
docker exec seed-cache qbittorrent-seed-cache repair-dangling            # read-only table
docker exec seed-cache qbittorrent-seed-cache repair-dangling --json     # same, JSON
docker exec seed-cache qbittorrent-seed-cache repair-dangling --apply --recheck
```

For every symlink into the SSD with no known mapping, it looks for files of the exact same size under `managed_paths` and **verifies their content against the torrent's SHA-1 piece hashes** from qB. Only links with exactly one verified file (the media-library copy wins over an identical real copy inside another qB save dir) are retargeted by `--apply`, to a relative bulk symlink; nothing is deleted, real files are never touched, links changed since the scan are skipped. `--recheck` asks qB to recheck the fully-repaired torrents (they sit in `missingFiles`). The daemon clears the marker on its next tick.

## Quick start

```bash
docker run -d --name seed-cache \
  --network host \
  -v /var/lib/seed-cache:/var/lib/seed-cache \
  -v /mnt/media:/mnt/media:ro \
  -v /path/to/config.yaml:/etc/qbittorrent-seed-cache/config.yaml:ro \
  nardo86/qbittorrent-seed-cache:latest
```

Host networking is the simplest way to let the daemon reach qBittorrent's
Web API on the host (e.g. `http://localhost:8080`) while still seeing the
SSD bind mount at the same absolute path as qBittorrent itself.

Multi-arch image: `linux/amd64`, `linux/arm64`.

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
  # A cold torrent must have >= this multiple of a hot torrent's density
  # (upload/day per GB) to evict it when the cache is full. >1 = hysteresis.
  displacement_factor: 2.0

poll_interval_sec: 300
# Max hot torrents evicted per tick by the displacement policy.
max_displacements_per_tick: 8
```

The state DB keeps the hotness window (`window_days`); rows older than a day are thinned hourly to one per hour per torrent (exactly preserving the upload sums the score uses) and the file is VACUUMed when mostly free pages, so it stays in the tens-to-low-hundreds of MB instead of growing to GBs at a short poll interval.

## Setup helpers

The [`tools/`](tools/) directory bundles one-shot helpers for the surrounding setup:

- [`post-import.sh`](tools/post-import.sh) — Sonarr/Radarr `Custom Script`: after each import, replaces the file in `torrents/` with a relative symlink to the bulk-fs file. Works with hardlinks enabled (converts them) or with pure Move.
- [`register-custom-script.sh`](tools/register-custom-script.sh) — registers `post-import.sh` in Sonarr+Radarr through the REST API (custom-script config lives in the app's SQLite, not in YAML, so it needs a one-shot HTTP call).
- [`migrate-hardlinks-to-symlinks.py`](tools/migrate-hardlinks-to-symlinks.py) — one-shot migration: converts existing hardlinks in `torrents/` to relative symlinks. Run once on the backlog before starting the mover.

## Status

| Component | State |
|---|---|
| Setup helpers (`tools/`)        | ✅ used in production |
| qB Web API client               | ✅ qB v4 & v5 (cookie auth) |
| Hotness rolling window          | ✅ EMA with reset detection |
| Atomic promote/demote           | ✅ tested against live qB |
| Quota & candidate selection     | ✅ per-infohash, multi-instance dedup |
| Daemon loop                     | ✅ demote → displace → headroom → promote |
| Crash / DB-loss recovery        | ✅ FS sidecars + startup reconciliation |
| Lost-mapping repair             | ✅ `repair-dangling` (piece-hash verified, dry run by default) |
| Resumable promotions            | ✅ partial copy survives restarts |
| Tests                           | ✅ pytest, incl. integration |
| CI / image publishing           | ✅ multi-arch (amd64, arm64) |

## Notes

- **Built with help from Claude (Anthropic).** Review the configuration before production use. No warranty.
- Image: <https://hub.docker.com/r/nardo86/qbittorrent-seed-cache>

## Support

⭐ Star • 🐛 Issue • 🔧 PR • ☕ <https://paypal.me/ErosNardi>

## License

MIT — see [LICENSE](LICENSE).
