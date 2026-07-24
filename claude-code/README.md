# prewalk for Claude Code

> Frontier model explores + plans + lands the first verified edit, then hands the
> live context window to a cheaper executor that finishes the rest.

Implementation of the **prewalk** technique ([Can Bölük / Stencil](https://stencil.so/blog/prewalk))
for **Claude Code**. Shares its engine with the Codex version (`../_shared/prewalk_core.py`).

## Install

**Prerequisite:** Python 3 on PATH (`python3 --version`; macOS: `brew install python`).

### Option A — installer script (recommended)

From the repo root:
```sh
./install.sh claude-code
# or point it at a project config: ./install.sh claude-code ./.claude
```
This copies the skills + presets into `~/.claude`, patches the skill paths, and
merges the prewalk hooks into `~/.claude/settings.json` (your existing settings
are preserved).

### Option B — manual

1. Copy the skills and presets:
   ```sh
   CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
   mkdir -p "$CFG/skills"
   cp -R skills/* "$CFG/skills/"
   cp presets.example.json "$CFG/prewalk-presets.json"
   ```
2. In the copied skills, replace `<PLUGIN_ROOT>` with the absolute path to this
   `claude-code/` directory (e.g. `/Users/you/prewalk/claude-code`).
3. Merge the `hooks` block from `settings.example.json` into your `settings.json`,
   replacing `<PREWALK_ROOT>` with the absolute path to this `claude-code/hooks`
   directory.
4. Edit `$CFG/prewalk-presets.json` — set the planner/executor model ids to what
   your install resolves (aliases `opus`/`sonnet`/`haiku`/`fable`, or full ids).
5. Restart Claude Code.

> The hooks vendor a copy of the engine at `hooks/_shared/prewalk_core.py` and
> `hooks/_bootstrap.py` finds it from any location, so you can move this directory
> after install without breaking anything.

## Use

```
/prewalk Add a settings page with tabbed sections
/prewalk Refactor the auth module --no-pause        # auto-swap at the checkpoint
```

Then at the `⏸️ PAUSE` checkpoint: review the plan and task #1, run **`/pw-go`**
(to hand off to the executor model and finish), or **`/pw-revise <changes>`**
(to revise the plan on the planner first).

Status / disarm (helpers, not skills):
```sh
python3 <claude-code>/hooks/_arm.py status "$CLAUDE_SESSION_ID"
python3 <claude-code>/hooks/_arm.py disarm "$CLAUDE_SESSION_ID"
```

## How it works

```
/prewalk <task>            → arms the run, suggests /model <planner>
  frontier (Opus/Fable)    → explores, writes a capped todo (every item has a
                             verify-word), completes ONLY task #1 + verifies it,
                             adds a `⏸️ PAUSE` todo, STOPS
  (you review)
/pw-go                     → injects the handoff note; you run /model <executor>
  executor (Haiku)         → finishes remaining todos in order, inheriting the
                             full trajectory; /model <planner> restores at the end
```

Three hooks drive the state machine:
- **PostToolUse** (`TodoWrite`) + **Stop** → `hooks/pause_detect.py`: detects the
  `⏸️ PAUSE` checkpoint (frontier→paused), detects executor completion (restore).
- **PreToolUse** (`Write|Edit|MultiEdit`) → `hooks/edit_gate.py`: blocks edits in
  the frontier phase until a valid capped todo list exists; disarms after a 2nd
  violation.

`/prewalk`, `/pw-go`, `/pw-revise` are skills that call the Python helpers in
`hooks/` (`_arm.py`, `_pw.py`).

**The model switch:** Claude Code cannot switch the model from a hook, so `/pw-go`
injects a handoff note instructing the model to run `/model <executor>` itself.
For a programmatic switch use the Agent SDK `setModel()` (v2.1.200+).

## Style B (appendix) — subagent variant

`agents/prewalk-executor.md` pins `model: haiku`. Point a skill at
`agent: prewalk-executor` for a lightweight handoff where the executor runs as a
fresh subagent (clean main window). **Caveat:** a subagent starts fresh — it gets
only the handoff *summary*, not the raw trajectory. Use only for long tasks where
Style A's re-send cost is too high.

## Differences from the article

- **Checkpoint at end of turn, not mid-edit** (hooks fire at turn/tool boundaries).
- **Explicit `/model` switch, not seamless continuation** (no request-rewrite
  middleware); the executor inherits the same conversation, so it's close.

## Limitations

- No request-rewrite middleware → handoff is `/model` + a note, not the Hermes form.
- Hooks can't switch the model directly; the skill instructs `/model`.
- Style A re-sends the full trajectory to the cheap model every turn (cache lost on
  `/model`).
- The edit gate keys off `Write|Edit|MultiEdit`; a file-mutating tool under another
  name isn't gated.
- Per-session state lives in `~/.claude/prewalk-state.json`; a restart mid-run loses
  the phase machine (the todo list + conversation survive — resume with `/pw-go`).

## Layout

```
claude-code/
├── hooks/
│   ├── _bootstrap.py          # locates prewalk_core.py from any layout
│   ├── _common.py             # host I/O shim
│   ├── _arm.py                # /prewalk helper: arm/status/disarm
│   ├── _pw.py                 # /pw-go + /pw-revise helper
│   ├── pause_detect.py        # Stop + PostToolUse(TodoWrite)
│   ├── edit_gate.py           # PreToolUse(Write|Edit|MultiEdit)
│   └── _shared/prewalk_core.py
├── skills/{prewalk,pw-go,pw-revise}/SKILL.md
├── agents/prewalk-executor.md # Style B
├── settings.example.json
└── presets.example.json
```
