#!/bin/sh
# Runs after the .deb or .rpm is installed (Tauri copies it into the package).
# Only prints how to start the app: the background service is set up by the
# app itself on its first start, as the user, never here as root.
# deb: "configure" on install and upgrade; rpm: 1 = install, 2 = upgrade.
case "$1" in
  configure | 1 | 2)
    cat <<'EOF'

RootMind is installed.
Start it from your app menu or in a terminal with:

    rootmind-desktop

On its first start the app sets up its background service
(systemd user services of your account). After an update,
restart the app to update the service.

EOF
    ;;
esac
exit 0
