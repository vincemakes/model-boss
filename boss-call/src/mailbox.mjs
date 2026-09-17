/**
 * The mailbox: rooms, members, messages, cursors.
 *
 * One Boss, N members, one line each. A room is a star and this module
 * enforces it: a member's post goes to the boss and nowhere else; the boss
 * posts to one member or to all. Everything is a file under
 * `~/.boss-call/<room>/`, append-only where it matters, so a crash loses
 * nothing and every message has a seq.
 *
 * Used by the CLI, by the kiso extension (in-process) and by `serve`.
 * No dependencies.
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmdirSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

export const HOME = process.env.BOSS_CALL_HOME ?? join(homedir(), ".boss-call");
export const KINDS = ["msg", "ask", "reply", "status"];

export class BossCallError extends Error {}

export function roomDir(room) {
	const d = join(HOME, room);
	mkdirSync(join(d, "cursors"), { recursive: true });
	return d;
}

export function listRooms() {
	if (!existsSync(HOME)) return [];
	return readdirSync(HOME, { withFileTypes: true })
		.filter((e) => e.isDirectory() && existsSync(join(HOME, e.name, "room.json")))
		.map((e) => e.name)
		.sort();
}

export function loadRoom(room) {
	const p = join(HOME, room, "room.json");
	if (!existsSync(p)) return { boss: null, members: {} };
	const data = JSON.parse(readFileSync(p, "utf8"));
	data.members ??= {};
	return data;
}

export function saveRoom(room, data) {
	writeFileSync(join(roomDir(room), "room.json"), JSON.stringify(data, null, 2) + "\n");
}

/**
 * A lock is a directory: mkdir is atomic on every POSIX filesystem and
 * needs no flock. A lock older than 10s belongs to a dead process and is
 * taken over.
 */
export function withLock(room, fn) {
	const lock = join(roomDir(room), ".lock");
	const deadline = Date.now() + 5000;
	for (;;) {
		try {
			mkdirSync(lock);
			break;
		} catch (err) {
			if (err.code !== "EEXIST") throw err;
			try {
				if (Date.now() - statSync(lock).mtimeMs > 10_000) rmdirSync(lock);
			} catch {}
			if (Date.now() > deadline) throw new BossCallError(`room ${room} is locked (stale ${lock}?)`);
			Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25);
		}
	}
	try {
		return fn();
	} finally {
		try {
			rmdirSync(lock);
		} catch {}
	}
}

export function readMessages(room) {
	const p = join(HOME, room, "messages.jsonl");
	if (!existsSync(p)) return [];
	const out = [];
	for (const line of readFileSync(p, "utf8").split("\n")) {
		if (!line.trim()) continue;
		try {
			out.push(JSON.parse(line));
		} catch {
			// a torn tail from a crash mid-write: skip, never guess
		}
	}
	return out;
}

export function getCursor(room, name) {
	const p = join(roomDir(room), "cursors", name);
	if (!existsSync(p)) return -1;
	const n = Number.parseInt(readFileSync(p, "utf8").trim(), 10);
	return Number.isFinite(n) ? n : -1;
}

export function setCursor(room, name, seq) {
	writeFileSync(join(roomDir(room), "cursors", name), `${seq}\n`);
}

// ---------------------------------------------------------------------------
// identity
// ---------------------------------------------------------------------------

function rootContains(root, here) {
	if (!root) return -1;
	const r = resolve(root.replace(/^~(?=\/|$)/, homedir()));
	return here === r || here.startsWith(r + "/") ? r.length : -1;
}

/** The room the cwd belongs to: --room, $BOSS_CALL_ROOM, the room whose
 *  boss or member root contains the cwd (longest wins), else the only room. */
export function resolveRoom(explicit, cwd = process.cwd()) {
	if (explicit) return explicit;
	if (process.env.BOSS_CALL_ROOM) return process.env.BOSS_CALL_ROOM;
	const here = resolve(cwd);
	const rooms = listRooms();
	let best = null;
	for (const room of rooms) {
		const data = loadRoom(room);
		const roots = [...Object.values(data.members).map((m) => m.root), data.boss?.root];
		for (const root of roots) {
			const k = rootContains(root, here);
			if (k >= 0 && (best === null || k > best.k)) best = { k, room };
		}
	}
	if (best) return best.room;
	if (rooms.length === 1) return rooms[0];
	throw new BossCallError(`which room? pass --room or run inside a registered repo (rooms: ${rooms.join(", ") || "none — host or join one"})`);
}

/** {name, role} — role is "boss" or "member". --me, $BOSS_CALL_ME, else the cwd. */
export function identify(room, explicit, cwd = process.cwd()) {
	const data = loadRoom(room);
	const boss = data.boss ?? {};
	const name = explicit ?? process.env.BOSS_CALL_ME;
	if (name) {
		if (boss.name === name) return { name, role: "boss" };
		if (data.members[name]) return { name, role: "member" };
		throw new BossCallError(`${name} is not in room ${room} (boss: ${boss.name ?? "-"}, members: ${Object.keys(data.members).join(", ") || "none"})`);
	}
	const here = resolve(cwd);
	let best = null;
	for (const [n, m] of Object.entries(data.members)) {
		const k = rootContains(m.root, here);
		if (k >= 0 && (best === null || k > best.k)) best = { k, name: n, role: "member" };
	}
	const k = rootContains(boss.root, here);
	if (k >= 0 && (best === null || k > best.k)) best = { k, name: boss.name, role: "boss" };
	if (!best) throw new BossCallError(`cannot tell who you are — pass --me <name>, or run inside a registered repo (boss-call who --room ${room})`);
	return { name: best.name, role: best.role };
}

// ---------------------------------------------------------------------------
// the star
// ---------------------------------------------------------------------------

export function host(room, name, { root, kiso } = {}) {
	const data = loadRoom(room);
	if (data.boss && data.boss.name !== name) throw new BossCallError(`room ${room} already has a boss: ${data.boss.name}`);
	data.boss = { name, ...(root ? { root: resolve(root) } : {}) };
	if (kiso) data.kiso = { ...(data.kiso ?? {}), ...kiso };
	saveRoom(room, data);
	return data;
}

export function joinRoom(room, name, root, { kiso } = {}) {
	const data = loadRoom(room);
	if (!data.boss) throw new BossCallError(`room ${room} has no boss yet — the boss runs \`boss-call host ${room}\` first`);
	if (data.boss.name === name) throw new BossCallError(`${name} is the boss of ${room}`);
	data.members[name] = { root: resolve(root), ...(kiso ? { kiso } : {}) };
	saveRoom(room, data);
	return data;
}

export function addressed(m, me) {
	return (m.to === me || m.to === "all") && m.from !== me;
}

export function post(room, { from, to, kind = "msg", text, ref }) {
	if (!KINDS.includes(kind)) throw new BossCallError(`kind must be one of ${KINDS.join(", ")}`);
	if (!text || !text.trim()) throw new BossCallError("empty message");
	const data = loadRoom(room);
	const bossName = data.boss?.name;
	let recipient = to;
	if (from === bossName) {
		if (!recipient) throw new BossCallError("the boss must say --to <member> or --to all");
		if (recipient !== "all" && !data.members[recipient]) throw new BossCallError(`${recipient} is not a member (members: ${Object.keys(data.members).join(", ") || "none"})`);
	} else {
		if (!data.members[from]) throw new BossCallError(`${from} is not in room ${room}`);
		recipient ??= bossName;
		if (recipient !== bossName) throw new BossCallError(`a member talks only to the boss (${bossName}); --to ${recipient} refused`);
	}
	return withLock(room, () => {
		const msgs = readMessages(room);
		const seq = msgs.length ? msgs[msgs.length - 1].seq + 1 : 0;
		const rec = { seq, ts: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), from, to: recipient, kind, text: text.replace(/\n+$/, "") };
		if (ref) rec.ref = ref;
		appendFileSync(join(roomDir(room), "messages.jsonl"), JSON.stringify(rec) + "\n");
		return rec;
	});
}

export function unread(room, me, { all = false } = {}) {
	const cur = getCursor(room, me);
	return readMessages(room).filter((m) => m.seq > cur && (all || addressed(m, me)));
}

export function ack(room, me) {
	const msgs = readMessages(room);
	if (!msgs.length) return -1;
	const last = msgs[msgs.length - 1].seq;
	withLock(room, () => setCursor(room, me, last));
	return last;
}

// ---------------------------------------------------------------------------
// waiting: the universal delivery. A session that has nothing to do blocks in
// `wait`; the call returns when mail arrives. Every harness has a shell tool
// and a blocking tool call, so no harness feature (loops, hooks, a supervisor)
// is needed. While it waits it leaves a heartbeat so the boss can see who is
// listening.
// ---------------------------------------------------------------------------

export function heartbeat(room, name, state) {
	const d = join(roomDir(room), "heartbeats");
	mkdirSync(d, { recursive: true });
	writeFileSync(join(d, name), JSON.stringify({ ts: new Date().toISOString(), state }) + "\n");
}

export function readHeartbeat(room, name) {
	const p = join(HOME, room, "heartbeats", name);
	if (!existsSync(p)) return null;
	try {
		return JSON.parse(readFileSync(p, "utf8"));
	} catch {
		return null;
	}
}

/** Block until mail for `me` arrives or `timeoutMs` passes. Returns the mail
 *  (acknowledged) or [] on timeout. Polls the file; cheap and portable. */
export function waitForMail(room, me, { timeoutMs = 110_000, pollMs = 1000, all = false } = {}) {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		heartbeat(room, me, "waiting");
		const msgs = unread(room, me, { all });
		if (msgs.length) {
			ack(room, me);
			heartbeat(room, me, "working");
			return msgs;
		}
		if (Date.now() >= deadline) return [];
		Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Math.min(pollMs, Math.max(0, deadline - Date.now())));
	}
}

export function status(room) {
	const data = loadRoom(room);
	const msgs = readMessages(room);
	const names = [data.boss?.name ?? "-", ...Object.keys(data.members).sort()];
	return {
		room,
		boss: data.boss?.name ?? null,
		total: msgs.length,
		rows: names.map((n) => {
			const cur = getCursor(room, n);
			const un = msgs.filter((m) => m.seq > cur && addressed(m, n));
			const hb = readHeartbeat(room, n);
			return {
				heartbeat: hb,
				name: n,
				role: n === data.boss?.name ? "boss" : "member",
				unread: un.length,
				asks: un.filter((m) => m.kind === "ask").length,
				lastPosted: msgs.filter((m) => m.from === n).map((m) => m.ts).sort().at(-1) ?? null,
			};
		}),
	};
}

export function formatMessage(m) {
	const ref = m.ref ? ` ref=${m.ref}` : "";
	return `--- #${m.seq} ${m.ts} ${m.from} -> ${m.to} [${m.kind}]${ref}\n${m.text}`;
}
