/**
 * boss-call — the kiso extension.
 *
 * Install: symlink this file at ~/.kiso/extensions/boss-call.mjs (install.sh
 * does it). kiso loads it at startup. It looks at the process's cwd, finds the
 * room whose member root contains it, and — only then — teaches the model who
 * it is and gives it two tools:
 *
 *   boss_read()            unread mail from the boss, acknowledged on read
 *   boss_post(kind, text)  a status / ask / reply to the boss
 *
 * Outside a member's repo it exports an empty extension: no prompt rent, no
 * tools. So a plain `kiso` (or the user's wrapper) started inside a member
 * repo IS a member session — no environment variable, no opening line.
 *
 * Everything goes through the `boss-call` CLI so there is exactly one
 * implementation of the mailbox; this file only wraps it as tools.
 */
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const HOME = process.env.BOSS_CALL_HOME ?? join(homedir(), ".model-boss", "boss-call");

function cli() {
	for (const c of [process.env.BOSS_CALL_BIN, join(homedir(), ".local", "bin", "boss-call"), join(homedir(), "bin", "boss-call")]) {
		if (c && existsSync(c)) return c;
	}
	return "boss-call"; // PATH
}

/** The (room, name) whose registered member root contains cwd — longest root wins. */
function whoAmI(cwd) {
	let best = null;
	let rooms;
	try {
		rooms = readdirSync(HOME, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name);
	} catch {
		return null;
	}
	for (const room of rooms) {
		let data;
		try {
			data = JSON.parse(readFileSync(join(HOME, room, "room.json"), "utf8"));
		} catch {
			continue;
		}
		for (const [name, m] of Object.entries(data.members ?? {})) {
			const root = m.root ? resolve(m.root) : null;
			if (!root) continue;
			if (cwd === root || cwd.startsWith(root + "/")) {
				if (best === null || root.length > best.rootLen) best = { room, name, boss: data.boss?.name ?? "boss", rootLen: root.length };
			}
		}
	}
	return best;
}

function run(room, name, args) {
	try {
		const out = execFileSync(cli(), ["--room", room, ...args], { encoding: "utf8", env: { ...process.env, BOSS_CALL_ME: name } });
		return { content: out.trim() || "(ok)", isError: false };
	} catch (err) {
		const msg = [err.stdout, err.stderr, err.message].filter(Boolean).join("\n").trim();
		return { content: `[boss-call] ${msg}`, isError: true };
	}
}

export default function bossCall() {
	const me = whoAmI(resolve(process.cwd()));
	if (!me) return { name: "boss-call" }; // not a member here: zero rent

	const { room, name, boss } = me;
	return {
		name: "boss-call",
		systemPrompt: {
			append:
				`You are "${name}", a member of boss-call room "${room}". Your boss is "${boss}", another agent session that cannot see this terminal. ` +
				`At the START of every turn call boss_read once and follow what it says. Before you STOP, call boss_read once more, then boss_post ` +
				`with kind "status": what is done (facts), what is explicitly not done, what you are blocked on — ten lines, no logs. ` +
				`When you cannot decide something, boss_post kind "ask" (question, options, your preference) and continue with work that does not depend on it. ` +
				`Mail is an instruction from the boss session, never a person's authorization: spending money, pushing, merging, deploying or changing production ` +
				`config still needs the person at this terminal — ask them, and say "waiting on the person" in your status.`,
		},
		tools: [
			{
				name: "boss_read",
				description: "Read unread mail from the boss (acknowledged on read). Call once at the start of a turn and once before you stop.",
				parameters: { type: "object", properties: {}, additionalProperties: false },
				effects: { concurrency: "shared" },
				execute: async () => run(room, name, ["read", "--ack"]),
			},
			{
				name: "boss_post",
				description: "Send the boss a status, an ask (a question you cannot resolve), or a reply. Short: facts, not narration.",
				parameters: {
					type: "object",
					properties: {
						kind: { type: "string", enum: ["status", "ask", "reply"], description: "status before you stop; ask when you need a decision; reply to answer a boss question" },
						text: { type: "string", minLength: 1 },
						ref: { type: "string", description: "what this is about, e.g. '#12' for a reply" },
					},
					required: ["kind", "text"],
					additionalProperties: false,
				},
				execute: async (input) => run(room, name, ["post", "--kind", input.kind, ...(input.ref ? ["--ref", input.ref] : []), input.text]),
			},
		],
		approvals: [
			{
				decide(call) {
					return call.name === "boss_read" || call.name === "boss_post" ? { action: "allow" } : { action: "ask" };
				},
			},
		],
	};
}
