import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

process.env.BOSS_CALL_HOME = mkdtempSync(join(tmpdir(), "boss-call-"));
delete process.env.BOSS_CALL_ROOM;
delete process.env.BOSS_CALL_ME;
const M = await import("../src/mailbox.mjs");

const repos = mkdtempSync(join(tmpdir(), "repos-"));
const A = join(repos, "a");
const B = join(repos, "b");
const C = join(repos, "c");
mkdirSync(join(A, "deep", "er"), { recursive: true });
mkdirSync(B);
mkdirSync(C);

test("host then join; the star is enforced on post", () => {
	M.host("r1", "boss");
	M.joinRoom("r1", "a", A);
	M.joinRoom("r1", "b", B);

	assert.throws(() => M.post("r1", { from: "boss", text: "hi" }), /--to/);
	const m0 = M.post("r1", { from: "boss", to: "all", text: "hello all" });
	assert.equal(m0.seq, 0);
	assert.throws(() => M.post("r1", { from: "a", to: "b", text: "psst" }), /talks only to the boss/);
	const m1 = M.post("r1", { from: "a", text: "done", kind: "status" });
	assert.deepEqual([m1.seq, m1.to], [1, "boss"]);
	assert.throws(() => M.post("r1", { from: "boss", to: "zed", text: "x" }), /not a member/);
	assert.throws(() => M.post("r1", { from: "a", text: "x", kind: "poke" }), /kind must be/);
	assert.throws(() => M.joinRoom("nope", "x", A), /no boss yet/);
});

test("unread is per recipient and per cursor; ack moves the cursor to the end", () => {
	assert.deepEqual(M.unread("r1", "b").map((m) => m.seq), [0]);
	assert.deepEqual(M.unread("r1", "boss").map((m) => m.seq), [1]);
	assert.deepEqual(M.unread("r1", "a").map((m) => m.seq), [0]); // own status not returned
	assert.equal(M.ack("r1", "b"), 1);
	assert.deepEqual(M.unread("r1", "b"), []);
	M.post("r1", { from: "boss", to: "b", text: "just you", kind: "msg" });
	assert.deepEqual(M.unread("r1", "b").map((m) => m.seq), [2]);
	assert.deepEqual(M.unread("r1", "a").map((m) => m.seq), [0]);
});

test("ack can stop at the last message actually delivered", () => {
	M.host("ack-batch", "boss");
	M.joinRoom("ack-batch", "worker", C);
	const first = M.post("ack-batch", { from: "boss", to: "worker", text: "first" });
	M.post("ack-batch", { from: "boss", to: "worker", text: "arrived later" });
	assert.equal(M.ack("ack-batch", "worker", first.seq), first.seq);
	assert.deepEqual(M.unread("ack-batch", "worker").map((m) => m.text), ["arrived later"]);
});

test("identity comes from the cwd, deepest root wins, and the room from the cwd too", () => {
	assert.deepEqual(M.identify("r1", undefined, join(A, "deep", "er")), { name: "a", role: "member" });
	assert.deepEqual(M.identify("r1", undefined, B), { name: "b", role: "member" });
	assert.throws(() => M.identify("r1", undefined, repos), /cannot tell who you are/);
	assert.deepEqual(M.identify("r1", "boss"), { name: "boss", role: "boss" });
	assert.throws(() => M.identify("r1", "ghost"), /not in room/);
	assert.equal(M.resolveRoom(undefined, join(A, "deep")), "r1");

	M.host("r2", "chief", { root: join(A, "deep") });
	assert.equal(M.resolveRoom(undefined, join(A, "deep", "er")), "r2"); // deeper root wins across rooms
	assert.equal(M.resolveRoom(undefined, A), "r1");
	assert.deepEqual(M.identify("r2", undefined, join(A, "deep")), { name: "chief", role: "boss" });
	assert.throws(() => M.resolveRoom(undefined, tmpdir()), /which room/);
});

test("an equal-depth repository match requires an explicit room", () => {
	M.host("same-root", "other-boss");
	M.joinRoom("same-root", "other-a", A);
	assert.throws(() => M.resolveRoom(undefined, A), /matches .*r1.*same-root/);
	assert.equal(M.resolveRoom("r1", A), "r1");
});

test("room and participant names cannot escape their state directories", () => {
	assert.throws(() => M.host("../outside", "boss"), /path-safe/);
	assert.throws(() => M.host("safe-room", "../boss"), /path-safe/);
});

test("the legacy mailbox home remains visible when the new home is absent", async () => {
	const { execFileSync } = await import("node:child_process");
	const home = mkdtempSync(join(tmpdir(), "boss-call-home-"));
	mkdirSync(join(home, ".model-boss", "boss-call"), { recursive: true });
	const env = { ...process.env, HOME: home };
	delete env.BOSS_CALL_HOME;
	const script = `import(${JSON.stringify(new URL("../src/mailbox.mjs?legacy-home-test", import.meta.url).href)}).then(M => process.stdout.write(M.HOME))`;
	const actual = execFileSync(process.execPath, ["--input-type=module", "-e", script], { env, encoding: "utf8" });
	assert.equal(actual, join(home, ".model-boss", "boss-call"));
});

test("status counts unread and asks per participant", () => {
	M.post("r1", { from: "a", text: "which db?", kind: "ask" });
	const s = M.status("r1");
	const boss = s.rows.find((r) => r.name === "boss");
	assert.equal(boss.role, "boss");
	assert.equal(boss.asks, 1);
	assert.equal(boss.unread, 2);
});

test("a torn last line in messages.jsonl is skipped, not guessed", async () => {
	const { appendFileSync } = await import("node:fs");
	appendFileSync(join(process.env.BOSS_CALL_HOME, "r1", "messages.jsonl"), '{"seq":99,"ts":"x","fr');
	const before = M.readMessages("r1").length;
	const m = M.post("r1", { from: "boss", to: "a", text: "after the tear" });
	assert.equal(m.seq, before); // seq continues from the last GOOD line
});

test("posting under contention keeps seq unique", async () => {
	const { execFileSync } = await import("node:child_process");
	const script = `import("${new URL("../src/mailbox.mjs", import.meta.url).pathname}").then(M => { for (let i = 0; i < 20; i++) M.post("r1", { from: "boss", to: "all", text: "c" + process.argv[1] + "-" + i }); })`;
	const before = M.readMessages("r1").length;
	await Promise.all([1, 2, 3].map((n) => new Promise((res, rej) => {
		import("node:child_process").then(({ execFile }) => execFile(process.execPath, ["--input-type=module", "-e", script, String(n)], { env: process.env }, (err) => (err ? rej(err) : res())));
	})));
	const seqs = M.readMessages("r1").map((m) => m.seq);
	assert.equal(seqs.length, before + 60);
	assert.equal(new Set(seqs).size, seqs.length);
	void execFileSync;
});

test("joining under contention preserves every member", async () => {
	M.host("join-race", "boss");
	const moduleUrl = new URL("../src/mailbox.mjs", import.meta.url).href;
	const script = `import(${JSON.stringify(moduleUrl)}).then(M => M.joinRoom("join-race", process.argv[1], process.argv[2]))`;
	await Promise.all(Array.from({ length: 12 }, (_, i) => new Promise((res, rej) => {
		import("node:child_process").then(({ execFile }) => execFile(
			process.execPath,
			["--input-type=module", "-e", script, `member-${i}`, A],
			{ env: process.env },
			(err) => (err ? rej(err) : res()),
		));
	})));
	assert.equal(Object.keys(M.loadRoom("join-race").members).length, 12);
});

test("waitForMail returns as soon as mail lands, acknowledged, and leaves a heartbeat", async () => {
	const { execFile } = await import("node:child_process");
	const t0 = Date.now();
	const script = `setTimeout(() => import("${new URL("../src/mailbox.mjs", import.meta.url).pathname}").then(M => M.post("r1", { from: "boss", to: "b", text: "wake up" })), 300)`;
	const child = new Promise((res, rej) => execFile(process.execPath, ["--input-type=module", "-e", script], { env: process.env }, (err) => (err ? rej(err) : res())));
	M.ack("r1", "b");
	const msgs = M.waitForMail("r1", "b", { timeoutMs: 5000, pollMs: 50 });
	await child;
	assert.equal(msgs.at(-1).text, "wake up");
	assert.ok(Date.now() - t0 < 3000, "returned promptly, not at the timeout");
	assert.deepEqual(M.unread("r1", "b"), []); // acknowledged
	assert.equal(M.readHeartbeat("r1", "b").state, "working");
	assert.equal(M.status("r1").rows.find((r) => r.name === "b").heartbeat.state, "working");
});

test("waitForMail times out empty and leaves a waiting heartbeat", () => {
	const t0 = Date.now();
	assert.deepEqual(M.waitForMail("r1", "b", { timeoutMs: 200, pollMs: 50 }), []);
	assert.ok(Date.now() - t0 >= 190);
	assert.equal(M.readHeartbeat("r1", "b").state, "waiting");
});

test("waitForMailAsync resolves on mail, and resolves [] promptly when the signal aborts", async () => {
	const ac = new AbortController();
	const t0 = Date.now();
	const aborted = M.waitForMailAsync("r1", "b", { timeoutMs: 10_000, pollMs: 50, signal: ac.signal });
	setTimeout(() => ac.abort(), 120);
	assert.deepEqual(await aborted, []);
	assert.ok(Date.now() - t0 < 2000, "abort ended the wait, not the timeout");

	const p = M.waitForMailAsync("r1", "b", { timeoutMs: 5000, pollMs: 50 });
	setTimeout(() => M.post("r1", { from: "boss", to: "b", text: "async wake" }), 150);
	const msgs = await p;
	assert.equal(msgs.at(-1).text, "async wake");
	assert.deepEqual(M.unread("r1", "b"), []);
});
