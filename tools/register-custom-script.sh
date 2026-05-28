#!/usr/bin/env bash
# Register tools/post-import.sh as a CustomScript notification in Sonarr
# and/or Radarr. The CustomScript config lives in the app's SQLite DB,
# not in YAML, so we configure it via the REST API.
#
# Usage:
#   ./register-custom-script.sh sonarr http://localhost:8989 /path/to/config.xml /in-container/path/to/post-import.sh
#   ./register-custom-script.sh radarr http://localhost:7878 /path/to/config.xml /in-container/path/to/post-import.sh
#
# Argument 3 is the path to the Sonarr/Radarr `config.xml` (used to read
# the API key non-interactively). Argument 4 is the path where the
# *arr container will execute post-import.sh — i.e. an *in-container*
# path; mount tools/ into the container and pass that mount point here.

set -euo pipefail

APP="${1:?app required: sonarr|radarr}"
URL="${2:?url required, e.g. http://localhost:8989}"
CONFIG_XML="${3:?path to Sonarr/Radarr config.xml required}"
SCRIPT_PATH="${4:?in-container path to post-import.sh required}"

API_KEY=$(grep -oP '<ApiKey>\K[^<]+' "$CONFIG_XML")
if [[ -z "$API_KEY" ]]; then
    echo "Could not read API key from $CONFIG_XML" >&2
    exit 1
fi

case "$APP" in
    sonarr)
        API_VERSION=v3
        BODY=$(cat <<EOF
{
  "name": "post-import-symlink",
  "implementation": "CustomScript",
  "configContract": "CustomScriptSettings",
  "fields": [{"name": "path", "value": "${SCRIPT_PATH}"}],
  "onDownload": true,
  "onUpgrade": true,
  "onGrab": false,
  "onImportComplete": false,
  "onRename": false,
  "onSeriesAdd": false,
  "onSeriesDelete": false,
  "onEpisodeFileDelete": false,
  "onEpisodeFileDeleteForUpgrade": false,
  "onHealthIssue": false,
  "includeHealthWarnings": false,
  "onHealthRestored": false,
  "onApplicationUpdate": false,
  "onManualInteractionRequired": false,
  "tags": []
}
EOF
)
        ;;
    radarr)
        API_VERSION=v3
        BODY=$(cat <<EOF
{
  "name": "post-import-symlink",
  "implementation": "CustomScript",
  "configContract": "CustomScriptSettings",
  "fields": [{"name": "path", "value": "${SCRIPT_PATH}"}],
  "onDownload": true,
  "onUpgrade": true,
  "onGrab": false,
  "onMovieAdded": false,
  "onMovieDelete": false,
  "onMovieFileDelete": false,
  "onMovieFileDeleteForUpgrade": false,
  "onRename": false,
  "onHealthIssue": false,
  "includeHealthWarnings": false,
  "onHealthRestored": false,
  "onApplicationUpdate": false,
  "onManualInteractionRequired": false,
  "tags": []
}
EOF
)
        ;;
    *)
        echo "Unknown app: $APP (use sonarr|radarr)" >&2
        exit 2
        ;;
esac

curl -fsS -X POST \
    -H "X-Api-Key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    "${URL}/api/${API_VERSION}/notification" \
    -d "${BODY}"
echo
echo "Registered post-import-symlink on ${APP}."
