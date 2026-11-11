#!/usr/bin/env bash
# Install Ubuntu packages on a GitHub runner, robust against mirror outages.
#
# The runners fetch from an Azure mirror (mirror+file:/etc/apt/apt-mirrors.txt).
# When its name does not resolve (seen on 2026-11-11, for over an hour), every
# download fails. Retry first; if that fails, point apt at archive.ubuntu.com
# directly and try once more.
#
# Usage: .github/scripts/apt-install.sh PACKAGE...
set -euo pipefail

install() {
  sudo apt-get update -q -o Acquire::Retries=3
  sudo apt-get install -y -q -o Acquire::Retries=3 --no-install-recommends "$@"
}

if install "$@"; then
  exit 0
fi

echo "::warning::apt failed with the runner's mirror; retrying with archive.ubuntu.com"
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$f" ] || continue
  sudo sed -i \
    -e 's|mirror+file:/etc/apt/apt-mirrors.txt|http://archive.ubuntu.com/ubuntu/|g' \
    -e 's|http://azure.archive.ubuntu.com/ubuntu/\?|http://archive.ubuntu.com/ubuntu/|g' \
    "$f"
done
install "$@"
