// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AgentInfo, ChatCenter, CreateSession } from "./Workbench";
import { SubagentPanel } from "./SubagentPanel";
import { api } from "./lib/api";
import { ApiError } from "./lib/errors";
import { streamMessage } from "./lib/sse";
import { initialSubagentConfig, validateSubagents } from "./lib/subagents";
import type { AgentConfig, AgentSession, ChatMessage, SubagentSession, Workspace } from "./lib/types";

vi.mock("./lib/sse", () => ({ streamMessage: vi.fn() }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.mocked(streamMessage).mockReset(); });

const workspace = { id: "workspace-1", name: "测试工作区" } as Workspace;
const session: AgentSession = {
  id: "session-1", status: "active", title: "主会话", workspace_id: workspace.id,
  profile_id: null, provider_binding_id: "binding-1", model_id: "model-1",
  thinking_level: null, model_configured: true, knowledge_base_ids: [],
  total_tokens: 0, context_tokens: 0, latest_run: null, config_version: 1,
  tools: ["read", "bash", "edit", "write", "call_subagents"],
  created_at: "2026-09-29T00:00:00Z", updated_at: "2026-09-29T00:00:00Z",
};
const definition = initialSubagentConfig(false).subagents[0];
const config: AgentConfig = { tools: session.tools!, subagents: [definition], config_version: 1 };

function provider(children: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}

function mockPanel(rows: SubagentSession[] = [], currentSession = session) {
  vi.spyOn(api, "agentConfig").mockResolvedValue(config);
  vi.spyOn(api, "session").mockResolvedValue(currentSession);
  vi.spyOn(api, "subagentSessions").mockResolvedValue({ items: rows });
  return provider(<SubagentPanel session={currentSession} focus={null} />);
}

test("new chat preserves the legacy request when subagents are off", async () => {
  vi.spyOn(api, "knowledgeBases").mockResolvedValue({ items: [] });
  const create = vi.spyOn(api, "createSession").mockResolvedValue(session);
  provider(<CreateSession open workspace={workspace} onClose={() => {}} onCreated={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "创建聊天" }));
  await waitFor(() => expect(create).toHaveBeenCalledWith({ workspace_id: workspace.id, knowledge_base_ids: [] }));
});

test.each([false, true])("new chat enables preset subagent with knowledge base=%s", async (withKb) => {
  vi.spyOn(api, "knowledgeBases").mockResolvedValue({ items: withKb ? [{ id: "kb-1", name: "资料库", status: "active" } as never] : [] });
  const create = vi.spyOn(api, "createSession").mockResolvedValue(session);
  provider(<CreateSession open workspace={workspace} onClose={() => {}} onCreated={() => {}} />);
  if (withKb) fireEvent.click(await screen.findByRole("checkbox", { name: "资料库" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "启用 call_subagents 工具" }));
  fireEvent.click(screen.getByRole("button", { name: "创建聊天" }));
  await waitFor(() => expect(create).toHaveBeenCalledWith({
    workspace_id: workspace.id, knowledge_base_ids: withKb ? ["kb-1"] : [],
    ...initialSubagentConfig(withKb),
  }));
});

test("definition validation rejects duplicates, recursive tools and unbound RAG", () => {
  expect(validateSubagents([definition, definition], false)).toMatch(/不能重复/);
  expect(validateSubagents([{ ...definition, tools: ["call_subagents"] }], false)).toMatch(/工具选择无效/);
  expect(validateSubagents([{ ...definition, tools: ["rag_search"] }], false)).toMatch(/绑定知识库/);
  expect(validateSubagents([], false)).toMatch(/1–16/);
});

test("right tab appears only for sessions with call_subagents", () => {
  const plain = { ...session, tools: null };
  const setTab = vi.fn();
  const view = provider(<AgentInfo session={plain} focus={null} tab="runtime" setTab={setTab} />);
  expect(screen.queryByRole("button", { name: "Subagent" })).toBeNull();
  view.rerender(<QueryClientProvider client={new QueryClient()}><AgentInfo session={session} focus={null} tab="runtime" setTab={setTab} /></QueryClientProvider>);
  expect(screen.getByRole("button", { name: "Subagent" })).toBeTruthy();
});

test("editor saves all definitions with the current tools and version", async () => {
  const save = vi.spyOn(api, "updateAgentConfig").mockResolvedValue({ ...session, config_version: 2 });
  mockPanel();
  fireEvent.click(screen.getByRole("button", { name: "定义设置" }));
  fireEvent.change(await screen.findByRole("textbox", { name: "Subagent 1 描述" }), { target: { value: "新的职责" } });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(session.id, {
    tools: config.tools, expected_config_version: 1,
    subagents: [{ ...definition, description: "新的职责" }],
  }));
  expect(await screen.findByText(/配置已保存/)).toBeTruthy();
});

test.each([
  ["session_config_conflict", "配置已被其他操作更新"],
  ["session_busy", "任务结束后可重试"],
] as const)("%s keeps the unsaved editor draft", async (code, note) => {
  vi.spyOn(api, "updateAgentConfig").mockRejectedValue(new ApiError(409, code));
  mockPanel();
  fireEvent.click(screen.getByRole("button", { name: "定义设置" }));
  const description = await screen.findByRole("textbox", { name: "Subagent 1 描述" }) as HTMLTextAreaElement;
  fireEvent.change(description, { target: { value: "保留的修改" } });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  expect(await screen.findByText(new RegExp(note))).toBeTruthy();
  expect(description.value).toBe("保留的修改");
});

test("running parent disables saving while leaving the draft editable", async () => {
  const running = { ...session, latest_run: { id: "run-1", status: "running", error: null, started_at: "2026-09-29T00:00:00Z", finished_at: null } };
  const save = vi.spyOn(api, "updateAgentConfig");
  mockPanel([], running);
  fireEvent.click(screen.getByRole("button", { name: "定义设置" }));
  fireEvent.change(await screen.findByRole("textbox", { name: "Subagent 1 描述" }), { target: { value: "运行时修改" } });
  expect((screen.getByRole("button", { name: "保存配置" }) as HTMLButtonElement).disabled).toBe(true);
  expect(save).not.toHaveBeenCalled();
});

const run = (status: string) => ({ id: "child-run", status, error: null, started_at: "2026-09-29T00:00:00Z", finished_at: status === "running" ? null : "2026-09-29T00:01:00Z" });
function child(id: string, taskIndex: number, toolCallId = "call-1", status = "completed"): SubagentSession {
  return { ...session, id, title: null, tools: definition.tools, latest_run: run(status),
    read_only: true, parent_session_id: session.id, parent_run_id: "parent-run", parent_tool_call_id: toolCallId,
    task_index: taskIndex, task: `任务 ${taskIndex + 1}`, subagent: definition };
}
const trace: ChatMessage[] = [
  { id: "m-1", run_id: "child-run", role: "user", content: "任务 1", sequence: 1, status: "completed", created_at: "2026-09-29T00:00:00Z", tool_call_id: null, tool_name: null, arguments: null, result: null, is_error: null, payload_truncated: false },
  { id: "m-2", run_id: "child-run", role: "tool_call", content: "", sequence: 2, status: "completed", created_at: "2026-09-29T00:00:01Z", tool_call_id: "read-1", tool_name: "read", arguments: { path: "README.md" }, result: null, is_error: null, payload_truncated: false },
  { id: "m-3", run_id: "child-run", role: "assistant", content: "**已完成**", sequence: 3, status: "completed", created_at: "2026-09-29T00:01:00Z", tool_call_id: null, tool_name: null, arguments: null, result: null, is_error: null, payload_truncated: false },
];

test("trace groups by parent call and opens child messages without write controls", async () => {
  const first = child("child-1", 0);
  vi.spyOn(api, "subagentSession").mockResolvedValue(first);
  const readMessages = vi.spyOn(api, "subagentMessages").mockResolvedValue({ items: trace });
  mockPanel([child("child-2", 1), child("child-3", 0, "call-2", "failed"), first]);
  expect(await screen.findByText(/call-1/)).toBeTruthy();
  expect(screen.getByText(/call-2/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /#1 generalist.*已完成.*任务 1/ }));
  const panel = await screen.findByLabelText("Subagent 只读轨迹");
  await waitFor(() => expect(within(panel).getByText(/已完成/)).toBeTruthy());
  expect(within(panel).getByText(/README.md/)).toBeTruthy();
  expect(readMessages).toHaveBeenCalledWith(session.id, first.id);
  expect(within(panel).queryByRole("textbox")).toBeNull();
  expect(within(panel).queryByRole("button", { name: "发送" })).toBeNull();
});

test("parent tool record opens the matching right-side call", async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.spyOn(api, "session").mockResolvedValue(session);
  vi.spyOn(api, "messages").mockResolvedValue({ items: [{ ...trace[1], id: "parent-tool", tool_call_id: "call-1", tool_name: "call_subagents" }] });
  vi.spyOn(api, "availableModels").mockResolvedValue({ items: [] });
  const open = vi.fn();
  provider(<ChatCenter workspace={workspace} selected={session} onOpenBindings={() => {}} onOpenSubagents={open} />);
  fireEvent.click(await screen.findByText("call_subagents"));
  fireEvent.click(screen.getByRole("button", { name: "查看 Subagent 轨迹" }));
  expect(open).toHaveBeenCalledWith("call-1");
});
