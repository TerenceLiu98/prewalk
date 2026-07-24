#!/usr/bin/env bash
# prewalk installer.
#
#   ./install.sh claude-code [CLAUDE_CONFIG_DIR]
#   ./install.sh codex       [CODEX_HOME]
#
# - claude-code: copies skills + presets into ~/.claude (or $CLAUDE_CONFIG_DIR),
#   merges the prewalk hooks into settings.json, and patches the <PLUGIN_ROOT>
#   placeholder in the copied skills to the absolute hooks/ path.
# - codex: copies presets into ~/.codex (or $CODEX_HOME). The plugin itself is
#   installed via the Codex marketplace (`codex plugin marketplace add` +
#   `codex plugin add`); this only stages presets.
#
# Re-runnable: merges idempotently (hooks are appended each run, so remove
# duplicates by hand if you re-run many times — or just run once).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"

die() { echo "install: $*" >&2; exit 1; }

install_claude_code() {
  local CFG="${2:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
  local SRC="$ROOT/claude-code"
  command -v python3 >/dev/null || die "python3 not found on PATH"
  mkdir -p "$CFG/skills"
  cp -R "$SRC/skills/"* "$CFG/skills/"
  cp "$SRC/presets.example.json" "$CFG/prewalk-presets.json"
  echo "✓ copied skills + presets → $CFG"

  # Patch <PLUGIN_ROOT> in the copied skills to the absolute hooks dir.
  python3 - "$CFG" "$SRC" <<'PY'
import json, os, sys
cfg, src = sys.argv[1], sys.argv[2]
import glob
for md in glob.glob(os.path.join(cfg, "skills", "*", "SKILL.md")):
    t = open(md).read()
    t = t.replace("<PLUGIN_ROOT>", src)
    open(md, "w").write(t)
print("✓ patched <PLUGIN_ROOT> in skills")
PY

  # Merge hooks into settings.json.
  python3 - "$CFG/settings.json" "$SRC/hooks" <<'PY'
import json, os, sys
path, hooks = sys.argv[1], sys.argv[2]
existing = {}
if os.path.exists(path):
    try: existing = json.load(open(path))
    except Exception:
        import shutil; shutil.copy(path, path + ".bak"); existing = {}
h = existing.get("hooks", {})
def cmd(p): return f'python3 "{os.path.join(hooks, p)}"'
for ev, grp in {
    "SessionStart": [{"hooks":[{"type":"command","command":cmd("export_session_id.py")}]}],
    "Stop": [{"hooks":[{"type":"command","command":cmd("pause_detect.py")}]}],
    "PostToolUse": [{"matcher":"TodoWrite","hooks":[{"type":"command","command":cmd("pause_detect.py")}]}],
    "PreToolUse": [{"matcher":"Write|Edit|MultiEdit","hooks":[{"type":"command","command":cmd("edit_gate.py")}]}],
}.items():
    h.setdefault(ev, []).extend(grp)
existing["hooks"] = h
json.dump(existing, open(path, "w"), indent=2, ensure_ascii=False)
print(f"✓ merged hooks → {path}")
PY

  echo
  echo "Done. Edit $CFG/prewalk-presets.json (planner/executor models), then restart Claude Code."
}

install_codex() {
  local HOME_DIR="${2:-${CODEX_HOME:-$HOME/.codex}}"
  command -v python3 >/dev/null || die "python3 not found on PATH"
  mkdir -p "$HOME_DIR"
  cp "$ROOT/codex/presets.example.toml" "$HOME_DIR/prewalk-presets.toml"
  echo "✓ copied presets → $HOME_DIR/prewalk-presets.toml"
  echo
  echo "Install the plugin itself (Codex uses a marketplace model, not 'plugin install'):"
  echo "  codex plugin marketplace add \"$ROOT\""
  echo "  codex plugin add prewalk@prewalk-marketplace"
  echo "  # or, once pushed to GitHub:"
  echo "  codex plugin marketplace add TerenceLiu98/prewalk"
  echo "  codex plugin add prewalk@prewalk-marketplace"
  echo
  echo "Then edit $HOME_DIR/prewalk-presets.toml and restart Codex."
}

case "$TARGET" in
  claude-code) install_claude_code "$@" ;;
  codex)      install_codex "$@" ;;
  *) die "usage: $0 claude-code|codex  (got: '$TARGET')";;
esac
