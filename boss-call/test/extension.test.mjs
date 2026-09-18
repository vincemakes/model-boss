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

test("boss_wait right after one's own fresh status returns the nudge once, then waits", async () => {
	const repo = mkdtempSync(join(tmpdir(), "repo-"));
	M.host("y", "boss");
	M.joinRoom("y", "m2", repo);
	const cwd = process.cwd();
	process.chdir(repo);
	let e;
	try {
		e = X.default();
	} finally {
		process.chdir(cwd);
	}
	const wait = e.tools.find((t) => t.name === "boss_wait");
	M.post("y", { from: "m2", kind: "status", text: "done X; next: Y" });
	const first = await wait.execute({ timeoutSeconds: 5 }, {});
	assert.match(first.content, /You posted status #0 .*do that step now/);
	const t0 = Date.now();
	const second = await wait.execute({ timeoutSeconds: 1 }, {});
	assert.match(second.content, /NO MAIL YET/);
	assert.ok(Date.now() - t0 >= 900, "the second call really waited");
});

test("BOSS_CALL=off and boss-call pause make the extension empty in a member repo", () => {
	const repo = mkdtempSync(join(tmpdir(), "repo-"));
	M.host("z", "boss");
	M.joinRoom("z", "m3", repo);
	const cwd = process.cwd();
	process.chdir(repo);
	try {
		assert.equal(X.default().tools.length, 3);
		process.env.BOSS_CALL = "off";
		assert.deepEqual(X.default(), { name: "boss-call" });
		delete process.env.BOSS_CALL;
		M.setPaused("z", "m3", true);
		assert.deepEqual(X.default(), { name: "boss-call" });
		assert.equal(M.status("z").rows.find((r) => r.name === "m3").paused, true);
		M.setPaused("z", "m3", false);
		assert.equal(X.default().tools.length, 3);
	} finally {
		process.chdir(cwd);
	}
});
