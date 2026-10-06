#!/bin/bash
set -euo pipefail
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_developer="$(xcode-select -p)"
task_frameworks="$task_developer/Library/Developer/Frameworks"
# Some Command Line Tools releases ship Testing.framework but omit its -F flag.
# Use that bundled framework; installing Xcode or another test dependency is unnecessary.
task_flags=()
if [[ "$task_developer" == */CommandLineTools && -d "$task_frameworks/Testing.framework" ]]; then
    task_flags=(-Xswiftc -F -Xswiftc "$task_frameworks"
                -Xlinker -F -Xlinker "$task_frameworks"
                -Xlinker -rpath -Xlinker "$task_frameworks"
                -Xlinker -rpath -Xlinker "$task_developer/Library/Developer/usr/lib")
fi
# Bash 3.2 treats an empty array as unbound under set -u.
swift test --package-path "$task_root/macos" --disable-xctest ${task_flags[@]+"${task_flags[@]}"} "$@"
