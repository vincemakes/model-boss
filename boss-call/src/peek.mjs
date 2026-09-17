/**
 * Read a kiso session from its durable log and say where it stands.
 *
 * The Boss uses this instead of asking a member to report: the log is the
 * truth, it costs the member nothing, and it never flatters.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export function sessionsDir() {
	return join(process.env.KISO_HOME ?? join(homedir(), ".kiso"), "sessions");
}

export function loadSession(path) {
	const events = [];
	for (const line of readFileSync(path, "utf8").split("\n")) {
		if (!line.trim()) continue;
		try {
			const rec = JSON.parse(line);
			if (rec.event && typeof rec.event === "object") events.push({ ...rec.event, _runId: rec.runId });
		} catch {}
	}
	return events;
}

export function listSessions() {
	const d = sessionsDir();
	if (!existsSync(d)) return [];
	return readdirSync(d)
		.filter((f) => f.endsWith(".jsonl"))
		.map((f) => ({ path: join(d, f), stem: f.slice(0, -6), mtime: statSync(join(d, f)).mtimeMs }))
		.sort((a, b) => b.mtime - a.mtime);
}

export function pickSession(spec = "latest", match = null) {
	const files = listSessions();
	if (spec !== "latest") {
		const hit = files.find((f) => f.stem === spec || f.stem.endsWith(spec));
		if (!hit) throw new Error(`no session matching ${spec} in ${sessionsDir()}`);
		return hit;
	}
	if (match) {
		for (const f of files) {
			const first = loadSession(f.path).find((e) => e.type === "user_input");
			if (first && String(first.content ?? "").includes(match)) return f;
		}
		throw new Error(`no session whose first input contains ${JSON.stringify(match)}`);
	}
	if (!files.length) throw new Error(`no sessions in ${sessionsDir()}`);
	return files[0];
}

export function summarize(events) {
	if (!events.length) return { empty: true };
	const lastRun = events[events.length - 1]._runId;
	const run = events.filter((e) => e._runId === lastRun);
	const inputs = events.filter((e) => e.type === "user_input");
	const terminal = [...run].reverse().find((e) => e.type === "terminal") ?? null;
	const lastIn = events.map((e) => e.type).lastIndexOf("user_input");
	const text = events
		.slice(lastIn + 1)
		.filter((e) => e.type === "text_delta")
		.map((e) => e.text ?? "")
		.join("")
		.trim();
	const decided = new Set(events.filter((e) => e.type === "permission_decided" || e.type === "permission_expired").map((e) => e.decisionId));
	const pending = events.filter((e) => e.type === "permission_requested" && !decided.has(e.decisionId));
	const receipts = new Set(run.filter((e) => /^tool_execution_(succeeded|failed|resolved)$/.test(e.type)).map((e) => e.executionId));
	const uncertain = run.filter((e) => e.type === "tool_execution_started" && !receipts.has(e.executionId));
	let state;
	if (terminal) state = `ended: ${terminal.outcome?.kind ?? "?"}`;
	else if (pending.length) state = `OPEN — WAITING ON A PERSON: ${pending.length} pending approval(s)`;
	else if (uncertain.length) state = `OPEN — ${uncertain.length} uncertain execution(s) (started, no receipt)`;
	else state = "OPEN — running or interrupted";
	return {
		events: events.length,
		turns: inputs.length,
		toolCalls: events.filter((e) => e.type === "tool_call_end").length,
		firstInput: String(inputs[0]?.content ?? ""),
		lastInput: String(inputs.at(-1)?.content ?? ""),
		state,
		text,
	};
}

export function peek(spec, { match = null, lines = 12, width = 160 } = {}) {
	const f = pickSession(spec, match);
	const s = summarize(loadSession(f.path));
	if (s.empty) return `${f.stem}: empty`;
	const one = (t) => t.replace(/\s+/g, " ").slice(0, width);
	const out = [
		`session ${f.stem}  last-write ${new Date(f.mtime).toISOString().replace("T", " ").slice(0, 19)}  events ${s.events}  turns ${s.turns}  tool_calls ${s.toolCalls}`,
		`first input : ${one(s.firstInput)}`,
		`last input  : ${one(s.lastInput)}`,
		`state       : ${s.state}`,
	];
	if (s.text) out.push("last text   :", ...s.text.split("\n").slice(-lines).map((l) => "  " + l.slice(0, width)));
	return out.join("\n");
}
