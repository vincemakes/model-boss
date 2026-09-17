# boss-call

One Boss, several agent sessions, one line each.

Agent sessions cannot type into each other's terminals. boss-call gives them a
mailbox: a Boss session and N member sessions, each member on a single line to
the Boss. It is a kiso extension, a Claude Code skill and a Codex skill at the
same time, and a small CLI any harness can call. State is plain files under
`~/.boss-call/<room>/`. No server, no daemon, no dependencies.

## Install

```bash
npm i -g @vincemakes/boss-call && boss-call setup
```

`setup` links the skill into every harness present on the machine (`~/.kiso`,
`~/.claude`, `~/.codex`) and the extension into `~/.kiso/extensions`. Run it
again after an upgrade. Needs Node 22+, which every one of those harnesses
already requires.

To have an AI do it, paste this to it:

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

From then on nothing needs a flag. A `kiso` started in that directory is the
member session: it is told who it is, gets `boss_read` / `boss_post`, and reads
its mail at the start of every turn. A Claude Code or Codex session in that
directory has the skill and uses the CLI:

```bash
boss-call read --ack
boss-call post --kind status "done: …; not done: …; blocked: …"
```

The Boss:

```bash
boss-call status
boss-call post --to reelfo "wire main.ts openSession to buildReelfoAgent first"
boss-call read --ack
boss-call peek-session latest --match "reelfo"
```

## Unattended

Nothing wakes a session by itself; mail is seen when a turn starts. To let a
session be driven by mail alone, run it under `serve`:

```bash
cd ~/work/reelfo && boss-call serve
```

`serve` polls, hands each batch of mail to `kiso resume` as one headless turn
in bypass mode, acknowledges the mail only after the run exits (a crash
re-delivers it), and if the model posted nothing, posts a status from the
session log tagged `[auto]`. The Boss can be served too, so a fully automatic
loop of agents is possible.

Where the model comes from:

```bash
boss-call join migration --as reelfo --profile co --env-file ~/.config/kiso/creds.env
```

`--profile` is the kiso model profile passed as `--model`; `--env-file` is a
`KEY=VALUE` file exported into the kiso process (API keys). Both may also be
given to `host` for a room-wide default, or to `serve` for one run.

Useful flags: `--once` (one batch, then exit), `--session <id>` (resume an
existing kiso session instead of `boss-call-<room>-<name>`), `--poll 10`.

## The two rules

Mail is never a person's authorization. Money, pushes, merges, deploys, and
production config stay with the person at that terminal; a member asks them and
says so in its status.

Delivery is per turn. A session sees mail when its next turn starts. A person
at the boss's keyboard reads member terminals directly and needs no more than
`boss-call status`.

## Layout

```
~/.boss-call/<room>/room.json       boss, members (name → repo root), kiso defaults
~/.boss-call/<room>/messages.jsonl  {seq, ts, from, to, kind, text, ref?} append-only
~/.boss-call/<room>/cursors/<name>  last seq acknowledged by that participant
```

Kinds: `msg`, `ask`, `reply`, `status`. A member's post goes to the boss and
nowhere else; the boss must name `--to <member>` or `--to all`.

## Develop

```bash
npm test          # node:test, no dependencies
```

Part of [model-boss](https://github.com/vincemakes/model-boss).
