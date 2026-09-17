import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

process.env.KISO_HOME = mkdtempSync(join(tmpdir(), "kiso-home-"));
const P = await import("../src/peek.mjs");

function log(name, events) {
	mkdirSync(P.sessionsDir(), { recursive: true });
	writeFileSync(join(P.sessionsDir(), `${name}.jsonl`), events.map((e) => JSON.stringify(e)).join("\n") + "\n");
}

test("summarize reads state from the log: ended, waiting on a person, uncertain", () => {
	log("s-ended", [
		{ runId: "r1", event: { type: "user_input", content: "do x" } },
		{ runId: "r1", event: { type: "text_delta", text: "done " } },
		{ runId: "r1", event: { type: "text_delta", text: "x" } },
		{ runId: "r1", event: { type: "terminal", outcome: { kind: "completed" } } },
	]);
	let s = P.summarize(P.loadSession(join(P.sessionsDir(), "s-ended.jsonl")));
	assert.equal(s.state, "ended: completed");
	assert.equal(s.text, "done x");
	assert.equal(s.turns, 1);

	log("s-pending", [
		{ runId: "r1", event: { type: "user_input", content: "push it" } },
		{ runId: "r1", event: { type: "permission_requested", decisionId: "d1" } },
	]);
	s = P.summarize(P.loadSession(join(P.sessionsDir(), "s-pending.jsonl")));
	assert.match(s.state, /WAITING ON A PERSON: 1/);

	log("s-uncertain", [
		{ runId: "r1", event: { type: "user_input", content: "run tests" } },
		{ runId: "r1", event: { type: "tool_execution_started", executionId: "e1" } },
	]);
	s = P.summarize(P.loadSession(join(P.sessionsDir(), "s-uncertain.jsonl")));
	assert.match(s.state, /1 uncertain execution/);

	log("s-decided", [
		{ runId: "r1", event: { type: "user_input", content: "push it" } },
		{ runId: "r1", event: { type: "permission_requested", decisionId: "d1" } },
		{ runId: "r1", event: { type: "permission_decided", decisionId: "d1" } },
	]);
	s = P.summarize(P.loadSession(join(P.sessionsDir(), "s-decided.jsonl")));
	assert.equal(s.state, "OPEN — running or interrupted");
});

test("pickSession: exact, suffix, latest, --match on first input", () => {
	assert.equal(P.pickSession("s-ended").stem, "s-ended");
	assert.equal(P.pickSession("ended").stem, "s-ended");
	assert.equal(P.pickSession("latest", "run tests").stem, "s-uncertain");
	assert.throws(() => P.pickSession("nope"), /no session matching/);
	assert.throws(() => P.pickSession("latest", "zzz"), /first input contains/);
});

test("peek renders a compact block with the state line", () => {
	const out = P.peek("s-ended");
	assert.match(out, /^session s-ended/);
	assert.match(out, /state\s+: ended: completed/);
	assert.match(out, /last text\s+:\n\s+done x/);
});
