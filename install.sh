#!/usr/bin/env bash
# Install from a standalone checkout, without copying plugin source.
set -Eeuo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
id=hessam.herdr-flow
[[ ${HERDR_ENV:-} == 1 ]] || { echo "Run this installer inside a Herdr-managed pane (HERDR_ENV=1)." >&2; exit 1; }
herdr_bin=${HERDR_BIN_PATH:-herdr}
for tool in python3 git opencode codex claude jq; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
done
command -v "$herdr_bin" >/dev/null || { echo "Missing Herdr binary: $herdr_bin" >&2; exit 1; }
current=$("$herdr_bin" plugin list --json | jq -r --arg id "$id" '.result.plugins[] | select(.plugin_id == $id) | .plugin_root' | head -n1)
if [[ -n $current && $(readlink -f "$current") != "$root" ]]; then
  echo "A different Herdr Flow installation is active: $current" >&2
  echo "Finish live tasks first; from Herdr explicitly unlink the old plugin, then rerun this installer. No live sessions were changed." >&2
  exit 1
fi

# Refuse to replace files installed by other tools or checkouts.
check_link() {
  local target=$1 dest=$2
  if [[ -e $dest || -L $dest ]] && { [[ ! -L $dest ]] || [[ $(readlink -f "$dest") != "$target" ]]; }; then
    echo "Refusing to replace existing path: $dest" >&2
    exit 1
  fi
}
check_link "$root/bin/herdr-flow" "$HOME/.local/bin/herdr-flow"
for source in "$root"/commands/claude/*.md; do check_link "$source" "$HOME/.claude/commands/$(basename "$source")"; done
for source in "$root"/commands/opencode/*.md; do check_link "$source" "$HOME/.config/opencode/commands/$(basename "$source")"; done
for source in "$root"/commands/codex/*.md; do check_link "$source" "$HOME/.codex/prompts/$(basename "$source")"; done
check_link "$root/skills/herdr-flow" "$HOME/.agents/skills/herdr-flow"
check_link "$root/skills/herdr-flow-plan" "$HOME/.agents/skills/herdr-flow-plan"
check_link "$root/skills/herdr-flow" "$HOME/.claude/skills/herdr-flow"

mkdir -p "$HOME/.local/bin" "$HOME/.claude/commands" "$HOME/.claude/skills" \
  "$HOME/.config/opencode/commands" "$HOME/.codex/prompts" "$HOME/.agents/skills"
# Marketplace installs are already registered; local checkouts still need to be linked.
if [[ -z $current ]]; then "$herdr_bin" plugin link "$root" >/dev/null; fi
config_dir=${HERDR_PLUGIN_CONFIG_DIR:-$("$herdr_bin" plugin config-dir "$id")}
mkdir -p "$config_dir"
if [[ ! -e $config_dir/config.json && ! -L $config_dir/config.json ]]; then
  cp "$root/config.json" "$config_dir/config.json"
  chmod 0600 "$config_dir/config.json"
fi
# Safe to rerun after a marketplace install; never replace a foreign or stale link.
ensure_link() {
  local target=$1 dest=$2
  [[ -L $dest && $(readlink -f "$dest") == "$target" ]] || ln -s "$target" "$dest"
}
ensure_link "$root/bin/herdr-flow" "$HOME/.local/bin/herdr-flow"
for source in "$root"/commands/claude/*.md; do ensure_link "$source" "$HOME/.claude/commands/$(basename "$source")"; done
for source in "$root"/commands/opencode/*.md; do ensure_link "$source" "$HOME/.config/opencode/commands/$(basename "$source")"; done
for source in "$root"/commands/codex/*.md; do ensure_link "$source" "$HOME/.codex/prompts/$(basename "$source")"; done
ensure_link "$root/skills/herdr-flow" "$HOME/.agents/skills/herdr-flow"
ensure_link "$root/skills/herdr-flow-plan" "$HOME/.agents/skills/herdr-flow-plan"
ensure_link "$root/skills/herdr-flow" "$HOME/.claude/skills/herdr-flow"
printf 'Set up Herdr Flow from %s\nConfigure private models in %s/config.json, then run herdr-flow doctor.\n' "$root" "$config_dir"
