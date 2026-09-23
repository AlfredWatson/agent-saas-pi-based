export type User = { id: string; email: string; status: string };
export type AuthToken = { access_token: string; token_type: string };
export type Workspace = { id: string; name: string; status: string; is_current: boolean };
export type Provider = { id: string; name: string };
export type ProviderBinding = { id: string; workspace_id: string; provider_id: string; display_name: string; status: string };
export type ProviderModel = { id: string; provider_id: string; name: string; thinking_levels: string[] };
export type AvailableModel = ProviderModel & { provider_binding_id: string; binding_name: string };
export type AgentProfile = { id: string; name: string; model_id: string; thinking_level: string | null };
export type LatestRun = { id: string; status: "running" | "completed" | "failed" | string; error: string | null; started_at: string; finished_at: string | null };
export type AgentSession = {
  id: string; status: string; title: string | null; knowledge_base_ids: string[];
  workspace_id: string; profile_id: string | null; provider_binding_id: string | null;
  model_id: string | null; thinking_level: string | null; model_configured: boolean;
  created_at: string; updated_at: string; latest_run: LatestRun | null;
};
export type ChatMessage = {
  id: string; run_id: string | null; role: "user" | "assistant" | "tool_call" | "tool_result";
  content: string; sequence: number; status: string; created_at: string;
  tool_call_id: string | null; tool_name: string | null; arguments: unknown; result: unknown;
  is_error: boolean | null; payload_truncated: boolean;
};
export type RuntimeState = { state: string; image: string | null; last_error: string | null; last_seen_at: string | null };
export type RagCapabilities = {
  file_backends: string[]; block_backends: string[]; chunk_backends: string[];
  vector_backends: string[]; graph_backends: string[]; document_extensions: string[];
  chunking_strategies: Record<string, Record<string, unknown>>;
};
export type KnowledgeBase = {
  id: string; workspace_id: string; name: string; status: string; version: number;
  file_backend: string; block_backend: string; chunk_backend: string; vector_backend: string; graph_backend: string;
  concurrency: { parsing: number; chunking: number; embedding: number; graph: number };
};
export type Stage = { status: string; progress: number; message: string | null; error: string | null; updated_at: string | null };
export type RagDocument = { id: string; filename: string; status: string; stages: Record<string, Stage>; [key: string]: unknown };
export type ProcessingJob = { id: string; kind: string; status: string; message: string | null; error: string | null; document_ids?: string[]; [key: string]: unknown };
export type RagModel = { kind: "embedding" | "llm" | "reranker" | string; protocol: string; base_url: string; model_name: string; thinking_effort: string | null; verified_at: string | null; [key: string]: unknown };
export type GraphNode = { id: string; name: string; entity_type?: string; description?: string };
export type GraphEdge = { id: string; source_node_id: string; target_node_id: string; relation: string; description?: string };
export type GraphArtifact = { id: string; name: string; kind: string; status?: string; [key: string]: unknown };
export type GraphDetail = { id: string; name: string; kind: string; nodes: GraphNode[]; edges: GraphEdge[] };
export type RetrievalItem = { document_id?: string; chunk_id?: string; score?: number; retrieval_score?: number; source?: string; text?: string; [key: string]: unknown };
export type RetrievalResult = { mode: string; items?: RetrievalItem[]; [key: string]: unknown };
export type SseEvent = { name: string; data: Record<string, unknown> };
