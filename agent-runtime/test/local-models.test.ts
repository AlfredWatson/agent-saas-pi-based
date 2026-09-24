import { afterEach, expect, test, vi } from "vitest";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { discoverLocalModels, localApiBase, localRuntimeProviderId } from "../src/local-models.js";
import { localCompactionSettings } from "../src/sessions/local-compaction.js";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; vi.restoreAllMocks(); });

test("local API URLs accept private addresses and reject sensitive destinations", async () => {
	expect(await localApiBase("http://10.20.1.2:8000")).toBe("http://10.20.1.2:8000/v1");
	expect(await localApiBase("http://172.17.0.1:30000/v1/")).toBe("http://172.17.0.1:30000/v1");
	for (const url of [
		"http://127.0.0.1:8000", "http://169.254.169.254", "http://8.8.8.8",
		"http://10.0.0.1@10.0.0.2", "http://10.0.0.1/v1?token=x", "file:///tmp/model",
	]) await expect(localApiBase(url)).rejects.toThrow("invalid_model_base_url");
});

test("discovery reads only valid OpenAI model IDs and does not redirect", async () => {
	const fetch = vi.fn(async (_url: string, options: RequestInit) => {
		expect(options.redirect).toBe("error");
		expect(options.headers).toEqual({ Authorization: "Bearer secret" });
		return new Response(JSON.stringify({ data: [{ id: "team/model", name: "Model" }] }), { status: 200 });
	});
	globalThis.fetch = fetch as typeof globalThis.fetch;
	expect(await discoverLocalModels("http://10.1.2.3:8000", "secret")).toEqual({
		base_url: "http://10.1.2.3:8000/v1", models: [{ id: "team/model", name: "Model" }],
	});
	expect(fetch.mock.calls[0]?.[0]).toBe("http://10.1.2.3:8000/v1/models");
	globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ data: [{ id: "same" }, { id: "same" }] }), { status: 200 }));
	await expect(discoverLocalModels("http://10.1.2.3:8000", "")).rejects.toThrow("invalid_model_catalog");
});

test("each Binding has its own Pi provider ID", () => {
	expect(localRuntimeProviderId("00000000-0000-0000-0000-000000000001")).not.toBe(localRuntimeProviderId("00000000-0000-0000-0000-000000000002"));
});

test("local compaction reserves room within the model context", () => {
	expect(localCompactionSettings({ id: "small", name: "Small", context_window: 8192, max_tokens: 1024, reasoning: false }))
		.toEqual({ reserveTokens: 2048, keepRecentTokens: 2048 });
});

test("local provider streams a tool call and handles its result", async () => {
	const root = await mkdtemp(join(tmpdir(), "local-agent-"));
	const previousRoot = process.env.RUNTIME_DATA_ROOT;
	process.env.RUNTIME_DATA_ROOT = root;
	const { createSession, configureManagedSession } = await import("../src/sessions/session-factory.js");
	const { mkdir } = await import("node:fs/promises");
	await mkdir(join(root, "workspaces", "ws"), { recursive: true });
	await writeFile(join(root, "workspaces", "ws", "note.txt"), "local model tool result");
	let calls = 0;
	globalThis.fetch = vi.fn(async (_url, init) => {
		calls++;
		expect(init?.redirect).toBe("error");
		const headers = new Headers(init?.headers);
		expect(headers.get("authorization")).toBe("Bearer local-no-key");
		expect(JSON.parse(String(init?.body)).stream_options).toEqual({ include_usage: true });
		const chunk = calls === 1
			? { id: "one", object: "chat.completion.chunk", created: 1, model: "local-test", choices: [{ index: 0, delta: { role: "assistant", tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "read", arguments: '{"path":"note.txt"}' } }] }, finish_reason: "tool_calls" }] }
			: { id: "two", object: "chat.completion.chunk", created: 2, model: "local-test", choices: [{ index: 0, delta: { role: "assistant", content: "Read complete" }, finish_reason: "stop" }] };
		const usage = { id: "usage", object: "chat.completion.chunk", created: 3, model: "local-test", choices: [], usage: { prompt_tokens: 100, completion_tokens: 15, total_tokens: 115 } };
		return new Response(`data: ${JSON.stringify(chunk)}\n\ndata: ${JSON.stringify(usage)}\n\ndata: [DONE]\n\n`, { status: 200, headers: { "content-type": "text/event-stream" } });
	}) as typeof globalThis.fetch;
	let managed;
	try {
		managed = await createSession("tenant", "00000000-0000-0000-0000-000000000010", {
			workspace_key: "ws", provider_id: "vllm", provider_binding_id: "00000000-0000-0000-0000-000000000001",
			base_url: "http://10.1.2.3:8000/v1", api_key: "", model_id: "local-test",
			local_model: { id: "local-test", name: "Local Test", context_window: 8192, max_tokens: 1024, reasoning: false },
		});
		const events: string[] = [];
		const assistantUsage: number[] = [];
		const stop = managed.session.subscribe((event) => {
			events.push(event.type);
			if (event.type === "message_end" && event.message.role === "assistant") assistantUsage.push(event.message.usage.totalTokens);
		});
		await managed.session.prompt("Read note.txt");
		stop();
		expect(calls).toBe(2);
		expect(events).toContain("tool_execution_end");
		expect(events).toContain("agent_settled");
		expect(assistantUsage).toEqual([115, 115]);
		expect(managed.session.settingsManager.getCompactionSettings()).toMatchObject({ reserveTokens: 2048, keepRecentTokens: 2048 });
		await configureManagedSession(managed, {
			workspace_key: "ws", provider_id: "sglang", provider_binding_id: "00000000-0000-0000-0000-000000000002",
			base_url: "http://10.2.3.4:30000/v1", api_key: "secret", model_id: "second-model",
			local_model: { id: "second-model", name: "Second", context_window: 16384, max_tokens: 2048, reasoning: true },
		});
		expect(managed.providerId).toBe("local-00000000-0000-0000-0000-000000000002");
		expect(managed.modelRuntime.getModel(managed.providerId, "second-model")?.baseUrl).toBe("http://10.2.3.4:30000/v1");
		expect(managed.session.settingsManager.getCompactionSettings()).toMatchObject({ reserveTokens: 4096, keepRecentTokens: 4096 });
	} finally {
		managed?.session.dispose();
		if (previousRoot === undefined) delete process.env.RUNTIME_DATA_ROOT;
		else process.env.RUNTIME_DATA_ROOT = previousRoot;
		await rm(root, { recursive: true, force: true });
	}
}, 15_000);

test("local model usage triggers compaction before the context is exhausted", async () => {
	const root = await mkdtemp(join(tmpdir(), "local-compact-"));
	const previousRoot = process.env.RUNTIME_DATA_ROOT;
	process.env.RUNTIME_DATA_ROOT = root;
	const { createSession } = await import("../src/sessions/session-factory.js");
	let calls = 0;
	globalThis.fetch = vi.fn(async () => {
		calls++;
		const chunk = { id: String(calls), object: "chat.completion.chunk", created: 1, model: "small", choices: [{ index: 0, delta: { role: "assistant", content: calls === 3 ? "Summary of previous work." : "OK" }, finish_reason: "stop" }] };
		const usage = { id: String(calls), object: "chat.completion.chunk", created: 1, model: "small", choices: [], usage: { prompt_tokens: calls === 2 ? 850 : 100, completion_tokens: 20, total_tokens: calls === 2 ? 870 : 120 } };
		return new Response(`data: ${JSON.stringify(chunk)}\n\ndata: ${JSON.stringify(usage)}\n\ndata: [DONE]\n\n`, { status: 200, headers: { "content-type": "text/event-stream" } });
	}) as typeof globalThis.fetch;
	let managed;
	try {
		managed = await createSession("tenant", "00000000-0000-0000-0000-000000000011", {
			workspace_key: "ws", provider_id: "vllm", provider_binding_id: "00000000-0000-0000-0000-000000000011",
			base_url: "http://10.1.2.3:8000/v1", api_key: "", model_id: "small",
			local_model: { id: "small", name: "Small", context_window: 1024, max_tokens: 128, reasoning: false },
		});
		await managed.session.prompt("A".repeat(1500));
		const events: string[] = [];
		const compactionSucceeded: boolean[] = [];
		const stop = managed.session.subscribe((event) => {
			events.push(event.type);
			if (event.type === "compaction_end") compactionSucceeded.push(Boolean(event.result));
		});
		await managed.session.prompt("B".repeat(1500));
		stop();
		expect(calls).toBe(3);
		expect(events).toContain("compaction_start");
		expect(events).toContain("compaction_end");
		expect(compactionSucceeded).toEqual([true]);
	} finally {
		managed?.session.dispose();
		if (previousRoot === undefined) delete process.env.RUNTIME_DATA_ROOT;
		else process.env.RUNTIME_DATA_ROOT = previousRoot;
		await rm(root, { recursive: true, force: true });
	}
}, 15_000);
