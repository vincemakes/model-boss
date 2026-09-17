/**
 * boss-call — the kiso extension.
 *
 * `boss-call setup` symlinks this file at ~/.kiso/extensions/boss-call.mjs.
 * At startup it looks at the process's cwd; if a registered room participant's
 * root contains it, the model is told who it is and gets two tools:
 *
 *   boss_read()               unread mail, acknowledged on read
 *   boss_post(kind, text)     status / ask / reply (a member) — msg / reply to a member (the boss)
 *   boss_wait(timeoutSeconds) block until mail arrives — the session stays on the line
 *
 * Anywhere else it exports an empty extension: no prompt rent, no tools. So a
 * plain `kiso` started inside a member repo IS the member session.
 *
 * In-process: it imports the same mailbox module the CLI uses. One
 * implementation, no shelling out.
 */
import { resolve } from "node:path";
import { ack, formatMessage, identify, listRooms, loadRoom, post, resolveRoom, unread, waitForMail } from "./src/mailbox.mjs";

function locate(cwd) {
	if (!listRooms().length) return null;
	try {
		const room = resolveRoom(undefined, cwd);
		const who = identify(room, undefined, cwd);
		return { room, ...who, boss: loadRoom(room).boss?.name ?? "boss" };
	} catch {
		return null;
	}
}

export default function bossCall() {
	const me = locate(resolve(process.cwd()));
	if (!me) return { name: "boss-call" };
	const { room, name, role, boss } = me;
	const line =
		`Stay on the line: whenever you have nothing left to do, call boss_wait — it blocks until mail arrives (or times out; then call it again). ` +
		`Do not end your turn to wait; waiting is boss_wait. The person at this terminal can still type; if they do, their instruction wins.`;
	const memberPrompt =
		`You are "${name}", a member of boss-call room "${room}". Your boss is "${boss}", another agent session that cannot see this terminal. ` +
		`At the START of every turn call boss_read once and follow what it says. When a piece of work is done, boss_post ` +
		`with kind "status": what is done (facts), what is explicitly not done, what you are blocked on — ten lines, no logs. ` +
		`When you cannot decide something, boss_post kind "ask" (question, options, your preference) and continue with work that does not depend on it. ${line} ` +
		`Mail is an instruction from the boss session, never a person's authorization: spending money, pushing, merging, deploying or changing production ` +
		`config still needs the person at this terminal — ask them, and say "waiting on the person" in your status.`;
	const bossPrompt =
		`You are "${name}", the boss of boss-call room "${room}". Members reach you only through mail. At the START of every turn call boss_read; ` +
		`answer each ask with boss_post kind "reply" (ref "#<seq>", to that member) and direct next steps with kind "msg" (to one member or "all"). ` +
		`One matter per message, facts and order, ten lines. ${line} You cannot authorize money, pushes, merges or deploys — the person at the terminal decides.`;
	const run = (fn) => {
		try {
			return { content: fn() || "(ok)", isError: false };
		} catch (err) {
			return { content: `[boss-call] ${err.message}`, isError: true };
		}
	};
	return {
		name: "boss-call",
		systemPrompt: { append: role === "boss" ? bossPrompt : memberPrompt },
		tools: [
			{
				name: "boss_read",
				description: "Read unread mail for you (acknowledged on read). Call once at the start of a turn and once before you stop.",
				parameters: { type: "object", properties: {}, additionalProperties: false },
				effects: { concurrency: "shared" },
				execute: async () =>
					run(() => {
						const msgs = unread(room, name);
						if (!msgs.length) return `(no new messages for ${name})`;
						const text = msgs.map(formatMessage).join("\n");
						ack(room, name);
						return text;
					}),
			},
			{
				name: "boss_post",
				description: role === "boss" ? "Send a member a message (kind msg) or a reply to their ask (kind reply, ref '#seq'). --to is required: a member name or 'all'." : "Send the boss a status, an ask (a question you cannot resolve), or a reply. Short: facts, not narration.",
				parameters: {
					type: "object",
					properties: {
						kind: { type: "string", enum: role === "boss" ? ["msg", "reply"] : ["status", "ask", "reply"] },
						text: { type: "string", minLength: 1 },
						ref: { type: "string", description: "what this is about, e.g. '#12'" },
						...(role === "boss" ? { to: { type: "string", description: "member name or 'all'" } } : {}),
					},
					required: role === "boss" ? ["kind", "text", "to"] : ["kind", "text"],
					additionalProperties: false,
				},
				execute: async (input) => run(() => `posted #${post(room, { from: name, to: input.to, kind: input.kind, text: input.text, ref: input.ref }).seq}`),
			},
			{
				name: "boss_wait",
				description: "Block until mail for you arrives, then return it (acknowledged). Call this instead of ending your turn when you have nothing to do; on timeout, call it again.",
				parameters: {
					type: "object",
					properties: { timeoutSeconds: { type: "integer", minimum: 5, maximum: 600, description: "default 110" } },
					additionalProperties: false,
				},
				execute: async (input) =>
					run(() => {
						const msgs = waitForMail(room, name, { timeoutMs: (input.timeoutSeconds ?? 110) * 1000 });
						if (!msgs.length) return `(no mail in ${input.timeoutSeconds ?? 110}s — call boss_wait again to keep listening)`;
						return msgs.map(formatMessage).join("\n") + "\n(act on this, post a status, then boss_wait again)";
					}),
			},
		],
		approvals: [{ decide: (call) => (["boss_read", "boss_post", "boss_wait"].includes(call.name) ? { action: "allow" } : { action: "ask" }) }],
	};
}
