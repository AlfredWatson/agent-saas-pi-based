// @vitest-environment jsdom
import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ChatCenter } from "./Workbench";
import { api } from "./lib/api";
import { streamMessage } from "./lib/sse";
import type { AgentSession, Workspace } from "./lib/types";

vi.mock("./lib/sse", () => ({ streamMessage: vi.fn() }));
beforeEach(() => { HTMLElement.prototype.scrollIntoView = vi.fn(); });
afterEach(() => { cleanup(); vi.useRealTimers(); focusManager.setFocused(undefined); vi.restoreAllMocks(); vi.mocked(streamMessage).mockReset(); });

const workspace = { id: "workspace-1", name: "测试工作区" } as Workspace;
const baseSession: AgentSession = {
  id: "session-1", status: "active", title: null, workspace_id: workspace.id,
  profile_id: null, provider_binding_id: "binding-1", model_id: "model-1",
  thinking_level: null, model_configured: true, knowledge_base_ids: [],
  total_tokens: 0, context_tokens: 0, latest_run: null,
  created_at: "2026-09-24T00:00:00Z", updated_at: "2026-09-24T00:00:00Z",
};

function renderChat(selected: AgentSession) {
  vi.spyOn(api, "messages").mockResolvedValue({ items: [] });
  vi.spyOn(api, "availableModels").mockResolvedValue({ items: [] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><ChatCenter key={selected.id} workspace={workspace} selected={selected} onOpenBindings={() => {}} /></QueryClientProvider>);
  return { ...view, client };
}

test("shows zero tokens for a new session and keeps counts visible beside a long title", async () => {
  const title = "这是一个很长的会话标题".repeat(12);
  const detail = { ...baseSession, title, total_tokens: 1234, context_tokens: 456 };
  vi.spyOn(api, "session").mockResolvedValue(detail);
  const view = renderChat(baseSession);
  const stats = screen.getByRole("group", { name: "会话 Token 统计" });
  expect(stats.textContent).toContain("累计 0 tokens");
  expect(stats.textContent).toContain("上下文 0 tokens");
  await waitFor(() => expect(stats.textContent).toContain("累计 1,234 tokens"));
  expect(stats.textContent).toContain("上下文 456 tokens");
  const heading = screen.getByRole("heading", { name: title });
  expect(heading.getAttribute("title")).toBe(title);
  expect(heading.className).toContain("truncate");
  expect(stats.className).toContain("shrink-0");

  const next = { ...baseSession, id: "session-2", title: "下一会话", total_tokens: 9876, context_tokens: 321 };
  vi.mocked(api.session).mockImplementation(async (id) => id === next.id ? next : detail);
  view.rerender(<QueryClientProvider client={view.client}><ChatCenter key={next.id} workspace={workspace} selected={next} onOpenBindings={() => {}} /></QueryClientProvider>);
  await waitFor(() => expect(screen.getByRole("group", { name: "会话 Token 统计" }).textContent).toContain("累计 9,876 tokens"));
  expect(screen.getByRole("heading", { name: "下一会话" })).toBeTruthy();
  expect(screen.getByRole("group", { name: "会话 Token 统计" }).textContent).toContain("上下文 321 tokens");
});

test.each(["completed", "failed"] as const)("refreshes persisted tokens immediately when a local stream %s", async (outcome) => {
  let detail = baseSession;
  const read = vi.spyOn(api, "session").mockImplementation(async () => detail);
  vi.mocked(streamMessage).mockImplementation(async () => {
    detail = { ...baseSession, total_tokens: 2560, context_tokens: 840 };
    if (outcome === "failed") throw new Error("stream_failed");
  });
  renderChat(baseSession);
  await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByPlaceholderText("处理任何事务..."), { target: { value: "继续" } });
  fireEvent.click(screen.getByTitle("发送"));
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("group", { name: "会话 Token 统计" }).textContent).toContain("累计 2,560 tokens");
  expect(screen.getByRole("group", { name: "会话 Token 统计" }).textContent).toContain("上下文 840 tokens");
});

test.each([
  { status: "completed", interval: 30_000 },
  { status: "running", interval: 5_000 },
] as const)("polls session detail every $interval ms when the latest run is $status", async ({ status, interval }) => {
  vi.useFakeTimers();
  const selected: AgentSession = { ...baseSession, latest_run: { id: "run-1", status, error: null, started_at: "2026-09-24T00:00:00Z", finished_at: status === "running" ? null : "2026-09-24T00:00:01Z" } };
  const read = vi.spyOn(api, "session").mockResolvedValue(selected);
  await act(async () => { renderChat(selected); await Promise.resolve(); });
  expect(read).toHaveBeenCalledTimes(1);
  await act(async () => { vi.advanceTimersByTime(interval - 1); await Promise.resolve(); });
  expect(read).toHaveBeenCalledTimes(1);
  await act(async () => { vi.advanceTimersByTime(1); await Promise.resolve(); });
  expect(read).toHaveBeenCalledTimes(2);
});

test("refreshes session detail when the page regains focus", async () => {
  const read = vi.spyOn(api, "session").mockResolvedValue(baseSession);
  renderChat(baseSession);
  await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
  await act(async () => { focusManager.setFocused(false); focusManager.setFocused(true); });
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
});
