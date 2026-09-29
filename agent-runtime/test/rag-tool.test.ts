import { afterEach, expect, test, vi } from "vitest";
import { createRagSearchTool, RagToolError } from "../src/rag/rag-tool.js";

const config = {
	baseUrl: "http://gateway.internal",
	secret: "runtime-rag-secret",
	tenant: "00000000-0000-0000-0000-000000000001",
	sessionId: "00000000-0000-0000-0000-000000000002",
	knowledgeBases: [{ id: "00000000-0000-0000-0000-000000000003", name: "handbook" }],
	timeoutMs: 1_000,
	maxResultBytes: 512,
};

afterEach(() => vi.unstubAllGlobals());

test("rag_search sends only fixed Session context and renders cited evidence", async () => {
	const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
		knowledge_base: { id: config.knowledgeBases[0].id, name: "handbook" },
		result: { mode: "hybrid", items: [{ document_id: "doc-1", chunk_id: "chunk-1", score: 0.9, source: "hybrid", text: "The deployment port is 21995." }] },
	}), { status: 200, headers: { "Content-Type": "application/json" } }));
	vi.stubGlobal("fetch", fetch);
	const tool = createRagSearchTool(config);
	const result = await tool.execute("call-1", {
		knowledge_base_id: config.knowledgeBases[0].id,
		query: "which port is used",
		mode: "hybrid",
		rerank: false,
	}, undefined, undefined, {} as never);

	expect(fetch).toHaveBeenCalledOnce();
	expect(fetch.mock.calls[0][0]).toBe("http://gateway.internal/internal/v1/runtime-rag/retrieve");
	expect(fetch.mock.calls[0][1].headers).toMatchObject({
		Authorization: "Bearer runtime-rag-secret",
		"X-Tenant-ID": config.tenant,
	});
	expect(JSON.parse(fetch.mock.calls[0][1].body)).toMatchObject({ session_id: config.sessionId, rerank: false });
	expect(result.content[0]).toMatchObject({ type: "text" });
	expect((result.content[0] as { text: string }).text).toContain("document_id=doc-1 chunk_id=chunk-1");
});

test("rag_search rejects knowledge bases that are not bound to the Session", async () => {
	const fetch = vi.fn();
	vi.stubGlobal("fetch", fetch);
	const tool = createRagSearchTool(config);
	await expect(tool.execute("call-1", {
		knowledge_base_id: "00000000-0000-0000-0000-000000000004",
		query: "forbidden",
	}, undefined, undefined, {} as never)).rejects.toEqual(new RagToolError("knowledge_base_not_bound"));
	expect(fetch).not.toHaveBeenCalled();
});

test("rag_search returns a safe error code for Gateway failures", async () => {
	vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "embedding_model_required" }), {
		status: 409, headers: { "Content-Type": "application/json" },
	})));
	const tool = createRagSearchTool(config);
	await expect(tool.execute("call-1", {
		knowledge_base_id: config.knowledgeBases[0].id,
		query: "find the answer",
	}, undefined, undefined, {} as never)).rejects.toThrow("rag_search_failed:embedding_model_required");
});

test("rag_search bounds model-visible evidence output", async () => {
	vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
		knowledge_base: { name: "handbook" },
		result: { mode: "vector", items: [{ document_id: "doc", chunk_id: "chunk", text: "x".repeat(20_000) }] },
	}), { status: 200, headers: { "Content-Type": "application/json" } })));
	const tool = createRagSearchTool({ ...config, maxResultBytes: 512 });
	const result = await tool.execute("call-1", {
		knowledge_base_id: config.knowledgeBases[0].id,
		query: "large",
	}, undefined, undefined, {} as never);
	const output = (result.content[0] as { text: string }).text;
	expect(Buffer.byteLength(output)).toBeLessThanOrEqual(512);
	expect(output).toContain("truncated");
});
