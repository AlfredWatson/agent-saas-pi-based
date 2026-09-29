import { Type } from "typebox";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";

export type SubagentDirectoryEntry = { name: string; description: string; tools: string[] };
export type SubagentTask = { name: string; task: string };

export type SubagentToolConfig = {
	baseUrl: string;
	secret: string;
	tenant: string;
	parentSessionId: string;
	subagents: SubagentDirectoryEntry[];
};

type CreatedTask = { session_id: string; name: string };
type TaskResult = { session_id: string; name: string; status: string; output: string | null; error: string | null };

export function createCallSubagentsTool(config: SubagentToolConfig): ToolDefinition {
	const base = `${config.baseUrl}/internal/v1/runtime-subagents`;
	const headers = { Authorization: `Bearer ${config.secret}`, "X-Tenant-ID": config.tenant, "Content-Type": "application/json" };
	const post = async (path: string, body?: unknown): Promise<Response> => fetch(`${base}${path}`, {
		method: "POST", headers, body: JSON.stringify(body ?? {}), signal: AbortSignal.timeout(30_000),
	});
	const get = async (path: string, parentSignal?: AbortSignal): Promise<Response> => fetch(`${base}${path}`, {
		headers,
		signal: parentSignal ? AbortSignal.any([parentSignal, AbortSignal.timeout(30_000)]) : AbortSignal.timeout(30_000),
	});
	return {
		name: "call_subagents",
		label: "Call subagents",
		description: "Run up to 16 configured subagent tasks with isolated conversations. Use a name from the system prompt's subagent directory.",
		promptSnippet: "Delegate up to 16 tasks to configured subagents and receive independent results.",
		parameters: Type.Object({ tasks: Type.Array(Type.Object({
			name: Type.String({ minLength: 1, maxLength: 64 }),
			task: Type.String({ minLength: 1, maxLength: 100_000 }),
		}), { minItems: 1, maxItems: 16 }) }),
		executionMode: "sequential",
		execute: async (toolCallId, params, signal) => {
			const tasks = (params as { tasks: SubagentTask[] }).tasks;
			if (tasks.some((task) => !config.subagents.some((item) => item.name === task.name))) {
				throw new Error("subagent_failed:unknown_subagent");
			}
			let created: Response | undefined;
			for (let attempt = 0; attempt < 2; attempt++) {
				try { created = await post("/batches", { parent_session_id: config.parentSessionId, tool_call_id: toolCallId, tasks }); break; }
				catch { /* idempotent Gateway batch creation permits one network retry */ }
			}
			if (!created) throw new Error("subagent_failed:batch_unavailable");
			if (!created.ok) throw new Error("subagent_failed:batch_unavailable");
			const data = await created.json() as { tasks?: CreatedTask[] };
			if (!Array.isArray(data.tasks) || data.tasks.length !== tasks.length) throw new Error("subagent_failed:invalid_batch");
			const ids = data.tasks.map((item) => item.session_id);
			const cancelAll = () => { for (const id of ids) void post(`/tasks/${id}/cancel`).catch(() => undefined); };
			signal?.addEventListener("abort", cancelAll, { once: true });
			if (signal?.aborted) cancelAll();
			const results: TaskResult[] = new Array(tasks.length);
			let next = 0;
			const worker = async () => {
				while (next < tasks.length) {
					const index = next++;
					const item = data.tasks![index];
					if (signal?.aborted) {
						results[index] = { session_id: item.session_id, name: item.name, status: "cancelled", output: null, error: "subagent_cancelled" };
						continue;
					}
					try {
						const response = await post(`/tasks/${item.session_id}/run`);
						if (!response.ok) throw new Error("subagent_unavailable");
						let value = await response.json() as TaskResult;
						for (let polls = 0; value.status === "running" && polls < 920; polls++) {
							if (signal?.aborted) throw new Error("subagent_cancelled");
							await new Promise((resolve) => setTimeout(resolve, 1000));
							const update = await get(`/tasks/${item.session_id}`, signal);
							if (!update.ok) throw new Error("subagent_unavailable");
							value = await update.json() as TaskResult;
						}
						if (value.status === "running") throw new Error("subagent_timeout");
						results[index] = value;
					} catch (error) {
						void post(`/tasks/${item.session_id}/cancel`).catch(() => undefined);
						const code = signal?.aborted ? "subagent_cancelled" : error instanceof Error && error.message === "subagent_timeout" ? "subagent_timeout" : "subagent_unavailable";
						results[index] = { session_id: item.session_id, name: item.name, status: signal?.aborted ? "cancelled" : "failed", output: null, error: code };
					}
				}
			};
			try {
				await Promise.all(Array.from({ length: Math.min(4, tasks.length) }, () => worker()));
			} finally {
				signal?.removeEventListener("abort", cancelAll);
			}
			const summary = results.map((item, index) => `[${index + 1}] ${item.name} (${item.session_id}) ${item.status}: ${item.output ?? item.error ?? ""}`).join("\n\n");
			return { content: [{ type: "text", text: summary }], details: { tasks: results } };
		},
	};
}
