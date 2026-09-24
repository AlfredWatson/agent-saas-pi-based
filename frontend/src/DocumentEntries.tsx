import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ErrorNotice } from "./components";
import { api } from "./lib/api";
import type { DerivedEntry, KnowledgeBase, RagDocument, Workspace } from "./lib/types";

type EntryKind = "blocks" | "chunks";
const PAGE_SIZE = 20;

function editBlockReason(document: RagDocument, kb: KnowledgeBase): string | null {
  if (kb.status !== "active") return "知识库正在处理，暂不能修改。";
  const vector = document.stages?.vectorization?.status ?? "not_started";
  const graph = document.stages?.graph?.status ?? "not_started";
  if ([vector, graph].some((status) => status === "queued" || status === "running")) return "请等待向量化或图谱提取任务结束，再删除相关派生数据。";
  if (vector !== "not_started" || graph !== "not_started") return "请先删除向量和图谱派生数据，再修改条目。";
  if (["parsing", "chunking"].some((kind) => ["queued", "running"].includes(document.stages?.[kind]?.status ?? ""))) return "请等待文档处理任务结束。";
  return null;
}

function EntryCard({ workspace, kb, document, kind, entry }: { workspace: Workspace; kb: KnowledgeBase; document: RagDocument; kind: EntryKind; entry: DerivedEntry }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(entry.text);
  const reason = editBlockReason(document, kb);
  const save = useMutation({
    mutationFn: () => api.updateDocumentEntry(workspace.id, kb.id, document.id, kind, entry.id, draft),
    onSuccess: (updated) => {
      setDraft(updated.text);
      setEditing(false);
      void qc.invalidateQueries({ queryKey: ["document-entries", workspace.id, kb.id, document.id] });
      void qc.invalidateQueries({ queryKey: ["documents", workspace.id, kb.id] });
    },
  });
  return <article className="rounded border border-slate-700 bg-slate-950/70 p-3 text-sm" data-testid={`${kind}-entry`}>
    <div className="flex flex-wrap items-center gap-2 text-xs muted">
      <span>#{entry.ordinal + 1}</span><span className="break-all">ID {entry.id}</span>
      {entry.block_id && <span className="break-all">block {entry.block_id}</span>}
      {entry.token_count !== undefined && <span>{entry.token_count} tokens</span>}
      <span className="flex-1" />
      {!editing && <button className="btn" disabled={Boolean(reason)} title={reason ?? undefined} onClick={() => { setDraft(entry.text); setEditing(true); }}>编辑正文</button>}
    </div>
    {editing ? <div className="mt-2 space-y-2">
      <label className="block">条目正文<textarea className="input mt-1 min-h-32 w-full" value={draft} onChange={(event) => setDraft(event.target.value)} /></label>
      <div className="flex gap-2"><button className="btn btn-primary" disabled={!draft.trim() || Boolean(reason) || save.isPending} onClick={() => save.mutate()}>保存</button><button className="btn" disabled={save.isPending} onClick={() => { setEditing(false); setDraft(entry.text); save.reset(); }}>取消</button></div>
    </div> : <p className="mt-2 whitespace-pre-wrap break-words">{entry.text}</p>}
    {reason && <p className="mt-2 text-xs text-amber-300">{reason}</p>}
    <details className="mt-2 text-xs muted"><summary className="cursor-pointer">更多信息</summary><pre className="mt-2 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify({ metadata: entry.metadata, ...(entry.strategy_snapshot ? { strategy_snapshot: entry.strategy_snapshot } : {}), content_hash: entry.content_hash }, null, 2)}</pre></details>
    <ErrorNotice error={save.error} />
  </article>;
}

export function DocumentEntries({ workspace, kb, document, kind }: { workspace: Workspace; kb: KnowledgeBase; document: RagDocument; kind: EntryKind }) {
  const [offset, setOffset] = useState(0);
  const entries = useQuery({
    queryKey: ["document-entries", workspace.id, kb.id, document.id, kind, offset],
    queryFn: () => api.documentEntries(workspace.id, kb.id, document.id, kind, PAGE_SIZE, offset),
  });
  const total = entries.data?.total ?? 0;
  return <section className="space-y-3 rounded-lg border border-slate-700 bg-slate-900/70 p-4" aria-label={kind === "blocks" ? "解析条目" : "切分条目"}>
    <div className="flex items-center justify-between"><h3 className="font-medium">{kind === "blocks" ? "解析后的 blocks" : "切分后的 chunks"}</h3><span className="text-xs muted">共 {total} 条</span></div>
    {entries.isPending && <p className="muted">正在加载条目…</p>}
    <ErrorNotice error={entries.error} />
    {entries.data?.items.map((entry) => <EntryCard key={entry.id} workspace={workspace} kb={kb} document={document} kind={kind} entry={entry} />)}
    {entries.data && total === 0 && <p className="muted">暂无条目。</p>}
    {total > PAGE_SIZE && <div className="flex items-center justify-end gap-2 text-xs"><button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</button><span>{Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(total / PAGE_SIZE)}</span><button className="btn" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button></div>}
  </section>;
}
