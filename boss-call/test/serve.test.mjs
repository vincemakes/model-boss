import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

process.env.BOSS_CALL_HOME = mkdtempSync(join(tmpdir(), "boss-call-"));
process.env.KISO_HOME = mkdtempSync(join(tmpdir(), "kiso-home-"));
delete process.env.BOSS_CALL_ROOM;
delete process.env.BOSS_CALL_ME;
const M = await import("../src/mailbox.mjs");
const S = await import("../src/serve.mjs");

const repo = mkdtempSync(join(tmpdir(), "repo-"));
M.host("w", "boss");
M.joinRoom("w", "worker", repo, { kiso: { profile: "co" } });

test("kisoArgv puts every flag AFTER the positional prompt (kiso parses it that way)", () => {
	const argv = S.kisoArgv({ session: "sid", prompt: "the prompt", profile: "co", mode: "bypass" });
	assert.deepEqual(argv, ["kiso", "resume", "sid", "the prompt", "--model", "co", "--mode", "bypass"]);
	assert.deepEqual(S.kisoArgv({ session: "sid", prompt: "p", mode: null }), ["kiso", "resume", "sid", "p"]);
});

test("parseEnvFile: KEY=V, export, quotes, comments; ~ expands", () => {
	const p = join(repo, "creds.env");
	writeFileSync(p, `# creds\nexport A_KEY="x y"\nB='z'\nC=plain\n\nnot a line\n`);
	assert.deepEqual(S.parseEnvFile(p), { A_KEY: "x y", B: "z", C: "plain" });
	assert.throws(() => S.parseEnvFile(join(repo, "missing.env")), /env file not found/);
	assert.deepEqual(S.parseEnvFile(undefined), {});
});

test("serve --once: hands mail to kiso as one turn, acks after exit, auto-status when the model posted nothing", () => {
	M.post("w", { from: "boss", to: "worker", text: "port the validator" });
	M.post("w", { from: "boss", to: "all", text: "and say hi" });
	const calls = [];
	const logs = [];
	// a fake kiso that writes a log and posts nothing
	const spawn = (bin, args, opts) => {
		calls.push({ bin, args, cwd: opts.cwd, env: opts.env });
		mkdirSync(join(process.env.KISO_HOME, "sessions"), { recursive: true });
		writeFileSync(join(process.env.KISO_HOME, "sessions", `${args[1]}.jsonl`), [
			JSON.stringify({ runId: "r1", event: { type: "user_input", content: args[2] } }),
			JSON.stringify({ runId: "r1", event: { type: "text_delta", text: "I did it." } }),
			JSON.stringify({ runId: "r1", event: { type: "terminal", outcome: { kind: "completed" } } }),
		].join("\n") + "\n");
		return { status: 0 };
	};
	const exit = S.serve({ room: "w", me: "worker", once: true, log: (l) => logs.push(l), spawn });
	assert.equal(exit, 0);
	assert.equal(calls.length, 1);
	const c = calls[0];
	assert.equal(c.bin, "kiso");
	assert.deepEqual(c.args.slice(0, 2), ["resume", "boss-call-w-worker"]);
	assert.match(c.args[2], /port the validator/);
	assert.match(c.args[2], /and say hi/);
	assert.match(c.args[2], /You are worker, a member/);
	assert.deepEqual(c.args.slice(3), ["--model", "co", "--mode", "bypass"]); // profile from the room
	assert.equal(c.cwd, repo);
	assert.equal(c.env.BOSS_CALL_ME, "worker");
	assert.equal(c.env.BOSS_CALL_ROOM, "w");

	assert.deepEqual(M.unread("w", "worker"), []); // acked after the run
	const auto = M.readMessages("w").at(-1);
	assert.equal(auto.from, "worker");
	assert.equal(auto.to, "boss");
	assert.equal(auto.kind, "status");
	assert.match(auto.text, /^\[auto\] run exited 0/);
	assert.match(auto.text, /ended: completed/);
	assert.match(auto.text, /I did it\./);
});

test("serve acknowledges only the batch handed to the run", () => {
	M.post("w", { from: "boss", to: "worker", text: "batch one" });
	const spawn = (bin, args) => {
		M.post("w", { from: "boss", to: "worker", text: "arrived while running" });
		const p = join(process.env.KISO_HOME, "sessions", `${args[1]}.jsonl`);
		mkdirSync(join(process.env.KISO_HOME, "sessions"), { recursive: true });
		writeFileSync(p, JSON.stringify({ runId: "batch", event: { type: "user_input", content: args[2] } }) + "\n", { flag: "a" });
		return { status: 0 };
	};
	S.serve({ room: "w", me: "worker", once: true, log: () => {}, spawn });
	assert.deepEqual(M.unread("w", "worker").map((m) => m.text), ["arrived while running"]);
	M.ack("w", "worker");
});

test("serve --once: when the model posts, no auto-status; a boss can be served too", () => {
	M.post("w", { from: "worker", text: "which branch?", kind: "ask" });
	const before = M.readMessages("w").length;
	const spawn = (bin, args) => {
		assert.match(args[2], /You are boss, the boss/);
		assert.match(args[2], /which branch\?/);
		M.post("w", { from: "boss", to: "worker", text: "main", kind: "reply", ref: "#2" });
		return { status: 0 };
	};
	S.serve({ room: "w", me: "boss", cwd: repo, once: true, log: () => {}, spawn, profile: "x", mode: "default" });
	const msgs = M.readMessages("w");
	assert.equal(msgs.length, before + 1);
	assert.equal(msgs.at(-1).kind, "reply");
	assert.deepEqual(M.unread("w", "boss"), []);
});

test("serve --once with nothing unread returns 0 without spawning", () => {
	let spawned = 0;
	const exit = S.serve({ room: "w", me: "boss", cwd: repo, once: true, log: () => {}, spawn: () => (spawned++, { status: 0 }) });
	assert.equal(exit, 0);
	assert.equal(spawned, 0);
});

test("a run that never started (session locked, log unchanged) keeps the mail unread", () => {
	M.post("w", { from: "boss", to: "worker", text: "again" });
	const before = M.readMessages("w").length;
	const logs = [];
	const exit = S.serve({ room: "w", me: "worker", once: true, log: (l) => logs.push(l), spawn: () => ({ status: 1 }) });
	assert.equal(exit, 1);
	assert.equal(M.readMessages("w").length, before); // no auto status either
	assert.ok(M.unread("w", "worker").some((m) => m.text === "again"));
	assert.match(logs.join("\n"), /run did not start .*open in another kiso/);
});

test("unrelated session growth does not make a failed run look started", () => {
	const spawn = (bin, args) => {
		const p = join(process.env.KISO_HOME, "sessions", `${args[1]}.jsonl`);
		mkdirSync(join(process.env.KISO_HOME, "sessions"), { recursive: true });
		writeFileSync(p, JSON.stringify({ runId: "other", event: { type: "user_input", content: "another process wrote this" } }) + "\n", { flag: "a" });
		return { status: 1 };
	};
	const exit = S.serve({ room: "w", me: "worker", once: true, log: () => {}, spawn });
	assert.equal(exit, 1);
	assert.ok(M.unread("w", "worker").some((m) => m.text === "again"));
});

test("a run that started and then crashed (log grew) still acks and reports the exit code", () => {
	const spawn = (bin, args) => {
		const p = join(process.env.KISO_HOME, "sessions", `${args[1]}.jsonl`);
		writeFileSync(p, JSON.stringify({ runId: "r9", event: { type: "user_input", content: args[2] } }) + "\n", { flag: "a" });
		return { status: 3 };
	};
	const exit = S.serve({ room: "w", me: "worker", once: true, log: () => {}, spawn });
	assert.equal(exit, 3);
	assert.match(M.readMessages("w").at(-1).text, /run exited 3/);
	assert.deepEqual(M.unread("w", "worker"), []);
});
