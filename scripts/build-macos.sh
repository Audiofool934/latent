#!/bin/bash
set -euo pipefail
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_configuration="${CONFIGURATION:-release}"
task_python="$task_root/.venv/bin/python"
if [[ ! -x "$task_python" ]]; then
    printf '%s\n' "Latent's local runtime is missing. Run uv sync in $task_root before building." >&2
    exit 1
fi
swift build --package-path "$task_root/macos" -c "$task_configuration" --product Latent
task_bin="$(swift build --package-path "$task_root/macos" -c "$task_configuration" --show-bin-path)"
task_app="$task_root/var/native/Latent.app"
mkdir -p "$task_app/Contents/MacOS" "$task_app/Contents/Resources"
cp "$task_bin/Latent" "$task_app/Contents/MacOS/Latent"
cp "$task_root/macos/Resources/Info.plist" "$task_app/Contents/Info.plist"
cp "$task_root/macos/Resources/Latent.icns" "$task_app/Contents/Resources/AppIcon.icns"
"$task_python" - "$task_app/Contents/Resources/LocalService.json" "$task_python" <<'PY'
import json
import sys
from pathlib import Path

from latent.cli import DEFAULT_STATE_DIR
from latent.gemini import GEMINI_STORE_NAME
from latent.workspace import DEFAULT_WORKSPACE_DIR

# Only local paths are packaged. Never copy shell environment or API credentials.
Path(sys.argv[1]).write_text(json.dumps({
    "python": sys.argv[2],
    "stateDirectory": str(DEFAULT_STATE_DIR.resolve()),
    "embeddingDirectory": str((DEFAULT_STATE_DIR / "embeddings" / GEMINI_STORE_NAME).resolve()),
    "workspaceDirectory": str(DEFAULT_WORKSPACE_DIR.resolve()),
}, indent=2) + "\n")
PY
codesign --force --sign - "$task_app"
touch "$task_app"
printf '%s\n' "$task_app"
