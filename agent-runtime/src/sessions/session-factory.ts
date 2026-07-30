import { mkdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { createAgentSession, ModelRuntime, SessionManager } from "@earendil-works/pi-coding-agent";
import { config } from "../config.js";
import type { ManagedSession } from "./session-registry.js";

function tenantPath(tenant: string, part: string): string {
	const path = resolve(config.dataRoot, "tenants", tenant, part);
	const root = resolve(config.dataRoot, "tenants", tenant);
	if (!path.startsWith(`${root}/`)) throw new Error("unsafe path");
	return path;
}

export async function createSession(tenant: string, input: { workspace_key: string; model_id: string; thinking_level?: string; api_key?: string; provider_id?: string }): Promise<ManagedSession> {
	const workspace = tenantPath(tenant, join("workspaces", input.workspace_key));
	const sessions = tenantPath(tenant, "sessions");
	await Promise.all([mkdir(workspace, { recursive: true }), mkdir(sessions, { recursive: true })]);
	const modelRuntime = await ModelRuntime.create();
	if (input.api_key && input.provider_id) modelRuntime.setRuntimeApiKey(input.provider_id, input.api_key);
	const { session } = await createAgentSession({ cwd: workspace, sessionManager: SessionManager.create(workspace, sessions), modelRuntime });
	if (input.thinking_level) session.setThinkingLevel(input.thinking_level as never);
	return { tenant, session, busy: false, sessionFile: session.sessionFile };
}
