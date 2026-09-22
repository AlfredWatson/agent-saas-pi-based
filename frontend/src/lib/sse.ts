import { accessToken, apiBase } from "./api";
import { ApiError } from "./errors";
import type { SseEvent } from "./types";

export async function streamMessage(
  sessionId: string,
  content: string,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = accessToken();
  const response = await fetch(`${apiBase}/sessions/${sessionId}/messages:stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify({ content }), signal,
  });
  if (!response.ok || !response.body) {
    let details: unknown; try { details = await response.json(); } catch { details = null; }
    const code = typeof details === "object" && details !== null && "detail" in details ? String((details as { detail: unknown }).detail) : "stream_failed";
    throw new ApiError(response.status, code, details);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let eventName = "message";
  let dataLines: string[] = [];
  const flush = () => {
    if (dataLines.length === 0) return;
    const raw = dataLines.join("\n");
    let data: Record<string, unknown> = {};
    try { data = JSON.parse(raw) as Record<string, unknown>; } catch { data = { raw }; }
    onEvent({ name: eventName, data });
    eventName = "message"; dataLines = [];
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffered += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const lines = buffered.split(/\r?\n/); buffered = lines.pop() ?? "";
      for (const line of lines) {
        if (!line) { flush(); continue; }
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (done) break;
    }
    if (buffered) {
      if (buffered.startsWith("data:")) dataLines.push(buffered.slice(5).trimStart());
      else if (buffered.startsWith("event:")) eventName = buffered.slice(6).trim();
    }
    flush();
  } finally { reader.releaseLock(); }
}
