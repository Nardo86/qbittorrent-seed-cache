# Architecture

## Path discipline

Every component that needs to resolve the `torrents/<release>/<file>` symlinks must see the SSD cache at the **same absolute path** as the host:

| Component        | Mount type | Notes                                                              |
|------------------|------------|--------------------------------------------------------------------|
| host             | -          | canonical absolute path, e.g. `/var/lib/seed-cache`                |
| qbittorrent (*)  | bind, rw   | identical path; qB reads from here while seeding                   |
| sonarr / radarr  | bind, ro   | identical path; needed because they read the symlink targets       |
| seed-cache mover | bind, rw   | identical path; this container writes the SSD copies               |

(*) If you run multiple qB instances (e.g. one behind a VPN and one not), they all bind the same SSD path.

The reason: a "hot" torrent's symlink points to the **absolute** SSD path. If qB and the host don't agree on what that path means, the symlink dangles.

The opposite is true for "cold" symlinks: they point into the bulk filesystem with a **relative** target. As long as `torrents/` and the bulk subtrees (e.g. `Films/`) live under the same shared mount, the relative symlink works regardless of what each container calls the mount root.

> **The mover needs write access to the bulk `torrents/` subtree.** Promote and demote both *rewrite* the `torrents/<release>/<file>` symlink (swapping it between the relative bulk target and the absolute SSD target). Those symlinks live in the bulk filesystem (under a `managed_paths` entry), so the mover's bind mount of that subtree must be **read-write**. Mounting the bulk read-only makes every retarget fail with `EROFS` *after* the SSD copy is made — promotions never complete, orphan SSD dirs accumulate, and the cache re-copies the same data each tick. Mount the rest of the bulk read-only if you like, but the managed subtree must be rw (see `docker-compose.example.yaml`).

## Atomic transitions

### Promote (cold → hot)

0. Write `<infohash>/.qbsc-promoting.json` (promotion in progress).
1. Copy `bulk_target → .ssd_target.qbsc-partial` in 8 MiB chunks (fadvise sequential, fsync). A `.json` next to the partial records source path/size/mtime and the kernel boot id.
2. `rename .qbsc-partial → ssd_target`  (atomic on a single fs)
3. Write the recovery sidecar `<infohash>/.qbsc-meta.json` (atomic tmp+rename, fsync) recording the `link → bulk_target` map, then remove `.qbsc-promoting.json`.
4. `symlink ssd_target → link.tmp`
5. `rename link.tmp → link`  (atomic; readers see old-or-new, never missing)

The sidecar (step 3) is written **before** any symlink is retargeted (steps 4–5). This is the key durability invariant: from the moment a link can point into the SSD, the filesystem already records where that link's bulk file lives. A crash at any point leaves state reconcilable from disk alone (see [State durability & recovery](#state-durability--recovery)).

Crash or stop during step 1: the partial and the intent marker stay. The tick's orphan reclaimer keeps a dir with an intent marker and no sidecar while the torrent is still in qB and the first attempt is < 24 h old, and the next promotion **resumes** the partial — but only if it was written for the same source during the same boot (within one boot the page cache guarantees every byte a killed process wrote; after a host reboot the tail may be lost, so it starts over). SIGTERM sets a flag checked between chunks, so a `docker stop` interrupts the copy cleanly within the stop timeout.

> Before this, SIGTERM only stopped the event loop while the copy thread kept going until Docker's SIGKILL; the random-named tmp file was left behind, the next start reclaimed the whole `<infohash>/` dir as an orphan (including already-complete files of a multi-file torrent) and copied from byte 0 again.

Crash between step 2 and step 5: the SSD copy (and possibly the sidecar) is orphaned but harmless. Next tick / next startup re-derives state from disk + qB and either re-points to the orphan or removes it.

### Demote (hot → cold)

1. Compute relative target from `link.parent` to `bulk_target`.
2. `symlink rel_target → link.tmp`
3. `rename link.tmp → link`  (atomic)
4. `rm -rf ssd_target` (after the symlink has been swapped — the SSD content is unreferenced)

Crash between step 3 and step 4: orphan SSD content. Cleaned up on next tick.

Step 1 also retargets every link of the persisted map (DB row, else sidecar) that is not among the live layouts but still resolves into the dir about to be removed. Demotions and displacements are skipped altogether in a tick where any qB instance failed to poll: that instance's upload would be missing from the score and its symlinks from the layouts — removing the shared SSD dir would leave them dangling with the mapping gone.

### Displacement (eviction when the cache is full)

Plain demotion only retires torrents that have themselves gone cold (below
`demote_max_upload_mb`). A cache full of *lukewarm* torrents — above the demote
threshold but far less valuable than the best cold candidates — therefore never
makes room for a more valuable torrent: there is no headroom and nothing is
demotable. Displacement closes that gap. Per tick, between the demote and
promote steps, the daemon may evict the **least dense hot** torrents so the
freed space lets the **densest cold** candidates be promoted.

Value is **density** — upload/day per byte of SSD occupied — not absolute rate.
The cache is size-constrained, so what should be maximised is upload served per
GB of cache. A 60 GB filmography uploading 30 GB/day (density 0.5) is a worse
occupant than a 2 GB release uploading 5 GB/day (density 2.5): caching the pack
would evict several of the small releases and serve *less* from the SSD. Ranking
by density also maximises the chance that one of qB's few active-upload slots is
served from the SSD rather than the HDD.

Guards against thrashing:

- **Hysteresis:** a hot torrent `H` is evicted only for a cold candidate `C`
  with `density(C) ≥ density(H) × displacement_factor` (default 2.0).
- **Time gates:** victims must have been hot ≥ `min_hot_minutes`; promoted
  torrents must have been cold ≥ `min_cold_minutes`. A just-evicted torrent
  cannot return until it ages back through the cold gate.
- **Never evict for nothing:** evictions are committed only once they actually
  let a candidate fit. A target too large to free room for (within
  `max_displacements_per_tick` evictions) is skipped, and the next, smaller
  candidate is tried instead — so a giant filmography can't stall the rebalance
  or cost evictions that buy no promotion.

## Multi-qB

When two qB instances share the same content (e.g. one VPN, one direct), the `torrents/<release>/<file>` paths are *different* (different download dirs), but the symlink targets (bulk file or SSD copy) can be **the same**. Tier state is tracked **per-infohash** (not per `(instance, infohash)`), so a single SSD copy can back multiple symlinks across instances. Disk accounting is by infohash, so the same content in two qB instances does not double-count against the quota. When the mover promotes a hot torrent, every instance that holds the same infohash gets its symlink retargeted in lockstep.

## State durability & recovery

The SSD cache is disposable; the canonical file always lives in the bulk filesystem. The one piece of state that is *not* derivable from the disposable cache is the `link → bulk_target` mapping: once a torrent is hot, its symlink points into the SSD, so `readlink` no longer reveals the original bulk file.

That mapping is persisted in **two** places, kept in sync:

- **The SQLite DB** (`tier.bulk_targets`) — the fast path read on every tick.
- **A per-torrent sidecar** `<ssd_cache_dir>/<infohash>/.qbsc-meta.json` — the filesystem's own copy, so the cache is self-describing even if the DB is lost.

```json
{
  "schema_version": 1,
  "infohash": "abc123…",
  "since_ts": 1700000000,
  "ssd_bytes": 12345678,
  "bulk_targets": { "<link path>": "<bulk file path>", … }
}
```

### Startup reconciliation

Before the first poll, `reconcile_startup` aligns the DB with the filesystem:

| Scenario | What it does |
|----------|--------------|
| **DB fresh / lost, SSD intact** | Rebuilds the hot tier rows from the sidecars. Without this the daemon would think the SSD is empty and over-promote on top of the existing cache until the disk fills. |
| **SSD dir deleted, DB intact** | The hot row's `<infohash>/` dir is gone, so its symlinks dangle. Retargets them back to the bulk fs (from `bulk_targets`) and drops the tier row — a demotion with nothing to delete. |
| **SSD dir with no recoverable mapping** | Deferred — reconcile can't tell a safe orphan from a real anomaly without qB's live data, so it leaves the dir for the first tick to judge (see below). |

A currently-hot torrent that the DB knows about but that predates the sidecar mechanism gets a sidecar written from the DB row (forward migration), so a *future* DB loss is covered.

### Orphan reclamation and the anomaly marker (tick-time)

An SSD `<infohash>/` dir with no recoverable mapping is either a **safe orphan** (nothing references it — e.g. a demote that crashed after retargeting the symlink to bulk but before the `rm`) or a **genuine anomaly** (a live symlink still points into it but the link→bulk mapping is gone). Telling them apart needs qB's live file list, which only exists during a tick — so the tick owns both:

- **Reclamation** (`_cleanup_fs_orphans`): a dir is *in use* iff its infohash is hot in the DB **or** a live qB symlink resolves into it this tick. Anything else is removed — safe by construction, since nothing references it.
- **Anomaly** (`anomaly.py`): a live symlink that resolves into the SSD (dangling or not) whose link is in neither the hot DB row nor the sidecar. Detection is per link. The affected torrents are **quarantined** for the tick (never promoted, demoted or displaced — a demote would `rm` the dir under a still-working unmapped link) and the bytes of their SSD dirs count against the quota. The marker `.qbsc-anomaly` lists `infohash<TAB>link`, keeps `first_seen` across restarts and is cleared once nothing is left; the `tick.anomaly_present` error is logged when the set changes and then once per `anomaly_log_interval_sec` (default 1 h). `suspend_promotions_on_anomaly: true` additionally pauses all promotions/displacements while any anomaly exists.

Reclamation and the marker update run only when **every** instance polled cleanly that tick — a down instance would make the "referenced" set incomplete and risk reclaiming a dir its torrents still use. The same guard protects the hot-tier orphan cleanup.

### Health vs. data anomalies

The container healthcheck answers "is the daemon alive?": SSD dir present and writable, heartbeat (`.qbsc-heartbeat`, deadline = now + 3 × poll interval + 10 min, refreshed every tick and every 30 s during a copy) not stale. The anomaly is a data problem that a restart cannot fix, so it is printed but does not fail the check unless `QBSC_HEALTHCHECK_FAIL_ON_ANOMALY=1`.

> **Incident (2026-09/10):** 37 torrents had 190 symlinks dangling into deleted SSD dirs with no mapping. The marker made the container unhealthy, `autoheal` restarted it every ~3 minutes (≈6 000 restarts in a month), each restart killed the in-flight 20 GB promotion, which the next start reclaimed and restarted from zero (one successful promotion in a month), and the resolver logged ~1.15 M per-file errors. How the mapping was lost is not known (the logs from before were gone); the demote-with-a-failed-poll path above produces exactly that state and is now closed.

### Repairing lost mappings (`repair-dangling`)

`qbittorrent-seed-cache repair-dangling` lists the anomalous links with a bulk candidate: a regular file of the exact same size under `managed_paths` whose content matches the torrent's SHA-1 piece hashes (a few whole pieces inside the file, offset from qB's file order cross-checked with `piece_range`). Statuses: `verified` (one matching file; the media-library file wins over an identical real copy in another qB save dir), `ambiguous`, `mismatch`, `unverifiable` (no whole piece inside the file, or v2-only torrent), `no_candidate`, `too_many_candidates`. Read-only by default; `--apply` retargets only `verified` links (relative bulk symlink, same as `retarget_to_bulk`) after checking they did not change since the scan; `--recheck` asks qB to recheck fully-repaired torrents. It never deletes anything and opens the DB read-only.

## State DB size

Snapshots are one row per (instance, torrent) per poll. At a 60 s poll with ~400 torrents on 2 instances and a 14-day window that is ~8 M rows. Every tick prunes rows older than the window; every hour the daemon also

- thins rows older than `snapshot_full_resolution_hours` (24) to the last row of each `snapshot_bucket_minutes` (60) bucket, keeping both rows around every counter reset. Removed rows always lie inside a non-decreasing run of `uploaded_session`, so the summed deltas the score uses are unchanged (only a window edge can shift by one bucket);
- VACUUMs the file (and truncates the WAL) when free pages are ≥ 50 % and ≥ 32 MiB — SQLite never shrinks the file after a DELETE otherwise.

The redundant `idx_snapshots_recent` index (same columns as the primary key) is dropped on open.

> **Why this matters:** the original incident was a daemon migration that started against a fresh DB while ~170 GB of already-promoted content sat on the SSD. The DB reported 0 bytes used, so the daemon promoted another ~150 GB on top and filled the disk. The sidecar + reconciliation closes that hole: the filesystem alone now carries enough state to rebuild the accounting.

## Failure modes

| Failure                                | Recovery                                                                 |
|----------------------------------------|--------------------------------------------------------------------------|
| SSD dies entirely                      | All "hot" symlinks dangle. Re-run `migrate-hardlinks-to-symlinks.py` (or equivalent) to rebuild relative cold symlinks; the mover repopulates the SSD over the next ticks. |
| State DB lost / replaced / corrupt     | Startup reconciliation rebuilds the hot tier rows from the on-disk sidecars (see [State durability & recovery](#state-durability--recovery)). Accounting is restored before the first promotion, so the daemon does not over-promote. |
| SSD cache dir(s) deleted, DB intact    | Startup reconciliation retargets the affected symlinks back to bulk and drops the tier rows. |
| Crashed demote (symlink→bulk done, SSD dir left) | Reclaimed automatically at tick time once it's unreferenced (`_cleanup_fs_orphans`). |
| Live symlink → SSD with lost mapping   | Flagged at tick time: torrent quarantined, anomaly marker + rate-limited error, container stays healthy. Repair with `repair-dangling`. |
| Daemon hung (e.g. stuck NFS read)      | Heartbeat goes stale → healthcheck unhealthy. |
| Restart / stop during a promotion copy | Partial copy kept and resumed on the next start (same boot). |
| qB restarts (counters reset)           | State store detects `uploaded_session` going backwards and treats the new value as the delta from zero. Hotness score is briefly noisy until window refills. |
| Mover restarts                         | State persists in SQLite **and** the sidecars. Hotness score is restored from the rolling window. Worst case: one tick of stale data. |
| Bulk fs unmounted                      | Promotions fail loudly (source missing). Existing hot torrents keep seeding from SSD until they're demoted; demotion is blocked because the relative symlink would dangle. The daemon should refuse to operate (`bulk_root` check) until it's back. |
| SSD fills up                           | `quota.headroom_bytes` goes negative. No new promotions. Coldest hot torrents are demoted next tick. |

## Why not move the real file?

We considered an alternative scheme where the real file *moves* to the SSD when hot and back to the bulk fs when cold (the symlink in `Films/` would invert direction). It was rejected:

- Emby/Jellyfin/Plex scan media libraries by walking real files. Moves cause re-scans, thumbnail regenerations, occasionally lost watch state.
- Multi-qB hardlink relationships (the same inode living in two qB download dirs *and* the media library) would need to be reconstructed after every move.
- Promotion and demotion would both require an HDD write — defeating the original goal of letting the HDD sleep.

Keeping the real file pinned in the bulk fs and using the SSD purely as a *copy* cache is simpler and respects the existing media-library structure.
