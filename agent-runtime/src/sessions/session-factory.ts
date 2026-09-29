import { mkdir, rm } from "node:fs/promises";
import { basename, resolve } from "node:path";
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { config } from "../config.js";
import { createRagSearchTool, type RagKnowledgeBase } from "../rag/rag-tool.js";
import { createPayloadRedactor } from "./event-projection.js";
import { guardLocalModelFetch, isLocalProvider, localApiBase, localRuntimeProviderId, type LocalModel } from "../local-models.js";
import { localCompactionSettings } from "./local-compaction.js";
import { createCallSubagentsTool, type SubagentDirectoryEntry } from "../subagents/call-subagents-tool.js";
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

export type SessionInput = { workspace_key: string; model_id: string; thinking_level?: string; api_key: string; provider_id: string; provider_binding_id?: string; base_url?: string; local_model?: LocalModel; session_file_key?: string; knowledge_bases?: RagKnowledgeBase[]; tools?: string[]; subagents?: SubagentDirectoryEntry[]; subagent_system_prompt?: string | null; config_version?: number };
export type WorkspaceSessionFile = { session_id: string; session_file_key?: string | null };

async function installLocalProvider(modelRuntime: ModelRuntime, input: SessionInput): Promise<string> {
	if (!input.provider_binding_id || !input.base_url || !input.local_model || input.local_model.id !== input.model_id || input.thinking_level) throw new Error("invalid_model");
	const base = await localApiBase(input.base_url);
	guardLocalModelFetch(base);
	const model = input.local_model;
	if (!Number.isInteger(model.context_window) || !Number.isInteger(model.max_tokens) || model.max_tokens < 1 || model.context_window <= model.max_tokens || typeof model.reasoning !== "boolean") throw new Error("invalid_model");
	const providerId = localRuntimeProviderId(input.provider_binding_id);
	modelRuntime.registerProvider(providerId, {
		name: input.provider_id,
		baseUrl: base,
		api: "openai-completions",
		apiKey: input.api_key || "local-no-key",
		models: [{
			id: model.id, name: model.name, api: "openai-completions", reasoning: model.reasoning,
			input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			contextWindow: model.context_window, maxTokens: model.max_tokens,
			compat: { supportsUsageInStreaming: true, maxTokensField: "max_tokens", supportsReasoningEffort: false },
		}],
	});
	return providerId;
}

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

/** Delete a single validated Pi trajectory without touching its Workspace. */
export async function deleteSessionData(sessionFileKey: string): Promise<void> {
	const sessions = runtimePath("sessions");
	await rm(sessionFilePath(sessionFileKey, sessions), { force: true });
}

/** Align an idle Pi session with the Gateway-owned configuration. */
export async function configureManagedSession(managed: ManagedSession, input: SessionInput): Promise<void> {
	if (managed.busy) throw new Error("session_busy");
	const providerId = isLocalProvider(input.provider_id)
		? await installLocalProvider(managed.modelRuntime, input)
		: input.provider_id;
	if (!isLocalProvider(input.provider_id) && input.provider_id !== "faux") await managed.modelRuntime.setRuntimeApiKey(input.provider_id, input.api_key);
	const model = managed.modelRuntime.getModel(providerId, input.model_id);
	if (!model) throw new Error("invalid_model");
	if (managed.providerId !== providerId || managed.modelId !== input.model_id) {
		await managed.session.setModel(model);
		managed.providerId = providerId;
		managed.modelId = input.model_id;
	}
	if (isLocalProvider(input.provider_id) && input.local_model) {
		managed.session.settingsManager.applyOverrides({ compaction: localCompactionSettings(input.local_model) });
	}
	if (input.thinking_level !== undefined && input.thinking_level !== managed.thinkingLevel) {
		managed.session.setThinkingLevel(input.thinking_level as never);
		managed.thinkingLevel = input.thinking_level;
	}
	// The Gateway only streams current-run events; always keep the active key in
	// the Runtime-side recursive redactor as a second safety boundary.
	managed.redactor = createPayloadRedactor([config.sharedSecret, config.ragSharedSecret, input.api_key]);
}

/** Runtime credentials deliberately live only in this ModelRuntime instance. */
export async function createSession(tenant: string, sessionId: string, input: SessionInput): Promise<ManagedSession> {
	const workspace = workspacePath(input.workspace_key);
	const sessions = runtimePath("sessions");
	const agentDir = runtimePath("agent");
	await Promise.all([mkdir(workspace, { recursive: true }), mkdir(sessions, { recursive: true }), mkdir(agentDir, { recursive: true })]);
	const modelRuntime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null });
	let providerId = input.provider_id;
	if (input.provider_id === "faux") {
		const faux = fauxProvider({ provider: "faux", models: [{ id: "faux-1", name: "Faux 1", reasoning: true }] });
		faux.setResponses([fauxAssistantMessage("OK")]);
		modelRuntime.registerNativeProvider(faux.provider);
	} else if (isLocalProvider(input.provider_id)) providerId = await installLocalProvider(modelRuntime, input);
	else await modelRuntime.setRuntimeApiKey(input.provider_id, input.api_key);
	const model = modelRuntime.getModel(providerId, input.model_id);
	if (!model) throw new Error("invalid_model");
	const manager = input.session_file_key
		? SessionManager.open(sessionFilePath(input.session_file_key, sessions), sessions, workspace)
		: SessionManager.create(workspace, sessions);
	const knowledgeBases = input.knowledge_bases ?? [];
	if (knowledgeBases.length > 0 && (!config.gatewayBaseUrl || !config.ragSharedSecret)) throw new Error("rag_runtime_not_configured");
	const selectedTools = input.tools ?? ["read", "bash", "edit", "write", ...(knowledgeBases.length ? ["rag_search"] : [])];
	const mainAllowed = new Set(["read", "bash", "edit", "write", "grep", "find", "ls", "rag_search", "call_subagents"]);
	if (selectedTools.some((tool) => !mainAllowed.has(tool)) || (input.subagent_system_prompt && selectedTools.includes("call_subagents"))) throw new Error("invalid_session_tools");
	const directory = input.subagents ?? [];
	if (selectedTools.includes("call_subagents") && (!directory.length || !config.gatewayBaseUrl || !config.ragSharedSecret)) throw new Error("invalid_session_tools");
	const loader = new DefaultResourceLoader({
		cwd: workspace, agentDir,
		...(input.subagent_system_prompt ? { systemPromptOverride: () => input.subagent_system_prompt! } : {}),
		...(!input.subagent_system_prompt && directory.length ? { appendSystemPromptOverride: (base: string[]) => [
			...base,
			`## Available subagents\n${directory.map((item) => `- ${item.name}: ${item.description}. Tools: ${item.tools.join(", ") || "none"}.`).join("\n")}\nUse call_subagents with the matching name and a specific task.`,
		] } : {}),
	});
	await loader.reload();
	const customTools = [];
	if (selectedTools.includes("rag_search")) {
		if (!knowledgeBases.length) throw new Error("invalid_session_tools");
		customTools.push(createRagSearchTool({
			baseUrl: config.gatewayBaseUrl!, secret: config.ragSharedSecret,
			tenant, sessionId, knowledgeBases,
			timeoutMs: config.ragRequestTimeoutMs, maxResultBytes: config.ragResultMaxBytes,
		}));
	}
	if (selectedTools.includes("call_subagents")) customTools.push(createCallSubagentsTool({
		baseUrl: config.gatewayBaseUrl!, secret: config.ragSharedSecret,
		tenant, parentSessionId: sessionId, subagents: directory,
	}));
	const { session } = await createAgentSession({
		cwd: workspace,
		agentDir,
		sessionManager: manager,
		modelRuntime,
		model,
		thinkingLevel: input.thinking_level as never,
		tools: selectedTools,
		resourceLoader: loader,
		customTools,
	});
	if (isLocalProvider(input.provider_id) && input.local_model) {
		session.settingsManager.applyOverrides({ compaction: localCompactionSettings(input.local_model) });
	}
	const sessionFile = session.sessionFile ? basename(session.sessionFile) : undefined;
	if (!sessionFile || !SAFE_SESSION_FILE_KEY.test(sessionFile)) throw new Error("session_file_missing");
	return {
		tenant,
		session,
		modelRuntime,
		providerId,
		modelId: input.model_id,
		thinkingLevel: input.thinking_level,
		busy: false,
		sessionFile,
		redactor: createPayloadRedactor([config.sharedSecret, config.ragSharedSecret, input.api_key]),
		configVersion: input.config_version ?? 1,
		isSubagent: Boolean(input.subagent_system_prompt),
	};
}
