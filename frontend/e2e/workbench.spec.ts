import { expect, test } from "@playwright/test";

const workspace = { id: "workspace-1", name: "default", status: "active", is_current: true };
const knowledgeBase = { id: "kb-1", workspace_id: "workspace-1", name: "产品文档", status: "active", version: 1, file_backend: "local", block_backend: "postgres", chunk_backend: "postgres", vector_backend: "pgvector", graph_backend: "postgres", concurrency: { parsing: 1, chunking: 1, embedding: 1, graph: 1 } };
const baseSession = { id: "session-1", status: "active", title: "测试会话", knowledge_base_ids: [], workspace_id: "workspace-1", profile_id: null, provider_binding_id: "binding-1", model_id: "model-1", thinking_level: "medium", model_configured: true, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", latest_run: null };
const document = { id: "document-uuid-1", original_filename: "用户手册.pdf", stored_filename: "safe-user-manual.pdf", status: "active", stages: { parsing: { status: "not_started", progress: 0 }, chunking: { status: "not_started", progress: 0 }, vectorization: { status: "not_started", progress: 0 }, graph: { status: "not_started", progress: 0 } } };

async function mockApi(page: import("@playwright/test").Page) {
  let sent = false; let fileRequests = 0; let bindingVisible = true;
  let session = { ...baseSession };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname; const method = route.request().method(); const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/login")) return json({ access_token: "browser-test-token", token_type: "bearer" });
    if (path.endsWith("/auth/me")) return json({ id: "user-1", email: "browser@example.com", status: "active" });
    if (path.endsWith("/sessions/session-1/messages:stream")) { sent = true; return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: message.accepted\ndata: {}\n\nevent: assistant.delta\ndata: {\"delta\":\"流式回复\"}\n\nevent: message.completed\ndata: {}\n\nevent: done\ndata: {}\n\n" }); }
    if (path.endsWith("/sessions/session-1/messages")) return json({ items: sent ? [{ id: "message-user", run_id: "run-1", role: "user", content: "你好", sequence: 1, status: "completed", created_at: "2026-01-01T00:00:00Z", tool_call_id: null, tool_name: null, arguments: null, result: null, is_error: null, payload_truncated: false }, { id: "message-agent", run_id: "run-1", role: "assistant", content: "流式回复", sequence: 2, status: "completed", created_at: "2026-01-01T00:00:01Z", tool_call_id: null, tool_name: null, arguments: null, result: null, is_error: null, payload_truncated: false }] : [] });
    if (path.endsWith("/sessions/session-1/model-config") && method === "PUT") { const body = route.request().postDataJSON() as { provider_binding_id: string; model_id: string; thinking_level: string | null }; session = { ...session, ...body, model_configured: true }; return json(session); }
    if (path.endsWith("/sessions/session-1")) return json(session);
    if (path.endsWith("/sessions")) return json({ items: [session] });
    if (path.endsWith("/workspaces/workspace-1/available-models")) return json({ items: [{ id: "model-1", provider_id: "openai", name: "GPT Test", thinking_levels: ["low", "medium", "high"], provider_binding_id: "binding-1", binding_name: "测试 Binding" }, { id: "model-2", provider_id: "openai", name: "GPT Alternate", thinking_levels: [], provider_binding_id: "binding-2", binding_name: "备用 Binding" }] });
    if (path.endsWith("/workspaces/workspace-1/knowledge-bases/kb-1/documents")) return json({ items: [document] });
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
  return { fileRequests: () => fileRequests };
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
  await expect(page.getByText("测试 Binding", { exact: true })).toBeVisible();
  await page.getByText("测试会话", { exact: true }).click();
  await page.getByLabel("选择模型").click();
  await page.getByText("GPT Alternate", { exact: true }).click();
  await page.getByRole("button", { name: "Provider Bindings", exact: true }).click();
  await page.getByTitle("删除 测试 Binding").click();
  await expect(page.getByText("测试 Binding", { exact: true })).toHaveCount(0);
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
