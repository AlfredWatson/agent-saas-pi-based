import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

const workspace = { id: "workspace-1", name: "default", status: "active", is_current: true };
const knowledgeBase = { id: "kb-1", workspace_id: "workspace-1", name: "产品文档", status: "active", version: 1, file_backend: "local", block_backend: "postgres", chunk_backend: "postgres", vector_backend: "pgvector", graph_backend: "postgres", concurrency: { parsing: 1, chunking: 1, embedding: 1, graph: 1 } };
const baseSession = { id: "session-1", status: "active", title: "测试会话", knowledge_base_ids: [], workspace_id: "workspace-1", profile_id: null, provider_binding_id: "binding-1", model_id: "model-1", thinking_level: "medium", model_configured: true, total_tokens: 0, context_tokens: 0, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", latest_run: null };
const document = { id: "document-uuid-1", original_filename: "用户手册.pdf", stored_filename: "safe-user-manual.pdf", status: "active", stages: { parsing: { status: "not_started", progress: 0 }, chunking: { status: "not_started", progress: 0 }, vectorization: { status: "not_started", progress: 0 }, graph: { status: "not_started", progress: 0 } } };

async function mockApi(page: import("@playwright/test").Page, options: { extraSession?: boolean; deleteFailure?: number } = {}) {
  let sent = false; let fileRequests = 0; let bindingVisible = true; let deleteRequests = 0; let sessionDeleted = false;
  let session = { ...baseSession };
  let extraSession = options.extraSession ? { ...baseSession, id: "session-2", title: "另一会话" } : null;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname; const method = route.request().method(); const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/login")) return json({ access_token: "browser-test-token", token_type: "bearer" });
    if (path.endsWith("/auth/me")) return json({ id: "user-1", email: "browser@example.com", status: "active" });
    if (path.endsWith("/sessions/session-1/messages:stream")) { sent = true; return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: message.accepted\ndata: {}\n\nevent: assistant.delta\ndata: {\"delta\":\"流式回复\"}\n\nevent: message.completed\ndata: {}\n\nevent: done\ndata: {}\n\n" }); }
    if (path.endsWith("/sessions/session-1/messages")) return json({ items: sent ? [{ id: "message-user", run_id: "run-1", role: "user", content: "你好", sequence: 1, status: "completed", created_at: "2026-01-01T00:00:00Z", tool_call_id: null, tool_name: null, arguments: null, result: null, is_error: null, payload_truncated: false }, { id: "message-agent", run_id: "run-1", role: "assistant", content: "流式回复", sequence: 2, status: "completed", created_at: "2026-01-01T00:00:01Z", tool_call_id: null, tool_name: null, arguments: null, result: null, is_error: null, payload_truncated: false }] : [] });
    if (path.endsWith("/sessions/session-1/model-config") && method === "PUT") { const body = route.request().postDataJSON() as { provider_binding_id: string; model_id: string; thinking_level: string | null }; session = { ...session, ...body, model_configured: true }; return json(session); }
    if (path.endsWith("/sessions/session-1")) {
      if (method === "PATCH") { session = { ...session, ...route.request().postDataJSON() as { title: string } }; return json(session); }
      if (method === "DELETE") { deleteRequests += 1; if (options.deleteFailure) return json({ detail: options.deleteFailure === 409 ? "session_busy" : "session_delete_incomplete" }, options.deleteFailure); sessionDeleted = true; return route.fulfill({ status: 204 }); }
      return json(session);
    }
    if (path.endsWith("/sessions/session-2")) {
      if (method === "DELETE") { deleteRequests += 1; if (options.deleteFailure) return json({ detail: options.deleteFailure === 409 ? "session_busy" : "session_delete_incomplete" }, options.deleteFailure); extraSession = null; return route.fulfill({ status: 204 }); }
      return json(extraSession);
    }
    if (path.endsWith("/sessions")) return json({ items: [sessionDeleted ? null : session, extraSession].filter(Boolean) });
    if (path.endsWith("/workspaces/workspace-1/available-models")) return json({ items: [{ id: "model-1", provider_id: "openai", name: "GPT Test", thinking_levels: ["low", "medium", "high"], provider_binding_id: "binding-1", binding_name: "测试 Binding" }, { id: "model-2", provider_id: "openai", name: "GPT Alternate", thinking_levels: [], provider_binding_id: "binding-2", binding_name: "备用 Binding" }] });
    if (path.endsWith("/workspaces/workspace-1/knowledge-bases/kb-1/documents")) return json({ items: [document] });
    if (path.endsWith("/workspaces/workspace-1/knowledge-bases/kb-1/retrieve") && method === "POST") {
      const { mode } = route.request().postDataJSON() as { mode: string };
      return mode === "graph"
        ? json({ mode, nodes: [{ id: "node-1", name: "产品", entity_type: "entity" }], edges: [{ id: "edge-1", source_node_id: "node-1", target_node_id: "node-2", relation: "服务" }], evidence: [{ document_id: document.id, chunk_id: "chunk-1", node_id: "node-1", edge_id: null }] })
        : json({ mode, items: [{ document_id: document.id, chunk_id: "chunk-1", score: 0.91, retrieval_score: 0.83, source: "vector", text: "检索命中的正文" }], rerank: { configured: true, applied: true, error: null } });
    }
    if (path.endsWith("/workspaces/workspace-1/knowledge-bases/kb-1/models")) return json({ items: [] });
    if (path.endsWith("/workspaces/workspace-1/knowledge-bases/kb-1/graphs/graph-1")) return json({ id: "graph-1", name: "产品关系图", kind: "document", nodes: [{ id: "node-1", name: "产品", entity_type: "entity" }, { id: "node-2", name: "用户", entity_type: "entity" }], edges: [{ id: "edge-1", source_node_id: "node-1", target_node_id: "node-2", relation: "服务" }] });
    if (path.endsWith("/workspaces/workspace-1/knowledge-bases/kb-1/graphs")) return json({ items: [{ id: "graph-1", name: "产品关系图", kind: "document" }] });
    if (path.endsWith("/workspaces/workspace-1/knowledge-bases")) return json({ items: [knowledgeBase] });
    if (path.endsWith("/workspaces/workspace-1/files/content")) return route.fulfill({ status: 200, contentType: "application/octet-stream", body: "downloaded content" });
    if (path.endsWith("/workspaces/workspace-1/files")) { fileRequests += 1; return json({ items: [{ path: "docs/readme.txt", size_bytes: 18, modified_at: "2026-01-01T00:00:00Z" }] }); }
    if (path.endsWith("/workspaces/workspace-1/provider-bindings/binding-1") && method === "DELETE") {
      if (session.provider_binding_id === "binding-1") return json({ detail: "provider_binding_in_use" }, 409);
      bindingVisible = false; return route.fulfill({ status: 204 });
    }
    if (path.endsWith("/workspaces/workspace-1/provider-bindings")) {
      return json({ items: bindingVisible ? [{ id: "binding-1", workspace_id: "workspace-1", provider_id: "openai", display_name: "测试 Binding", status: "active" }] : [] });
    }
    if (path.endsWith("/providers")) return json({ providers: [{ id: "openai", name: "OpenAI" }] });
    if (path.endsWith("/workspaces")) return json({ items: [workspace] });
    if (path.endsWith("/runtime")) return json({ state: "stopped", image: null, last_error: null, last_seen_at: null });
    return json({});
  });
  return { fileRequests: () => fileRequests, deleteRequests: () => deleteRequests };
}

async function signIn(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("browser@example.com");
  await page.getByLabel("密码").fill("very-long-browser-password");
  await page.getByRole("button", { name: "登录" }).click();
}

test("three-pane workbench keeps icon rails after collapsing", async ({ page }) => {
  await mockApi(page); await signIn(page);
  await expect(page.getByText("智能体应用开发工具集")).toBeVisible();
  await page.getByTitle("收起目录栏").click();
  await expect(page.getByTitle("展开目录栏")).toBeVisible();
  await expect(page.getByText("显示目录")).toHaveCount(0);
  await page.getByTitle("收起信息栏").click();
  await expect(page.getByTitle("展开信息栏")).toBeVisible();
  await expect(page.getByText("显示信息")).toHaveCount(0);
});

test("category navigation stays on a guide page and workspace files refresh and download", async ({ page }) => {
  const api = await mockApi(page); await signIn(page);
  await page.getByRole("button", { name: "知识库", exact: true }).click();
  await expect(page.getByRole("heading", { name: "知识库" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "Agent", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agent" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const before = api.fileRequests();
  await page.getByTitle("刷新文件").click();
  await expect.poll(api.fileRequests).toBeGreaterThan(before);
  const download = page.waitForEvent("download");
  await page.getByTitle("下载 docs/readme.txt").click();
  expect((await download).suggestedFilename()).toBe("readme.txt");
});

test("binding name is required and an in-use binding stays visible until its session switches", async ({ page }) => {
  await mockApi(page); await signIn(page);
  await page.getByRole("button", { name: "Provider Bindings", exact: true }).click();
  await page.getByRole("button", { name: "添加 Binding" }).click();
  await page.getByRole("combobox").selectOption("openai");
  await page.getByPlaceholder("API Key").fill("secret");
  await expect(page.getByRole("button", { name: "保存" })).toBeDisabled();
  await page.getByRole("dialog").getByRole("button", { name: "关闭" }).click();
  await page.getByTitle("删除 测试 Binding").click();
  await expect(page.getByText("仍有会话正在使用该 Provider Binding，请先切换这些会话的模型。")).toBeVisible();
  await expect(page.getByTitle("删除 测试 Binding")).toBeVisible();
  await page.getByText("测试会话", { exact: true }).click();
  await page.getByLabel("选择模型").click();
  await page.getByText("GPT Alternate", { exact: true }).click();
  await page.getByRole("button", { name: "Provider Bindings", exact: true }).click();
  await page.getByTitle("删除 测试 Binding").click();
  await expect(page.getByTitle("删除 测试 Binding")).toHaveCount(0);
});

test("knowledge settings and chat composer expose the requested controls", async ({ page }) => {
  await mockApi(page); await signIn(page);
  await page.getByText("产品文档", { exact: true }).click();
  await expect(page.getByText("document-uuid-1")).toBeVisible();
  await expect(page.getByText("用户手册.pdf")).toBeVisible();
  for (const tab of ["模型设置", "知识图谱", "高级设置", "检索实验"]) { await page.getByRole("button", { name: tab }).click(); await expect(page.getByRole("button", { name: tab })).toBeVisible(); }
  await page.getByRole("button", { name: "知识图谱" }).click();
  await page.getByText("产品关系图", { exact: true }).click();
  await expect(page.getByRole("button", { name: "返回文档" })).toBeVisible();
  await page.getByRole("button", { name: "返回文档" }).click();
  await page.getByText("Agent", { exact: true }).click();
  await page.getByText("测试会话", { exact: true }).click();
  await page.getByLabel("添加文件").click();
  await expect(page.getByText("添加照片和文件")).toBeVisible();
  await page.getByLabel("选择模型").click();
  await expect(page.getByText("GPT Test", { exact: true })).toBeVisible();
  await expect(page.getByText("思考强度")).toBeVisible();
  await page.getByPlaceholder("处理任何事务...").fill("你好");
  await page.getByTitle("发送").click();
  await expect(page.getByText("你好", { exact: true })).toBeVisible();
  await expect(page.getByText("流式回复", { exact: true })).toBeVisible();
});

test("session menu renames the active chat and exports its public history as JSON", async ({ page }) => {
  await mockApi(page); await signIn(page);
  await page.getByRole("button", { name: "测试会话", exact: true }).click();
  await expect(page.getByRole("heading", { name: "测试会话" })).toBeVisible();
  await expect(page.getByRole("button", { name: "删除会话" })).toHaveCount(0);

  const actions = page.getByRole("button", { name: "会话操作：测试会话" });
  await actions.click();
  await expect(page.getByRole("menuitem", { name: "重命名" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menuitem", { name: "重命名" })).toHaveCount(0);
  await actions.click();
  await page.getByRole("heading", { name: "测试会话" }).click();
  await expect(page.getByRole("menuitem", { name: "重命名" })).toHaveCount(0);
  await actions.click();
  await page.getByRole("menuitem", { name: "重命名" }).click();
  await page.getByRole("dialog", { name: "重命名会话" }).getByRole("textbox", { name: "会话名称" }).fill("  改名后的会话  ");
  await page.getByRole("dialog", { name: "重命名会话" }).getByRole("button", { name: "保存" }).click();
  await expect(page.getByRole("heading", { name: "改名后的会话" })).toBeVisible();
  await expect(page.getByRole("button", { name: "改名后的会话", exact: true })).toBeVisible();

  const history = [{ id: "tool-1", run_id: "run-1", role: "tool_call", content: "", sequence: 1, status: "completed", created_at: "2026-01-01T00:00:00Z", tool_call_id: "call-1", tool_name: "search", arguments: { query: "产品" }, result: null, is_error: null, payload_truncated: false }, { id: "tool-2", run_id: "run-1", role: "tool_result", content: "结果", sequence: 2, status: "completed", created_at: "2026-01-01T00:00:01Z", tool_call_id: "call-1", tool_name: "search", arguments: null, result: { count: 1 }, is_error: false, payload_truncated: false }];
  await page.route("**/api/v1/sessions/session-1/messages", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: history }) }));
  await page.getByRole("button", { name: "会话操作：改名后的会话" }).click();
  const pendingDownload = page.waitForEvent("download");
  await page.getByRole("menuitem", { name: "导出会话" }).click();
  const download = await pendingDownload;
  expect(download.suggestedFilename()).toBe("agent-session-session-1.json");
  const exported = JSON.parse(await readFile(await download.path(), "utf8")) as { session: typeof baseSession; messages: typeof history };
  expect(exported.session.title).toBe("改名后的会话");
  expect(exported.messages).toEqual(history);
});

test("session deletion requires confirmation and only removes the chosen session", async ({ page }) => {
  const api = await mockApi(page, { extraSession: true }); await signIn(page);
  await page.getByRole("button", { name: "测试会话", exact: true }).click();
  await page.getByRole("button", { name: "会话操作：另一会话" }).click();
  await page.getByRole("menuitem", { name: "删除会话" }).click();
  await expect(page.getByRole("dialog", { name: "删除会话" })).toContainText("另一会话");
  await page.getByRole("dialog", { name: "删除会话" }).getByRole("button", { name: "取消" }).click();
  expect(api.deleteRequests()).toBe(0);
  await page.getByRole("button", { name: "会话操作：另一会话" }).click();
  await page.getByRole("menuitem", { name: "删除会话" }).click();
  await page.getByRole("dialog", { name: "删除会话" }).getByRole("button", { name: "确认删除" }).click();
  await expect(page.getByRole("button", { name: "另一会话", exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "测试会话" })).toBeVisible();
  expect(api.deleteRequests()).toBe(1);

  await page.getByRole("button", { name: "会话操作：测试会话" }).click();
  await page.getByRole("menuitem", { name: "删除会话" }).click();
  await page.getByRole("dialog", { name: "删除会话" }).getByRole("button", { name: "确认删除" }).click();
  await expect(page).toHaveURL(/\/chat$/);
  expect(api.deleteRequests()).toBe(2);
});

test("a failed session deletion keeps the confirmation dialog and explains the error", async ({ page }) => {
  const api = await mockApi(page, { deleteFailure: 409 }); await signIn(page);
  await page.getByRole("button", { name: "会话操作：测试会话" }).click();
  await page.getByRole("menuitem", { name: "删除会话" }).click();
  await page.getByRole("dialog", { name: "删除会话" }).getByRole("button", { name: "确认删除" }).click();
  await expect(page.getByRole("dialog", { name: "删除会话" })).toContainText("该会话正在执行任务");
  await expect(page.getByRole("button", { name: "测试会话", exact: true })).toBeVisible();
  expect(api.deleteRequests()).toBe(1);
});

test("retrieval results render for document and graph modes without blanking the workbench", async ({ page }) => {
  await mockApi(page); await signIn(page);
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await page.getByText("产品文档", { exact: true }).click();
  await page.getByRole("button", { name: "检索实验" }).click();
  await page.getByRole("textbox", { name: "查询" }).fill("产品");
  await page.getByRole("button", { name: "运行检索" }).click();
  await expect(page.getByText("检索命中的正文")).toBeVisible();
  await expect(page.getByText("score 0.9100")).toBeVisible();
  await page.getByRole("combobox", { name: "模式" }).selectOption("graph");
  await page.getByRole("button", { name: "运行检索" }).click();
  await expect(page.getByText("模式：graph")).toBeVisible();
  await expect(page.getByText("节点 · entity")).toBeVisible();
  await expect(page.getByText("来源：document-uuid-1 / chunk-1")).toBeVisible();
  await expect(page.getByRole("button", { name: "检索实验" })).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("new chat presets a subagent and the right rail shows its read-only trace", async ({ page }) => {
  await mockApi(page);
  const preset = {
    name: "generalist", description: "处理主 Agent 委派的独立任务",
    system_prompt: "你是主 Agent 委派的通用子代理。仅完成收到的任务，必要时使用可用工具，并清楚报告结果及依据。",
    tools: ["read", "bash", "edit", "write", "rag_search"],
  };
  const tools = [...preset.tools, "call_subagents"];
  let created = false;
  let createdBody: Record<string, unknown> | null = null;
  let config = { tools, subagents: [preset], config_version: 1 };
  let updatedBody: Record<string, unknown> | null = null;
  let childStatus = "queued";
  const newSession = () => ({ ...baseSession, id: "session-3", title: "Subagent 会话", knowledge_base_ids: ["kb-1"], tools, config_version: config.config_version });
  const child = () => ({ ...newSession(), id: "child-1", title: null, tools: preset.tools, read_only: true,
    parent_session_id: "session-3", parent_run_id: "run-3", parent_tool_call_id: "call-3", task_index: 0,
    task: "检查手册", subagent: preset,
    latest_run: { id: "child-run-1", status: childStatus, error: null, started_at: "2026-01-01T00:00:00Z", finished_at: childStatus === "completed" ? "2026-01-01T00:00:10Z" : null } });
  await page.route("**/api/v1/sessions**", async (route) => {
    const path = new URL(route.request().url()).pathname; const method = route.request().method();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/sessions") && method === "POST") {
      createdBody = route.request().postDataJSON() as Record<string, unknown>;
      created = true;
      return json(newSession(), 201);
    }
    if (path.endsWith("/sessions") && method === "GET") return json({ items: created ? [baseSession, newSession()] : [baseSession] });
    if (path.endsWith("/sessions/session-3/agent-config")) {
      if (method === "PUT") {
        updatedBody = route.request().postDataJSON() as Record<string, unknown>;
        config = { tools, subagents: (updatedBody.subagents as typeof preset[]), config_version: 2 };
        return json(newSession());
      }
      return json(config);
    }
    if (path.endsWith("/sessions/session-3/subagents/child-1/messages")) return json({ items: [
      { id: "child-message-1", run_id: "child-run-1", role: "user", content: "检查手册", sequence: 1, status: "completed", created_at: "2026-01-01T00:00:00Z", tool_call_id: null, tool_name: null, arguments: null, result: null, is_error: null, payload_truncated: false },
      { id: "child-message-2", run_id: "child-run-1", role: "assistant", content: "已检查手册", sequence: 2, status: "completed", created_at: "2026-01-01T00:00:10Z", tool_call_id: null, tool_name: null, arguments: null, result: null, is_error: null, payload_truncated: false },
    ] });
    if (path.endsWith("/sessions/session-3/subagents/child-1")) return json(child());
    if (path.endsWith("/sessions/session-3/subagents")) return json({ items: [child()] });
    if (path.endsWith("/sessions/session-3/messages")) return json({ items: [
      { id: "parent-tool-1", run_id: "run-3", role: "tool_call", content: "", sequence: 1, status: "completed", created_at: "2026-01-01T00:00:00Z", tool_call_id: "call-3", tool_name: "call_subagents", arguments: { tasks: [{ name: "generalist", task: "检查手册" }] }, result: null, is_error: null, payload_truncated: false },
    ] });
    if (path.endsWith("/sessions/session-3")) return json(newSession());
    return route.fallback();
  });
  await signIn(page);
  await page.getByTitle("新聊天").click();
  const dialog = page.getByRole("dialog", { name: "新聊天" });
  await dialog.getByRole("checkbox", { name: "产品文档" }).check();
  await dialog.getByRole("checkbox", { name: "启用 call_subagents 工具" }).check();
  await dialog.getByRole("button", { name: "创建聊天" }).click();
  await expect(page.getByRole("heading", { name: "Subagent 会话" })).toBeVisible();
  expect(createdBody).toEqual({ workspace_id: "workspace-1", knowledge_base_ids: ["kb-1"], tools, subagents: [preset] });
  await page.getByRole("button", { name: "Subagent", exact: true }).click();
  await expect(page.getByRole("button", { name: /#1 generalist.*排队中.*检查手册/ })).toBeVisible();
  childStatus = "completed";
  await expect(page.getByRole("button", { name: /#1 generalist.*已完成.*检查手册/ })).toBeVisible({ timeout: 7_000 });
  await page.getByRole("button", { name: "定义设置" }).click();
  await page.getByRole("textbox", { name: "Subagent 1 描述" }).fill("审阅文件与知识库");
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect(page.getByText(/配置已保存/)).toBeVisible();
  expect(updatedBody).toEqual({ tools, subagents: [{ ...preset, description: "审阅文件与知识库" }], expected_config_version: 1 });
  await page.getByRole("button", { name: "任务轨迹" }).click();
  await page.getByRole("button", { name: /#1 generalist.*已完成.*检查手册/ }).click();
  const trace = page.getByLabel("Subagent 只读轨迹");
  await expect(trace.getByText("已检查手册")).toBeVisible();
  await expect(trace.getByRole("textbox")).toHaveCount(0);
  await page.locator("details").filter({ has: page.locator("summary", { hasText: "call_subagents" }) }).first().locator("summary").click();
  await page.getByRole("button", { name: "查看 Subagent 轨迹" }).click();
  await expect(trace.getByText("已检查手册")).toBeVisible();
  await page.getByRole("button", { name: "测试会话", exact: true }).click();
  await expect(page.getByRole("button", { name: "Subagent", exact: true })).toHaveCount(0);
});
