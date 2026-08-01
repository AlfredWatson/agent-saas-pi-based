import Fastify from "fastify";
import { Readable } from "node:stream";
import { config } from "./config.js";
import { tenantFrom } from "./auth/internal-auth.js";
import { SessionRegistry } from "./sessions/session-registry.js";
import { createSession, type SessionInput } from "./sessions/session-factory.js";
import { fauxProvider, InMemoryCredentialStore } from "@earendil-works/pi-ai";

const app = Fastify({ logger: true });
const registry = new SessionRegistry();
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
app.get<{ Querystring: { provider_id?: string }; }>("/internal/v1/models", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	const runtime = await (await import("@earendil-works/pi-coding-agent")).ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null });
	const providerId = request.query.provider_id;
	if (providerId === "faux") runtime.registerNativeProvider(fauxProvider({ provider: "faux", models: [{ id: "faux-1", name: "Faux 1", reasoning: true }] }).provider);
	return { models: runtime.getModels(providerId).map((model) => ({ id: model.id, provider_id: model.provider, name: model.name, thinking_levels: model.reasoning ? ["minimal", "low", "medium", "high"] : [] })) };
});
app.put<{ Params: { id: string }; Body: SessionInput }>("/internal/v1/sessions/:id", async (request, reply) => {
	const tenant = authenticated(request, reply); if (!tenant) return;
	let managed = registry.get(request.params.id);
	if (!managed) { managed = await createSession(tenant, request.body); registry.set(request.params.id, managed); }
	if (managed.tenant !== tenant) return reply.code(403).send({ error: "tenant_mismatch" });
	return { pi_session_id: managed.session.sessionId, session_file_key: managed.sessionFile };
});
app.post<{ Params: { id: string }; Body: { content: string } }>("/internal/v1/sessions/:id/chat", async (request, reply) => {
	const tenant = authenticated(request, reply); if (!tenant) return;
	const managed = registry.get(request.params.id); if (!managed || managed.tenant !== tenant) return reply.code(404).send({ error: "session_not_found" });
	if (managed.busy) return reply.code(409).send({ error: "session_busy" });
	managed.busy = true; reply.header("content-type", "application/x-ndjson");
	const events: string[] = []; const unsubscribe = managed.session.subscribe((event) => {
		if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") events.push(JSON.stringify({ type: "text_delta", delta: event.assistantMessageEvent.delta }));
		if (event.type === "tool_execution_start") events.push(JSON.stringify({ type: "tool_started", tool: event.toolName }));
		if (event.type === "tool_execution_end") events.push(JSON.stringify({ type: "tool_completed", tool: event.toolName }));
		if (event.type === "agent_settled") events.push(JSON.stringify({ type: "agent_settled" }));
	});
	void managed.session.prompt(request.body.content).catch((error: unknown) => events.push(JSON.stringify({ type: "error", error: "agent_failed" }))).finally(() => { managed.busy = false; unsubscribe(); });
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
await app.listen({ host: config.host, port: config.port });
