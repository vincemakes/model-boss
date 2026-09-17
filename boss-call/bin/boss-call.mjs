#!/usr/bin/env node
/**
 * boss-call — one Boss, several members, one line each.
 *
 *   boss-call setup                      install the kiso extension + the skill into every harness present
 *   boss-call host <room>                become the boss of a room
 *   boss-call join <room> --as <name>    join a room as a member; the current directory is your repo
 *   boss-call who                        which side you are on
 *   boss-call read [--ack]               unread mail for you
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
import { BossCallError, ack, formatMessage, host, identify, joinRoom, listRooms, loadRoom, post, readMessages, resolveRoom, status, unread } from "../src/mailbox.mjs";
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

const HARNESSES = [".kiso", ".claude", ".codex"];

function link(target, at) {
	mkdirSync(dirname(at), { recursive: true });
	try {
		if (lstatSync(at).isSymbolicLink() && readlinkSync(at) === target) return "kept";
		rmSync(at, { recursive: true, force: true });
	} catch {}
	symlinkSync(target, at);
	return "linked";
}

function cmdSetup() {
	const present = HARNESSES.filter((h) => existsSync(join(homedir(), h)));
	if (!present.length) die("no ~/.kiso, ~/.claude or ~/.codex found — install one of those first, then run setup again");
	for (const h of present) {
		const r = link(PKG, join(homedir(), h, "skills", "boss-call"));
		console.log(`  ${r.padEnd(6)} ~/${h}/skills/boss-call -> ${PKG}`);
		if (h === ".kiso") {
			const r2 = link(join(PKG, "kiso-extension.mjs"), join(homedir(), h, "extensions", "boss-call.mjs"));
			console.log(`  ${r2.padEnd(6)} ~/.kiso/extensions/boss-call.mjs -> ${join(PKG, "kiso-extension.mjs")}`);
		}
	}
	console.log("\nnext: the boss runs `boss-call host <room>`; each member runs `boss-call join <room> --as <name>` inside its repo.");
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
			for (const r of s.rows) console.log(`  ${r.role.padEnd(6)} ${r.name.padEnd(12)} unread=${String(r.unread).padStart(3)} asks=${String(r.asks).padStart(2)} last-posted=${r.lastPosted ?? "-"}`);
			return 0;
		}
		case "tail": {
			const n = Number.parseInt(flags.n ?? "20", 10);
			for (const m of readMessages(room).slice(-n)) console.log(`#${String(m.seq).padStart(4)} ${m.ts} ${m.from.padStart(9)} -> ${m.to.padEnd(9)} [${m.kind.padEnd(6)}] ${(m.text.split("\n")[0] ?? "").slice(0, 110)}`);
			return 0;
		}
		case "read": {
			const { name } = identify(room, flags.me, flags.cwd);
			const msgs = unread(room, name, { all: Boolean(flags.all) });
			if (!msgs.length) console.log(`(no new messages for ${name} in room ${room})`);
			else {
				for (const m of msgs) console.log(formatMessage(m));
				if (flags.ack) console.log(`(acked through #${ack(room, name)})`);
			}
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
				envFile: flags["env-file"], poll: Number.parseInt(flags.poll ?? "30", 10), once: Boolean(flags.once),
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

function cmdWho({ room, me, cwd }) {
	const data = loadRoom(room);
	let who = { name: "?", role: "unknown" };
	try {
		who = identify(room, me, cwd);
	} catch {}
	console.log(`room    : ${room}  (${join(process.env.BOSS_CALL_HOME ?? join(homedir(), ".boss-call"), room)})`);
	console.log(`you     : ${who.name}  (${who.role})`);
	console.log(`boss    : ${data.boss?.name ?? "-"}${data.boss?.root ? `  root=${data.boss.root}` : ""}`);
	console.log("members :");
	for (const [n, m] of Object.entries(data.members)) console.log(`  ${n.padEnd(12)} ${m.root ?? ""}`);
	if (who.role === "member") console.log(`\nyou talk only to ${data.boss?.name}: \`boss-call post "..."\` goes there by default.`);
	else if (who.role === "boss") console.log("\nyou may `post --to <member>` or `--to all`; members can only reach you.");
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
