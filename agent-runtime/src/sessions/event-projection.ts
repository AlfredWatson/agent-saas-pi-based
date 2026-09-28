/**
 * The Runtime is the first public boundary for tool payloads.  Keep this
 * module dependency-free so the exact same, deliberately small contract can
 * be tested without starting Fastify or a model.
 */
export const MAX_TOOL_PAYLOAD_BYTES = 256 * 1024;
const PREVIEW_BYTES = 8 * 1024;
const SENSITIVE_FIELD = /(?:api[_-]?key|token|secret|password|authorization|credential|cookie)/i;

export type SafePayload = { value: unknown; payloadTruncated: boolean; originalBytes: number };
export type PayloadRedactor = { payload(value: unknown): SafePayload; text(value: unknown, fallback: string): string };
export type CompletedTool = { toolCallId: string; toolName: string; result: unknown; isError: boolean; payloadTruncated: boolean };

function jsonString(value: unknown): string {
	try { return JSON.stringify(value); } catch { return '"[Unserializable]"'; }
}

function bytePreview(value: string): string {
	let bytes = 0;
	let end = 0;
	for (const character of value) {
		const size = Buffer.byteLength(character);
		if (bytes + size > PREVIEW_BYTES) break;
		bytes += size;
		end += character.length;
	}
	return value.slice(0, end);
}

function jsonValue(value: unknown, secrets: readonly string[], seen = new WeakSet<object>(), depth = 0): unknown {
	if (depth > 64) return "[Depth limit]";
	if (typeof value === "string") {
		return secrets.reduce((safe, secret) => secret ? safe.split(secret).join("[REDACTED]") : safe, value);
	}
	if (value === null || typeof value === "boolean" || typeof value === "number") return value;
	if (typeof value === "bigint") return value.toString();
	if (typeof value === "undefined" || typeof value === "function" || typeof value === "symbol") return String(value);
	if (typeof value !== "object") return String(value);
	if (seen.has(value)) return "[Circular]";
	seen.add(value);
	if (Array.isArray(value)) return value.map((entry) => jsonValue(entry, secrets, seen, depth + 1));
	return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, entry]) => [
		key,
		SENSITIVE_FIELD.test(key) ? "[REDACTED]" : jsonValue(entry, secrets, seen, depth + 1),
	]));
}

export function createPayloadRedactor(secretValues: readonly string[]): PayloadRedactor {
	const secrets = secretValues.filter((value) => value.length > 0);
	const payload = (value: unknown): SafePayload => {
		// Size is deliberately measured before redaction but never logged or
		// returned verbatim.  Otherwise a huge secret-valued field could evade
		// the payload limit merely because it becomes "[REDACTED]".
		const originalBytes = Buffer.byteLength(jsonString(value));
		const safe = jsonValue(value, secrets);
		const serialized = jsonString(safe);
		if (originalBytes <= MAX_TOOL_PAYLOAD_BYTES) return { value: safe, payloadTruncated: false, originalBytes };
		return { value: { preview: bytePreview(serialized), original_bytes: originalBytes }, payloadTruncated: true, originalBytes };
	};
	return {
		payload,
		text(value: unknown, fallback: string): string {
			const safe = payload(value).value;
			if (typeof safe === "string" && safe.length > 0) return safe;
			return typeof safe === "string" ? fallback : jsonString(safe);
		},
	};
}

function textContent(content: unknown, redactor: PayloadRedactor, fallback: string): string {
	const blocks = Array.isArray(content) ? content : [];
	const text = blocks
		.filter((block): block is { type: string; text?: unknown } => typeof block === "object" && block !== null && "type" in block)
		.filter((block) => block.type === "text")
		.map((block) => typeof block.text === "string" ? block.text : "")
		.join("");
	return redactor.text(text, fallback);
}

/** Converts only user-visible Pi message_end events into the NDJSON contract. */
export function projectMessageEnd(message: unknown, completedTools: ReadonlyMap<string, CompletedTool>, redactor: PayloadRedactor): Record<string, unknown> | undefined {
	if (typeof message !== "object" || message === null) return undefined;
	const item = message as Record<string, unknown>;
	if (item.role === "assistant") {
		const blocks = Array.isArray(item.content) ? item.content : [];
		const stopReason = item.stopReason === "length" ? "length" : undefined;
		const content = blocks.flatMap((block): Record<string, unknown>[] => {
			if (typeof block !== "object" || block === null) return [];
			const value = block as Record<string, unknown>;
			if (value.type === "text") return [{ type: "text", content: redactor.text(value.text, "[empty assistant text]") }];
			if (value.type !== "toolCall") return []; // thinking is intentionally excluded.
			const args = redactor.payload(value.arguments);
			return [{ type: "tool_call", tool_call_id: String(value.id), tool_name: String(value.name), args: args.value, payload_truncated: args.payloadTruncated }];
		});
		return { type: "message_end", message: { role: "assistant", content, ...(stopReason ? { stop_reason: stopReason } : {}) } };
	}
	if (item.role === "toolResult") {
		const toolCallId = String(item.toolCallId ?? "");
		const completed = completedTools.get(toolCallId);
		const fallbackResult = { content: item.content, details: item.details };
		const result = completed ? { value: completed.result, payloadTruncated: completed.payloadTruncated } : redactor.payload(fallbackResult);
		return {
			type: "message_end",
			message: {
				role: "tool_result",
				tool_call_id: toolCallId,
				tool_name: String(item.toolName ?? completed?.toolName ?? "unknown"),
				content: textContent(item.content, redactor, "[tool result]"),
				result: result.value,
				is_error: Boolean(item.isError ?? completed?.isError),
				payload_truncated: result.payloadTruncated,
			},
		};
	}
	return undefined;
}
