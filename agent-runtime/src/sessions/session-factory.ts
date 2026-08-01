import { mkdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { createAgentSession, ModelRuntime, SessionManager } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { config } from "../config.js";
import type { ManagedSession } from "./session-registry.js";

function tenantPath(tenant: string, part: string): string {
	const path = resolve(config.dataRoot, "tenants", tenant, part);
	const root = resolve(config.dataRoot, "tenants", tenant);
	if (!path.startsWith(`${root}/`)) throw new Error("unsafe path");
	return path;
}

export type SessionInput = { workspace_key: string; model_id: string; thinking_level?: string; api_key: string; provider_id: string; session_file_key?: string };

/** Runtime credentials deliberately live only in this ModelRuntime instance. */
export async function createSession(tenant: string, input: SessionInput): Promise<ManagedSession> {
	const workspace = tenantPath(tenant, join("workspaces", input.workspace_key));
	const sessions = tenantPath(tenant, "sessions");
	const agentDir = tenantPath(tenant, "agent");
	await Promise.all([mkdir(workspace, { recursive: true }), mkdir(sessions, { recursive: true }), mkdir(agentDir, { recursive: true })]);
	const modelRuntime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null });
	if (input.provider_id === "faux") {
		const faux = fauxProvider({ provider: "faux", models: [{ id: "faux-1", name: "Faux 1", reasoning: true }] });
		faux.setResponses([fauxAssistantMessage("OK")]);
		modelRuntime.registerNativeProvider(faux.provider);
	} else await modelRuntime.setRuntimeApiKey(input.provider_id, input.api_key);
	const model = modelRuntime.getModel(input.provider_id, input.model_id);
	if (!model) throw new Error("invalid_model");
	const manager = input.session_file_key
		? SessionManager.open(input.session_file_key, sessions, workspace)
		: SessionManager.create(workspace, sessions);
	const { session } = await createAgentSession({
		cwd: workspace,
		agentDir,
		sessionManager: manager,
		modelRuntime,
		model,
		thinkingLevel: input.thinking_level as never,
	});
	return { tenant, session, busy: false, sessionFile: session.sessionFile };
}
