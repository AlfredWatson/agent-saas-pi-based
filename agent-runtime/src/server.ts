import Fastify from "fastify";
import { config } from "./config.js";
import { tenantFrom } from "./auth/internal-auth.js";
import { SessionRegistry } from "./sessions/session-registry.js";
import { createSession } from "./sessions/session-factory.js";

const app = Fastify({ logger: true });
const registry = new SessionRegistry();
const providers = [{ id: "anthropic", name: "Anthropic" }, { id: "openai", name: "OpenAI" }];

function authenticated(request: Parameters<typeof tenantFrom>[0], reply: { code(code: number): { send(body: object): void } }): string | undefined {
	try { return tenantFrom(request); } catch (error) { reply.code(401).send({ error: error instanceof Error ? error.message : "unauthorized" }); return undefined; }
}

app.get("/internal/v1/health", async () => ({ status: "ok" }));
app.get("/internal/v1/providers", async (request, reply) => { if (!authenticated(request, reply)) return; return { providers }; });
app.post<{ Body: { provider_id: string; api_key: string } }>("/internal/v1/providers/validate", async (request, reply) => {
	if (!authenticated(request, reply)) return;
	if (!providers.some((provider) => provider.id === request.body.provider_id) || request.body.api_key.length === 0) return reply.code(422).send({ error: "invalid_provider" });
	return { models: [] };
});
app.put<{ Params: { id: string }; Body: { workspace_key: string; model_id: string; thinking_level?: string; api_key?: string; provider_id?: string } }>("/internal/v1/sessions/:id", async (request, reply) => {
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
	const events: string[] = []; const unsubscribe = managed.session.subscribe((event) => { if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") events.push(JSON.stringify({ type: "text_delta", delta: event.assistantMessageEvent.delta })); });
	void managed.session.prompt(request.body.content).then(() => events.push(JSON.stringify({ type: "agent_settled" }))).catch((error: unknown) => events.push(JSON.stringify({ type: "error", error: String(error) }))).finally(() => { managed.busy = false; unsubscribe(); });
	return (async function* () { while (managed.busy || events.length > 0) { const event = events.shift(); if (event) yield `${event}\n`; else await new Promise((resolve) => setTimeout(resolve, 10)); } })();
});
for (const action of ["abort", "steer", "follow-up"] as const) app.post<{ Params: { id: string }; Body: { content?: string } }>(`/internal/v1/sessions/:id/${action}`, async (request, reply) => {
	const tenant = authenticated(request, reply); if (!tenant) return;
	const managed = registry.get(request.params.id); if (!managed || managed.tenant !== tenant) return reply.code(404).send({ error: "session_not_found" });
	if (action === "abort") await managed.session.abort(); else if (!managed.busy) return reply.code(409).send({ error: "session_not_running" }); else if (action === "steer") await managed.session.steer(request.body.content ?? ""); else await managed.session.followUp(request.body.content ?? "");
	return { status: "accepted" };
});
await app.listen({ host: "127.0.0.1", port: config.port });
