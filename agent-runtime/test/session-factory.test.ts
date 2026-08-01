import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, test } from "vitest";

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))); });

test("Faux session uses the explicit model and Pi default coding tools", async () => {
	const root = await mkdtemp(join(tmpdir(), "pi-saas-runtime-")); roots.push(root);
	process.env.RUNTIME_DATA_ROOT = root;
	const { createSession } = await import("../src/sessions/session-factory.js");
	const managed = await createSession("00000000-0000-0000-0000-000000000001", { workspace_key: "workspace", provider_id: "faux", api_key: "unused", model_id: "faux-1", thinking_level: "low" });
	expect(managed.session.agent.state.tools.map((tool) => tool.name).sort()).toEqual(["bash", "edit", "read", "write"]);
	const events: string[] = [];
	const stop = managed.session.subscribe((event) => events.push(event.type));
	await managed.session.prompt("Say OK");
	stop(); managed.session.dispose();
	expect(events).toContain("agent_settled");
	expect(events).toContain("message_update");
});
