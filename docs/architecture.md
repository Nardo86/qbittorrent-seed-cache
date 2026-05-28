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

## Atomic transitions

### Promote (cold → hot)

1. `cp bulk_target ssd_target.tmp`  (full copy, fsync, fadvise sequential)
2. `rename ssd_target.tmp → ssd_target`  (atomic on a single fs)
3. `symlink ssd_target → link.tmp`
4. `rename link.tmp → link`  (atomic; readers see old-or-new, never missing)

Crash between step 2 and step 4: the SSD copy is orphaned but harmless. Next tick re-derives state from disk + qB and either re-points to the orphan or removes it.

### Demote (hot → cold)

1. Compute relative target from `link.parent` to `bulk_target`.
2. `symlink rel_target → link.tmp`
3. `rename link.tmp → link`  (atomic)
4. `rm -rf ssd_target` (after the symlink has been swapped — the SSD content is unreferenced)

Crash between step 3 and step 4: orphan SSD content. Cleaned up on next tick.

## Multi-qB

When two qB instances share the same content (e.g. one VPN, one direct), the `torrents/<release>/<file>` paths are *different* (different download dirs), but the symlink targets (bulk file or SSD copy) can be **the same**. The mover tracks tier state per `(instance, infohash)`, so each qB's symlink is retargeted independently — but a single SSD copy can back multiple symlinks. Disk accounting is therefore tracked by infohash, not by `(instance, infohash)`, to avoid double-counting.

> Status: the multi-qB consolidation logic is not yet implemented in the scaffold. The first version will treat each (instance, infohash) independently, duplicating SSD bytes if the same content is in two qB instances. A follow-up will deduplicate by `infohash`.

## Failure modes

| Failure                                | Recovery                                                                 |
|----------------------------------------|--------------------------------------------------------------------------|
| SSD dies entirely                      | All "hot" symlinks dangle. Re-run `migrate-hardlinks-to-symlinks.py` (or equivalent) to rebuild relative cold symlinks; the mover repopulates the SSD over the next ticks. |
| qB restarts (counters reset)           | State store detects `uploaded_session` going backwards and treats the new value as the delta from zero. Hotness score is briefly noisy until window refills. |
| Mover restarts                         | State persists in SQLite. Hotness score is restored from the rolling window. Worst case: one tick of stale data. |
| Bulk fs unmounted                      | Promotions fail loudly (source missing). Existing hot torrents keep seeding from SSD until they're demoted; demotion is blocked because the relative symlink would dangle. The daemon should refuse to operate (`bulk_root` check) until it's back. |
| SSD fills up                           | `quota.headroom_bytes` goes negative. No new promotions. Coldest hot torrents are demoted next tick. |

## Why not move the real file?

We considered an alternative scheme where the real file *moves* to the SSD when hot and back to the bulk fs when cold (the symlink in `Films/` would invert direction). It was rejected:

- Emby/Jellyfin/Plex scan media libraries by walking real files. Moves cause re-scans, thumbnail regenerations, occasionally lost watch state.
- Multi-qB hardlink relationships (the same inode living in two qB download dirs *and* the media library) would need to be reconstructed after every move.
- Promotion and demotion would both require an HDD write — defeating the original goal of letting the HDD sleep.

Keeping the real file pinned in the bulk fs and using the SSD purely as a *copy* cache is simpler and respects the existing media-library structure.
