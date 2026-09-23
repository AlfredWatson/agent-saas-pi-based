export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, details?: unknown) {
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

const messages: Record<string, string> = {
  invalid_credentials: "邮箱或密码不正确。",
  invalid_token: "登录已过期，请重新登录。",
  email_exists: "该邮箱已注册。",
  runtime_unavailable: "Agent Runtime 当前不可用，请检查运行环境。",
  session_busy: "该会话正在执行任务，请先终止或等待完成。",
  session_delete_incomplete: "会话文件暂时无法清理，数据库内容尚未删除，可稍后重试。",
  invalid_session_title: "会话标题不能为空。",
  invalid_profile_or_workspace: "请选择可用的 Profile 和工作区。",
  invalid_knowledge_base_binding: "知识库必须属于当前工作区且处于可用状态。",
  agent_rag_unavailable: "Runtime 尚未配置到 Gateway 的 RAG 内部地址。",
  knowledge_base_exists: "当前工作区已有同名知识库。",
  vector_backend_not_enabled: "该向量后端未在当前部署中启用。",
  embedding_model_required: "请先配置并验证 embedding 模型。",
  llm_model_required: "请先配置并验证 LLM 模型。",
  document_processing: "文档正在处理，暂不能执行此操作。",
  file_exists: "目标路径已存在；如需替换，请确认覆盖。",
  file_too_large: "文件超过当前工作区的单文件限制。",
  invalid_binding_name: "请填写 Provider Binding 的显示名。",
  provider_binding_in_use: "仍有会话正在使用该 Provider Binding，请先切换这些会话的模型。",
};

export function readableError(error: unknown): string {
  if (error instanceof ApiError) return messages[error.code] ?? "请求未能完成。";
  if (error instanceof Error) return error.message;
  return "发生未知错误。";
}

export function errorCode(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null;
}
