# prewalk for Codex

> Frontier model explores + plans + lands the first verified edit, then hands the
> live context window to a cheaper executor that finishes the rest.

Implementation of the **prewalk** technique ([Can Bölük / Stencil](https://stencil.so/blog/prewalk))
for **Codex CLI** ([openai/codex](https://github.com/openai/codex)). Shares its
engine with the Claude Code version. Packaged as a Codex **plugin**.

## Install

**Prerequisite:** Python 3 on PATH (`python3 --version`).

### From GitHub (once you've pushed this repo)

```sh
codex plugin install github.com/<you>/prewalk
```
Codex clones the repo and loads the plugin. The plugin is self-contained — the
engine is vendored at `hooks/_shared/prewalk_core.py`, and `hooks/_bootstrap.py`
finds it regardless of where Codex places the plugin.

### From a local path

```sh
codex plugin install /path/to/prewalk/codex
```

### Stage presets

```sh
./install.sh codex        # copies presets → ~/.codex/prewalk-presets.toml
# or manually:
cp presets.example.toml "${CODEX_HOME:-$HOME/.codex}/prewalk-presets.toml"
```
Edit the planner/executor model ids in that file to what your Codex install
resolves (run `/model` in Codex to see ids). Then restart Codex.

> If you prefer not to use the plugin packaging, copy `skills/*/SKILL.md` into
> `~/.codex/skills/` (or `.agents/skills/`) and merge the hook tables into
> `~/.codex/config.toml` (see `hooks/hooks.json` for the exact tables).

## Use

```
$prewalk Add a settings page with tabbed sections
$prewalk Refactor the auth module --no-pause        # auto-swap at the checkpoint
```

At the `⏸️ PAUSE` checkpoint: review the plan and task #1, run **`/pw-go`**
(to hand off to the executor model and finish), or **`/pw-revise <changes>`**
(to revise the plan on the planner first).

Status / disarm:
```sh
python3 <plugin>/hooks/_arm.py status "$CODEX_SESSION_ID"
python3 <plugin>/hooks/_arm.py disarm "$CODEX_SESSION_ID"
```

## How it works

```
$prewalk <task>           → arms the run, suggests /model <planner>
  frontier (Sol/etc.)     → explores, writes a capped todo (every item has a
                            verify-word), completes ONLY task #1 + verifies it,
                            adds a `⏸️ PAUSE` todo, STOPS
  (you review)
/pw-go                    → injects the handoff note; you run /model <executor>
  executor (Luna/GLM/…)   → finishes remaining todos in order, inheriting the
                            full trajectory; /model <planner> restores at the end
```

Two hooks (`hooks/hooks.json`):
- **Stop** → `hooks/pause_detect.py`: detects the `⏸️ PAUSE` checkpoint (Codex
  fires Stop at turn end, where the frontier pauses), transitions frontier→paused,
  and detects executor completion (restore planner).
- **PreToolUse** (`apply_patch|Edit|Write`) → `hooks/edit_gate.py`: blocks edits in
  the frontier phase until a valid capped todo list exists; disarms after a 2nd
  violation.

`$prewalk`, `/pw-go`, `/pw-revise` are skills that call the Python helpers in
`hooks/` (`_arm.py`, `_pw.py`).

> **Codex hook coverage:** `PreToolUse` only intercepts "simple" shell calls and
> file-edit tools (`apply_patch`), not every command or `WebSearch`. The edit gate
> keys off `apply_patch`, which is the surface that matters.

**The model switch:** Codex cannot switch the thread model from a hook or MCP tool
(no such API). So `/pw-go` injects a handoff note instructing the model to run
`/model <executor>` itself; the TUI parses queued slash commands. For a programmatic
switch, drive the app-server and pass `model` to `turn/start` (becomes the thread
default), or script `codex exec resume --last --model <executor>`.

## Style B (appendix) — subagent variant

`agents/prewalk-executor.toml` pins a cheap `model`. Spawn it via
`spawn_agent(name="prewalk-executor", ...)` with the frontier's handoff summary as
the instruction. **Caveat:** a subagent starts fresh — it gets only the handoff
*summary*, not the trajectory. Use only for long tasks where Style A's re-send
cost is too high.

## Differences from the article

- **Checkpoint at end of turn, not mid-edit** (Codex hooks fire at turn/tool
  boundaries; the swap is detected on `Stop`).
- **Explicit `/model` switch, not seamless continuation** (no request-rewrite
  middleware); the executor inherits the same thread, so it's close.

## Limitations

- No request-rewrite middleware → handoff is `/model` + a note, not the Hermes form.
- Hooks/MCP can't switch the model directly; the skill instructs `/model` (or use
  app-server `turn/start { model }`).
- Style A re-sends the full trajectory to the cheap model every turn.
- `PreToolUse` doesn't intercept every shell call / `WebSearch`; the gate keys off
  `apply_patch`.
- Per-session state lives in `$CODEX_HOME/prewalk-state.json`; a restart mid-run
  loses the phase machine (todo list + thread survive — resume with `/pw-go`).

## Layout

```
codex/
├── manifest.toml             # plugin manifest
├── hooks/
│   ├── hooks.json            # hook registration (Stop + PreToolUse)
│   ├── _bootstrap.py         # locates prewalk_core.py from any layout
│   ├── _common.py            # host I/O shim
│   ├── _arm.py               # $prewalk helper: arm/status/disarm
│   ├── _pw.py                # /pw-go + /pw-revise helper
│   ├── pause_detect.py       # Stop hook
│   ├── edit_gate.py          # PreToolUse(apply_patch|Edit|Write)
│   └── _shared/prewalk_core.py
├── skills/{prewalk,pw-go,pw-revise}/SKILL.md
├── agents/prewalk-executor.toml   # Style B
└── presets.example.toml
```
