// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ChatCenter } from "./Workbench";
import { api } from "./lib/api";
import { streamMessage } from "./lib/sse";
import type { AgentSession, Workspace } from "./lib/types";

vi.mock("./lib/sse", () => ({ streamMessage: vi.fn() }));
beforeEach(() => { HTMLElement.prototype.scrollIntoView = vi.fn(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.mocked(streamMessage).mockReset(); });

const session = {
  id: "session-1", status: "active", title: "测试会话", workspace_id: "workspace-1",
  profile_id: null, model_configured: true, knowledge_base_ids: [],
  provider_binding_id: "binding-1", model_id: "model-1", thinking_level: null,
  created_at: "2026-09-24T00:00:00Z", updated_at: "2026-09-24T00:00:00Z", latest_run: null,
} satisfies AgentSession;
const workspace = { id: "workspace-1", name: "测试工作区" } as Workspace;

function renderChat() {
  vi.spyOn(api, "session").mockResolvedValue(session);
  vi.spyOn(api, "messages").mockResolvedValue({ items: [] });
  vi.spyOn(api, "availableModels").mockResolvedValue({ items: [] });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ChatCenter workspace={workspace} selected={session} onOpenBindings={() => {}} />
  </QueryClientProvider>);
}

test("compaction status appears beside existing assistant text and clears on terminal events", async () => {
  let emit: (event: { name: string; data: Record<string, unknown> }) => void = () => {};
  let finish: () => void = () => {};
  vi.mocked(streamMessage).mockImplementation(async (_id, _content, onEvent) => {
    emit = onEvent;
    await new Promise<void>((resolve) => { finish = resolve; });
  });
  renderChat();
  fireEvent.change(screen.getByPlaceholderText("处理任何事务..."), { target: { value: "继续任务" } });
  fireEvent.click(screen.getByTitle("发送"));
  await waitFor(() => expect(streamMessage).toHaveBeenCalled());

  act(() => {
    emit({ name: "assistant.delta", data: { delta: "已有回复" } });
    emit({ name: "compaction.started", data: { reason: "threshold" } });
  });
  expect(screen.getByText("已有回复")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toBe("正在压缩…");

  for (const terminal of ["compaction.ended", "message.completed", "message.failed", "done"]) {
    act(() => emit({ name: terminal, data: {} }));
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("已有回复")).toBeTruthy();
    act(() => emit({ name: "compaction.started", data: { reason: "overflow" } }));
    expect(screen.getByRole("status")).toBeTruthy();
  }
  await act(async () => finish());
  expect(screen.queryByRole("status")).toBeNull();
});

test("compaction status clears if the stream fails", async () => {
  let emit: (event: { name: string; data: Record<string, unknown> }) => void = () => {};
  let fail: (error: Error) => void = () => {};
  vi.mocked(streamMessage).mockImplementation(async (_id, _content, onEvent) => {
    emit = onEvent;
    await new Promise<void>((_resolve, reject) => { fail = reject; });
  });
  renderChat();
  fireEvent.change(screen.getByPlaceholderText("处理任何事务..."), { target: { value: "继续任务" } });
  fireEvent.click(screen.getByTitle("发送"));
  await waitFor(() => expect(streamMessage).toHaveBeenCalled());
  act(() => emit({ name: "compaction.started", data: { reason: "threshold" } }));
  expect(screen.getByRole("status")).toBeTruthy();
  expect(screen.queryByText("Agent 正在思考…")).toBeNull();
  await act(async () => fail(new Error("stream_failed")));
  expect(screen.queryByRole("status")).toBeNull();
});
