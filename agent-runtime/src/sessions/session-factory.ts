import { mkdir, rm } from "node:fs/promises";
import { basename, resolve } from "node:path";
import { createAgentSession, ModelRuntime, SessionManager } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { config } from "../config.js";
import { createRagSearchTool, type RagKnowledgeBase } from "../rag/rag-tool.js";
import { createPayloadRedactor } from "./event-projection.js";
import type { ManagedSession } from "./session-registry.js";

const SAFE_PATH_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const SAFE_SESSION_FILE_KEY = /^[A-Za-z0-9][A-Za-z0-9._-]*\.jsonl$/;

export class InvalidSessionFileKeyError extends Error {
	constructor() { super("invalid_session_file_key"); }
}

function runtimePath(...parts: string[]): string {
	const root = resolve(config.dataRoot);
	const path = resolve(root, ...parts);
	if (path !== root && !path.startsWith(`${root}/`)) throw new Error("unsafe path");
	return path;
}

function workspacePath(workspaceKey: string): string {
	if (!SAFE_PATH_SEGMENT.test(workspaceKey)) throw new Error("invalid_workspace_key");
	return runtimePath("workspaces", workspaceKey);
}

function sessionFilePath(sessionFileKey: string, sessions: string): string {
	if (!SAFE_SESSION_FILE_KEY.test(sessionFileKey)) throw new InvalidSessionFileKeyError();
	return resolve(sessions, sessionFileKey);
}

export type SessionInput = { workspace_key: string; model_id: string; thinking_level?: string; api_key: string; provider_id: string; session_file_key?: string; knowledge_bases?: RagKnowledgeBase[] };
export type WorkspaceSessionFile = { session_id: string; session_file_key?: string | null };

/** Make the writable HOME available before any Pi tool or subprocess needs it. */
export async function ensureRuntimeHome(): Promise<void> {
	await mkdir(runtimePath("home"), { recursive: true });
}

/** Delete only the supplied session trajectories and the requested workspace. */
export async function deleteWorkspaceData(workspaceKey: string, sessions: WorkspaceSessionFile[]): Promise<void> {
	const workspace = workspacePath(workspaceKey);
	const sessionDir = runtimePath("sessions");
	const sessionFiles = sessions
		.map(({ session_file_key }) => session_file_key ? sessionFilePath(session_file_key, sessionDir) : undefined)
		.filter((path): path is string => path !== undefined);
	for (const sessionFile of sessionFiles) await rm(sessionFile, { force: true });
	await rm(workspace, { recursive: true, force: true });
}

/** Runtime credentials deliberately live only in this ModelRuntime instance. */
export async function createSession(tenant: string, sessionId: string, input: SessionInput): Promise<ManagedSession> {
	const workspace = workspacePath(input.workspace_key);
	const sessions = runtimePath("sessions");
	const agentDir = runtimePath("agent");
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
		? SessionManager.open(sessionFilePath(input.session_file_key, sessions), sessions, workspace)
		: SessionManager.create(workspace, sessions);
	const knowledgeBases = input.knowledge_bases ?? [];
	if (knowledgeBases.length > 0 && (!config.gatewayBaseUrl || !config.ragSharedSecret)) throw new Error("rag_runtime_not_configured");
	const { session } = await createAgentSession({
		cwd: workspace,
		agentDir,
		sessionManager: manager,
		modelRuntime,
		model,
		thinkingLevel: input.thinking_level as never,
		customTools: knowledgeBases.length > 0 ? [createRagSearchTool({
			baseUrl: config.gatewayBaseUrl!,
			secret: config.ragSharedSecret,
			tenant,
			sessionId,
			knowledgeBases,
			timeoutMs: config.ragRequestTimeoutMs,
			maxResultBytes: config.ragResultMaxBytes,
		})] : [],
	});
	const sessionFile = session.sessionFile ? basename(session.sessionFile) : undefined;
	if (!sessionFile || !SAFE_SESSION_FILE_KEY.test(sessionFile)) throw new Error("session_file_missing");
	return { tenant, session, busy: false, sessionFile, redactor: createPayloadRedactor([config.sharedSecret, config.ragSharedSecret, input.api_key]) };
}
