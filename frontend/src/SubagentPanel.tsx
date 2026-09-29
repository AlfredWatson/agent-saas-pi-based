import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ErrorNotice } from "./components";
import { api } from "./lib/api";
import { ApiError } from "./lib/errors";
import { SUBAGENT_TOOLS, validateSubagents } from "./lib/subagents";
import type { AgentSession, ChatMessage, SubagentDefinition, SubagentSession } from "./lib/types";

const PAGE_SIZE = 50;
const ACTIVE_STATUSES = new Set(["queued", "running"]);
const statusLabels: Record<string, string> = {
  queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消",
};

export type SubagentFocus = { toolCallId: string; nonce: number } | null;

function copyDefinitions(items: SubagentDefinition[]): SubagentDefinition[] {
  return items.map((item) => ({ ...item, tools: [...item.tools] }));
}

function groupSessions(items: SubagentSession[]): Array<{ key: string; runId: string; toolCallId: string; items: SubagentSession[] }> {
  const groups = new Map<string, { key: string; runId: string; toolCallId: string; items: SubagentSession[] }>();
  for (const item of items) {
    const key = `${item.parent_run_id}:${item.parent_tool_call_id}`;
    if (!groups.has(key)) groups.set(key, { key, runId: item.parent_run_id, toolCallId: item.parent_tool_call_id, items: [] });
    groups.get(key)!.items.push(item);
  }
  return [...groups.values()].map((group) => ({ ...group, items: group.items.sort((a, b) => a.task_index - b.task_index) }));
}

export function SubagentPanel({ session, focus }: { session: AgentSession; focus: SubagentFocus }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<SubagentDefinition[] | null>(null);
  const [baseline, setBaseline] = useState<SubagentDefinition[] | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [selectedChildId, setSelectedChildId] = useState<string | null>(null);
  const [view, setView] = useState<"trace" | "settings">("trace");
  const [notice, setNotice] = useState("");
  const [validation, setValidation] = useState<string | null>(null);
  const handledFocus = useRef<number | null>(null);
  const config = useQuery({ queryKey: ["agent-config", session.id], queryFn: () => api.agentConfig(session.id) });
  const parent = useQuery({
    queryKey: ["session", session.id], queryFn: () => api.session(session.id),
    refetchInterval: (query) => query.state.data?.latest_run?.status === "running" ? 3_000 : 30_000,
  });
  const busy = parent.data?.latest_run?.status === "running" || (!parent.data && session.latest_run?.status === "running");

  useEffect(() => {
    if (config.data && version === null) {
      setDraft(copyDefinitions(config.data.subagents));
      setBaseline(copyDefinitions(config.data.subagents));
      setVersion(config.data.config_version);
    }
  }, [config.data, version]);

  const children = useInfiniteQuery({
    queryKey: ["subagent-sessions", session.id],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.subagentSessions(session.id, PAGE_SIZE, pageParam),
    getNextPageParam: (last, pages) => last.items.length === PAGE_SIZE ? pages.length * PAGE_SIZE : undefined,
    refetchInterval: (query) => {
      const rows = query.state.data?.pages.flatMap((page) => page.items) ?? [];
      return busy || rows.some((item) => ACTIVE_STATUSES.has(item.latest_run?.status ?? "")) ? 2_000 : false;
    },
  });
  const rows = useMemo(() => {
    const unique = new Map<string, SubagentSession>();
    for (const page of children.data?.pages ?? []) for (const item of page.items) unique.set(item.id, item);
    return [...unique.values()];
  }, [children.data]);
  const groups = useMemo(() => groupSessions(rows), [rows]);
  const { hasNextPage, isFetching: childrenFetching, isError: childrenError, fetchNextPage } = children;
  const selectedRow = rows.find((item) => item.id === selectedChildId);
  const child = useQuery({
    queryKey: ["subagent-session", session.id, selectedChildId],
    queryFn: () => api.subagentSession(session.id, selectedChildId!), enabled: Boolean(selectedChildId),
    refetchInterval: ACTIVE_STATUSES.has(selectedRow?.latest_run?.status ?? "") ? 2_000 : false,
  });
  const selected = child.data ? { ...child.data, latest_run: selectedRow?.latest_run ?? child.data.latest_run } : selectedRow;
  const messages = useQuery({
    queryKey: ["subagent-messages", session.id, selectedChildId],
    queryFn: () => api.subagentMessages(session.id, selectedChildId!), enabled: Boolean(selectedChildId),
    refetchInterval: ACTIVE_STATUSES.has(selected?.latest_run?.status ?? "") ? 2_000 : false,
  });

  useEffect(() => {
    if (!focus || handledFocus.current === focus.nonce) return;
    const match = rows.find((item) => item.parent_tool_call_id === focus.toolCallId);
    if (match) { setSelectedChildId(match.id); setView("trace"); handledFocus.current = focus.nonce; }
    else if (hasNextPage && !childrenFetching && !childrenError) void fetchNextPage();
  }, [focus, rows, hasNextPage, childrenFetching, childrenError, fetchNextPage]);

  const save = useMutation({
    mutationFn: () => api.updateAgentConfig(session.id, {
      tools: config.data!.tools,
      subagents: draft!.map((item) => ({
        name: item.name.trim(), description: item.description.trim(),
        system_prompt: item.system_prompt.trim(), tools: item.tools,
      })),
      expected_config_version: version!,
    }),
    onSuccess: (updated) => {
      const savedDefinitions = draft!.map((item) => ({ ...item, name: item.name.trim(), description: item.description.trim(), system_prompt: item.system_prompt.trim() }));
      setDraft(savedDefinitions);
      setBaseline(copyDefinitions(savedDefinitions));
      setVersion(updated.config_version ?? null);
      qc.setQueryData(["agent-config", session.id], {
        tools: config.data!.tools,
        subagents: savedDefinitions,
        config_version: updated.config_version,
      });
      setNotice("配置已保存；后续调用将使用新定义。");
      void qc.invalidateQueries({ queryKey: ["agent-config", session.id] });
      void qc.invalidateQueries({ queryKey: ["session", session.id] });
      void qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  const reload = async () => {
    const value = await config.refetch();
    if (value.data) {
      setDraft(copyDefinitions(value.data.subagents));
      setBaseline(copyDefinitions(value.data.subagents));
      setVersion(value.data.config_version);
      setValidation(null);
      setNotice("");
      save.reset();
    }
  };
  const update = (index: number, change: Partial<SubagentDefinition>) => {
    setDraft((items) => items?.map((item, position) => position === index ? { ...item, ...change } : item) ?? null);
    setValidation(null); setNotice("");
  };
  const submit = () => {
    if (!draft || version === null || !config.data) return;
    const issue = validateSubagents(draft, session.knowledge_base_ids.length > 0);
    setValidation(issue);
    if (!issue) save.mutate();
  };
  const isDirty = draft !== null && baseline !== null && JSON.stringify(draft) !== JSON.stringify(baseline);

  return <div className="mt-4 space-y-6 text-sm">
    <div className="flex gap-2 border-b border-slate-800 pb-2" role="group" aria-label="Subagent 栏内容">
      <button type="button" className={`btn flex-1 ${view === "trace" ? "border-sky-500 text-sky-200" : ""}`} aria-pressed={view === "trace"} onClick={() => setView("trace")}>任务轨迹</button>
      <button type="button" className={`btn flex-1 ${view === "settings" ? "border-sky-500 text-sky-200" : ""}`} aria-pressed={view === "settings"} onClick={() => setView("settings")}>定义设置</button>
    </div>
    {view === "settings" && <section aria-label="Subagent 设置" className="space-y-3">
      <div className="flex items-center justify-between gap-2"><h3 className="font-medium">Subagent 设置</h3><span className="text-xs muted">{draft?.length ?? 0}/16</span></div>
      {config.isPending && <p className="muted">正在读取配置…</p>}
      <ErrorNotice error={config.error ?? save.error} />
      {save.error instanceof ApiError && save.error.code === "session_config_conflict" && <p className="text-xs text-amber-300">配置已被其他操作更新。请重新加载后再保存。</p>}
      {save.error instanceof ApiError && save.error.code === "session_busy" && <p className="text-xs text-amber-300">任务结束后可重试；当前编辑内容已保留。</p>}
      {validation && <p role="alert" className="text-xs text-rose-300">{validation}</p>}
      {notice && <p role="status" className="text-xs text-emerald-300">{notice}</p>}
      {busy && <p className="text-xs text-amber-300">会话运行中，暂不能保存配置。</p>}
      {draft?.map((item, index) => <div key={index} className="space-y-2 rounded-lg border border-slate-800 bg-slate-900 p-3">
        <div className="flex items-center justify-between"><strong>定义 {index + 1}</strong><button type="button" className="btn p-1" title={`删除 Subagent ${item.name || index + 1}`} onClick={() => { setDraft((items) => items?.filter((_, position) => position !== index) ?? null); setValidation(null); setNotice(""); }}><Trash2 size={14} /></button></div>
        <label className="block text-xs">名称<input className="input mt-1" aria-label={`Subagent ${index + 1} 名称`} maxLength={64} value={item.name} onChange={(event) => update(index, { name: event.target.value })} /></label>
        <label className="block text-xs">描述<textarea className="input mt-1 min-h-16" aria-label={`Subagent ${index + 1} 描述`} maxLength={500} value={item.description} onChange={(event) => update(index, { description: event.target.value })} /></label>
        <label className="block text-xs">System prompt<textarea className="input mt-1 min-h-32" aria-label={`Subagent ${index + 1} System prompt`} maxLength={20_000} value={item.system_prompt} onChange={(event) => update(index, { system_prompt: event.target.value })} /></label>
        <fieldset><legend className="text-xs">工具</legend><div className="mt-1 grid grid-cols-2 gap-1">{SUBAGENT_TOOLS.filter((tool) => tool !== "rag_search" || session.knowledge_base_ids.length > 0).map((tool) => <label key={tool} className="flex items-center gap-2 text-xs"><input type="checkbox" checked={item.tools.includes(tool)} onChange={() => update(index, { tools: item.tools.includes(tool) ? item.tools.filter((value) => value !== tool) : [...item.tools, tool] })} />{tool}</label>)}</div></fieldset>
      </div>)}
      <div className="flex flex-wrap gap-2"><button type="button" className="btn" disabled={!draft || draft.length >= 16} onClick={() => { setDraft((items) => [...(items ?? []), { name: "", description: "", system_prompt: "", tools: [] }]); setValidation(null); }}><Plus size={14} />添加定义</button><button type="button" className="btn" disabled={config.isFetching} onClick={() => void reload()}><RefreshCw size={14} />重新加载</button><button type="button" className="btn btn-primary" disabled={!isDirty || busy || save.isPending} onClick={submit}>保存配置</button></div>
    </section>}

    {view === "trace" && <section aria-label="Subagent 轨迹" className="space-y-3">
      <div className="flex items-center justify-between gap-2"><h3 className="font-medium">Subagent 轨迹</h3><button type="button" className="btn p-1" title="刷新 Subagent 轨迹" onClick={() => { void children.refetch(); if (selectedChildId) { void child.refetch(); void messages.refetch(); } }}><RefreshCw size={14} /></button></div>
      <ErrorNotice error={children.error ?? child.error ?? messages.error} />
      {children.isPending && <p className="muted">正在读取任务…</p>}
      {!children.isPending && rows.length === 0 && <p className="text-xs muted">尚无 Subagent 调用。</p>}
      {groups.map((group) => <div key={group.key} className="space-y-2 rounded-lg border border-slate-800 p-2">
        <div className="break-all text-xs muted">主 Run {group.runId}<br />调用 {group.toolCallId}</div>
        {group.items.map((item) => <button key={item.id} type="button" className={`w-full rounded p-2 text-left text-xs ${selectedChildId === item.id ? "bg-sky-500/15 text-sky-100" : "bg-slate-900 hover:bg-slate-800"}`} onClick={() => setSelectedChildId(item.id)}>
          <span className="flex justify-between gap-2"><strong>#{item.task_index + 1} {item.subagent.name}</strong><span>{statusLabels[item.latest_run?.status ?? ""] ?? item.latest_run?.status ?? "-"}</span></span>
          <span className="mt-1 block whitespace-pre-wrap break-words muted">{item.task}</span>
        </button>)}
      </div>)}
      {children.hasNextPage && <button type="button" className="btn w-full" disabled={children.isFetchingNextPage} onClick={() => void children.fetchNextPage()}>{children.isFetchingNextPage ? "读取中…" : "加载更多任务"}</button>}
      {selected && <div className="space-y-3 rounded-lg border border-sky-900/70 bg-slate-900 p-3" aria-label="Subagent 只读轨迹">
        <div><h4 className="font-medium">{selected.subagent.name} · #{selected.task_index + 1}</h4><p className="mt-1 break-all text-xs muted">子会话 {selected.id}</p><p className="mt-1 text-xs">{statusLabels[selected.latest_run?.status ?? ""] ?? selected.latest_run?.status ?? "-"}{selected.latest_run?.error ? ` · ${selected.latest_run.error}` : ""}</p></div>
        <p className="whitespace-pre-wrap break-words text-xs">任务：{selected.task}</p>
        <details className="text-xs"><summary>调用时的定义</summary><p className="mt-2 whitespace-pre-wrap">{selected.subagent.description}</p><p className="mt-1 muted">工具：{selected.subagent.tools.join(", ") || "无"}</p><pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words">{selected.subagent.system_prompt}</pre></details>
        {messages.isPending && <p className="muted">正在读取消息…</p>}
        {!messages.isPending && !messages.data?.items.length && <p className="text-xs muted">暂无消息。</p>}
        <div className="space-y-2">{messages.data?.items.map((message) => <TraceMessage key={message.id} message={message} />)}</div>
      </div>}
    </section>}
  </div>;
}

function TraceMessage({ message }: { message: ChatMessage }) {
  const title = message.role === "tool_call" ? `调用 ${message.tool_name ?? "工具"}` : message.role === "tool_result" ? `结果 ${message.tool_name ?? "工具"}` : message.role === "assistant" ? "Subagent" : "任务";
  return <article className="rounded bg-slate-950 p-2 text-xs"><div className="flex justify-between gap-2 font-medium"><span>{title}</span>{message.is_error && <span className="text-rose-300">失败</span>}</div>
    {message.content && <div className="markdown mt-2 break-words"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>}
    {message.role === "tool_call" && <pre className="mt-2 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(message.arguments, null, 2)}</pre>}
    {message.role === "tool_result" && <pre className="mt-2 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(message.result, null, 2)}</pre>}
    {message.payload_truncated && <p className="mt-1 muted">内容已截断</p>}
  </article>;
}
