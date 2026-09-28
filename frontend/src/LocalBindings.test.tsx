// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Bindings, ChatCenter } from "./Workbench";
import { api } from "./lib/api";
import { ApiError } from "./lib/errors";
import type { AgentSession, ProviderBinding, ProviderModel, Workspace } from "./lib/types";

afterEach(() => { cleanup(); vi.restoreAllMocks(); Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView"); });

const workspace = { id: "workspace-1", name: "测试工作区" } as Workspace;
const binding = (id: string, providerId: string): ProviderBinding => ({ id, workspace_id: workspace.id, provider_id: providerId, display_name: id, status: "active", base_url: `http://10.0.0.${id === "one" ? 1 : 2}:8000/v1` });
const model = (id: string, status: "pending" | "ready" | "unavailable", values?: Partial<ProviderModel>): ProviderModel => ({ id, name: id, provider_id: "vllm", thinking_levels: [], status, context_window: null, max_tokens: null, reasoning: null, ...values });

function renderBindings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Bindings workspace={workspace} /></QueryClientProvider>);
  return client;
}

test.each(["vllm", "sglang"])("creates local %s with an empty key and opens discovered models", async (providerId) => {
  const created = binding("one", providerId);
  let items: ProviderBinding[] = [];
  vi.spyOn(api, "providers").mockResolvedValue({ providers: [{ id: "faux", name: "Faux" }] });
  vi.spyOn(api, "workspaceBindings").mockImplementation(async () => ({ items }));
  vi.spyOn(api, "workspaceBindingModels").mockResolvedValue({ models: [model("local-1", "pending")] });
  const create = vi.spyOn(api, "createWorkspaceBinding").mockImplementation(async () => { items = [created]; return created; });
  renderBindings();
  fireEvent.click(screen.getByRole("button", { name: "添加 Binding" }));
  fireEvent.change(await screen.findByRole("combobox", { name: "Provider" }), { target: { value: providerId } });
  fireEvent.change(screen.getByRole("textbox", { name: "显示名称" }), { target: { value: "本地推理" } });
  fireEvent.change(screen.getByRole("textbox", { name: /^服务地址/ }), { target: { value: "http://10.0.0.1:8000" } });
  expect((screen.getByLabelText("API Key（可选）") as HTMLInputElement).required).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(create).toHaveBeenCalledWith(workspace.id, { provider_id: providerId, display_name: "本地推理", api_key: "", base_url: "http://10.0.0.1:8000" }));
  expect(await screen.findByText("local-1", { selector: "legend" })).toBeTruthy();
  expect(screen.getByText("待配置")).toBeTruthy();
});

test.each(["http://127.0.0.1:30018/v1", "http://localhost:30018/v1", "http://[::1]:30018/v1"])("explains why loopback URL %s cannot be used", async (url) => {
  vi.spyOn(api, "providers").mockResolvedValue({ providers: [] });
  vi.spyOn(api, "workspaceBindings").mockResolvedValue({ items: [] });
  const create = vi.spyOn(api, "createWorkspaceBinding");
  renderBindings();
  fireEvent.click(screen.getByRole("button", { name: "添加 Binding" }));
  fireEvent.change(screen.getByRole("combobox", { name: "Provider" }), { target: { value: "vllm" } });
  fireEvent.change(screen.getByRole("textbox", { name: "显示名称" }), { target: { value: "本地推理" } });
  fireEvent.change(screen.getByRole("textbox", { name: "服务地址" }), { target: { value: url } });
  expect(screen.getByRole("alert").textContent).toContain("Runtime 容器自身");
  expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
  expect(create).not.toHaveBeenCalled();
});

test("built-in provider still requires a key and sends no base URL", async () => {
  vi.spyOn(api, "providers").mockResolvedValue({ providers: [{ id: "faux", name: "Faux" }] });
  vi.spyOn(api, "workspaceBindings").mockResolvedValue({ items: [] });
  const create = vi.spyOn(api, "createWorkspaceBinding").mockResolvedValue(binding("one", "faux"));
  renderBindings();
  fireEvent.click(screen.getByRole("button", { name: "添加 Binding" }));
  await screen.findByRole("option", { name: "Faux" });
  fireEvent.change(screen.getByRole("combobox", { name: "Provider" }), { target: { value: "faux" } });
  fireEvent.change(screen.getByRole("textbox", { name: "显示名称" }), { target: { value: "内置模型" } });
  expect(screen.queryByRole("textbox", { name: /^服务地址/ })).toBeNull();
  expect((screen.getByLabelText("API Key") as HTMLInputElement).required).toBe(true);
  expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "test-key" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(create).toHaveBeenCalledWith(workspace.id, { provider_id: "faux", display_name: "内置模型", api_key: "test-key" }));
});

test("configures each model, isolates bindings, and keeps the old directory after refresh failure", async () => {
  const first = binding("one", "vllm"); const second = binding("two", "sglang");
  const catalogs: Record<string, ProviderModel[]> = {
    one: [model("first-model", "pending")],
    two: [model("second-model", "ready", { provider_id: "sglang", context_window: 8192, max_tokens: 1024, reasoning: false })],
  };
  vi.spyOn(api, "workspaceBindings").mockResolvedValue({ items: [first, second] });
  vi.spyOn(api, "providers").mockResolvedValue({ providers: [] });
  const readModels = vi.spyOn(api, "workspaceBindingModels").mockImplementation(async (_workspace, id) => ({ models: catalogs[id] }));
  const available = vi.spyOn(api, "availableModels").mockResolvedValue({ items: [] });
  const configure = vi.spyOn(api, "configureWorkspaceBindingModel").mockImplementation(async (_workspace, id, body) => {
    const ready = model(body.model_id, "ready", { context_window: body.context_window, max_tokens: body.max_tokens, reasoning: body.reasoning });
    catalogs[id] = [ready]; return ready;
  });
  const refresh = vi.spyOn(api, "refreshWorkspaceBindingModels")
    .mockImplementationOnce(async () => {
      catalogs.one = [catalogs.one[0], model("new-model", "pending"), model("old-model", "unavailable", { context_window: 4096, max_tokens: 512, reasoning: false })];
      return { models: catalogs.one };
    })
    .mockRejectedValueOnce(new ApiError(503, "model_service_unavailable"));
  const client = renderBindings();
  fireEvent.click(await screen.findAllByRole("button", { name: "配置模型" }).then((items) => items[0]));
  expect(await screen.findByText("first-model", { selector: "legend" })).toBeTruthy();
  expect(screen.queryByText("second-model", { selector: "legend" })).toBeNull();
  fireEvent.change(screen.getByRole("spinbutton", { name: "上下文窗口" }), { target: { value: "8192" } });
  fireEvent.change(screen.getByRole("spinbutton", { name: "最大输出 token" }), { target: { value: "8192" } });
  expect((screen.getByRole("button", { name: "保存模型参数" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByRole("spinbutton", { name: "最大输出 token" }), { target: { value: "1024" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "识别思考输出" }));
  fireEvent.click(screen.getByRole("button", { name: "保存模型参数" }));
  await waitFor(() => expect(configure).toHaveBeenCalledWith(workspace.id, "one", { model_id: "first-model", context_window: 8192, max_tokens: 1024, reasoning: true }));
  await waitFor(() => expect(screen.getByText("已配置")).toBeTruthy());
  expect(client.getQueryState(["available-models", workspace.id])?.isInvalidated ?? true).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "刷新模型" }));
  expect(await screen.findByText("new-model", { selector: "legend" })).toBeTruthy();
  expect(screen.getByText("old-model", { selector: "legend" })).toBeTruthy();
  const unavailable = screen.getByText("old-model", { selector: "legend" }).closest("fieldset")!;
  expect(within(unavailable).getByText("不可用")).toBeTruthy();
  expect(within(unavailable).queryByRole("button", { name: "保存模型参数" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "刷新模型" }));
  expect(await screen.findByText("Runtime 无法连接模型服务；请检查服务是否监听容器可达地址及运行状态。")).toBeTruthy();
  expect(screen.getByText("new-model", { selector: "legend" })).toBeTruthy();
  expect(available).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "收起模型" }));
  fireEvent.click(screen.getAllByRole("button", { name: "配置模型" })[1]);
  expect(await screen.findByText("second-model", { selector: "legend" })).toBeTruthy();
  expect(screen.queryByText("first-model", { selector: "legend" })).toBeNull();
  expect(readModels).toHaveBeenCalledWith(workspace.id, "two");
  expect(refresh).toHaveBeenCalledTimes(2);
});

test("chat lists only ready models and local models have no thinking intensity", async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  const session: AgentSession = { id: "session-1", status: "active", workspace_id: workspace.id, profile_id: null, provider_binding_id: "one", model_id: "ready-local", thinking_level: null, model_configured: true, knowledge_base_ids: [], title: "测试会话", total_tokens: 0, context_tokens: 0, latest_run: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" };
  vi.spyOn(api, "session").mockResolvedValue(session);
  vi.spyOn(api, "messages").mockResolvedValue({ items: [] });
  vi.spyOn(api, "availableModels").mockResolvedValue({ items: [
    { id: "ready-local", provider_id: "vllm", name: "可用模型", thinking_levels: ["high"], status: "ready", provider_binding_id: "one", binding_name: "本地服务" },
    { id: "pending-local", provider_id: "vllm", name: "待配置模型", thinking_levels: [], status: "pending", provider_binding_id: "one", binding_name: "本地服务" },
    { id: "offline-local", provider_id: "vllm", name: "下线模型", thinking_levels: [], status: "unavailable", provider_binding_id: "one", binding_name: "本地服务" },
  ] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ChatCenter workspace={workspace} selected={session} onOpenBindings={() => {}} /></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "选择模型" }));
  expect(screen.getByRole("button", { name: /可用模型/ })).toBeTruthy();
  expect(screen.queryByText("待配置模型")).toBeNull();
  expect(screen.queryByText("下线模型")).toBeNull();
  expect(screen.queryByText("思考强度")).toBeNull();
});
