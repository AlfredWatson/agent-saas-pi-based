import { afterEach, expect, test, vi } from "vitest";
import { createCallSubagentsTool } from "../src/subagents/call-subagents-tool.js";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; vi.restoreAllMocks(); });

test("call_subagents runs sixteen tasks with at most four active and preserves result order", async () => {
	let active = 0;
	let maximum = 0;
	const tool = createCallSubagentsTool({ baseUrl: "http://gateway", secret: "secret", tenant: "tenant", parentSessionId: "parent", subagents: [{ name: "worker", description: "Do work", tools: ["read"] }] });
	globalThis.fetch = vi.fn(async (url) => {
		const path = String(url);
		if (path.endsWith("/batches")) return Response.json({ tasks: Array.from({ length: 16 }, (_, index) => ({ session_id: `child-${index}`, name: "worker" })) });
		if (path.endsWith("/run")) {
			active++;
			maximum = Math.max(maximum, active);
			await new Promise((resolve) => setTimeout(resolve, 2));
			active--;
			const index = Number(path.match(/child-(\d+)/)?.[1]);
			return Response.json({ session_id: `child-${index}`, name: "worker", status: index === 3 ? "failed" : "completed", output: index === 3 ? null : `answer-${index}`, error: index === 3 ? "subagent_timeout" : null });
		}
		throw new Error("unexpected request");
	}) as typeof fetch;
	const result = await tool.execute("call-1", { tasks: Array.from({ length: 16 }, (_, index) => ({ name: "worker", task: `task-${index}` })) }, undefined, undefined, {} as never);
	const details = result.details as { tasks: Array<{ session_id: string; status: string }> };
	expect(maximum).toBe(4);
	expect(details.tasks.map((item) => item.session_id)).toEqual(Array.from({ length: 16 }, (_, index) => `child-${index}`));
	expect(details.tasks[3].status).toBe("failed");
	expect(details.tasks[4].status).toBe("completed");
});

test("call_subagents cancels all child sessions on parent abort", async () => {
	const cancelled: string[] = [];
	const controller = new AbortController();
	const tool = createCallSubagentsTool({ baseUrl: "http://gateway", secret: "secret", tenant: "tenant", parentSessionId: "parent", subagents: [{ name: "worker", description: "Do work", tools: [] }] });
	globalThis.fetch = vi.fn(async (url) => {
		const path = String(url);
		if (path.endsWith("/batches")) return Response.json({ tasks: [{ session_id: "child-0", name: "worker" }, { session_id: "child-1", name: "worker" }] });
		if (path.endsWith("/cancel")) { cancelled.push(path); return Response.json({ status: "cancelled" }); }
		if (path.endsWith("/run")) {
			controller.abort();
			return Response.json({ session_id: path.includes("child-0") ? "child-0" : "child-1", name: "worker", status: "cancelled", output: null, error: "subagent_cancelled" });
		}
		throw new Error("unexpected request");
	}) as typeof fetch;
	await tool.execute("call-2", { tasks: [{ name: "worker", task: "one" }, { name: "worker", task: "two" }] }, controller.signal, undefined, {} as never);
	await vi.waitFor(() => expect(cancelled).toHaveLength(2));
});

test("call_subagents polls a running task until its durable result is ready", async () => {
	let reads = 0;
	const tool = createCallSubagentsTool({ baseUrl: "http://gateway", secret: "secret", tenant: "tenant", parentSessionId: "parent", subagents: [{ name: "worker", description: "Do work", tools: [] }] });
	globalThis.fetch = vi.fn(async (url, init) => {
		const path = String(url);
		if (path.endsWith("/batches")) return Response.json({ tasks: [{ session_id: "child", name: "worker" }] });
		if (path.endsWith("/run")) return Response.json({ session_id: "child", name: "worker", status: "running", output: null, error: null });
		if (path.endsWith("/tasks/child") && init?.method !== "POST") {
			reads++;
			return Response.json({ session_id: "child", name: "worker", status: "completed", output: "done", error: null });
		}
		throw new Error("unexpected request");
	}) as typeof fetch;
	const result = await tool.execute("call-3", { tasks: [{ name: "worker", task: "one" }] }, undefined, undefined, {} as never);
	expect(reads).toBe(1);
	expect((result.details as { tasks: Array<{ output: string }> }).tasks[0].output).toBe("done");
});
