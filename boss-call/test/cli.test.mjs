import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const BIN = fileURLToPath(new URL("../bin/boss-call.mjs", import.meta.url));
const HOME = mkdtempSync(join(tmpdir(), "boss-call-"));
const KISO_HOME = mkdtempSync(join(tmpdir(), "kiso-home-"));
const repos = mkdtempSync(join(tmpdir(), "repos-"));
const A = join(repos, "alpha");
mkdirSync(A);

function bc(args, { cwd = repos, env = {}, input } = {}) {
	const e = { ...process.env, BOSS_CALL_HOME: HOME, KISO_HOME, ...env };
	delete e.BOSS_CALL_ROOM;
	delete e.BOSS_CALL_ME;
	return execFileSync(process.execPath, [BIN, ...args], { cwd, env: e, input, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
}

test("host / join / who from the cwd", () => {
	assert.match(bc(["host", "team"]), /you are the boss of team as boss/);
	assert.match(bc(["join", "team", "--as", "alpha"], { cwd: A }), /joined team as alpha/);
	const who = bc(["who"], { cwd: A });
	assert.match(who, /you\s+: alpha\s+\(member\)/);
	assert.match(who, /you talk only to boss/);
});

test("post/read/ack round trip through the CLI; the star holds", () => {
	assert.match(bc(["post", "--me", "boss", "--to", "alpha", "start with the tests"]), /posted #0 boss -> alpha \[msg\]/);
	const r = bc(["read", "--ack"], { cwd: A });
	assert.match(r, /#0 .* boss -> alpha \[msg\]\nstart with the tests/);
	assert.match(r, /acked through #0/);
	assert.match(bc(["read"], { cwd: A }), /no new messages for alpha/);
	assert.match(bc(["post", "--kind", "status", "tests green"], { cwd: A }), /posted #1 alpha -> boss \[status\]/);
	assert.match(bc(["post"], { cwd: A, input: "from stdin\n" }), /posted #2/);
	assert.throws(() => bc(["post", "--to", "beta", "x"], { cwd: A }), /talks only to the boss/);
	assert.throws(() => bc(["post", "--me", "boss", "x"]), /--to/);
	assert.match(bc(["status"]), /boss\s+boss\s+unread=\s+2 asks=\s+0/);
	assert.match(bc(["tail", "-n", "1"]), /#\s+2 .* alpha -> boss/);
});

test("serve --once drives a fake kiso binary with flags after the prompt", () => {
	const fake = join(repos, "fake-kiso");
	const out = join(repos, "argv.json");
	writeFileSync(fake, `#!/bin/sh\nprintf '%s\\0' "$@" > "${out}"\nmkdir -p "$KISO_HOME/sessions"\nprintf '{"runId":"r","event":{"type":"user_input","content":"x"}}\\n{"runId":"r","event":{"type":"terminal","outcome":{"kind":"completed"}}}\\n' > "$KISO_HOME/sessions/$2.jsonl"\n`);
	chmodSync(fake, 0o755);
	bc(["post", "--me", "boss", "--to", "alpha", "do the thing"]);
	const log = bc(["serve", "--once", "--kiso", fake, "--profile", "co"], { cwd: A });
	assert.match(log, /1 new message\(s\)/);
	assert.match(log, /posted an automatic status/);
	const argv = readFileSync(out, "utf8").split("\0").filter(Boolean);
	assert.equal(argv[0], "resume");
	assert.equal(argv[1], "boss-call-team-alpha");
	assert.match(argv[2], /do the thing/);
	assert.deepEqual(argv.slice(3), ["--model", "co", "--mode", "bypass"]);
	assert.match(bc(["read", "--me", "boss"]), /\[auto\] run exited 0/);
});

test("help prints the command table; unknown command exits 2", () => {
	assert.match(bc(["help"]), /boss-call join <room> --as <name>/);
	assert.throws(() => bc(["frobnicate"]), /unknown command frobnicate/);
});
