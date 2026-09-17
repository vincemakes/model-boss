# boss-call — one Boss, several members, one line each

Model Boss dispatches a bounded task packet to a worker and audits the
evidence. `boss-call` is a smaller, separate thing: a persistent mailbox so a
Boss session can follow several independent terminal sessions it cannot type
into, and they can report or ask without spending the Boss's tokens on chatter.

The relationship is a star and the CLI enforces it: a room has exactly one
Boss; a member's `post` goes to the Boss and nowhere else; the Boss posts to
one member or to all. Any agent that can run a shell command can be either
side — Claude Code, Codex, kiso, a script.

```
boss-call/
  boss-call.py    the CLI (Python 3.11+, stdlib only)         -> ~/.local/bin/boss-call
  boss-call.mjs   the kiso extension: boss_read / boss_post   -> ~/.kiso/extensions/
  SKILL.md        one skill, installed to ~/.kiso, ~/.claude and ~/.codex skills dirs
  install.sh
```

State: `~/.model-boss/boss-call/<room>/` — `room.json` (boss, members with
repo roots, the kiso launcher), `messages.jsonl` (append-only, seq),
`cursors/<name>` (what each participant has acknowledged).

## Zero-config on the member side

Identity and room come from the cwd: a registered member root contains it.
So inside a member repo, `boss-call who` needs no flags, and — with the kiso
extension installed — a plain `kiso` (or your own wrapper) started there IS
the member session: the extension appends who-you-are to the system prompt
and adds `boss_read` / `boss_post` as tools. No environment variable, no
opening line. Outside a member repo the extension is empty and costs nothing.

```bash
boss-call/install.sh
boss-call --room migration init --boss boss --launcher kiso-co-bypass \
    --member flowpix2=~/Desktop/devv/flowpix2 --member uooki=~/Desktop/devv/uooki \
    --member reelfo=~/Desktop/devv/reelfo

cd ~/Desktop/devv/reelfo && kiso-co-bypass       # a member session, that is all
boss-call serve                                  # or: unattended, in that repo
boss-call post --me boss --to reelfo "…"         # the Boss
boss-call peek-session latest --match reelfo     # the Boss reads a kiso log directly
```

## Unattended: `serve`

An interactive session reads its inbox at the start of its next turn, so
somebody has to give it a turn. `boss-call serve` is that somebody: it waits
for mail, hands each batch to the room's launcher as
`<launcher> resume <session> "<mail>"` (stdin closed, `KISO_MODE=bypass` —
the shape kiso's own subagent extension uses), acknowledges the mail only
after the run exits (a crashed run re-delivers), and when the model posted no
status, posts one from the durable log tagged `[auto]`. With the Boss on a
periodic wakeup (`/loop 15m boss-call read --me boss --ack`) neither side
needs a person.

kiso parses flags AFTER the positional arguments: `kiso resume <id> "<prompt>"
--model co --mode bypass` works, flags before `resume` swallow the prompt. A
launcher wrapper must therefore append its own flags after `"$@"`.

A message is never an authorization — money, pushes, merges and deploys stay
with the person at the terminal; a headless run that needs one posts an `ask`
and stops.

Not part of the packaged Model Boss skill (`scripts/package-skill.sh` does
not ship it).
