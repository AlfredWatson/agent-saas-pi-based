import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, test } from "vitest";
import { sessionTokenStats } from "../src/sessions/token-stats.js";

test("compaction changes only active context and the snapshot survives a Runtime restart", async () => {
	const root = await mkdtemp(join(tmpdir(), "pi-token-stats-"));
	const previousRoot = process.env.RUNTIME_DATA_ROOT;
	process.env.RUNTIME_DATA_ROOT = root;
	const sessionId = "00000000-0000-0000-0000-000000000025";
	let managed;
	let resumed;
	try {
		const { createSession } = await import("../src/sessions/session-factory.js");
		await mkdir(join(root, "workspaces", "ws"), { recursive: true });
		managed = await createSession("tenant", sessionId, {
			workspace_key: "ws", provider_id: "faux", api_key: "unused", model_id: "faux-1",
		});
		const manager = managed.session.sessionManager;
		expect(sessionTokenStats(manager)).toEqual({ total_tokens: 0, context_tokens: 0 });
		await managed.session.prompt("A".repeat(2000));
		const before = sessionTokenStats(manager);
		expect(before.total_tokens).toBeGreaterThan(0);
		expect(before.total_tokens).toBe(before.context_tokens);
		const originalMessages = manager.getEntries().filter((entry) => entry.type === "message");
		const lastMessage = originalMessages.at(-1);
		expect(lastMessage).toBeDefined();
		manager.appendCompaction("Short summary.", lastMessage!.id, before.context_tokens);
		managed.session.agent.state.messages = manager.buildSessionContext().messages;
		const compacted = sessionTokenStats(manager);
		expect(compacted.total_tokens).toBe(before.total_tokens);
		expect(compacted.context_tokens).toBeLessThan(before.context_tokens);
		await managed.session.prompt("Follow up message.");
		const after = sessionTokenStats(manager);
		expect(after.total_tokens).toBeGreaterThan(compacted.total_tokens);
		expect(after.context_tokens).toBeGreaterThan(compacted.context_tokens);
		const key = managed.sessionFile;
		managed.session.dispose();
		resumed = await createSession("tenant", sessionId, {
			workspace_key: "ws", provider_id: "faux", api_key: "unused", model_id: "faux-1", session_file_key: key,
		});
		expect(sessionTokenStats(resumed.session.sessionManager)).toEqual(after);
		const entries = (await readFile(join(root, "sessions", key), "utf8")).trim().split("\n").map((line) => JSON.parse(line));
		expect(entries.filter((entry) => entry.type === "message").length).toBeGreaterThan(originalMessages.length);
		expect(entries.some((entry) => entry.type === "compaction")).toBe(true);
	} finally {
		resumed?.session.dispose();
		managed?.session.dispose();
		if (previousRoot === undefined) delete process.env.RUNTIME_DATA_ROOT;
		else process.env.RUNTIME_DATA_ROOT = previousRoot;
		await rm(root, { recursive: true, force: true });
	}
});
