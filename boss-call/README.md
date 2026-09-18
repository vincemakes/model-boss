# boss-call

One Boss, several agent sessions, one line each.

Agent sessions cannot type into one another's terminals. `boss-call` gives them
a local mailbox: one Boss and any number of members, with every member connected
only to the Boss. The same CLI-and-skill path works in Claude Code, Codex, pi,
opencode, and kiso. State is plain files under `~/.boss-call/<room>/`; there is
no server, daemon, or harness extension in the normal path.

**Sessions are ordinary by default.** Registering a repository does not enlist
every session started there. A session becomes a Boss or member only after the
person tells that session **“follow the boss-call skill”**. In Claude Code,
`/boss-call` is the equivalent shortcut. A newly opened session needs the
instruction once.

## Requirements

- Node.js 22 or newer.
- A supported agent harness with a shell tool: Claude Code, Codex, pi,
  opencode, or kiso.
- `~/.local/bin` on `PATH` so the linked `boss-call` command can be found.

## Install

boss-call is distributed directly from the Model Boss repository. It is only a
CLI and skills: there is no package-registry installation and no harness
extension.

```bash
git clone https://github.com/vincemakes/model-boss.git "$HOME/.local/share/model-boss"
node "$HOME/.local/share/model-boss/boss-call/bin/boss-call.mjs" setup
boss-call help
```

`setup` links the skill into `~/.agents/skills` and into the harness-specific
skill directories that already exist: `~/.claude/skills`, `~/.codex/skills`,
and `~/.kiso/skills`. If a harness is installed later, run `boss-call setup`
again.

When `boss-call` is not already on `PATH`, `setup` links the CLI at
`~/.local/bin/boss-call`. It does not edit shell startup files, so add that
directory to `PATH` yourself if necessary:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The setup is safe to repeat after an update. It refreshes user-level skill
symlinks and removes an old kiso extension symlink if it finds one. It does
**not** install an extension and does **not** write `AGENTS.md`, `CLAUDE.md`, or
any other file in a registered repository.

To ask an agent to install it for you, paste:

> Clone `https://github.com/vincemakes/model-boss` to
> `~/.local/share/model-boss`, run
> `node ~/.local/share/model-boss/boss-call/bin/boss-call.mjs setup`, add
> `~/.local/bin` to `PATH` if needed, and confirm that `boss-call help` works.
> Do not modify any repository `AGENTS.md` or `CLAUDE.md` file.

## Quick start

In the Boss's working directory, create a room and register that directory as
the Boss identity:

```bash
boss-call host migration --root "$PWD"
```

In each member repository, register the current directory under a unique name:

```bash
cd ~/work/reelfo
boss-call join migration --as reelfo

cd ~/work/flowpix2
boss-call join migration --as flowpix2
```

`join` only records the repository path in
`~/.boss-call/migration/room.json`. It does not touch either repository.

Now start kiso, Claude Code, Codex, pi, or opencode exactly as usual in each
registered directory. Each new Boss or member session starts as an ordinary
session. Tell it:

```text
follow the boss-call skill
```

The skill first runs `boss-call who`, resolves the room and identity from the
current directory, reads pending mail, and enters the work/report/wait loop.
If you do not give that instruction, nothing boss-call-specific happens in the
session.

The identity can also be supplied explicitly with `--room <room>` and
`--me <name>`, or with `BOSS_CALL_ROOM` and `BOSS_CALL_ME`. Directory matching
uses the most specific registered root, with symlinks resolved.

## The interactive loop

A member uses:

```bash
boss-call read --ack
boss-call post --kind status "done: …; not done: …; blocked: …"
boss-call post --kind ask "which db? options: A / B; I prefer A"
boss-call wait --timeout 590
```

The Boss uses:

```bash
boss-call status
boss-call read --ack
boss-call post --to reelfo "wire main.ts openSession to buildReelfoAgent first"
boss-call post --to all "stop and report"
boss-call post --to reelfo --kind reply --ref '#12' "B"
boss-call tail -n 30
boss-call wait --timeout 590
```

Members can post only to the Boss. The Boss can address one member or all
members. Message kinds are `msg`, `ask`, `reply`, and `status`.

`wait` is the delivery mechanism. It blocks in the harness's shell tool until
mail arrives, acknowledges that mail, prints it, and returns control to the
agent. While the tool is blocked, the model spends no generation tokens. Set
the shell tool timeout slightly above the CLI timeout: for example,
`boss-call wait --timeout 590` with a 600-second Claude Code Bash timeout or
kiso `timeoutMs`. For a harness with an unknown or short limit, use the
25-second default and repeat immediately when it returns empty.

While blocked, the participant writes a heartbeat. `boss-call status` reports
`listening`, `working <age> ago`, or `never listened`. A `wait` immediately
after the participant's own fresh status returns a one-time nudge: a status is
a report, not the end of a turn. If the status named a next step, the agent must
do it; the second `wait` blocks normally.

There is no scheduler behind the generic loop. A session is callable only
while it remains inside `boss-call wait`. If it ends, crashes, or exhausts its
context, start or resume a session and say **“follow the boss-call skill”**
again.

## How autonomous is it?

After activation, ordinary development can run without a person relaying each
step. The Boss assigns work; members read it, edit code, run checks, report
facts, ask only when blocked, and wait for more work. This works well when the
task, repository scope, and acceptance criteria are already clear.

Human involvement remains intentional at two boundaries:

- **Authorization:** mail is never a person's authorization. Spending money,
  pushing, merging, deploying, and changing production configuration require
  the person at that terminal. A Boss cannot grant that authority by mail.
- **Judgment:** a member sends one focused `ask` when a product or technical
  choice cannot be safely inferred. It includes options and a preference, then
  continues any work that does not depend on the answer.

The generic five-harness path is interactive: a person starts the session once
and the session stays on the line through `wait`. Truly headless wake-up is
currently available only for kiso through `boss-call serve`, described below.

## What is recorded?

The boss-call mailbox is **not** the same thing as a Claude or Codex message
history. It stores only the short messages that participants explicitly post
through the CLI. A line in `messages.jsonl` looks like:

```json
{"seq":12,"ts":"2026-09-18T06:30:00Z","from":"reelfo","to":"boss","kind":"status","text":"tests pass; next: wire the route"}
```

The room uses these local files:

```text
~/.boss-call/<room>/room.json          Boss, member names, repository roots, optional kiso settings
~/.boss-call/<room>/messages.jsonl     append-only {seq, ts, from, to, kind, text, ref?}
~/.boss-call/<room>/cursors/<name>     last sequence acknowledged by that participant
~/.boss-call/<room>/heartbeats/<name>  last waiting/working heartbeat
~/.boss-call/<room>/nudged/<name>      last status that triggered the continue-working nudge
```

Set `BOSS_CALL_HOME` to use a different mailbox root. These are plain local
files, not encrypted records; any local process with filesystem permission can
read them. Upgrades from the original Python CLI continue using
`~/.model-boss/boss-call` automatically when that legacy directory exists and
`~/.boss-call` does not; set `BOSS_CALL_HOME` when both roots exist and you want
to select one explicitly.

boss-call does not copy hidden reasoning, the full assistant transcript, tool
inputs or outputs, diffs, or token usage into the mailbox. Claude Code, Codex,
pi, and opencode keep their own histories according to their own rules, and
boss-call does not read them.

kiso is the one optional exception. kiso itself writes event logs under
`~/.kiso/sessions/<id>.jsonl`. `boss-call peek-session` can read that kiso
format and summarize the last user input, text output, permission state,
tool-execution lifecycle, and terminal outcome. `serve` also uses the log to
tell whether a run really started and to produce an `[auto]` status when the
model posted nothing. The kiso event log remains separate from the boss-call
mailbox; it is not a Claude-style message copied by boss-call.

## Optional headless mode: kiso only

When nobody will open an interactive session in a member repository, `serve`
can poll for mail and hand each batch to `kiso resume` as one turn:

```bash
cd ~/work/reelfo
boss-call join migration --as reelfo --profile co --env-file ~/.config/agent/creds.env
boss-call serve                 # or --once, --session <id>, --poll 10
```

`--profile` selects the kiso model profile. `--env-file` loads a simple
`KEY=VALUE` file into the child process. Mail is acknowledged only after the
run exits; if the process never starts and the session log does not change,
the mail stays unread for retry. If a run exits without posting, `serve` posts
a status tagged `[auto]` from the kiso log. A Boss can be served too.

`serve` does not turn Boss mail into human authorization. A headless member
must still stop and ask when it reaches an authorization boundary. There is no
equivalent headless launcher for Claude Code, Codex, pi, or opencode today.

## Update

```bash
git -C "$HOME/.local/share/model-boss" pull --ff-only
node "$HOME/.local/share/model-boss/boss-call/bin/boss-call.mjs" setup
```

Room state is independent of the checkout and stays under `~/.boss-call`.

## Develop

```bash
cd boss-call
node --test
```

The CLI has no runtime dependencies. boss-call is part of
[model-boss](https://github.com/vincemakes/model-boss).
