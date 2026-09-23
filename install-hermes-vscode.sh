#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
TARGET="${BIN_DIR}/hermes-acp"
HERMES_TARGET="${BIN_DIR}/hermes"

mkdir -p "$BIN_DIR"
ln -sfn "$REPO_DIR/hermes-acp.sh" "$TARGET"
ln -sfn "$REPO_DIR/hermes-acp.sh" "$HERMES_TARGET"

printf 'Installed %s -> %s\n' "$TARGET" "$REPO_DIR/hermes-acp.sh"
printf 'Installed %s -> %s\n' "$HERMES_TARGET" "$REPO_DIR/hermes-acp.sh"
if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
  printf 'Add %s to PATH, then restart VS Code.\n' "$BIN_DIR" >&2
fi
