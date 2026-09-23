// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { RetrievalLab } from "./Workbench";
import { api } from "./lib/api";
import type { KnowledgeBase, Workspace } from "./lib/types";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

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
