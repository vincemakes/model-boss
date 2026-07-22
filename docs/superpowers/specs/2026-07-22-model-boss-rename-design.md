# Model Boss Rename Design

**Date:** 2026-07-22  
**Status:** Approved for implementation planning

## Goal

Rename Token Saver to **Model Boss** and make the new identity consistent across the
repository, install paths, runtime package, CLI, configuration, generated skill bundle,
documentation, tests, social artwork, GitHub repository, Git branch, and local checkout.

Model Boss keeps the existing cross-model orchestration protocol unchanged. The host
still chooses the conversation's main-loop model before Model Boss runs. Model Boss
places planning, implementation, evidence gates, review, and integration around that
inherited main loop; it does not dynamically replace the main loop.

## Audience and positioning

The primary audience is global developers using Claude Code, Codex, or compatible
local model CLIs. The product name should be memorable in conversation and immediately
suggest a hierarchy of models doing different jobs.

- **Product name:** Model Boss
- **Canonical slug:** `model-boss`
- **Primary slogan:** **Big models think. Small models ship.**
- **Technical subtitle:** **Cross-model coding orchestration**

The slogan is the marketing shorthand. The documentation must retain the precise
capability language: model roles are relative to the selected workflow, and the system
does not claim a universal provider-independent ranking of model quality.

## User-facing model

Lite and Max remain because they represent different authority placements:

- **Lite:** the inherited main loop is the Boss. It plans, reasons, reviews, and
  integrates. An optional secondary worker performs bounded implementation.
- **Max:** a distinct verified authority reviewer is the Boss. The inherited main loop
  coordinates and audits, and it may implement directly or delegate to another worker.

“Boss” is the concise product metaphor for the authority holder. Protocol code and
security documentation continue to use the exact terms `authority`, `main loop`,
`reviewer`, and `worker` where precision matters.

## Canonical identifiers

All active product surfaces move to the new identity:

| Surface | Canonical value |
|---|---|
| GitHub repository | `vincemakes/model-boss` |
| Local checkout | `/Users/vinve/Desktop/devv/model-boss` |
| Git branch | `codex/model-boss` |
| Skill frontmatter | `name: model-boss` |
| Python package | `runtime/model_boss` and imports under `model_boss` |
| CLI entry script | `scripts/model-boss.py` |
| Config files | `config/model-boss.example.json`, `config/model-boss.schema.json` |
| Environment prefix | `MODEL_BOSS_` |
| State and task paths | The exact paths defined below |
| Claude Code agents | `model-boss-<role>.md`; frontmatter `name: model-boss-<role>` |
| Codex agents | `model-boss-<role>.toml`; TOML `name = "model_boss_<role>"` |
| Release artifact | `dist/model-boss.skill` |

Internal JSON schema IDs, user-agent strings, temporary-directory prefixes, help text,
errors, examples, and test fixtures follow the same canonical naming. Existing generic
protocol field names such as `authority_mode`, `main_loop`, and `reviewer` do not change.

### Exact path and environment contract

| Purpose | Model Boss value |
|---|---|
| Project configuration | `<repository>/.model-boss.json` |
| POSIX user configuration | `$XDG_CONFIG_HOME/model-boss/config.json` when `XDG_CONFIG_HOME` is absolute; otherwise `$HOME/.config/model-boss/config.json` |
| PowerShell user configuration | `$HOME\.config\model-boss\config.json` unless an absolute `XDG_CONFIG_HOME` is supplied |
| POSIX credentials | `$XDG_CONFIG_HOME/model-boss/credentials.json` when absolute; otherwise `$HOME/.config/model-boss/credentials.json` |
| PowerShell credentials | `$HOME\.config\model-boss\credentials.json` unless an absolute `XDG_CONFIG_HOME` is supplied |
| Explicit credentials override | `MODEL_BOSS_CREDENTIALS=<absolute path>` |
| Child invocation manifest | `MODEL_BOSS_INVOCATION_MANIFEST` |
| Trusted gate-failure packet | `MODEL_BOSS_TRUSTED_GATE_FAILURES` |
| Example secret source | `MODEL_BOSS_PROVIDER_API_KEY` |
| Invocation directory | `<temp-parent>/model-boss-invocation-<invocation-id>` |
| Consumed receipt | `<temp-parent>/.model-boss-consumed-<invocation-id>.json` |
| Sealed receipt | `<temp-parent>/.model-boss-sealed-<invocation-id>.json` |
| Final sealed receipt | `<temp-parent>/.model-boss-sealed-final-<invocation-id>.json` |
| Temporary files/directories | Existing purpose suffixes with a `model-boss-` or `.model-boss-` prefix |
| Isolated worker Git ref | `refs/heads/model-boss-worker` |

The runtime does not invent Windows roaming-profile conventions that it does not
currently implement. All supported hosts use the same absolute-XDG-or-`HOME/.config`
resolution rule, expressed with the platform's path separator.

## Migration boundary

This is a clean product rename, not a permanent dual-brand release.

- Active commands, installation examples, generated files, and package contents use
  only `model-boss`.
- The former names `token-saver` and `fable-token-saver` may remain only in the explicit
  migration and provenance allowlist below: migration notes and triggers, the narrow
  credential-migration implementation, immutable design history, Git history, and
  dated benchmark provenance where changing the label would falsify the record.
- No duplicate Python package or long-lived legacy CLI shim is shipped. The migration
  note provides the old-to-new command, config, and installation-path mapping.
- GitHub's repository rename redirect covers old clone URLs, while canonical docs and
  the local `origin` use the new URL.

Normal Model Boss discovery reads only `.model-boss.json`, the `model-boss` user
configuration directory, `MODEL_BOSS_*` variables, and the new credentials path. It
never silently reads an old path or environment variable.

Credential migration is the one explicit compatibility operation. `setup-providers`
may read a legacy non-symlink `providers.env` only when the user invokes that command
or supplies `--legacy-source`; it writes a new `model-boss/credentials.json` with the
existing atomic no-overwrite behavior, a private directory, and `0600` file mode on
POSIX. If the Model Boss destination already exists, it returns `already_configured`
and changes neither file. It never copies from `token-saver/credentials.json`
automatically, never honors `TOKEN_SAVER_*` variables, never overwrites a destination,
and never deletes or edits any legacy source. Users with an existing JSON credential
file either keep using an explicit `MODEL_BOSS_CREDENTIALS` absolute path or copy it
manually after verifying permissions.

### Deterministic old-name audit

The case-insensitive audit rejects `token-saver`, `token_saver`, `TOKEN_SAVER`,
`Token Saver`, and `fable-token-saver` outside this exact allowlist:

- `docs/superpowers/specs/2026-07-22-model-boss-rename-design.md`, because it defines
  the migration boundary and mappings.
- The superseded dated 2026-07-21 spec and plan, which remain immutable design history.
- A single clearly titled migration section in each README and developer note.
- The `SKILL.md` description and trigger evaluations that recognize requests to migrate
  from either former name.
- Credential-migration tests and implementation literals for the explicit legacy
  `providers.env` source only.
- Git history and packaged benchmark provenance that would become misleading if its
  original recorded project label were rewritten.

No active install command, canonical URL, help output, config example, schema ID, agent
declaration, package manifest, runtime import, environment contract, or generated
artifact is allowlisted.

## Documentation and artwork

README files, SKILL instructions, references, agent prompts, examples, developer notes,
benchmarks, and generated help must introduce Model Boss consistently. The first screen
of each README explains the Boss metaphor, then immediately states that the main loop is
host-selected and immutable.

The social card is regenerated at `1774 × 887` around the Model Boss name and the
Lite/Max hierarchy. It must avoid provider-specific branding, show the product name and
primary slogan without text artifacts, and remain legible when inspected at a
`600 × 300` thumbnail. The GitHub repository description is exactly **“Big models
think. Small models ship. Cross-model coding orchestration for Claude Code and
Codex.”** Its topics are `ai-agents`, `claude-code`, `codex`, `developer-tools`,
`model-orchestration`, and `multi-model`. The committed card is also uploaded as the
GitHub social preview through the authenticated repository settings before release is
declared complete.

## Verification and release

Implementation is complete only when all of the following hold:

1. Tests are changed first to assert the new package, CLI, paths, metadata, and artifact.
2. A repository-wide old-name audit finds former names only in the approved migration
   and provenance allowlist.
3. Unit, integration, sandbox, documentation, skill-validation, packaging,
   reproducibility, and extracted-bundle smoke tests pass.
4. The new social card is visually inspected.
5. Release preflight confirms a clean worktree, an absent local destination checkout,
   availability of `vincemakes/model-boss`, authenticated repository-admin permission,
   the expected `main` default branch, and permission to push, open, and merge a pull
   request without bypassing branch protection.
6. The GitHub repository is renamed to `vincemakes/model-boss`, the local remote is
   updated, `codex/model-boss` is pushed, a pull request is created and merged, and the
   merged default branch plus repository description, topics, and social preview are
   verified.
7. The local checkout is renamed to `/Users/vinve/Desktop/devv/model-boss` only after
   all commands that depend on the old working directory have finished.

Any failed verification stops release. Repository renaming, pushing, merging, and local
directory renaming are release steps and must not conceal an uncommitted or failing
tree.

## Non-goals

- Dynamically selecting or replacing the host-selected main-loop model.
- Changing the Lite/Max state machine, evidence contract, sandbox model, or authority
  separation rules.
- Merging the separate generic `model-router` skill into this repository.
- Introducing a hosted service, UI, billing system, or provider-specific control plane.
