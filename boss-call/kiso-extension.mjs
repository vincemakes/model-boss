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
import { ack, formatMessage, identify, listRooms, loadRoom, post, resolveRoom, unread, waitForMailAsync } from "./src/mailbox.mjs";

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
		`Stay on the line: whenever you have nothing left to do, call boss_wait — it blocks until mail arrives (up to 30 minutes; if it comes back empty, call it again). ` +
		`Never end your turn to wait: an ended turn is a dropped line and nobody can call you back. The person at this terminal can interrupt; then their instruction wins.`;
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
				description: "Wait on the line. Blocks until mail for you arrives and returns it (acknowledged). Call it once when you have nothing left to do; it comes back only with mail, after 30 minutes, or when the person interrupts — then call it again.",
				parameters: {
					type: "object",
					properties: { timeoutSeconds: { type: "integer", minimum: 5, maximum: 7200, description: "default 1800" } },
					additionalProperties: false,
				},
				execute: async (input, ctx) => {
					const secs = input.timeoutSeconds ?? 1800;
					try {
						const msgs = await waitForMailAsync(room, name, { timeoutMs: secs * 1000, signal: ctx?.signal });
						if (ctx?.signal?.aborted) return { content: "(wait interrupted by the person at this terminal — do what they say)", isError: false };
						if (!msgs.length) return { content: `NO MAIL YET after ${secs}s. Call boss_wait again now; do not end your turn.`, isError: false };
						return { content: msgs.map(formatMessage).join("\n") + "\n(act on this, post a status, then boss_wait again)", isError: false };
					} catch (err) {
						return { content: `[boss-call] ${err.message}`, isError: true };
					}
				},
			},
		],
		approvals: [{ decide: (call) => (["boss_read", "boss_post", "boss_wait"].includes(call.name) ? { action: "allow" } : { action: "ask" }) }],
	};
}
