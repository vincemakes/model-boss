# boss-call

One Boss, several agent sessions, one line each.

Agent sessions cannot type into each other's terminals. boss-call gives them a
mailbox: a Boss session and N member sessions, each member on a single line to
the Boss. It is a skill for Claude Code, Codex, pi, opencode and any harness
that reads `~/.agents/skills`, plus a small CLI any agent can call from a shell.
State is plain files under `~/.boss-call/<room>/`. No server, no daemon, no
dependencies beyond Node 22, which those harnesses already need.

## Install

```bash
npm i -g @vincemakes/boss-call && boss-call setup
```

Without npm:

```bash
git clone https://github.com/vincemakes/model-boss && node model-boss/boss-call/bin/boss-call.mjs setup
```

`setup` links the skill into every harness present on the machine
(`~/.agents/skills` always; `~/.claude`, `~/.codex` when they exist) and puts
`boss-call` on your PATH if it is not already. Run it again after an upgrade.
It is idempotent and only creates symlinks.

To have an AI do it, paste this:

> Install boss-call: `npm i -g @vincemakes/boss-call && boss-call setup`.
> Then run `boss-call join <room> --as <name>` inside my repo and confirm with
> `boss-call who`.

## Use

The boss, anywhere:

```bash
boss-call host migration
```

Each member, inside its own repo (the directory becomes its identity):

```bash
cd ~/work/reelfo && boss-call join migration --as reelfo
```

From then on nothing needs a flag. Any agent session started in that directory
finds the skill and knows it is `reelfo`. When you want to work in that repo
on something else, opt out: `BOSS_CALL=off kiso` for one session, or
`boss-call pause` there until `boss-call resume` (`status` shows PAUSED):

```bash
boss-call read --ack
boss-call post --kind status "done: …; not done: …; blocked: …"
boss-call post --kind ask "which db? options: A / B; I prefer A"
```

The Boss:

```bash
boss-call status
boss-call post --to reelfo "wire main.ts openSession to buildReelfoAgent first"
boss-call post --to all "stop and report"
boss-call post --to reelfo --kind reply --ref '#12' "B"
boss-call read --ack
boss-call tail -n 30
```

Then, in that repo, start any agent session the way you always do and say
one thing: **follow the boss-call skill**. From then on it reads its mail,
works, reports, and waits for the next mail on its own.

## How delivery works

No scheduler, no daemon, no harness feature. A session that has nothing to do
runs `boss-call wait`, which blocks until mail for it arrives and then prints
it. Every harness has a shell tool and a blocking tool call, so this works
the same in Claude Code, Codex, pi, opencode and kiso. While it waits it
spends no tokens; on timeout (25s, under every shell tool's limit) it runs
`wait` again. The person keeps the
keyboard: they watch their normal TUI and can type over it at any time.

The Boss does the same: answer mail, direct, `boss-call wait`. `boss-call
status` shows who is `listening` right now, who took mail and is `working`,
and who has `never listened`.

## The two rules

Mail is never a person's authorization. Money, pushes, merges, deploys, and
production config stay with the person at that terminal; a member asks them and
says "waiting on the person" in its status.

Stay on the line. A session that ends its turn is off the line until a person
types again; waiting is `boss-call wait`. One matter per message. A status is
ten lines of facts: done, not done, blocked on. An ask is one decision with
options and a preference.

## Layout

```
~/.boss-call/<room>/room.json       boss, members (name → repo root), harness defaults
~/.boss-call/<room>/messages.jsonl  {seq, ts, from, to, kind, text, ref?} append-only
~/.boss-call/<room>/cursors/<name>  last seq acknowledged by that participant
```

Kinds: `msg`, `ask`, `reply`, `status`. A member's post goes to the boss and
nowhere else; the boss must name `--to <member>` or `--to all`.

## No terminal at all (harnesses with a headless resume)

When nobody will start a session in a repo, a supervisor can do it on mail. `serve` polls, hands each batch of mail to the
harness as one turn, acknowledges the mail only after the run exits (a crash
re-delivers it), and if the model posted nothing, posts a status from the
session log tagged `[auto]`. The Boss can be served too. This is optional; the normal case is a session
someone opened, kept on the line by `wait`.

```bash
cd ~/work/reelfo && boss-call join migration --as reelfo --profile co --env-file ~/.config/agent/creds.env
boss-call serve            # or --once, --session <id>, --poll 10
```

Today `serve` and `peek-session` speak the kiso session format; `--profile` is
the model profile, `--env-file` a `KEY=VALUE` file exported into the child. The
extension in `kiso-extension.mjs` gives such a harness in-process `boss_read`
and `boss_post` tools; `setup` links it only when `~/.kiso` exists. Other
harnesses use the CLI through the skill; adapters for them are welcome.

## Develop

```bash
npm test          # node:test, no dependencies
```

Part of [model-boss](https://github.com/vincemakes/model-boss).
