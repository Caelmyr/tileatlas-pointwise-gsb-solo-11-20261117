#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
docker compose up -d --build
printf 'TileAtlas: http://127.0.0.1:8914\n'
