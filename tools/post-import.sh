#!/usr/bin/env bash
# Sonarr/Radarr "On Import" custom script.
# Dopo che l'app sposta il file da torrents/ alla media library,
# qui ricreiamo un symlink relativo nel path originale del torrent
# in modo che qBittorrent possa continuare a seedare leggendo dal
# file (reale) nella media library.

set -euo pipefail

log() { echo "[post-import] $*"; }

if [[ -n "${sonarr_eventtype:-}" ]]; then
    app="sonarr"
    event="$sonarr_eventtype"
    src="${sonarr_episodefile_sourcepath:-}"
    dst="${sonarr_episodefile_path:-}"
elif [[ -n "${radarr_eventtype:-}" ]]; then
    app="radarr"
    event="$radarr_eventtype"
    src="${radarr_moviefile_sourcepath:-}"
    dst="${radarr_moviefile_path:-}"
else
    log "no sonarr/radarr env vars detected; skipping"
    exit 0
fi

case "$event" in
    Download) ;;
    Test)
        log "[$app] test event ok"
        exit 0
        ;;
    *)
        log "[$app] event=$event not handled; skipping"
        exit 0
        ;;
esac

if [[ -z "$src" || -z "$dst" ]]; then
    log "[$app] missing src or dst (src='$src' dst='$dst')"
    exit 1
fi

if [[ ! -f "$dst" ]]; then
    log "[$app] destination file not found: $dst"
    exit 1
fi

if [[ -e "$src" || -L "$src" ]]; then
    rm -f -- "$src"
fi

mkdir -p -- "$(dirname -- "$src")"
ln -srf -- "$dst" "$src"

log "[$app] linked: $src -> $(readlink -- "$src")"
