// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import { ExpandedDirectory, RetrievalLab } from "./Workbench";
import { api } from "./lib/api";
import type { AgentSession, ChatMessage, KnowledgeBase, Workspace } from "./lib/types";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

test("successful retrieval renders the API's top-level document and graph results", async () => {
  const retrieve = vi.spyOn(api, "retrieve")
    .mockResolvedValueOnce({ mode: "hybrid", items: [{ document_id: "document-1", chunk_id: "chunk-1", score: 0.91, source: "vector", text: "检索命中的正文" }], rerank: { configured: false, applied: false, error: null } })
    .mockResolvedValueOnce({ mode: "graph", nodes: [{ id: "node-1", name: "产品" }], edges: [], evidence: [{ document_id: "document-1", chunk_id: "chunk-1", node_id: "node-1", edge_id: null }] });
  const workspace = { id: "workspace-1" } as Workspace;
  const kb = { id: "kb-1" } as KnowledgeBase;
  render(<QueryClientProvider client={new QueryClient()}><RetrievalLab workspace={workspace} kb={kb} documents={[]} /></QueryClientProvider>);

  fireEvent.change(screen.getByRole("textbox", { name: "查询" }), { target: { value: "产品" } });
  fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
  await waitFor(() => expect(screen.getByText("检索命中的正文")).toBeTruthy());
  expect(screen.getByText("score 0.9100")).toBeTruthy();

  fireEvent.change(screen.getByRole("combobox", { name: "模式" }), { target: { value: "graph" } });
  fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
  await waitFor(() => expect(screen.getByText("模式：graph")).toBeTruthy());
  expect(screen.getByText("节点 · entity")).toBeTruthy();
  expect(screen.getByText("来源：document-1 / chunk-1")).toBeTruthy();
  expect(retrieve).toHaveBeenCalledTimes(2);
});

const session = { id: "session-1", title: "测试会话", workspace_id: "workspace-1" } as AgentSession;
const sessionWorkspace = { id: "workspace-1", name: "测试工作区", is_current: true } as Workspace;

function renderSessionDirectory() {
  const noop = () => {};
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={["/chat/session-1"]}>
    <ExpandedDirectory userEmail="user@example.com" workspaces={[sessionWorkspace]} workspace={sessionWorkspace} kbs={[]} sessions={[session]} expanded={sessionWorkspace.id} onOpenWorkspace={noop} onKnowledgeHome={noop} onKnowledge={noop} onCreateKnowledge={noop} onSession={noop} onAgent={noop} onNewChat={noop} onCreateWorkspace={noop} onCollapse={noop} onSignOut={noop} />
  </MemoryRouter></QueryClientProvider>);
}

test("session menu trims renames and requires confirmation before deletion", async () => {
  const rename = vi.spyOn(api, "updateSessionTitle").mockResolvedValue({ ...session, title: "新名称" });
  const remove = vi.spyOn(api, "deleteSession").mockResolvedValue(undefined);
  renderSessionDirectory();

  fireEvent.click(screen.getByRole("button", { name: "会话操作：测试会话" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
  fireEvent.change(screen.getByRole("textbox", { name: "会话名称" }), { target: { value: "  新名称  " } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(rename).toHaveBeenCalledWith("session-1", "新名称"));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

  fireEvent.click(screen.getByRole("button", { name: "会话操作：测试会话" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "删除会话" }));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(remove).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "会话操作：测试会话" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "删除会话" }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(remove).toHaveBeenCalledExactlyOnceWith("session-1"));
});

test("session export downloads the complete public message projection", async () => {
  const toolMessage: ChatMessage = { id: "message-1", run_id: "run-1", role: "tool_result", content: "找到结果", sequence: 1, status: "completed", created_at: "2026-01-01T00:00:00Z", tool_call_id: "call-1", tool_name: "search", arguments: null, result: { count: 2 }, is_error: false, payload_truncated: false };
  vi.spyOn(api, "session").mockResolvedValue(session);
  vi.spyOn(api, "messages").mockResolvedValue({ items: [toolMessage] });
  const createUrl = vi.fn().mockReturnValue("blob:session-export");
  vi.stubGlobal("URL", class MockURL extends URL { static createObjectURL = createUrl; static revokeObjectURL = vi.fn(); });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  renderSessionDirectory();

  fireEvent.click(screen.getByRole("button", { name: "会话操作：测试会话" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "导出会话" }));
  await waitFor(() => expect(createUrl).toHaveBeenCalledTimes(1));
  const blob = createUrl.mock.calls[0][0] as Blob;
  const contents = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(reader.error); reader.readAsText(blob); });
  expect(JSON.parse(contents)).toEqual({ session, messages: [toolMessage] });
  expect(click).toHaveBeenCalledTimes(1);
});

test("a failed session export shows an error without creating a download", async () => {
  vi.spyOn(api, "session").mockRejectedValue(new Error("读取会话失败"));
  vi.spyOn(api, "messages").mockResolvedValue({ items: [] });
  const createUrl = vi.fn();
  vi.stubGlobal("URL", class MockURL extends URL { static createObjectURL = createUrl; });
  renderSessionDirectory();

  fireEvent.click(screen.getByRole("button", { name: "会话操作：测试会话" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "导出会话" }));
  await waitFor(() => expect(screen.getByText("读取会话失败")).toBeTruthy());
  expect(createUrl).not.toHaveBeenCalled();
});

test("a failed session deletion keeps the confirmation open", async () => {
  vi.spyOn(api, "deleteSession").mockRejectedValue(new Error("会话正在运行"));
  renderSessionDirectory();

  fireEvent.click(screen.getByRole("button", { name: "会话操作：测试会话" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "删除会话" }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(screen.getByText("会话正在运行")).toBeTruthy());
  expect(screen.getByRole("dialog", { name: "删除会话" })).toBeTruthy();
});
