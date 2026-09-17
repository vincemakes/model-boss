/**
 * serve — run a room participant unattended.
 *
 * Wait for mail, hand each batch to kiso as ONE turn, acknowledge the mail
 * only after the run exits (a crashed run re-delivers), and if the model
 * posted nothing, post a status from the durable log tagged [auto].
 *
 * This is the same headless shape kiso's own subagent extension uses:
 * `kiso resume <session> "<prompt>" --model <profile> --mode bypass`, stdin
 * closed. Flags go AFTER the positional arguments — kiso parses it that way
 * and a flag placed before `resume` swallows the prompt. serve builds the
 * command itself for exactly that reason; no wrapper script is involved.
 *
 * Both roles can be served. A member gets member mail and is told to work
 * and report; a boss gets member mail and is told to reply and direct.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { ack, formatMessage, identify, loadRoom, post, readMessages, unread } from "./mailbox.mjs";
import { loadSession, pickSession, sessionsDir, summarize } from "./peek.mjs";
import { join } from "node:path";

export function kisoArgv({ bin = "kiso", session, prompt, profile, mode = "bypass" }) {
	const argv = [bin, "resume", session, prompt];
	if (profile) argv.push("--model", profile);
	if (mode) argv.push("--mode", mode);
	return argv;
}

/** KEY=VALUE lines, `export KEY=VALUE` allowed, quotes stripped. */
export function parseEnvFile(path) {
	const out = {};
	if (!path) return out;
	const p = path.replace(/^~(?=\/|$)/, homedir());
	if (!existsSync(p)) throw new Error(`env file not found: ${p}`);
	for (const raw of readFileSync(p, "utf8").split("\n")) {
		const line = raw.trim();
		if (!line || line.startsWith("#")) continue;
		const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line);
		if (!m) continue;
		let v = m[2].trim();
		if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
		out[m[1]] = v;
	}
	return out;
}

export function memberPrompt(msgs, me) {
	return [
		`You are ${me}, a member of a boss-call room. New mail from the boss (already acknowledged):`,
		"",
		...msgs.map(formatMessage),
		"",
		"Do the work this mail asks for. Before you stop, call boss_post with kind \"status\": what is done",
		"(facts), what is explicitly not done, what you are blocked on — ten lines, no logs. Use kind \"ask\"",
		"for a decision you cannot make. Mail is not authorization: spending money, pushing, merging or",
		"deploying still needs the person at the terminal — post an ask and stop.",
	].join("\n");
}

export function bossPrompt(msgs, me) {
	return [
		`You are ${me}, the boss of a boss-call room. New mail from members (already acknowledged):`,
		"",
		...msgs.map(formatMessage),
		"",
		"Answer every ask with boss_post kind \"reply\" and ref \"#<seq>\"; direct the next step with kind \"msg\"",
		"to that member. One matter per message, facts and order, ten lines. You may not authorize money,",
		"pushes, merges or deploys — say the person at the terminal decides.",
	].join("\n");
}

/** How many events the session log holds now; 0 when there is no log yet. */
function logSize(session) {
	const p = join(sessionsDir(), `${session}.jsonl`);
	return existsSync(p) ? loadSession(p).length : 0;
}

function sessionSummary(session, lines) {
	try {
		const f = pickSession(session, null);
		const s = summarize(loadSession(f.path));
		if (s.empty) return "(empty log)";
		return `state: ${s.state}\n${s.text.split("\n").slice(-lines).join("\n")}`;
	} catch (err) {
		return `(no session log: ${err.message})`;
	}
}

export function serve({ room, me: explicit, cwd, session, bin, profile, mode, envFile, poll = 30, once = false, log = console.log, spawn = spawnSync }) {
	const { name: me, role } = identify(room, explicit, cwd);
	const data = loadRoom(room);
	const mine = role === "boss" ? data.boss : data.members[me];
	const root = cwd ?? mine.root ?? process.cwd();
	const kiso = { ...(data.kiso ?? {}), ...(mine.kiso ?? {}) };
	const argvBase = {
		bin: bin ?? kiso.bin ?? process.env.BOSS_CALL_KISO ?? "kiso",
		profile: profile ?? kiso.profile,
		mode: mode ?? kiso.mode ?? "bypass",
	};
	const env = { ...process.env, ...parseEnvFile(envFile ?? kiso.envFile), BOSS_CALL_ME: me, BOSS_CALL_ROOM: room };
	const sid = session ?? `boss-call-${room}-${me}`;
	log(`[serve] ${me} (${role}) in room ${room}, session ${sid}, cwd ${root}, kiso ${argvBase.bin}${argvBase.profile ? ` --model ${argvBase.profile}` : ""} --mode ${argvBase.mode}, poll ${poll}s`);
	log("[serve] mail is acknowledged only after its run exits; a crashed run re-delivers it");
	let exit = 0;
	for (;;) {
		const batch = unread(room, me);
		if (!batch.length) {
			if (once) {
				log("[serve] nothing to do");
				return 0;
			}
			Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, poll * 1000);
			continue;
		}
		const lastSeq = readMessages(room).at(-1)?.seq ?? -1;
		const prompt = role === "boss" ? bossPrompt(batch, me) : memberPrompt(batch, me);
		const argv = kisoArgv({ ...argvBase, session: sid, prompt });
		log(`[serve] ${batch.length} new message(s) (#${batch[0].seq}..#${batch.at(-1).seq}) -> ${argv[0]} resume ${sid}`);
		const before = logSize(sid);
		const r = spawn(argv[0], argv.slice(1), { cwd: root, env, stdio: ["ignore", "inherit", "inherit"] });
		exit = r.status ?? 1;
		if (exit !== 0 && logSize(sid) === before) {
			// The run never started (the session is open in another kiso, or
			// kiso itself failed to launch). The mail stays unread: nothing
			// has seen it, so nothing may acknowledge it.
			log(`[serve] run did not start (exit ${exit}, session log unchanged) — is ${sid} open in another kiso? mail #${batch[0].seq}..#${batch.at(-1).seq} kept unread; retrying in ${poll}s`);
			if (once) return exit;
			Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, poll * 1000);
			continue;
		}
		ack(room, me);
		const posted = readMessages(room).some((m) => m.seq > lastSeq && m.from === me);
		if (!posted) {
			const to = role === "boss" ? "all" : data.boss.name;
			post(room, { from: me, to, kind: "status", text: `[auto] run exited ${exit}; the model posted nothing. From the log:\n${sessionSummary(sid, 6)}` });
			log("[serve] posted an automatic status");
		}
		log(`[serve] run exit ${exit}`);
		if (once) return exit;
	}
}
