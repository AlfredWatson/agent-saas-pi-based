import { ApiError } from "./errors";
import type {
  AgentProfile, AgentSession, AuthToken, AvailableModel, ChatMessage, GraphArtifact, GraphDetail,
  DerivedEntry, DerivedEntryPage, KnowledgeBase, ProcessingJob, Provider, ProviderBinding, ProviderModel, RagOperation,
  RagCapabilities, RagDocument, RagModel, RetrievalResult, RuntimeState, User, Workspace,
} from "./types";

const apiBase = (import.meta.env.VITE_API_BASE ?? "/api/v1").replace(/\/$/, "");
const tokenKey = "pi-saas.access-token";

export function accessToken(): string | null { return sessionStorage.getItem(tokenKey); }
export function storeToken(token: string): void { sessionStorage.setItem(tokenKey, token); }
export function clearToken(): void { sessionStorage.removeItem(tokenKey); }

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = accessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");
  const response = await fetch(`${apiBase}${path}`, { ...init, headers });
  if (response.status === 401) {
    clearToken();
    window.dispatchEvent(new Event("pi-saas:unauthorized"));
  }
  if (!response.ok) {
    let details: unknown;
    try { details = await response.json(); } catch { details = await response.text(); }
    const code = typeof details === "object" && details !== null && "detail" in details && typeof (details as { detail: unknown }).detail === "string"
      ? (details as { detail: string }).detail : "request_failed";
    throw new ApiError(response.status, code, details);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

async function download(path: string): Promise<Blob> {
  const headers = new Headers({ Accept: "application/octet-stream" });
  const token = accessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${apiBase}${path}`, { headers });
  if (response.status === 401) {
    clearToken();
    window.dispatchEvent(new Event("pi-saas:unauthorized"));
  }
  if (!response.ok) {
    let details: unknown;
    try { details = await response.json(); } catch { details = await response.text(); }
    const code = typeof details === "object" && details !== null && "detail" in details && typeof (details as { detail: unknown }).detail === "string"
      ? (details as { detail: string }).detail : "request_failed";
    throw new ApiError(response.status, code, details);
  }
  return response.blob();
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const putJson = (body: unknown): RequestInit => ({ method: "PUT", body: JSON.stringify(body) });

export const api = {
  register: (email: string, password: string) => request<AuthToken>("/auth/register", json({ email, password })),
  login: (email: string, password: string) => request<AuthToken>("/auth/login", json({ email, password })),
  me: () => request<User>("/auth/me"),
  workspaces: () => request<{ items: Workspace[] }>("/workspaces"),
  createWorkspace: (name: string) => request<Workspace>("/workspaces", json({ name })),
  switchWorkspace: (id: string) => request<Workspace>(`/workspaces/${id}:switch`, { method: "POST" }),
  deleteWorkspace: (id: string) => request<void>(`/workspaces/${id}`, { method: "DELETE" }),
  workspaceFiles: (id: string) => request<{ items: Array<{ path: string; size_bytes: number; modified_at: string }> }>(`/workspaces/${id}/files`),
  uploadWorkspaceFile: (id: string, path: string, file: File, overwrite: boolean) => {
    const form = new FormData(); form.set("path", path); form.set("file", file); form.set("overwrite", String(overwrite));
    return request(`/workspaces/${id}/files`, { method: "POST", body: form });
  },
  deleteWorkspaceFile: (id: string, path: string) => request<void>(`/workspaces/${id}/files?path=${encodeURIComponent(path)}`, { method: "DELETE" }),
  downloadWorkspaceFile: (id: string, path: string) => download(`/workspaces/${id}/files/content?path=${encodeURIComponent(path)}`),
  providers: () => request<{ providers: Provider[] }>("/providers"),
  workspaceBindings: (workspaceId: string) => request<{ items: ProviderBinding[] }>(`/workspaces/${workspaceId}/provider-bindings`),
  createWorkspaceBinding: (workspaceId: string, body: { provider_id: string; display_name: string; api_key: string }) => request<ProviderBinding>(`/workspaces/${workspaceId}/provider-bindings`, json(body)),
  deleteWorkspaceBinding: (workspaceId: string, id: string) => request<void>(`/workspaces/${workspaceId}/provider-bindings/${id}`, { method: "DELETE" }),
  availableModels: (workspaceId: string) => request<{ items: AvailableModel[] }>(`/workspaces/${workspaceId}/available-models`),
  bindings: () => request<{ items: ProviderBinding[] }>("/provider-bindings"),
  createBinding: (body: { provider_id: string; display_name: string; api_key: string }) => request<ProviderBinding>("/provider-bindings", json(body)),
  deleteBinding: (id: string) => request<void>(`/provider-bindings/${id}`, { method: "DELETE" }),
  bindingModels: (id: string) => request<{ models: ProviderModel[] }>(`/provider-bindings/${id}/models`),
  profiles: () => request<{ items: AgentProfile[] }>("/agent-profiles"),
  createProfile: (body: { name: string; provider_binding_id: string; model_id: string; thinking_level?: string | null }) => request<AgentProfile>("/agent-profiles", json(body)),
  updateProfile: (id: string, body: { name: string; provider_binding_id: string; model_id: string; thinking_level?: string | null }) => request<AgentProfile>(`/agent-profiles/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteProfile: (id: string) => request<void>(`/agent-profiles/${id}`, { method: "DELETE" }),
  runtime: () => request<RuntimeState>("/runtime"),
  startRuntime: () => request<RuntimeState>("/runtime:start", { method: "POST" }),
  recreateRuntime: () => request<RuntimeState>("/runtime:recreate", { method: "POST" }),
  sessions: () => request<{ items: AgentSession[] }>("/sessions"),
  session: (id: string) => request<AgentSession>(`/sessions/${id}`),
  createSession: (body: { profile_id?: string; workspace_id: string; knowledge_base_ids: string[] }) => request<AgentSession>("/sessions", json(body)),
  setSessionModelConfig: (id: string, body: { provider_binding_id: string; model_id: string; thinking_level: string | null }) => request<AgentSession>(`/sessions/${id}/model-config`, putJson(body)),
  updateSessionTitle: (id: string, title: string) => request<AgentSession>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deleteSession: (id: string) => request<void>(`/sessions/${id}`, { method: "DELETE" }),
  messages: (id: string) => request<{ items: ChatMessage[] }>(`/sessions/${id}/messages`),
  controlSession: (id: string, action: "abort" | "steer" | "follow-up", content?: string) => request(`/sessions/${id}/${action}`, { method: "POST", body: content ? JSON.stringify({ content }) : undefined }),
  ragCapabilities: () => request<RagCapabilities>("/rag/capabilities"),
  knowledgeBases: (workspaceId: string) => request<{ items: KnowledgeBase[] }>(`/workspaces/${workspaceId}/knowledge-bases`),
  knowledgeBase: (workspaceId: string, id: string) => request<KnowledgeBase>(`/workspaces/${workspaceId}/knowledge-bases/${id}`),
  createKnowledgeBase: (workspaceId: string, body: Record<string, string>) => request<KnowledgeBase>(`/workspaces/${workspaceId}/knowledge-bases`, json(body)),
  updateKnowledgeBase: (workspaceId: string, id: string, body: Record<string, unknown>) => request<KnowledgeBase>(`/workspaces/${workspaceId}/knowledge-bases/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteKnowledgeBase: (workspaceId: string, id: string) => request<{ operation_id: string; status: string }>(`/workspaces/${workspaceId}/knowledge-bases/${id}`, { method: "DELETE" }),
  copyKnowledgeBase: (workspaceId: string, id: string, name: string) => request<{ operation_id: string; target_knowledge_base_id: string; status: string }>(`/workspaces/${workspaceId}/knowledge-bases/${id}/copy`, json({ name })),
  ragOperation: (id: string) => request<RagOperation>(`/rag/operations/${id}`),
  documents: (workspaceId: string, kbId: string) => request<{ items: RagDocument[] }>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/documents`),
  documentEntries: (workspaceId: string, kbId: string, documentId: string, kind: "blocks" | "chunks", limit: number, offset: number) => request<DerivedEntryPage>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/documents/${documentId}/${kind}?limit=${limit}&offset=${offset}`),
  updateDocumentEntry: (workspaceId: string, kbId: string, documentId: string, kind: "blocks" | "chunks", entryId: string, text: string) => request<DerivedEntry>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/documents/${documentId}/${kind}/${entryId}`, { method: "PATCH", body: JSON.stringify({ text }) }),
  uploadDocuments: (workspaceId: string, kbId: string, files: File[]) => {
    const form = new FormData(); files.forEach((file) => form.append("files", file));
    return request<{ items: Array<{ status: string; document?: RagDocument; error?: string }> }>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/documents`, { method: "POST", body: form });
  },
  updateChunking: (workspaceId: string, kbId: string, documentId: string, strategy: string, config: Record<string, unknown>) => request(`/${["workspaces", workspaceId, "knowledge-bases", kbId, "documents", documentId, "chunking-config"].join("/")}`, putJson({ strategy, config })),
  deleteDerivedDocumentData: (workspaceId: string, kbId: string, documentId: string, kind: "blocks" | "chunks" | "vectors" | "graph") => request<void>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/documents/${documentId}/${kind}`, { method: "DELETE" }),
  deleteDocuments: (workspaceId: string, kbId: string, documentIds: string[]) => request<{ items: Array<{ document_id: string; status: string; error_code?: string }> }>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/documents:delete`, json({ document_ids: documentIds })),
  submitStage: (workspaceId: string, kbId: string, kind: "parsing" | "chunking" | "vectorization" | "graph-extraction", documentIds: string[]) => {
    const body = kind === "parsing" ? { items: documentIds.map((document_id) => ({ document_id })) } : { document_ids: documentIds };
    return request<ProcessingJob>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/jobs/${kind}`, json(body));
  },
  submitStageBatch: (workspaceId: string, kbId: string, kind: "parsing" | "chunking" | "vectorization" | "graph-extraction", documentIds: string[]) => request<{ items: Array<{ document_id: string; status: string; error_code?: string }> }>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/jobs/${kind}:batch`, json({ document_ids: documentIds })),
  jobs: (workspaceId: string, kbId: string) => request<{ items: ProcessingJob[] }>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/jobs`),
  ragModels: (workspaceId: string, kbId: string) => request<{ items: RagModel[] }>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/models`),
  setRagModel: (workspaceId: string, kbId: string, kind: "embedding" | "llm" | "reranker", body: Record<string, string | null>) => request<RagModel>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/${kind}-model`, putJson(body)),
  deleteReranker: (workspaceId: string, kbId: string) => request<void>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/reranker-model`, { method: "DELETE" }),
  retrieve: (workspaceId: string, kbId: string, body: Record<string, unknown>) => request<RetrievalResult>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/retrieve`, json(body)),
  graphs: (workspaceId: string, kbId: string) => request<{ items: GraphArtifact[] }>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/graphs`),
  graph: (workspaceId: string, kbId: string, graphId: string) => request<GraphDetail>(`/workspaces/${workspaceId}/knowledge-bases/${kbId}/graphs/${graphId}`),
};

export { apiBase };
