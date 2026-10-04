#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APPDATA=/mnt/user/appdata/bitcoin-paper-bot-v3
IMAGE=ghcr.io/gadonk/bitcoin-paper-bot:v3
[[ "$(id -u)" == 0 ]] || { echo 'Run as root in Unraid Terminal.' >&2; exit 1; }
# Requires the first published image to be public. Fail before changing files otherwise.
docker pull "$IMAGE"
install -d -m 0770 -o 99 -g 100 "$APPDATA/config" "$APPDATA/data"
if [[ ! -f "$APPDATA/config/config.json" ]]; then
  [[ ! -e "$APPDATA/data/paper.json" ]] || { echo 'Existing trading state has no config; restore its config first.' >&2; exit 1; }
  install -m 0660 -o 99 -g 100 "$SOURCE_DIR/config-v3.json" "$APPDATA/config/config.json"
fi
docker run --rm --read-only --user 99:100 \
  --mount "type=bind,src=$APPDATA/config,dst=/config,readonly" \
  --mount "type=bind,src=$APPDATA/data,dst=/data,readonly" \
  --entrypoint python "$IMAGE" -c '
import json
from pathlib import Path
from bot import config, Paper
c=config("/config/config.json")
if c.get("strategy") != "ai_v3":
    raise ValueError("V3 config required")
p=Path("/data/paper.json")
Paper(c,json.loads(p.read_text()) if p.exists() else None)
print("V3 configuration OK")
'
TEMPLATES=/boot/config/plugins/dockerMan/templates-user
install -d -m 0755 "$TEMPLATES"
install -m 0644 "$SOURCE_DIR/my-bitcoin-paper-bot-v3.xml" "$TEMPLATES/my-bitcoin-paper-bot-v3.xml"
echo 'Ready: Docker > Add Container > bitcoin-paper-bot-v3 > Apply.'
echo 'Existing v3: edit its Repository to ghcr.io/gadonk/bitcoin-paper-bot:v3 and Apply; keep existing paths and settings.'
echo 'Future releases: use Unraid Check for Updates, then Update.'
