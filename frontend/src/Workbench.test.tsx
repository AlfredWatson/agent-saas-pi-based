// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import { ExpandedDirectory, KnowledgeCenter, RetrievalLab } from "./Workbench";
import { DocumentEntries } from "./DocumentEntries";
import { api } from "./lib/api";
import { ApiError } from "./lib/errors";
import type { AgentSession, ChatMessage, KnowledgeBase, RagDocument, Workspace } from "./lib/types";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const ragWorkspace = { id: "workspace-1", name: "工作区" } as Workspace;
const ragKb = { id: "kb-1", name: "原知识库", status: "active" } as KnowledgeBase;
const ragStage = (status: string, progress: number) => ({ status, progress, message: null, error: null, updated_at: null });
const ragDocument: RagDocument = { id: "document-1", original_filename: "手册.pdf", stored_filename: "手册.pdf", status: "active", stages: {
  parsing: ragStage("succeeded", 100), chunking: ragStage("succeeded", 100),
  vectorization: ragStage("not_started", 0), graph: ragStage("not_started", 0),
} };

function CurrentPath() { return <output data-testid="current-path">{useLocation().pathname}</output>; }
function renderKnowledgeCenter() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={["/knowledge/kb-1"]}><CurrentPath /><KnowledgeCenter workspace={ragWorkspace} kb={ragKb} graphId={null} onBackFromGraph={() => {}} /></MemoryRouter></QueryClientProvider>);
}

test("document stage arrows switch exclusively and reset pagination when reopened", async () => {
  vi.spyOn(api, "documents").mockResolvedValue({ items: [ragDocument] });
  const entries = vi.spyOn(api, "documentEntries").mockImplementation(async (_workspace, _kb, _document, kind, _limit, offset) => ({
    items: [{ id: `${kind}-${offset}`, ordinal: offset, text: `${kind} 正文 ${offset}`, content_hash: "hash", metadata: {} }],
    total: kind === "blocks" ? 21 : 1, limit: 20, offset,
  }));
  renderKnowledgeCenter();
  fireEvent.click(await screen.findByRole("button", { name: "展开解析条目：手册.pdf" }));
  expect(await screen.findByText("blocks 正文 0")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(await screen.findByText("blocks 正文 20")).toBeTruthy();
  expect(entries).toHaveBeenCalledWith("workspace-1", "kb-1", "document-1", "blocks", 20, 20);
  fireEvent.click(screen.getByRole("button", { name: "展开切分条目：手册.pdf" }));
  expect(await screen.findByText("chunks 正文 0")).toBeTruthy();
  expect(screen.queryByText("blocks 正文 20")).toBeNull();
  expect(screen.getByRole("button", { name: "展开解析条目：手册.pdf" }).getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByRole("button", { name: "收起切分条目：手册.pdf" }).getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "展开解析条目：手册.pdf" }));
  expect(await screen.findByText("blocks 正文 0")).toBeTruthy();
  expect(screen.queryByText("chunks 正文 0")).toBeNull();
  expect(screen.queryByText("blocks 正文 20")).toBeNull();
  expect(screen.getByRole("button", { name: "收起解析条目：手册.pdf" }).getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("button", { name: "展开切分条目：手册.pdf" }).getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: "收起解析条目：手册.pdf" }));
  expect(screen.queryByText("blocks 正文 0")).toBeNull();
  expect(screen.queryByText("chunks 正文 0")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "展开切分条目：手册.pdf" }));
  expect(await screen.findByText("chunks 正文 0")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "收起切分条目：手册.pdf" }));
  expect(screen.queryByText("chunks 正文 0")).toBeNull();
});

test("block text can be saved before derived processing", async () => {
  vi.spyOn(api, "documents").mockResolvedValue({ items: [ragDocument] });
  vi.spyOn(api, "documentEntries").mockResolvedValue({ items: [{ id: "block-1", ordinal: 0, text: "旧正文", content_hash: "old", metadata: {} }], total: 1, limit: 20, offset: 0 });
  const update = vi.spyOn(api, "updateDocumentEntry").mockResolvedValue({ id: "block-1", ordinal: 0, text: "新正文", content_hash: "new", metadata: {} });
  renderKnowledgeCenter();
  fireEvent.click(await screen.findByRole("button", { name: "展开解析条目：手册.pdf" }));
  fireEvent.click(await screen.findByRole("button", { name: "编辑正文" }));
  fireEvent.change(screen.getByRole("textbox", { name: "条目正文" }), { target: { value: "新正文" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("workspace-1", "kb-1", "document-1", "blocks", "block-1", "新正文"));
});

test("processed documents explain why entry editing is disabled", async () => {
  vi.spyOn(api, "documentEntries").mockResolvedValue({ items: [{ id: "block-1", ordinal: 0, text: "正文", content_hash: "hash", metadata: {} }], total: 1, limit: 20, offset: 0 });
  const processed = { ...ragDocument, stages: { ...ragDocument.stages, vectorization: ragStage("succeeded", 100) } };
  render(<QueryClientProvider client={new QueryClient()}><DocumentEntries workspace={ragWorkspace} kb={ragKb} document={processed} kind="blocks" /></QueryClientProvider>);
  expect(await screen.findByText("正文")).toBeTruthy();
  expect((screen.getByRole("button", { name: "编辑正文" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("请先删除向量和图谱派生数据，再修改条目。")).toBeTruthy();
});

test("version publishing opens the copied knowledge base after completion", async () => {
  vi.spyOn(api, "documents").mockResolvedValue({ items: [] });
  const copy = vi.spyOn(api, "copyKnowledgeBase").mockResolvedValue({ operation_id: "operation-1", target_knowledge_base_id: "kb-2", status: "queued" });
  vi.spyOn(api, "ragOperation").mockResolvedValue({ id: "operation-1", kind: "copy", status: "succeeded", message: null, error: null });
  renderKnowledgeCenter();
  fireEvent.click(screen.getByRole("button", { name: "版本发布" }));
  fireEvent.change(screen.getByRole("textbox", { name: "新知识库名称" }), { target: { value: "  发布版  " } });
  fireEvent.click(screen.getByRole("button", { name: "确认发布" }));
  await waitFor(() => expect(copy).toHaveBeenCalledWith("workspace-1", "kb-1", "发布版"));
  await waitFor(() => expect(screen.getByTestId("current-path").textContent).toBe("/knowledge/kb-2"));
});

test("version publishing keeps the dialog open for duplicate names and copy failures", async () => {
  vi.spyOn(api, "documents").mockResolvedValue({ items: [] });
  const copy = vi.spyOn(api, "copyKnowledgeBase").mockRejectedValueOnce(new ApiError(409, "knowledge_base_exists"))
    .mockResolvedValueOnce({ operation_id: "operation-2", target_knowledge_base_id: "kb-2", status: "queued" });
  vi.spyOn(api, "ragOperation").mockResolvedValue({ id: "operation-2", kind: "copy", status: "failed", message: "failed", error: "copy_failed" });
  renderKnowledgeCenter();
  fireEvent.click(screen.getByRole("button", { name: "版本发布" }));
  fireEvent.change(screen.getByRole("textbox", { name: "新知识库名称" }), { target: { value: "发布版" } });
  fireEvent.click(screen.getByRole("button", { name: "确认发布" }));
  expect(await screen.findByText("当前工作区已有同名知识库。")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "确认发布" }));
  expect(await screen.findByText("copy_failed")).toBeTruthy();
  expect(screen.getByRole("dialog", { name: "版本发布" })).toBeTruthy();
  expect(copy).toHaveBeenCalledTimes(2);
});

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
