# boss-call — one Boss, several members, one line each

Model Boss dispatches a bounded task packet to a worker and audits the
evidence. `boss-call` is a smaller, separate thing: a persistent mailbox so a
Boss session can follow several independent terminal sessions it cannot type
into, and they can report or ask without spending the Boss's tokens on chatter.

The relationship is a star and the CLI enforces it: a room has exactly one
Boss; a member's `post` goes to the Boss and nowhere else; the Boss posts to
one member or to all. Any agent that can run a shell command can be either
side — Claude Code, Codex, kiso, a script — because the whole contract is one
`SKILL.md` and one command.

```
boss-call/
  boss-call.py   the CLI (Python 3.11+, stdlib only)
  SKILL.md       one skill, installed to ~/.kiso, ~/.claude and ~/.codex skills dirs
  install.sh
```

State: `~/.model-boss/boss-call/<room>/` — `room.json` (boss + members with
repo roots, so a session knows who it is from its cwd), `messages.jsonl`
(append-only, seq), `cursors/<name>` (what each participant has acknowledged).

```bash
boss-call/install.sh
boss-call --room migration init --boss boss \
    --member flowpix2=~/Desktop/devv/flowpix2 --member uooki=~/Desktop/devv/uooki \
    --member reelfo=~/Desktop/devv/reelfo
export BOSS_CALL_ROOM=migration              # in every terminal, or pass --room
boss-call who                                # which side am I on
boss-call post --me boss --to reelfo "…"     # Boss
boss-call read --ack                         # member, at the start of each turn
boss-call post --kind status "…"             # member, before it stops
boss-call peek-session latest --match reelfo # Boss reads a kiso log directly
```

Delivery is per turn: a member reads its inbox when its next turn starts.
Nothing here drives an idle session; a person (or a driver) still gives it a
turn. A message is never an authorization — money, pushes, merges and deploys
stay with the person at the terminal.

Not part of the packaged Model Boss skill (`scripts/package-skill.sh` does
not ship it).
