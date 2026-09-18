#!/usr/bin/env node
/**
 * boss-call — one Boss, several members, one line each.
 *
 *   boss-call setup                      link the skill into every harness on this machine
 *   boss-call host <room>                become the boss of a room
 *   boss-call join <room> --as <name>    join a room as a member; the current directory is your repo
 *   boss-call who                        which side you are on
 *   boss-call read [--ack]               unread mail for you
 *   boss-call wait [--timeout 25]        block until mail arrives (acknowledged), then print it
 *                                        (--timeout 590 with a 600 s shell-tool timeout: fewer empty returns)
 *   boss-call post [--kind K] "text"     a member: to the boss; the boss: --to <member>|all
 *   boss-call status | tail [-n N]       the room at a glance
 *   boss-call serve [--once]             run unattended: mail -> one kiso turn -> status
 *   boss-call peek-session [id|latest]   where a kiso session stands, from its log
 *
 * Room and identity come from the current directory once you have joined;
 * --room / --me / BOSS_CALL_ROOM / BOSS_CALL_ME override.
 */
import { existsSync, lstatSync, mkdirSync, readlinkSync, rmSync, symlinkSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { HOME, BossCallError, ack, formatMessage, heartbeat, host, identify, joinRoom, loadRoom, post, readMessages, resolveRoom, status, statusNudge, unread, waitForMail } from "../src/mailbox.mjs";
import { peek } from "../src/peek.mjs";
import { serve } from "../src/serve.mjs";

const PKG = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function parse(argv) {
	const flags = {};
	const pos = [];
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (a === "--") {
			pos.push(...argv.slice(i + 1));
			break;
		}
		if (a.startsWith("--")) {
			const [k, inline] = a.slice(2).split(/=(.*)/s);
			const next = argv[i + 1];
			if (inline !== undefined) flags[k] = inline;
			else if (next !== undefined && !next.startsWith("--")) {
				flags[k] = next;
				i++;
			} else flags[k] = true;
		} else if (a === "-n" && argv[i + 1]) {
			flags.n = argv[++i];
		} else pos.push(a);
	}
	return { flags, pos };
}

function die(msg) {
	console.error(`boss-call: ${msg}`);
	process.exit(2);
}

function numberFlag(value, fallback, label, { integer = false, min = 0 } = {}) {
	const n = Number(value ?? fallback);
	if (!Number.isFinite(n) || (integer && !Number.isInteger(n)) || n < min) die(`${label} must be ${integer ? "an integer" : "a number"} >= ${min}`);
	return n;
}

/**
 * Where harnesses look for skills. `~/.agents/skills` is the shared convention
 * (pi, opencode and others read it); Claude Code and Codex have their own
 * directories. boss-call uses only these skill links and the CLI.
 */
const SKILL_DIRS = [
	{ at: [".agents", "skills"], always: true, note: "pi, opencode, any agentskills.io harness" },
	{ at: [".claude", "skills"], note: "Claude Code" },
	{ at: [".codex", "skills"], note: "Codex" },
	{ at: [".kiso", "skills"], note: "kiso" },
];

function link(target, at) {
	mkdirSync(dirname(at), { recursive: true });
	let current;
	try {
		current = lstatSync(at);
	} catch (err) {
		if (err.code !== "ENOENT") throw err;
	}
	if (current?.isSymbolicLink() && readlinkSync(at) === target) return "kept";
	if (current?.isSymbolicLink()) rmSync(at);
	else if (current) throw new BossCallError(`refusing to replace existing non-symlink: ${at}`);
	symlinkSync(target, at);
	return "linked";
}

function onPath(name) {
	return (process.env.PATH ?? "").split(":").some((d) => d && existsSync(join(d, name)));
}

function cmdSetup() {
	const home = homedir();
	for (const d of SKILL_DIRS) {
		if (!d.always && !existsSync(join(home, d.at[0]))) continue;
		const r = link(PKG, join(home, ...d.at, "boss-call"));
		console.log(`  ${r.padEnd(6)} ~/${d.at.join("/")}/boss-call  (${d.note})`);
	}
	const oldExt = join(home, ".kiso", "extensions", "boss-call.mjs");
	try {
		if (lstatSync(oldExt).isSymbolicLink()) {
			rmSync(oldExt);
			console.log("  removed ~/.kiso/extensions/boss-call.mjs (legacy extension; boss-call is CLI + skills only)");
		}
	} catch {}
	if (!onPath("boss-call")) {
		const r = link(join(PKG, "bin", "boss-call.mjs"), join(home, ".local", "bin", "boss-call"));
		console.log(`  ${r.padEnd(6)} ~/.local/bin/boss-call  (add ~/.local/bin to PATH if it is not)`);
	}
	console.log("\nnext: the boss runs `boss-call host <room>`; each member runs `boss-call join <room> --as <name>` inside its repo.\nA session becomes a member only when the person says: follow the boss-call skill. Otherwise it is an ordinary session.");
}

function main(argv) {
	const { flags, pos } = parse(argv);
	const cmd = pos.shift();
	if (!cmd || cmd === "help" || flags.help) {
		console.log(import.meta.url ? readHelp() : "");
		return 0;
	}
	if (cmd === "setup") return cmdSetup(), 0;

	if (cmd === "host") {
		const room = pos[0] ?? flags.room ?? die("host <room>");
		const name = flags.as ?? "boss";
		const kiso = kisoOpts(flags);
		host(room, name, { root: flags.root ? resolve(flags.root) : undefined, kiso });
		console.log(`you are the boss of ${room} as ${name}${kiso ? ` (serve: kiso${kiso.profile ? ` --model ${kiso.profile}` : ""})` : ""}`);
		return cmdWho({ room, me: name });
	}
	if (cmd === "join") {
		const room = pos[0] ?? flags.room ?? die("join <room> --as <name>");
		const name = flags.as ?? die("join needs --as <name>");
		const root = resolve(flags.root ?? process.cwd());
		joinRoom(room, name, root, { kiso: kisoOpts(flags) });
		console.log(`joined ${room} as ${name}, repo ${root}`);
		return cmdWho({ room, me: name });
	}

	const room = resolveRoom(flags.room, flags.cwd);
	switch (cmd) {
		case "who":
			return cmdWho({ room, me: flags.me, cwd: flags.cwd });
		case "status": {
			const s = status(room);
			console.log(`room ${room}: boss=${s.boss ?? "-"}  ${s.total} messages`);
			for (const r of s.rows) console.log(`  ${r.role.padEnd(6)} ${r.name.padEnd(12)} unread=${String(r.unread).padStart(3)} asks=${String(r.asks).padStart(2)} ${presence(r.heartbeat).padEnd(22)} last-posted=${r.lastPosted ?? "-"}`);
			return 0;
		}
		case "tail": {
			const n = numberFlag(flags.n, 20, "-n", { integer: true });
			for (const m of readMessages(room).slice(-n)) console.log(`#${String(m.seq).padStart(4)} ${m.ts} ${m.from.padStart(9)} -> ${m.to.padEnd(9)} [${m.kind.padEnd(6)}] ${(m.text.split("\n")[0] ?? "").slice(0, 110)}`);
			return 0;
		}
		case "read": {
			const { name } = identify(room, flags.me, flags.cwd);
			const msgs = unread(room, name, { all: Boolean(flags.all) });
			if (!msgs.length) console.log(`(no new messages for ${name} in room ${room})`);
			else {
				for (const m of msgs) console.log(formatMessage(m));
				if (flags.ack) console.log(`(acked through #${ack(room, name, msgs.at(-1).seq)})`);
			}
			return 0;
		}
		case "wait": {
			const { name } = identify(room, flags.me, flags.cwd);
			const timeout = numberFlag(flags.timeout, 25, "--timeout"); // under every harness's shell-tool timeout (kiso: 30s)
			const nudge = statusNudge(room, name);
			if (nudge) {
				console.log(nudge);
				return 0;
			}
			const msgs = waitForMail(room, name, { timeoutMs: timeout * 1000, all: Boolean(flags.all) });
			if (!msgs.length) {
				console.log(`NO MAIL YET for ${name} after ${timeout}s. You are on the line only while this command runs.\nRun \`boss-call wait --timeout ${timeout}\` again NOW, with the same shell-tool timeout. Do not summarize, do not end your turn: an ended turn is a dropped line and nobody can call you back.`);
				return 0;
			}
			for (const m of msgs) console.log(formatMessage(m));
			console.log(`(acked through #${msgs.at(-1).seq}. Act on it and keep working; post a status when a piece is done and continue at once. Run \`boss-call wait\` only when nothing is left to do.)`);
			return 0;
		}
		case "ack": {
			const { name } = identify(room, flags.me, flags.cwd);
			console.log(`acked through #${ack(room, name)} for ${name}`);
			return 0;
		}
		case "post": {
			const { name } = identify(room, flags.me ?? flags.from, flags.cwd);
			const text = pos.length ? pos.join(" ") : readStdin();
			const rec = post(room, { from: name, to: flags.to, kind: flags.kind ?? "msg", text, ref: flags.ref });
			console.log(`posted #${rec.seq} ${rec.from} -> ${rec.to} [${rec.kind}]`);
			return 0;
		}
		case "serve":
			return serve({
				room, me: flags.me, cwd: flags.cwd, session: flags.session, bin: flags.kiso, profile: flags.profile, mode: flags.mode,
				envFile: flags["env-file"], poll: numberFlag(flags.poll, 30, "--poll", { min: 0.001 }), once: Boolean(flags.once),
			});
		case "peek-session":
			console.log(peek(pos[0] ?? "latest", { match: flags.match ?? null, lines: Number.parseInt(flags.lines ?? "12", 10), width: Number.parseInt(flags.width ?? "160", 10) }));
			return 0;
		default:
			die(`unknown command ${cmd} — try boss-call help`);
	}
}

function kisoOpts(flags) {
	const k = {};
	if (flags.profile) k.profile = flags.profile;
	if (flags["env-file"]) k.envFile = flags["env-file"];
	if (flags.kiso) k.bin = flags.kiso;
	if (flags.mode) k.mode = flags.mode;
	return Object.keys(k).length ? k : undefined;
}

function presence(hb) {
	if (!hb) return "never listened";
	const age = (Date.now() - Date.parse(hb.ts)) / 1000;
	if (hb.state === "waiting" && age < 180) return "listening";
	const ago = age < 90 ? `${Math.round(age)}s` : age < 5400 ? `${Math.round(age / 60)}m` : `${Math.round(age / 3600)}h`;
	return `${hb.state} ${ago} ago`;
}

function cmdWho({ room, me, cwd }) {
	const data = loadRoom(room);
	let who = { name: "?", role: "unknown" };
	try {
		who = identify(room, me, cwd);
	} catch {}
	console.log(`room    : ${room}  (${join(HOME, room)})`);
	console.log(`you     : ${who.name}  (${who.role})`);
	console.log(`boss    : ${data.boss?.name ?? "-"}${data.boss?.root ? `  root=${data.boss.root}` : ""}`);
	console.log("members :");
	for (const [n, m] of Object.entries(data.members)) console.log(`  ${n.padEnd(12)} ${m.root ?? ""}`);
	if (who.role === "member") console.log(`\nyou talk only to ${data.boss?.name}: \`boss-call post "..."\` goes there by default.`);
	else if (who.role === "boss") console.log("\nyou may `post --to <member>` or `--to all`; members can only reach you.");
	if (who.role !== "unknown") console.log("to put an agent session started here on the line, tell it: follow the boss-call skill");
	return 0;
}

function readStdin() {
	try {
		return require("node:fs").readFileSync(0, "utf8");
	} catch {
		return "";
	}
}

function readHelp() {
	const src = require("node:fs").readFileSync(fileURLToPath(import.meta.url), "utf8");
	return src.split("/**")[1].split("*/")[0].split("\n").map((l) => l.replace(/^\s*\*\s?/, "")).join("\n").trim();
}

import { createRequire } from "node:module";
const require = createRequire(import.meta.url);

try {
	process.exit(main(process.argv.slice(2)) ?? 0);
} catch (err) {
	if (err instanceof BossCallError) die(err.message);
	throw err;
}
