import Fastify from "fastify";
import { Readable } from "node:stream";
import { config } from "./config.js";
import { tenantFrom } from "./auth/internal-auth.js";
import { SessionRegistry } from "./sessions/session-registry.js";
import { configureManagedSession, createSession, deleteSessionData, deleteWorkspaceData, ensureRuntimeHome, InvalidSessionFileKeyError, type SessionInput, type WorkspaceSessionFile } from "./sessions/session-factory.js";
import { projectMessageEnd, type CompletedTool } from "./sessions/event-projection.js";
import { projectCompactionEvent } from "./sessions/compaction-event.js";
import { sessionTokenStats } from "./sessions/token-stats.js";
import { fauxProvider, getSupportedThinkingLevels, InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { discoverLocalModels, LocalModelError } from "./local-models.js";
import {
	deleteWorkspaceFile,
	downloadWorkspaceFile,
	InvalidWorkspaceFilePathError,
	listWorkspaceFiles,
	UnsupportedWorkspaceFileTypeError,
	uploadWorkspaceFile,
	WorkspaceFileExistsError,
	WorkspaceFileNotFoundError,
	WorkspaceFileTooLargeError,
	WorkspaceStorageLimitError,
} from "./workspaces/file-storage.js";

const app = Fastify({ logger: true, bodyLimit: config.workspaceFileMaxBytes + 1 });
const registry = new SessionRegistry();
app.addContentTypeParser("application/octet-stream", (_request, payload, done) => done(null, payload));

function fileError(reply: { code(code: number): { send(body: object): unknown } }, error: unknown): unknown {
	if (error instanceof InvalidWorkspaceFilePathError) return reply.code(422).send({ error: error.message });
	if (error instanceof UnsupportedWorkspaceFileTypeError) return reply.code(422).send({ error: error.message });
	if (error instanceof WorkspaceFileExistsError) return reply.code(409).send({ error: error.message });
	if (error instanceof WorkspaceFileNotFoundError) return reply.code(404).send({ error: error.message });
	if (error instanceof WorkspaceStorageLimitError) return reply.code(409).send({ error: error.message });
	if (error instanceof WorkspaceFileTooLargeError) return reply.code(413).send({ error: error.message });
	throw error;
}
async function providerCatalog(): Promise<{ id: string; name: string }[]> {
	const runtime = await (await import("@earendil-works/pi-coding-agent")).ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null });
	return [...runtime.getProviders().map((provider) => ({ id: provider.id, name: provider.name ?? provider.id })), { id: "faux", name: "Faux" }];
}

function authenticated(request: Parameters<typeof tenantFrom>[0], reply: { code(code: number): { send(body: object): void } }): string | undefined {
	try { 
		return tenantFrom(request); 
	} catch (error) { 
		reply.code(401).send({ error: error instanceof Error ? error.message : "unauthorized" }); 
		return undefined; 
	}
}

app.get("/internal/v1/health", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	return { status: "ok" };
});
app.get("/internal/v1/providers", async (request, reply) => { if (!authenticated(request, reply)) return; return { providers: await providerCatalog() }; });
app.post<{ Body: { provider_id: string; api_key: string } }>("/internal/v1/providers/validate", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	if (!(await providerCatalog()).some((provider) => provider.id === request.body.provider_id) || request.body.api_key.length === 0) return reply.code(422).send({ error: "invalid_provider" });
	// Binding is storage-only. It must not make a provider/model request.
	return { accepted: true };
});
app.post<{ Body: { base_url: string; api_key: string } }>("/internal/v1/local-models/discover", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	try { return await discoverLocalModels(request.body.base_url, request.body.api_key); }
	catch (error) {
		if (error instanceof LocalModelError) return reply.code(error.message === "model_service_unavailable" ? 503 : 422).send({ error: error.message });
		throw error;
	}
});
app.get<{ Querystring: { provider_id?: string }; }>("/internal/v1/models", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	const runtime = await (await import("@earendil-works/pi-coding-agent")).ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null });
	const providerId = request.query.provider_id;
	if (providerId === "faux") runtime.registerNativeProvider(fauxProvider({ provider: "faux", models: [{ id: "faux-1", name: "Faux 1", reasoning: true }] }).provider);
	return { models: runtime.getModels(providerId).map((model) => ({ id: model.id, provider_id: model.provider, name: model.name, thinking_levels: model.reasoning ? getSupportedThinkingLevels(model) : [] })) };
});
app.put<{ Params: { id: string }; Body: SessionInput }>("/internal/v1/sessions/:id", async (request, reply) => {
	const tenant = authenticated(request, reply); if (!tenant) return;
	let managed = registry.get(request.params.id);
	if (!managed) {
		try {
			managed = await createSession(tenant, request.params.id, request.body);
		} catch (error) {
			if (error instanceof InvalidSessionFileKeyError) return reply.code(422).send({ error: "invalid_session_file_key" });
			if (error instanceof LocalModelError) return reply.code(422).send({ error: error.message });
			if (error instanceof Error && error.message === "invalid_model") return reply.code(422).send({ error: "invalid_model" });
			throw error;
		}
		registry.set(request.params.id, managed);
	}
	if (managed.tenant !== tenant) return reply.code(403).send({ error: "tenant_mismatch" });
	try {
		await configureManagedSession(managed, request.body);
	} catch (error) {
		if (error instanceof LocalModelError) return reply.code(422).send({ error: error.message });
		if (error instanceof Error && error.message === "session_busy") return reply.code(409).send({ error: "session_busy" });
		if (error instanceof Error && error.message === "invalid_model") return reply.code(422).send({ error: "invalid_model" });
		throw error;
	}
	return { pi_session_id: managed.session.sessionId, session_file_key: managed.sessionFile, ...sessionTokenStats(managed.session.sessionManager) };
});
app.delete<{ Params: { workspaceKey: string }; Body: { sessions?: WorkspaceSessionFile[] } }>("/internal/v1/workspaces/:workspaceKey", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	const sessions = request.body?.sessions;
	if (!Array.isArray(sessions) || sessions.some((entry) => typeof entry?.session_id !== "string" || (entry.session_file_key !== undefined && entry.session_file_key !== null && typeof entry.session_file_key !== "string"))) {
		return reply.code(422).send({ error: "invalid_workspace_delete_request" });
	}
	const sessionIds = sessions.map((entry) => entry.session_id);
	if (registry.anyBusy(sessionIds)) return reply.code(409).send({ error: "workspace_busy" });
	try {
		await deleteWorkspaceData(request.params.workspaceKey, sessions);
	} catch (error) {
		if (error instanceof InvalidSessionFileKeyError || (error instanceof Error && error.message === "invalid_workspace_key")) return reply.code(422).send({ error: "invalid_workspace_delete_request" });
		throw error;
	}
	for (const sessionId of sessionIds) registry.delete(sessionId);
	return reply.code(204).send();
});
app.delete<{ Params: { id: string }; Body: { session_file_key?: string } }>("/internal/v1/sessions/:id", async (request, reply) => {
	const tenant = authenticated(request, reply); if (!tenant) return;
	if (typeof request.body?.session_file_key !== "string") return reply.code(422).send({ error: "invalid_session_delete_request" });
	const managed = registry.get(request.params.id);
	// Do not reveal whether another tenant happens to have this in-memory ID.
	if (managed && managed.tenant !== tenant) return reply.code(404).send({ error: "session_not_found" });
	if (managed?.busy) return reply.code(409).send({ error: "session_busy" });
	try {
		await deleteSessionData(request.body.session_file_key);
	} catch (error) {
		if (error instanceof InvalidSessionFileKeyError) return reply.code(422).send({ error: "invalid_session_delete_request" });
		throw error;
	}
	registry.delete(request.params.id);
	return reply.code(204).send();
});
app.get<{ Params: { workspaceKey: string } }>("/internal/v1/workspaces/:workspaceKey/files", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	try {
		return { items: await listWorkspaceFiles(request.params.workspaceKey) };
	} catch (error) {
		return fileError(reply, error);
	}
});
app.get<{ Params: { workspaceKey: string }; Querystring: { path?: string } }>("/internal/v1/workspaces/:workspaceKey/files/content", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	if (typeof request.query.path !== "string") return reply.code(422).send({ error: "invalid_file_path" });
	try {
		const file = await downloadWorkspaceFile(request.params.workspaceKey, request.query.path);
		return reply
			.header("content-type", "application/octet-stream")
			.header("content-length", String(file.size_bytes))
			.send(file.content);
	} catch (error) {
		return fileError(reply, error);
	}
});
app.put<{ Params: { workspaceKey: string }; Querystring: { path?: string; overwrite?: string }; Body: AsyncIterable<Uint8Array> }>("/internal/v1/workspaces/:workspaceKey/files", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	if (typeof request.query.path !== "string" || (request.query.overwrite !== undefined && request.query.overwrite !== "true" && request.query.overwrite !== "false")) {
		return reply.code(422).send({ error: "invalid_file_path" });
	}
	try {
		const file = await uploadWorkspaceFile(request.params.workspaceKey, request.query.path, request.body, request.query.overwrite === "true");
		return reply.code(file.created ? 201 : 200).send(file);
	} catch (error) {
		return fileError(reply, error);
	}
});
app.delete<{ Params: { workspaceKey: string }; Querystring: { path?: string } }>("/internal/v1/workspaces/:workspaceKey/files", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	if (typeof request.query.path !== "string") return reply.code(422).send({ error: "invalid_file_path" });
	try {
		await deleteWorkspaceFile(request.params.workspaceKey, request.query.path);
		return reply.code(204).send();
	} catch (error) {
		return fileError(reply, error);
	}
});
app.post<{ Params: { id: string }; Body: { content: string } }>("/internal/v1/sessions/:id/chat", async (request, reply) => {
	const tenant = authenticated(request, reply); if (!tenant) return;
	const managed = registry.get(request.params.id); if (!managed || managed.tenant !== tenant) return reply.code(404).send({ error: "session_not_found" });
	if (managed.busy) return reply.code(409).send({ error: "session_busy" });
	managed.busy = true; reply.header("content-type", "application/x-ndjson");
	const events: string[] = [];
	const completedTools = new Map<string, CompletedTool>();
	const enqueue = (event: Record<string, unknown>) => events.push(JSON.stringify(event));
	const unsubscribe = managed.session.subscribe((event) => {
		const compaction = projectCompactionEvent(event);
		if (compaction) enqueue(compaction);
		if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") events.push(JSON.stringify({ type: "text_delta", delta: event.assistantMessageEvent.delta }));
		if (event.type === "tool_execution_start") {
			const args = managed.redactor.payload(event.args);
			enqueue({ type: "tool_started", toolCallId: event.toolCallId, toolName: event.toolName, args: args.value, payload_truncated: args.payloadTruncated });
		}
		if (event.type === "tool_execution_end") {
			const result = managed.redactor.payload(event.result);
			completedTools.set(event.toolCallId, { toolCallId: event.toolCallId, toolName: event.toolName, result: result.value, isError: event.isError, payloadTruncated: result.payloadTruncated });
			enqueue({ type: "tool_completed", toolCallId: event.toolCallId, toolName: event.toolName, result: result.value, isError: event.isError, payload_truncated: result.payloadTruncated });
		}
		if (event.type === "message_end") {
			const projected = projectMessageEnd(event.message, completedTools, managed.redactor);
			if (projected) enqueue(projected);
		}
		if (event.type === "agent_settled") {
			enqueue({ type: "token_snapshot", ...sessionTokenStats(managed.session.sessionManager) });
			enqueue({ type: "agent_settled" });
		}
	});
	void managed.session.prompt(request.body.content).catch((_error: unknown) => {
		enqueue({ type: "token_snapshot", ...sessionTokenStats(managed.session.sessionManager) });
		enqueue({ type: "error", error: "agent_failed" });
	}).finally(() => { managed.busy = false; unsubscribe(); });
	return reply.send(Readable.from((async function* () {
		while (managed.busy || events.length > 0) {
			const event = events.shift();
			if (event) yield `${event}\n`;
			else await new Promise((resolve) => setTimeout(resolve, 10));
		}
	})()));
});
for (const action of ["abort", "steer", "follow-up"] as const) app.post<{ Params: { id: string }; Body: { content?: string } }>(`/internal/v1/sessions/:id/${action}`, async (request, reply) => {
	const tenant = authenticated(request, reply); if (!tenant) return;
	const managed = registry.get(request.params.id); if (!managed || managed.tenant !== tenant) return reply.code(404).send({ error: "session_not_found" });
	if (action === "abort") await managed.session.abort(); else if (!managed.busy) return reply.code(409).send({ error: "session_not_running" }); else if (action === "steer") await managed.session.steer(request.body.content ?? ""); else await managed.session.followUp(request.body.content ?? "");
	return { status: "accepted" };
});
await ensureRuntimeHome();
await app.listen({ host: config.host, port: config.port });
