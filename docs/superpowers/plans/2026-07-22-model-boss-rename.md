# Model Boss Rename Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename every active Token Saver product surface to Model Boss, preserve the sealed Lite/Max protocol, verify the installable artifact, and publish the result as `vincemakes/model-boss` with the local checkout at `/Users/vinve/Desktop/devv/model-boss`.

**Architecture:** Treat the work as an interface migration, not a protocol rewrite. Encode the new public contract in failing tests, move the Python/config/CLI identities without changing state-machine fields, rebrand installation and documentation surfaces, rebuild the deterministic skill archive, and run the complete release gate. Rename the GitHub repository and local checkout only after the committed tree is clean and independently reviewed.

**Tech Stack:** Python 3 standard library, `unittest`, POSIX shell, Git/GitHub CLI, Claude Code and Codex agent declarations, deterministic ZIP skill packaging, image generation for the social card.

---

## File structure and responsibilities

- `runtime/model_boss/` — renamed runtime package; protocol behavior remains unchanged.
- `scripts/model-boss.py` — canonical CLI entry script.
- `config/model-boss.example.json` and `config/model-boss.schema.json` — canonical configuration and schema identity.
- `tests/test_branding.py` — deterministic canonical-file and former-name audit.
- Existing `tests/test_*.py` — behavioral coverage under `runtime.model_boss` imports and Model Boss paths.
- `SKILL.md`, `README.md`, `README.zh-CN.md`, `references/`, and `docs/DEVNOTES.zh-CN.md` — product identity, setup, protocol, and migration instructions.
- `assets/agents/` and `agents/openai.yaml` — role-only source filenames with Model Boss declaration names and install destinations.
- `runtime/model_boss/package.py`, `scripts/package-skill.sh`, and `scripts/validate.sh` — deterministic `dist/model-boss.skill` production and validation.
- `media/og.png` — `1774 × 887` Model Boss social card.
- The dated 2026-07-21 plan/spec — immutable superseded design history, not active installation guidance.

## Chunk 1: Canonical code and installation identity

### Task 1: Lock the new identity with failing tests

**Files:**
- Create: `tests/test_branding.py`
- Modify: `tests/test_skill_content.py`
- Modify: `tests/test_docs.py`
- Modify: `tests/test_agent_assets.py`
- Modify: `tests/test_package.py`

- [ ] **Step 1: Add a deterministic branding audit**

Create `tests/test_branding.py`. It must scan text files outside `.git`, `dist`, cache
directories, and binary files; require the five canonical Model Boss files; reject the
five obsolete active paths; and permit former names only through these exact rules:

```python
FULL_FILE_ALLOWLIST = {
    "docs/superpowers/specs/2026-07-21-token-saver-cross-platform-design.md",
    "docs/superpowers/plans/2026-07-21-token-saver-cross-platform.md",
    "docs/superpowers/specs/2026-07-22-model-boss-rename-design.md",
    "docs/superpowers/plans/2026-07-22-model-boss-rename.md",
    "tests/test_branding.py",
}
MIGRATION_SECTIONS = {
    "README.md": "## Migrating from Token Saver",
    "README.zh-CN.md": "## 从 Token Saver 迁移",
    "docs/DEVNOTES.zh-CN.md": "## 从 Token Saver 迁移",
}
LINE_ALLOWLIST = {
    "SKILL.md": (
        r'^\s+.*migrat.*(?:token saver|fable-token-saver).*$' ,
    ),
    "runtime/model_boss/cli.py": (
        r'^\s*home / "\.claude" / "fable-token-saver" / "providers\.env"\s*$',
    ),
    "runtime/model_boss/package.py": (
        r'^\s*if .*\.name (?:==|in) .*"(?:token-saver|fable-token-saver)\.skill".*$',
    ),
    "tests/test_setup_credentials.py": (r'^.*fable-token-saver.*$',),
    "tests/test_setup_wrappers.py": (r'^.*fable-token-saver.*$',),
    "tests/test_docs.py": (
        r'^.*(?:Migrating from Token Saver|从 Token Saver 迁移).*$',
    ),
    "tests/test_skill_content.py": (
        r'^.*migrate from Token Saver or fable-token-saver.*$',
    ),
    "tests/test_package.py": (
        r'^.*(?:token-saver|fable-token-saver)\.skill.*$',
    ),
    "tests/test_evals.py": (
        r'^.*(?:token saver|fable-token-saver).*$' ,
    ),
    "evals/evals.json": (
        r'^\s*"prompt": ".*(?:token saver|fable-token-saver).*"[,]?\s*$',
    ),
}
```

Compile former-name detection as case-insensitive
`r"token[-_ ]saver|fable-token-saver"`. A README/devnote hit is allowed only while the
current level-two section heading equals its exact `MIGRATION_SECTIONS` value. A line
rule must fully match one compiled regex; substring markers are forbidden. Report every
violation as `path:line:text` in one assertion.

- [ ] **Step 2: Change focused expectations to the approved design**

Update existing tests to require all of these exact facts:

- `SKILL.md` metadata name is `model-boss`, `$model-boss` is the default prompt, and
  its title and commands use Model Boss/`scripts/model-boss.py`; its description contains
  the exact migration clause from Step 1; existing state/evidence/Lite/Max assertions
  remain unchanged.
- Both READMEs use `# Model Boss`, show “Big models think. Small models ship.” and
  “Cross-model coding orchestration” in their first twelve nonblank lines, state there
  that the main loop is inherited, explain that Boss means the workflow's authority
  holder, clarify that big/small roles are relative rather than a universal model
  ranking, use `https://github.com/vincemakes/model-boss`, and contain the exact
  migration headings from Step 1.
- Claude declarations use `model-boss-<role>`; Codex TOML declarations use
  `model_boss_<role>`; source filenames remain role-only.
- Package tests expect archive root `model-boss/`, imports under
  `runtime/model_boss/`, `scripts/model-boss.py`, and output `model-boss.skill`; focused
  tests require obsolete `token-saver.skill` and `fable-token-saver.skill` outputs to
  fail safely.

- [ ] **Step 3: Run the focused tests and observe the red state**

```bash
python3 -m unittest tests.test_branding tests.test_skill_content tests.test_docs tests.test_agent_assets tests.test_package -v
```

Expected: FAIL because the tree still exposes the former product identity.

- [ ] **Step 4: Commit only the red tests**

```bash
git add tests/test_branding.py tests/test_skill_content.py tests/test_docs.py tests/test_agent_assets.py tests/test_package.py
git commit -m "test: define model boss identity"
```

### Task 2: Move the runtime, CLI, config, and path contract

**Files:**
- Rename: `runtime/token_saver/` → `runtime/model_boss/`
- Rename: `scripts/token-saver-route.py` → `scripts/model-boss.py`
- Rename: `config/token-saver.example.json` → `config/model-boss.example.json`
- Rename: `config/token-saver.schema.json` → `config/model-boss.schema.json`
- Modify: `runtime/model_boss/*.py`
- Modify: `tests/test_bundle.py`, `tests/test_cli.py`, `tests/test_config.py`, `tests/test_evidence_encoding.py`, `tests/test_integration.py`, `tests/test_process.py`, `tests/test_provider_exec.py`, `tests/test_repository.py`, `tests/test_resources.py`, `tests/test_reviewer_transport.py`, `tests/test_routing.py`, `tests/test_sandbox_linux.py`, `tests/test_sandbox_macos.py`, `tests/test_sandbox_unit.py`, `tests/test_setup_credentials.py`, `tests/test_setup_wrappers.py`, `tests/test_worker_transport.py`
- Modify: `scripts/setup-model-providers.sh`

- [ ] **Step 1: Move the canonical files**

```bash
git mv runtime/token_saver runtime/model_boss
git mv scripts/token-saver-route.py scripts/model-boss.py
git mv config/token-saver.example.json config/model-boss.example.json
git mv config/token-saver.schema.json config/model-boss.schema.json
```

- [ ] **Step 2: Replace imports and runtime-visible prefixes with an explicit hit manifest**

First record every hit:

```bash
rg -n 'runtime\.token_saver|token_saver|TOKEN_SAVER_|token-saver|Token Saver' runtime/model_boss scripts config tests
```

Apply only these categories:

```text
runtime.token_saver        -> runtime.model_boss
runtime/token_saver        -> runtime/model_boss
TOKEN_SAVER_               -> MODEL_BOSS_
token-saver-invocation     -> model-boss-invocation
.token-saver               -> .model-boss
token-saver-worker         -> model-boss-worker
token-saver-* temp prefixes -> model-boss-* temp prefixes
token_saver test/import identifiers -> model_boss
Token Saver active runtime/config/test prose -> Model Boss
```

Do not mechanically edit `tests/test_branding.py`, the two obsolete-output assertions
in `tests/test_package.py`, or the explicit `fable-token-saver/providers.env` migration
literals. Apply the prose replacement to active runtime errors, docstrings, CLI help,
schema metadata, and test expectations, while leaving package manifest/product hits to
Task 3 and user-facing documentation to Chunk 2.

- [ ] **Step 3: Implement exact config and credential discovery**

In `runtime/model_boss/config.py`, return `<config-root>/model-boss/config.json` and
`<repo>/.model-boss.json`. In `runtime/model_boss/cli.py`, use
`MODEL_BOSS_CREDENTIALS`, `MODEL_BOSS_INVOCATION_MANIFEST`,
`MODEL_BOSS_TRUSTED_GATE_FAILURES`, and `<config-root>/model-boss/credentials.json`.
Normal discovery must ignore old JSON paths and `TOKEN_SAVER_*` variables. Preserve
only the explicit no-overwrite legacy `~/.claude/fable-token-saver/providers.env`
migration in `setup-providers`.

- [ ] **Step 4: Verify the remaining former-name hits are deliberately deferred**

Re-run the Step 2 `rg` command. Every remaining hit must belong to one of: package
identity owned by Task 3, active documentation owned by Chunk 2, the branding audit,
obsolete-output rejection tests, or explicit credential migration. Unexpected runtime
or test identifiers block the commit.

- [ ] **Step 5: Run focused runtime tests**

```bash
python3 -m unittest tests.test_config tests.test_cli tests.test_setup_credentials tests.test_setup_wrappers tests.test_provider_exec -v
```

Expected: PASS for the new path/environment contract and explicit legacy migration.

- [ ] **Step 6: Commit the runtime rename**

```bash
git add runtime scripts config tests
git commit -m "refactor: rename runtime to model boss"
```

### Task 3: Rename skill, agent, and package installation surfaces

**Files:**
- Modify: `SKILL.md`
- Modify: `assets/agents/claude-code/*.md`
- Modify: `assets/agents/codex/*.toml`
- Modify: `agents/openai.yaml`
- Modify: `runtime/model_boss/package.py`
- Modify: `scripts/package-skill.sh`
- Modify: `scripts/validate.sh`
- Delete: `dist/token-saver.skill`
- Create: `dist/model-boss.skill`

- [ ] **Step 1: Establish the new skill and agent identities**

Set only the `SKILL.md` title/frontmatter identity needed by packaging to Model Boss:
use `name: model-boss` and retain the existing folded `description: >-` form under
1024 characters. The folded value begins `Use when`, contains Model Boss triggers, and contains the
former names only in one exact `migrate from Token Saver or fable-token-saver` clause.
Leave the remaining skill body for Task 5 so its focused assertions stay red. Set Claude
frontmatter to `model-boss-<role>` and Codex TOML names to `model_boss_<role>`; keep the
role-only source filenames, model IDs, sandbox settings, and reasoning effort unchanged.
Update `agents/openai.yaml` to invoke `$model-boss`.

- [ ] **Step 2: Rename the package and validator contract**

In `runtime/model_boss/package.py`, set the archive root to `model-boss`, enumerate the
new config/runtime/CLI paths, require `name: model-boss`, reject output files named
`token-saver.skill` or `fable-token-saver.skill`, and use Model Boss in errors. Make
`scripts/package-skill.sh` write `dist/model-boss.skill`; make `scripts/validate.sh`
reproduce, compare, validate, and unzip the new artifact.

- [ ] **Step 3: Build the interim canonical artifact**

```bash
git rm dist/token-saver.skill
bash scripts/package-skill.sh
```

Expected: `dist/model-boss.skill` exists and the obsolete artifact does not. This
interim archive is rebuilt after all documentation and artwork changes in Chunk 2.

- [ ] **Step 4: Run installation-surface tests**

```bash
python3 -m unittest \
  tests.test_agent_assets \
  tests.test_package \
  tests.test_branding.BrandingContractTests.test_canonical_files_exist_and_former_active_files_do_not \
  -v
```

Expected: PASS for agent declarations, archive manifest, reproducibility, obsolete
output rejection, and canonical files. The full former-name audit remains red until
Chunk 2.

- [ ] **Step 5: Commit installation identity**

```bash
git add SKILL.md assets agents runtime/model_boss/package.py scripts dist tests
git commit -m "build: package model boss"
```

## Chunk 2: Public language, migration, artwork, and validation

### Task 4: Rewrite READMEs and migration guidance

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/DEVNOTES.zh-CN.md`
- Modify: `tests/test_docs.py`

- [ ] **Step 1: Run the already-red documentation assertions**

```bash
python3 -m unittest tests.test_docs -v
```

Expected: FAIL on Model Boss title, first-screen positioning, canonical links/commands,
and migration headings.

- [ ] **Step 2: Rewrite active setup and overview content**

Lead both READMEs with Model Boss, the primary slogan, technical subtitle, and the
immutable-main-loop clarification. In that first screen, define Boss as the authority
holder and say explicitly that “big” and “small” describe workflow-relative roles, not
a universal ranking of providers or models. Preserve Lite/Max and all fail-closed
claims. Rename clone URLs, skill/install directories, Claude/Codex installed agent
filenames, CLI commands, temp-parent examples, config paths, credentials, environment
variables, artifact names, and cross-links.

- [ ] **Step 3: Add the exact migration sections**

Add `## Migrating from Token Saver`, `## 从 Token Saver 迁移`, and the same Chinese
heading in the devnote. Map repository, install directory, CLI, Python import, project
config, user config, credentials, environment variables, agent names, and artifact.
State that normal discovery ignores old paths; `setup-providers` alone may read the
legacy env file explicitly; it never overwrites or deletes legacy data.

- [ ] **Step 4: Run documentation tests and commit**

```bash
python3 -m unittest tests.test_docs -v
```

Expected: PASS.

```bash
git add README.md README.zh-CN.md docs/DEVNOTES.zh-CN.md tests/test_docs.py
git commit -m "docs: introduce model boss"
```

### Task 5: Rebrand the skill protocol and adapter references

**Files:**
- Modify: `SKILL.md`
- Modify: `references/protocol.md`
- Modify: `references/routing.md`
- Modify: `references/adapters/claude-code.md`
- Modify: `references/adapters/codex.md`
- Modify: `references/adapters/external-cli.md`
- Modify: `references/profiles/*.json`
- Modify: `assets/agents/prompts/*.md`
- Modify: `assets/agents/claude-code/*.md`
- Modify: `assets/agents/codex/*.toml`
- Modify: `tests/test_skill_content.py`
- Modify: `tests/test_agent_assets.py`

- [ ] **Step 1: Run the focused skill assertions**

```bash
python3 -m unittest tests.test_skill_content tests.test_agent_assets -v
```

Expected: FAIL while active protocol prose and commands still use the former identity.

- [ ] **Step 2: Rebrand without changing the protocol**

Use Model Boss and `scripts/model-boss.py` throughout. Describe “Boss” as the authority
holder: inline main loop in Lite, distinct verified reviewer in Max. Preserve exactly
the ordered states, sealed `authority_mode`, canonical fingerprints, three-hash
approval, evidence requirements, failure statuses, two-revision limit, sandbox rules,
and inherited-main-loop invariant. Keep provider/model names only in profiles/examples,
not state transitions.

- [ ] **Step 3: Run skill/reference tests and commit**

```bash
python3 -m unittest tests.test_skill_content tests.test_agent_assets -v
```

Expected: PASS with no protocol assertion changes.

```bash
git add SKILL.md references assets tests/test_skill_content.py tests/test_agent_assets.py
git commit -m "docs: rebrand model boss protocol"
```

### Task 6: Rebrand benchmark and evaluation surfaces

**Files:**
- Modify: `BENCHMARKS.md`
- Modify: `BENCHMARKS.zh-CN.md`
- Modify: `benchmarks/benchmark.json`
- Modify: `benchmarks/benchmark.md`
- Modify: `benchmarks/trigger-eval.json`
- Modify: `evals/evals.json`
- Modify: `evals/routing-evals.json`
- Modify: `evals/skill-pressure-results.md`
- Modify: `evals/skill-pressure-scenarios.md`
- Modify: `tests/test_evals.py`

- [ ] **Step 1: Change eval assertions first and observe failure**

Require active scenario labels and trigger phrases to use Model Boss, require positive
Model Boss triggers, and retain exactly two migration-trigger prompts containing the
former names in `evals/evals.json`. Do not alter recorded numeric measurements.

```bash
python3 -m unittest tests.test_evals -v
```

Expected: FAIL on old active labels or missing Model Boss triggers.

- [ ] **Step 2: Rewrite active labels while preserving provenance**

Rename report/scenario titles and prose to Model Boss, update current paths and command
examples, retain all measurements and caveats, and keep former names only in the two
explicit migration-trigger prompt lines allowed by `tests/test_branding.py`.

- [ ] **Step 3: Run eval tests and commit**

```bash
python3 -m unittest tests.test_evals -v
```

Expected: PASS with unchanged benchmark values.

```bash
git add BENCHMARKS.md BENCHMARKS.zh-CN.md benchmarks evals tests/test_evals.py
git commit -m "test: rebrand model boss evaluations"
```

### Task 7: Regenerate and inspect the social card

**Files:**
- Modify: `media/og.png`

- [ ] **Step 1: Load image-generation instructions and inspect the current card**

Read the `imagegen` skill completely, inspect `media/og.png` at original detail, and use
it only as a layout reference. The imagegen skill controls the edit operation.

- [ ] **Step 2: Generate the replacement**

Generate exactly `1774 × 887` with this direction:

```text
GitHub social preview for an open-source developer tool named “MODEL BOSS”. Minimal
editorial technical identity, dark neutral background, crisp high-contrast typography,
restrained electric accent, no provider logos, no mascots. Prominently typeset “MODEL
BOSS” and “Big models think. Small models ship.” Show two compact hierarchy diagrams:
LITE has the main loop leading an optional worker; MAX has an authority reviewer above
a main loop with an optional worker below. All labels must remain readable at 600×300.
No extra prose, malformed text, or low-contrast decoration.
```

- [ ] **Step 3: Verify dimensions and both inspection sizes**

```bash
file media/og.png
sips -g pixelWidth -g pixelHeight media/og.png
preview_dir=$(mktemp -d)
sips -z 300 600 media/og.png --out "$preview_dir/og-600x300.png"
```

Expected: original is `1774 × 887`; thumbnail is `600 × 300`. Inspect both images and
regenerate if any required text, label, hierarchy, or contrast is wrong.

- [ ] **Step 4: Commit the card**

```bash
git add media/og.png
git commit -m "docs: rebrand model boss social card"
```

### Task 8: Rebuild and run the complete local release gate

**Files:**
- Regenerate: `dist/model-boss.skill`
- Modify only if a gate exposes a defect: files already in scope above

- [ ] **Step 1: Rebuild after every packaged source change**

```bash
bash scripts/package-skill.sh
```

Expected: the tracked `dist/model-boss.skill` contains the final docs, references,
agents, runtime, config, CLI, and media inputs selected by the package manifest.

- [ ] **Step 2: Run and inspect the former-name audit**

```bash
python3 -m unittest tests.test_branding -v
rg -n -i 'token[-_ ]saver|fable-token-saver' --glob '!dist/**' --glob '!.git/**'
```

Expected: the test passes; every printed hit is one exact allowlisted
migration/provenance/rejection case.

- [ ] **Step 3: Run the complete validator**

```bash
bash scripts/validate.sh
```

Expected: all Python tests pass; Linux sandbox may skip only when its verified backend
is unavailable; skill validation passes; the committed package matches a fresh build
byte-for-byte; archive validation and unzip checks pass.

- [ ] **Step 4: Smoke-test the extracted archive with exact commands**

```bash
bundle_tmp=$(mktemp -d)
unzip -q dist/model-boss.skill -d "$bundle_tmp"
test -d "$bundle_tmp/model-boss"
test "$(find "$bundle_tmp" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = "1"
cd "$bundle_tmp/model-boss"
python3 scripts/model-boss.py --help
python3 scripts/model-boss.py validate-config config/model-boss.example.json
```

Expected: one archive root, both CLI commands exit `0`, help identifies Model Boss, and
the bundled example config validates.

- [ ] **Step 5: Run the named platform/sandbox smoke suites**

From the repository root run:

```bash
python3 -m unittest tests.test_sandbox_macos tests.test_sandbox_linux tests.test_setup_wrappers tests.test_provider_exec -v
```

Expected: supported current-host checks pass; Linux may report only the explicit
verified-backend skip; native Windows behavior remains covered by unit fixtures and
fails closed for unsupported external writers.

- [ ] **Step 6: Commit the rebuilt final bundle before review**

```bash
git add dist/model-boss.skill
git commit -m "build: finalize model boss bundle"
```

Expected: the final deterministic artifact is part of `HEAD` and the worktree is clean.
If the rebuild is already byte-identical to the tracked artifact, verify the clean tree
and skip only this commit.

- [ ] **Step 7: Request independent review and close blocking findings**

Use `superpowers:requesting-code-review` against `origin/main...HEAD`. After fixes, run
their focused tests and repeat Steps 1–5. Rebuild the artifact after every packaged
source fix, stage source plus `dist/model-boss.skill`, and commit before re-requesting
review:

```bash
git add -A
git commit -m "chore: finalize model boss release"
```

Expected: clean worktree, all reviewed bytes committed in `HEAD`, approved review, and
a fresh successful complete validator.

## Chunk 3: Publish, merge, and rename the checkout

### Task 9: Rename and publish the GitHub repository

**External state:**
- Rename repository: `vincemakes/token-saver` → `vincemakes/model-boss`
- Rename branch: `codex/token-saver-cross-platform` → `codex/model-boss`
- Update local `origin`, repository description, topics, and social preview

- [ ] **Step 1: Run non-mutating release preflight**

```bash
git status --short
git branch --show-current
git fetch origin --prune
git merge-base --is-ancestor origin/main HEAD
if git show-ref --verify --quiet refs/heads/codex/model-boss; then
  local_ref_status=0
else
  local_ref_status=$?
fi
case "$local_ref_status" in
  0) echo "local target branch already exists" >&2; exit 1 ;;
  1) ;;
  *) echo "local ref lookup failed" >&2; exit 1 ;;
esac
if git ls-remote --exit-code --heads origin refs/heads/codex/model-boss >/dev/null 2>&1; then
  remote_ref_status=0
else
  remote_ref_status=$?
fi
case "$remote_ref_status" in
  0) echo "remote target branch already exists" >&2; exit 1 ;;
  2) ;;
  *) echo "remote ref lookup failed" >&2; exit 1 ;;
esac
test ! -e /Users/vinve/Desktop/devv/model-boss
gh auth status
gh repo view vincemakes/token-saver --json defaultBranchRef,viewerPermission,url
gh api repos/vincemakes/token-saver --jq '{allow_merge_commit,allow_squash_merge,allow_rebase_merge}'
gh api repos/vincemakes/token-saver/rules/branches/main
```

Expected: clean tree; current branch `codex/token-saver-cross-platform`; `origin/main`
is an ancestor of the reviewed head; both local and remote `codex/model-boss` lookups
report no ref; destination absent; authenticated `vincemakes`; `ADMIN`; default branch
`main`; and at least one merge strategy compatible with the returned branch rules is
enabled. The rules endpoint normally returns HTTP 200 with an array; an empty array
means no matching ruleset. A failed ancestor check requires integrating `origin/main`
and repeating the entire local release gate. Network/auth errors are blockers.

Query classic branch protection separately and distinguish its benign 404:

```bash
protection_out=$(mktemp)
protection_err=$(mktemp)
if ! gh api repos/vincemakes/token-saver/branches/main/protection >"$protection_out" 2>"$protection_err"; then
  rg -q 'Branch not protected.*HTTP 404' "$protection_err"
fi
```

Expected: either valid protection JSON or the explicit GitHub “Branch not protected”
404. A generic Not Found, authentication failure, or network failure is a blocker.

Check target-name availability without treating every API failure as availability:

```bash
target_out=$(mktemp)
target_err=$(mktemp)
if gh api repos/vincemakes/model-boss >"$target_out" 2>"$target_err"; then
  echo "target repository already exists" >&2
  exit 1
fi
rg -q 'HTTP 404|Not Found' "$target_err"
```

Expected: the request fails specifically with GitHub HTTP 404. Any other response stops
remote mutation.

- [ ] **Step 2: Rename the branch and GitHub repository**

```bash
git branch -m codex/model-boss
gh repo rename model-boss --repo vincemakes/token-saver --yes
git remote set-url origin https://github.com/vincemakes/model-boss.git
```

Expected: current branch `codex/model-boss`; canonical origin URL; the new repository
lookup succeeds; the old URL redirects through GitHub. Record the reviewed head with
`git rev-parse HEAD`; it is the only head eligible for the PR merge.

- [ ] **Step 3: Set and verify repository metadata**

```bash
gh repo edit vincemakes/model-boss --description "Big models think. Small models ship. Cross-model coding orchestration for Claude Code and Codex."
gh api --method PUT repos/vincemakes/model-boss/topics --input -
```

Pass this exact JSON to the second command on standard input:

```json
{"names":["ai-agents","claude-code","codex","developer-tools","model-orchestration","multi-model"]}
```

Verify explicitly:

```bash
gh repo view vincemakes/model-boss --json nameWithOwner,url,description,defaultBranchRef
gh api repos/vincemakes/model-boss/topics --jq '.names | sort | join(",")'
```

Expected: `vincemakes/model-boss`, canonical URL, default branch `main`, exact
description, and sorted topics
`ai-agents,claude-code,codex,developer-tools,model-orchestration,multi-model`.

- [ ] **Step 4: Push and create the pull request**

```bash
git push -u origin codex/model-boss
gh pr create --base main --head codex/model-boss --title "feat: launch Model Boss" --body "Renames Token Saver to Model Boss, preserves Lite/Max authority semantics, and ships the verified cross-platform package."
git rev-parse HEAD
git ls-remote origin refs/heads/codex/model-boss
gh pr view --json url,headRefOid,isDraft,state
```

Expected: push succeeds; PR is open and non-draft; `headRefOid`, local `HEAD`, and the
remote branch SHA are identical. Capture the returned PR URL and reviewed head SHA.

- [ ] **Step 5: Wait for checks and merge the pinned head normally**

Set `pr_url=$(gh pr view --json url --jq .url)` and inspect `gh pr checks "$pr_url"
--json name,state,bucket,workflow`. Derive required status-check contexts from both the
saved classic protection JSON (`required_status_checks`) and matching ruleset entries
of type `required_status_checks`. Run `gh pr checks "$pr_url" --required --watch
--fail-fast`. If it reports that no required checks exist, proceed only when both API
sources also contain zero required contexts; then watch any non-required checks with
`gh pr checks "$pr_url" --watch --fail-fast`. A failure, cancellation, authentication
error, contradictory required-check state, or timeout stops the merge; zero configured
checks is acceptable only when the initial checks JSON is empty.

Re-read repository merge settings and branch rules. If a matching rule requires a merge
queue, submit strategy-less so GitHub can enqueue it. Otherwise choose the first
compatible enabled strategy: merge commit when permitted and linear history is not
required, squash when enabled, then rebase when enabled. Run exactly one of:

```bash
pr_url=$(gh pr view --json url --jq .url)
reviewed_head=$(git rev-parse HEAD)
gh pr checks "$pr_url" --json name,state,bucket,workflow
gh pr merge "$pr_url" --delete-branch --match-head-commit "$reviewed_head"
gh pr merge "$pr_url" --merge --delete-branch --match-head-commit "$reviewed_head"
gh pr merge "$pr_url" --squash --delete-branch --match-head-commit "$reviewed_head"
gh pr merge "$pr_url" --rebase --delete-branch --match-head-commit "$reviewed_head"
```

The last four merge commands are alternatives; execute only the queue-compatible or
selected normal strategy. Never
use `--admin`, force push, or bypass protection. If GitHub queues the merge, poll
`gh pr view "$pr_url" --json state,mergeCommit` in short intervals until `state` is
`MERGED`; a closed-without-merge state or ten-minute timeout is a blocker. Capture
`merge_oid=$(gh pr view "$pr_url" --json mergeCommit --jq .mergeCommit.oid)`, fetch
`origin`, and run `git merge-base --is-ancestor "$merge_oid" origin/main`. Expected:
success and the reviewed PR is contained in remote `main`.

- [ ] **Step 6: Upload and inspect the GitHub social preview**

Use the authenticated GitHub repository Settings page to upload `media/og.png`. Verify
the rendered preview shows `MODEL BOSS`, the slogan, and both hierarchies without
cropping. Reload the Settings page and capture a screenshot proving the persisted image
is still selected. If browser automation cannot reach the authenticated control, stop
and report this single external UI blocker rather than claiming release completion.

- [ ] **Step 7: Synchronize local `main` and verify the merge**

```bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
pr_url=$(gh pr list --state merged --head codex/model-boss --limit 1 --json url --jq '.[0].url')
merge_oid=$(gh pr view "$pr_url" --json mergeCommit --jq .mergeCommit.oid)
git merge-base --is-ancestor "$merge_oid" origin/main
shasum -a 256 dist/model-boss.skill
```

Expected: clean `main`; local and remote IDs match; canonical README, skill metadata,
CLI, and `dist/model-boss.skill` exist at that commit; the captured merge commit is on
`origin/main`; and the final artifact SHA-256 is recorded.

### Task 10: Rename the local checkout last

**External state:**
- Rename: `/Users/vinve/Desktop/devv/fable-token-saver` → `/Users/vinve/Desktop/devv/model-boss`

- [ ] **Step 1: Reconfirm exact source and destination**

```bash
test -d /Users/vinve/Desktop/devv/fable-token-saver/.git
test ! -e /Users/vinve/Desktop/devv/model-boss
git -C /Users/vinve/Desktop/devv/fable-token-saver status --short
```

Expected: exact source exists, destination is absent, worktree is clean.

- [ ] **Step 2: Move only the explicit checkout and verify from the new path**

```bash
cd /Users/vinve/Desktop/devv
mv fable-token-saver model-boss
test ! -e /Users/vinve/Desktop/devv/fable-token-saver
test -d /Users/vinve/Desktop/devv/model-boss/.git
git -C /Users/vinve/Desktop/devv/model-boss status --short --branch
test "$(git -C /Users/vinve/Desktop/devv/model-boss branch --show-current)" = "main"
test "$(git -C /Users/vinve/Desktop/devv/model-boss rev-parse HEAD)" = "$(git -C /Users/vinve/Desktop/devv/model-boss rev-parse origin/main)"
test "$(git -C /Users/vinve/Desktop/devv/model-boss remote get-url origin)" = "https://github.com/vincemakes/model-boss.git"
```

Expected: old path absent; new path is a clean `main` checkout with matching remote head
and canonical origin. No tracked files were deleted; the directory move can be reversed
by renaming it back if needed.

- [ ] **Step 3: Report final release facts**

Report merged PR URL and commit, validation result, package SHA-256, GitHub URL, and new
local path. State that the checkout was renamed in place and no duplicate remains.
