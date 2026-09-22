import { afterEach, expect, test, vi } from "vitest";
import { clearToken, storeToken } from "./api";
import { streamMessage } from "./sse";

afterEach(() => {
  clearToken();
  vi.unstubAllGlobals();
});

test("parses arbitrarily split POST SSE frames in order", async () => {
  storeToken("test-token");
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode("event: message.accepted\ndata: {}\n\nevent: assistant.delta\nda"));
      controller.enqueue(encoder.encode("ta: {\"delta\":\"你好\"}\n\nevent: done\ndata: {}\n\n"));
      controller.close();
    },
  });
  const fetch = vi.fn().mockResolvedValue(new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }));
  vi.stubGlobal("fetch", fetch);
  const events: string[] = [];

  await streamMessage("session-1", "hello", (event) => events.push(`${event.name}:${String(event.data.delta ?? "")}`));

  expect(fetch.mock.calls[0][1]).toMatchObject({ method: "POST", headers: expect.objectContaining({ Authorization: "Bearer test-token" }) });
  expect(events).toEqual(["message.accepted:", "assistant.delta:你好", "done:"]);
});
