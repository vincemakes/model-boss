import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

process.env.BOSS_CALL_HOME = mkdtempSync(join(tmpdir(), "boss-call-"));
delete process.env.BOSS_CALL_ROOM;
delete process.env.BOSS_CALL_ME;
const M = await import("../src/mailbox.mjs");
const X = await import("../kiso-extension.mjs");

test("outside any member root the extension is empty", () => {
	const e = X.default();
	assert.deepEqual(e, { name: "boss-call" });
});

test("inside a member root: identity, three tools, and the shell wait is denied toward boss_wait", () => {
	const repo = mkdtempSync(join(tmpdir(), "repo-"));
	M.host("x", "boss");
	M.joinRoom("x", "m1", repo);
	const cwd = process.cwd();
	process.chdir(repo);
	try {
		const e = X.default();
		assert.match(e.systemPrompt.append, /You are "m1", a member of boss-call room "x"/);
		assert.deepEqual(e.tools.map((t) => t.name), ["boss_read", "boss_post", "boss_wait"]);
	} finally {
		process.chdir(cwd);
	}
	assert.deepEqual(X.decideCall({ name: "boss_wait", input: {} }), { action: "allow" });
	const d = X.decideCall({ name: "shell", input: { command: "cd /x && boss-call wait --timeout 20 2>&1 | tail -8" } });
	assert.equal(d.action, "deny");
	assert.match(d.reason, /Call boss_wait now/);
	assert.deepEqual(X.decideCall({ name: "shell", input: { command: "boss-call read --ack" } }), { action: "abstain" });
	assert.deepEqual(X.decideCall({ name: "read_file", input: { path: "boss-call-wait.md" } }), { action: "abstain" });
});
