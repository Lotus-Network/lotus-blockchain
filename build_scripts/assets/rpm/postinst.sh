#!/usr/bin/env bash
# Post install script for the UI .rpm to place symlinks in places to allow the CLI to work similarly in both versions

set -e

ln -s /opt/lotus/resources/app.asar.unpacked/daemon/lotus /usr/bin/lotus || true
