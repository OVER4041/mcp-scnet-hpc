#!/usr/bin/env bash
set -euo pipefail
scnet_root="$(cd -- "$(dirname -- "$0")" && pwd -P)"
scnet_python="$scnet_root/.venv/bin/python"
scnet_config="$scnet_root/config.toml"
if [[ $# -gt 0 ]]; then scnet_config="$1"; fi
[[ -x "$scnet_python" ]] || { echo "Run uv sync --locked first." >&2; exit 1; }
[[ -f "$scnet_config" ]] || { echo "Copy config.example.toml to config.toml and configure your SSH alias." >&2; exit 1; }
if codex mcp get scnet-hpc --json >/dev/null 2>&1; then
  echo "scnet-hpc is already registered; review it before replacing." >&2
  exit 1
fi
codex mcp add scnet-hpc -- "$scnet_python" -X utf8 -m scnet_mcp.server --config "$scnet_config"
scnet_codex_root="$(printenv CODEX_HOME || true)"
if [[ -z "$scnet_codex_root" ]]; then scnet_codex_root="$HOME/.codex"; fi
"$scnet_python" -X utf8 "$scnet_root/scripts/tune_codex.py" "$scnet_codex_root/config.toml" "$scnet_python"
echo "Registered scnet-hpc. Restart the MCP connection or open a new Codex chat."
