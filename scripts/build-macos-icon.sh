#!/bin/bash
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_source="$task_root/assets/brand/Latent-Mark-v1.svg"
task_output="$task_root/macos/Resources/Latent.icns"

if ! command -v rsvg-convert >/dev/null; then
    printf '%s\n' 'Regenerating the icon requires librsvg (rsvg-convert).' >&2
    exit 1
fi

task_tmp="$(mktemp -d "${TMPDIR:-/tmp}/latent-icon.XXXXXX")"
trap 'rm -rf "$task_tmp"' EXIT
task_iconset="$task_tmp/Latent.iconset"
mkdir "$task_iconset"

rsvg-convert --format pdf "$task_source" --output "$task_tmp/mark.pdf"
swift "$task_root/scripts/render-macos-icon.swift" "$task_tmp/mark.pdf" "$task_iconset"

iconutil --convert icns "$task_iconset" --output "$task_tmp/Latent.icns"
cp "$task_tmp/Latent.icns" "$task_output"
printf '%s\n' "$task_output"
