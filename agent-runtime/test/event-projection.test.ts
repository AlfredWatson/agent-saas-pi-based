import { expect, test } from "vitest";
import { MAX_TOOL_PAYLOAD_BYTES, createPayloadRedactor, projectMessageEnd } from "../src/sessions/event-projection.js";

test("tool payloads redact credential fields and credential values before NDJSON", () => {
	const redactor = createPayloadRedactor(["provider-key", "runtime-shared-secret"]);
	const payload = redactor.payload({ api_key: "provider-key", nested: { Authorization: "Bearer provider-key" }, echo: "runtime-shared-secret" });
	expect(payload).toEqual({
		value: { api_key: "[REDACTED]", nested: { Authorization: "[REDACTED]" }, echo: "[REDACTED]" },
		payloadTruncated: false,
		originalBytes: expect.any(Number),
	});
});

test("oversize results keep a safe bounded preview and byte count", () => {
	const payload = createPayloadRedactor(["provider-key"]).payload({ result: "provider-key".repeat(MAX_TOOL_PAYLOAD_BYTES) });
	expect(payload.payloadTruncated).toBe(true);
	expect(payload.value).toMatchObject({ original_bytes: expect.any(Number), preview: expect.not.stringContaining("provider-key") });
});

test("message_end preserves Pi assistant/tool-result order and excludes thinking", () => {
	const redactor = createPayloadRedactor(["provider-key"]);
	const result = redactor.payload({ output: "ok", token: "provider-key" });
	const completed = new Map([[
		"call-1",
		{ toolCallId: "call-1", toolName: "read", result: result.value, isError: true, payloadTruncated: result.payloadTruncated },
	]]);
	const assistant = projectMessageEnd({ role: "assistant", content: [
		{ type: "thinking", thinking: "never emit this" },
		{ type: "text", text: "I'll inspect it." },
		{ type: "toolCall", id: "call-1", name: "read", arguments: { path: "a.txt", password: "provider-key" } },
	] }, completed, redactor);
	const toolResult = projectMessageEnd({ role: "toolResult", toolCallId: "call-1", toolName: "read", content: [{ type: "text", text: "ok" }], isError: true }, completed, redactor);
	expect(assistant).toEqual({ type: "message_end", message: { role: "assistant", content: [
		{ type: "text", content: "I'll inspect it." },
		{ type: "tool_call", tool_call_id: "call-1", tool_name: "read", args: { path: "a.txt", password: "[REDACTED]" }, payload_truncated: false },
	] } });
	expect(toolResult).toMatchObject({ type: "message_end", message: { role: "tool_result", tool_call_id: "call-1", tool_name: "read", is_error: true, result: { output: "ok", token: "[REDACTED]" } } });
});

test("message_end reports output length without exposing other provider stop details", () => {
	const redactor = createPayloadRedactor([]);
	const message = { role: "assistant", stopReason: "length", content: [{ type: "text", text: "incomplete" }] };
	expect(projectMessageEnd(message, new Map(), redactor)).toEqual({
		type: "message_end", message: { role: "assistant", stop_reason: "length", content: [{ type: "text", content: "incomplete" }] },
	});
});
