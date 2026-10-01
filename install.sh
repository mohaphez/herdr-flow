#!/usr/bin/env bash
# Install from a standalone checkout, without copying plugin source.
set -Eeuo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
id=hessam.herdr-flow
[[ ${HERDR_ENV:-} == 1 ]] || { echo "Run this installer inside a Herdr-managed pane (HERDR_ENV=1)." >&2; exit 1; }
for tool in herdr python3 git opencode codex claude jq; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
done
current=$(herdr plugin list --json | jq -r --arg id "$id" '.result.plugins[] | select(.plugin_id == $id) | .plugin_root' | head -n1)
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
if [[ -z $current ]]; then herdr plugin link "$root" >/dev/null; fi
config_dir=$(herdr plugin config-dir "$id")
mkdir -p "$config_dir"
if [[ ! -e $config_dir/config.json && ! -L $config_dir/config.json ]]; then
  cp "$root/config.json" "$config_dir/config.json"
  chmod 0600 "$config_dir/config.json"
fi
ln -s "$root/bin/herdr-flow" "$HOME/.local/bin/herdr-flow"
for source in "$root"/commands/claude/*.md; do ln -s "$source" "$HOME/.claude/commands/$(basename "$source")"; done
for source in "$root"/commands/opencode/*.md; do ln -s "$source" "$HOME/.config/opencode/commands/$(basename "$source")"; done
for source in "$root"/commands/codex/*.md; do ln -s "$source" "$HOME/.codex/prompts/$(basename "$source")"; done
ln -s "$root/skills/herdr-flow" "$HOME/.agents/skills/herdr-flow"
ln -s "$root/skills/herdr-flow-plan" "$HOME/.agents/skills/herdr-flow-plan"
ln -s "$root/skills/herdr-flow" "$HOME/.claude/skills/herdr-flow"
printf 'Installed standalone Herdr Flow from %s\nConfigure private models in %s/config.json, then run herdr-flow doctor.\n' "$root" "$config_dir"
