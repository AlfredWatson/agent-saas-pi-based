import type { SubagentDefinition } from "./types";

export const DEFAULT_AGENT_TOOLS = ["read", "bash", "edit", "write"];
export const SUBAGENT_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls", "rag_search"];

export function initialSubagentConfig(hasKnowledgeBase: boolean): { tools: string[]; subagents: SubagentDefinition[] } {
  const tools = [...DEFAULT_AGENT_TOOLS, ...(hasKnowledgeBase ? ["rag_search"] : [])];
  return {
    tools: [...tools, "call_subagents"],
    subagents: [{
      name: "generalist",
      description: "处理主 Agent 委派的独立任务",
      system_prompt: "你是主 Agent 委派的通用子代理。仅完成收到的任务，必要时使用可用工具，并清楚报告结果及依据。",
      tools,
    }],
  };
}

export function validateSubagents(definitions: SubagentDefinition[], hasKnowledgeBase: boolean): string | null {
  if (definitions.length < 1 || definitions.length > 16) return "请配置 1–16 个 Subagent。";
  const names = new Set<string>();
  for (const item of definitions) {
    const name = item.name.trim();
    if (!name || name.length > 64 || /\s/.test(name)) return "名称必须为 1–64 个不含空格的字符。";
    if (names.has(name)) return "Subagent 名称不能重复。";
    names.add(name);
    if (!item.description.trim() || item.description.trim().length > 500) return "描述必须为 1–500 个字符。";
    if (!item.system_prompt.trim() || item.system_prompt.trim().length > 20_000) return "System prompt 必须为 1–20,000 个字符。";
    if (item.tools.includes("call_subagents") || new Set(item.tools).size !== item.tools.length || item.tools.some((tool) => !SUBAGENT_TOOLS.includes(tool))) return "Subagent 工具选择无效。";
    if (!hasKnowledgeBase && item.tools.includes("rag_search")) return "使用 rag_search 前需要绑定知识库。";
  }
  return null;
}
