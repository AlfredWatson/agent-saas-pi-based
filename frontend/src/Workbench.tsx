import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Background, Controls, MiniMap, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { ArrowLeft, Bot, ChevronDown, ChevronRight, ChevronUp, Database, Download, FileUp, FlaskConical, FolderOpen, LogOut, MessageSquare, MoreHorizontal, Network, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Paperclip, Plus, RefreshCw, Send, Settings2, SlidersHorizontal, Trash2, Upload } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { ConfirmButton, ErrorNotice, Form, Modal } from "./components";
import { DocumentEntries } from "./DocumentEntries";
import { SubagentPanel, type SubagentFocus } from "./SubagentPanel";
import { api } from "./lib/api";
import { useAuth } from "./lib/auth";
import { streamMessage } from "./lib/sse";
import { initialSubagentConfig } from "./lib/subagents";
import type { AgentSession, AvailableModel, ChatMessage, KnowledgeBase, ProviderBinding, ProviderModel, RagDocument, Workspace } from "./lib/types";

const COLLAPSED_WIDTH = 64;
const stages = [["parsing", "解析"], ["chunking", "切分"], ["vectorization", "向量化"], ["graph-extraction", "图谱提取"]] as const;
const stageKey: Record<string, string> = { parsing: "parsing", chunking: "chunking", vectorization: "vectorization", "graph-extraction": "graph" };
const stageLabels: Record<string, string> = { parsing: "解析", chunking: "切分", vectorization: "向量化", graph: "图谱提取", "graph-extraction": "图谱提取" };

type KnowledgeTab = "models" | "graphs" | "advanced" | "retrieve";
type AgentTab = "files" | "bindings" | "runtime" | "subagents";
type PendingChat = { content: string; response: string; tools: Array<{ name: string; data: Record<string, unknown> }>; compacting: boolean };

function usePanes() {
  const [left, setLeft] = useState(() => Number(localStorage.getItem("pi.left-width")) || 280);
  const [right, setRight] = useState(() => Number(localStorage.getItem("pi.right-width")) || 360);
  const [leftCollapsed, setLeftCollapsed] = useState(() => localStorage.getItem("pi.left-collapsed") === "true");
  const [rightCollapsed, setRightCollapsed] = useState(() => localStorage.getItem("pi.right-collapsed") === "true");
  useEffect(() => localStorage.setItem("pi.left-width", String(left)), [left]);
  useEffect(() => localStorage.setItem("pi.right-width", String(right)), [right]);
  useEffect(() => localStorage.setItem("pi.left-collapsed", String(leftCollapsed)), [leftCollapsed]);
  useEffect(() => localStorage.setItem("pi.right-collapsed", String(rightCollapsed)), [rightCollapsed]);
  const drag = (side: "left" | "right") => (event: React.PointerEvent) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    const start = event.clientX; const initial = side === "left" ? left : right;
    const move = (next: PointerEvent) => {
      const delta = next.clientX - start;
      const value = Math.max(220, Math.min(520, side === "left" ? initial + delta : initial - delta));
      (side === "left" ? setLeft : setRight)(value);
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
  };
  return { left, right, leftCollapsed, rightCollapsed, setLeftCollapsed, setRightCollapsed, drag };
}

function current(workspaces: Workspace[]) { return workspaces.find((item) => item.is_current) ?? workspaces[0]; }
function documentName(document: RagDocument) { return document.original_filename || document.stored_filename || "未命名文件"; }

export function Workbench() {
  const { user, signOut } = useAuth();
  const navigate = useNavigate(); const location = useLocation(); const params = useParams(); const qc = useQueryClient(); const panes = usePanes();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [agentTab, setAgentTab] = useState<AgentTab>(location.pathname === "/settings" ? "bindings" : "files");
  const [knowledgeTab, setKnowledgeTab] = useState<KnowledgeTab>("models");
  const [graphId, setGraphId] = useState<string | null>(null);
  const [workspaceModal, setWorkspaceModal] = useState(false);
  const [knowledgeModal, setKnowledgeModal] = useState(false);
  const [sessionModal, setSessionModal] = useState(false);
  const [subagentFocus, setSubagentFocus] = useState<SubagentFocus>(null);
  const workspacesQuery = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const workspaces = workspacesQuery.data?.items ?? []; const workspace = current(workspaces);
  const sessionsQuery = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });
  const sessions = sessionsQuery.data?.items ?? [];
  const kbsQuery = useQuery({ queryKey: ["knowledge-bases", workspace?.id], queryFn: () => api.knowledgeBases(workspace!.id), enabled: Boolean(workspace) });
  const kbs = kbsQuery.data?.items ?? [];
  const selectedKb = params.knowledgeBaseId ? kbs.find((item) => item.id === params.knowledgeBaseId) : undefined;
  const selectedSession = params.sessionId ? sessions.find((item) => item.id === params.sessionId) : undefined;
  const hasSubagents = Boolean(selectedSession?.tools?.includes("call_subagents"));
  useEffect(() => { if (workspace && expanded === null) setExpanded(workspace.id); }, [workspace, expanded]);
  useEffect(() => { setGraphId(null); }, [selectedKb?.id]);
  useEffect(() => { if (!hasSubagents && agentTab === "subagents") setAgentTab("files"); }, [hasSubagents, agentTab]);
  const switchWorkspace = useMutation({ mutationFn: api.switchWorkspace, onSuccess: () => { void qc.invalidateQueries({ queryKey: ["workspaces"] }); void qc.invalidateQueries({ queryKey: ["sessions"] }); navigate("/chat"); } });
  const openWorkspace = (item: Workspace) => { setExpanded(expanded === item.id ? null : item.id); if (item.id !== workspace?.id) switchWorkspace.mutate(item.id); };
  const chooseKnowledge = () => { setKnowledgeModal(false); setSessionModal(false); navigate("/knowledge"); };
  const chooseAgent = () => { setAgentTab("files"); setKnowledgeModal(false); setSessionModal(false); navigate("/chat"); };
  const startNewChat = () => { setKnowledgeModal(false); setAgentTab("files"); setSessionModal(true); navigate("/chat"); };
  const grid = `${panes.leftCollapsed ? COLLAPSED_WIDTH : panes.left}px 6px minmax(480px,1fr) 6px ${panes.rightCollapsed ? COLLAPSED_WIDTH : panes.right}px`;
  return <main className="min-h-screen bg-slate-950 text-slate-100"><div className="hidden min-h-screen lg:grid" style={{ gridTemplateColumns: grid }}>
    <aside className="flex min-w-0 flex-col overflow-hidden border-r border-slate-800 bg-slate-950">
      {panes.leftCollapsed
        ? <CollapsedDirectory workspace={workspace} kbs={kbs} sessions={sessions} onExpand={() => panes.setLeftCollapsed(false)} onWorkspace={openWorkspace} onKnowledgeHome={() => { panes.setLeftCollapsed(false); chooseKnowledge(); }} onKnowledge={(id) => { panes.setLeftCollapsed(false); navigate(`/knowledge/${id}`); }} onAgent={() => { panes.setLeftCollapsed(false); chooseAgent(); }} onSession={(id) => { panes.setLeftCollapsed(false); navigate(`/chat/${id}`); }} onNewChat={() => { panes.setLeftCollapsed(false); startNewChat(); }} />
        : <ExpandedDirectory userEmail={user?.email} workspaces={workspaces} workspace={workspace} kbs={kbs} sessions={sessions} expanded={expanded} onOpenWorkspace={openWorkspace} onKnowledgeHome={chooseKnowledge} onKnowledge={(id) => navigate(`/knowledge/${id}`)} onCreateKnowledge={() => setKnowledgeModal(true)} onSession={(id) => navigate(`/chat/${id}`)} onAgent={chooseAgent} onNewChat={startNewChat} onCreateWorkspace={() => setWorkspaceModal(true)} onCollapse={() => panes.setLeftCollapsed(true)} onSignOut={signOut} />}
    </aside>
    <div className={`bg-slate-900 ${panes.leftCollapsed ? "" : "cursor-col-resize hover:bg-sky-500/60"}`} onPointerDown={panes.leftCollapsed ? undefined : panes.drag("left")} />
    <section className="min-w-0 overflow-hidden">{selectedKb && workspace
      ? <KnowledgeCenter workspace={workspace} kb={selectedKb} graphId={graphId} onBackFromGraph={() => setGraphId(null)} />
      : location.pathname === "/knowledge" ? <KnowledgeLanding />
      : selectedSession ? <ChatCenter key={selectedSession.id} workspace={workspace} selected={selectedSession} onOpenBindings={() => { setAgentTab("bindings"); panes.setRightCollapsed(false); }} onOpenSubagents={(toolCallId) => { setSubagentFocus((previous) => ({ toolCallId, nonce: (previous?.nonce ?? 0) + 1 })); setAgentTab("subagents"); panes.setRightCollapsed(false); }} />
      : <AgentLanding />}</section>
    <div className={`bg-slate-900 ${panes.rightCollapsed ? "" : "cursor-col-resize hover:bg-sky-500/60"}`} onPointerDown={panes.rightCollapsed ? undefined : panes.drag("right")} />
    <aside className="scrollbar min-w-0 overflow-auto border-l border-slate-800 bg-slate-950">
      {panes.rightCollapsed
        ? <CollapsedInfo knowledge={Boolean(selectedKb)} hasSubagents={hasSubagents} knowledgeTab={knowledgeTab} agentTab={agentTab} onExpand={() => panes.setRightCollapsed(false)} onKnowledgeTab={(tab) => { setKnowledgeTab(tab); panes.setRightCollapsed(false); }} onAgentTab={(tab) => { setAgentTab(tab); panes.setRightCollapsed(false); }} />
        : <><div className="flex h-14 items-center justify-end border-b border-slate-800 px-3"><IconButton title="收起信息栏" onClick={() => panes.setRightCollapsed(true)}><PanelRightClose size={16} /></IconButton></div>{selectedKb && workspace ? <KnowledgeInfo workspace={workspace} kb={selectedKb} tab={knowledgeTab} setTab={setKnowledgeTab} onOpenGraph={setGraphId} /> : <AgentInfo workspace={workspace} session={selectedSession} focus={subagentFocus} tab={agentTab} setTab={setAgentTab} />}</>}
    </aside>
  </div><div className="lg:hidden p-4"><p className="muted">请在桌面宽度打开工作台以使用三栏布局。</p></div>
  <CreateWorkspace open={workspaceModal} onClose={() => setWorkspaceModal(false)} onCreated={(id) => { setWorkspaceModal(false); switchWorkspace.mutate(id); }} />
  {workspace && <CreateKnowledge open={knowledgeModal} workspace={workspace} onClose={() => setKnowledgeModal(false)} onCreated={(id) => { setKnowledgeModal(false); void qc.invalidateQueries({ queryKey: ["knowledge-bases", workspace.id] }); navigate(`/knowledge/${id}`); }} />}
  {workspace && <CreateSession open={sessionModal} workspace={workspace} onClose={() => setSessionModal(false)} onCreated={(id) => { setSessionModal(false); void qc.invalidateQueries({ queryKey: ["sessions"] }); navigate(`/chat/${id}`); }} />}
  </main>;
}

function IconButton({ title, onClick, children }: { title: string; onClick: () => void; children: React.ReactNode }) { return <button className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-slate-300 hover:bg-slate-800 hover:text-sky-200" title={title} aria-label={title} onClick={onClick}>{children}</button>; }
function CollapsedDirectory({ workspace, kbs, sessions, onExpand, onWorkspace, onKnowledgeHome, onKnowledge, onAgent, onSession, onNewChat }: { workspace?: Workspace; kbs: KnowledgeBase[]; sessions: AgentSession[]; onExpand: () => void; onWorkspace: (workspace: Workspace) => void; onKnowledgeHome: () => void; onKnowledge: (id: string) => void; onAgent: () => void; onSession: (id: string) => void; onNewChat: () => void }) {
  return <><div className="flex h-14 items-center justify-center border-b border-slate-800"><IconButton title="展开目录栏" onClick={onExpand}><PanelLeftOpen size={18} /></IconButton></div><nav className="scrollbar flex min-h-0 flex-1 flex-col items-center gap-1 overflow-auto py-2"><IconButton title={workspace ? `${workspace.name} workspace` : "workspace"} onClick={() => workspace && onWorkspace(workspace)}><Database size={18} /></IconButton><IconButton title="知识库" onClick={onKnowledgeHome}><FolderOpen size={18} /></IconButton>{kbs.map((kb) => <IconButton key={kb.id} title={kb.name} onClick={() => onKnowledge(kb.id)}><FolderOpen size={18} /></IconButton>)}<IconButton title="Agent" onClick={onAgent}><Bot size={18} /></IconButton>{sessions.filter((session) => session.workspace_id === workspace?.id).map((session) => <IconButton key={session.id} title={session.title ?? "新聊天"} onClick={() => onSession(session.id)}><MessageSquare size={18} /></IconButton>)}<IconButton title="新聊天" onClick={onNewChat}><Plus size={18} /></IconButton></nav></>;
}
export function ExpandedDirectory({ userEmail, workspaces, workspace, kbs, sessions, expanded, onOpenWorkspace, onKnowledgeHome, onKnowledge, onCreateKnowledge, onSession, onAgent, onNewChat, onCreateWorkspace, onCollapse, onSignOut }: { userEmail?: string; workspaces: Workspace[]; workspace?: Workspace; kbs: KnowledgeBase[]; sessions: AgentSession[]; expanded: string | null; onOpenWorkspace: (workspace: Workspace) => void; onKnowledgeHome: () => void; onKnowledge: (id: string) => void; onCreateKnowledge: () => void; onSession: (id: string) => void; onAgent: () => void; onNewChat: () => void; onCreateWorkspace: () => void; onCollapse: () => void; onSignOut: () => void }) {
  const navigate = useNavigate(); const location = useLocation(); const qc = useQueryClient();
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ kind: "rename" | "delete"; session: AgentSession } | null>(null);
  const [title, setTitle] = useState(""); const [error, setError] = useState<unknown>(null); const [busy, setBusy] = useState(false);
  const [exportError, setExportError] = useState<unknown>(null);
  const [knowledgeDialog, setKnowledgeDialog] = useState<{ kind: "rename" | "delete"; kb: KnowledgeBase } | null>(null);
  const [knowledgeName, setKnowledgeName] = useState(""); const [knowledgeError, setKnowledgeError] = useState<unknown>(null); const [knowledgeBusy, setKnowledgeBusy] = useState(false);
  const [deleteOperation, setDeleteOperation] = useState<{ id: string; workspaceId: string } | null>(null);
  const deletion = useQuery({ queryKey: ["rag-operation", deleteOperation?.id], queryFn: () => api.ragOperation(deleteOperation!.id), enabled: Boolean(deleteOperation), refetchInterval: (query) => ["succeeded", "failed"].includes(query.state.data?.status ?? "") ? false : 2000 });
  const deletionPending = Boolean(deleteOperation) && !["succeeded", "failed"].includes(deletion.data?.status ?? "");
  useEffect(() => {
    if (deletion.data?.status !== "succeeded" || !deleteOperation) return;
    void qc.invalidateQueries({ queryKey: ["knowledge-bases", deleteOperation.workspaceId] });
    setKnowledgeDialog(null); setKnowledgeError(null); setDeleteOperation(null);
  }, [deletion.data?.status, deleteOperation, qc]);
  const closeDialog = () => { if (!busy) { setDialog(null); setError(null); } };
  const closeKnowledgeDialog = () => { if (!knowledgeBusy) setKnowledgeDialog(null); };
  const selectKnowledgeAction = (kb: KnowledgeBase, kind: "rename" | "delete") => {
    if (deletionPending) return;
    setOpenMenuId(null); setKnowledgeError(null); setDeleteOperation(null);
    setKnowledgeName(kb.name); setKnowledgeDialog({ kind, kb });
  };
  const submitKnowledgeAction = async () => {
    if (!knowledgeDialog || !workspace || knowledgeBusy || deleteOperation || (knowledgeDialog.kind === "rename" && !knowledgeName.trim())) return;
    setKnowledgeBusy(true); setKnowledgeError(null);
    try {
      if (knowledgeDialog.kind === "rename") {
        await api.updateKnowledgeBase(workspace.id, knowledgeDialog.kb.id, { name: knowledgeName.trim() });
        void qc.invalidateQueries({ queryKey: ["knowledge-bases", workspace.id] });
        setKnowledgeDialog(null);
      } else {
        const result = await api.deleteKnowledgeBase(workspace.id, knowledgeDialog.kb.id);
        setDeleteOperation({ id: result.operation_id, workspaceId: workspace.id });
        void qc.invalidateQueries({ queryKey: ["knowledge-bases", workspace.id] });
        if (location.pathname === `/knowledge/${knowledgeDialog.kb.id}`) navigate("/knowledge");
      }
    } catch (cause) { setKnowledgeError(cause); }
    finally { setKnowledgeBusy(false); }
  };
  const selectAction = (session: AgentSession, kind: "rename" | "export" | "delete") => {
    setOpenMenuId(null); setError(null); setExportError(null);
    if (kind === "export") {
      void (async () => {
        try {
          const [detail, history] = await Promise.all([api.session(session.id), api.messages(session.id)]);
          const blob = new Blob([`${JSON.stringify({ session: detail, messages: history.items }, null, 2)}\n`], { type: "application/json;charset=utf-8" });
          const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
          try {
            anchor.href = url; anchor.download = `agent-session-${session.id}.json`;
            document.body.append(anchor); anchor.click();
          } finally {
            anchor.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 0);
          }
        } catch (cause) { setExportError(cause); }
      })();
      return;
    }
    setTitle(session.title ?? ""); setDialog({ kind, session });
  };
  const submitAction = async () => {
    if (!dialog || busy || (dialog.kind === "rename" && !title.trim())) return;
    setBusy(true); setError(null);
    try {
      if (dialog.kind === "rename") {
        await api.updateSessionTitle(dialog.session.id, title.trim());
        void qc.invalidateQueries({ queryKey: ["session", dialog.session.id] });
      } else {
        await api.deleteSession(dialog.session.id);
        if (location.pathname === `/chat/${dialog.session.id}`) navigate("/chat");
      }
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      setDialog(null);
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  };
  return <>
    <div className="flex h-14 items-center gap-2 border-b border-slate-800 px-4 font-semibold"><span className="truncate">智能体应用开发工具集</span><IconButton title="收起目录栏" onClick={() => { setOpenMenuId(null); onCollapse(); }}><PanelLeftClose size={16} /></IconButton></div>
    <nav className="scrollbar min-h-0 flex-1 overflow-auto p-2">{workspaces.map((item) => <WorkspaceTree key={item.id} workspace={item} active={item.id === workspace?.id} expanded={expanded === item.id} knowledgeBases={item.id === workspace?.id ? kbs : []} sessions={sessions.filter((session) => session.workspace_id === item.id)} onOpen={() => { setOpenMenuId(null); onOpenWorkspace(item); }} onKnowledgeHome={onKnowledgeHome} onKnowledge={onKnowledge} onCreateKnowledge={onCreateKnowledge} onSession={onSession} onAgent={onAgent} onNewChat={onNewChat} openMenuId={openMenuId} onMenuToggle={(id) => setOpenMenuId(openMenuId === id ? null : id)} onMenuClose={() => setOpenMenuId(null)} onSessionAction={selectAction} onKnowledgeAction={selectKnowledgeAction} knowledgeOperationPending={deletionPending} />)}<button className="mt-2 flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm muted hover:bg-slate-800 hover:text-slate-100" onClick={onCreateWorkspace}><Plus size={16} />创建 workspace</button></nav>
    {exportError && <div className="border-t border-slate-800 p-2" role="alert"><ErrorNotice error={exportError} /><button className="mt-1 text-xs text-slate-300 underline" onClick={() => setExportError(null)}>关闭提示</button></div>}
    {!knowledgeDialog && deletionPending && <p role="status" className="border-t border-slate-800 p-2 text-xs text-sky-200">正在删除知识库（{deletion.data?.status ?? "提交中"}）…</p>}
    {!knowledgeDialog && deleteOperation && (deletion.isError || deletion.data?.status === "failed") && <div className="border-t border-slate-800 p-2" role="alert"><ErrorNotice error={deletion.error ?? new Error(deletion.data?.error || "知识库删除失败。")} /><button className="mt-1 text-xs text-slate-300 underline" onClick={() => setDeleteOperation(null)}>关闭提示</button></div>}
    <footer className="flex items-center gap-2 border-t border-slate-800 p-3 text-sm"><span className="min-w-0 flex-1 truncate">{userEmail}</span><IconButton title="退出登录" onClick={onSignOut}><LogOut size={16} /></IconButton></footer>
    <Modal open={Boolean(dialog)} title={dialog?.kind === "rename" ? "重命名会话" : "删除会话"} onClose={closeDialog}>
      {dialog?.kind === "rename" ? <Form onSubmit={(event) => { event.preventDefault(); void submitAction(); }}><label className="block text-sm">会话名称<input autoFocus className="input mt-1" maxLength={256} value={title} onChange={(event) => setTitle(event.target.value)} /></label><ErrorNotice error={error} /><div className="flex justify-end gap-2"><button className="btn" type="button" disabled={busy} onClick={closeDialog}>取消</button><button className="btn btn-primary" disabled={busy || !title.trim()}>保存</button></div></Form>
        : dialog?.kind === "delete" ? <div className="space-y-4"><p className="text-sm">确定删除会话“{dialog.session.title ?? "新聊天"}”吗？该会话的历史消息也会删除。</p><ErrorNotice error={error} /><div className="flex justify-end gap-2"><button className="btn" disabled={busy} onClick={closeDialog}>取消</button><button className="btn btn-danger" disabled={busy} onClick={() => void submitAction()}>确认删除</button></div></div> : null}
    </Modal>
    <Modal open={Boolean(knowledgeDialog)} title={knowledgeDialog?.kind === "rename" ? "重命名知识库" : "删除知识库"} onClose={closeKnowledgeDialog}>
      {knowledgeDialog?.kind === "rename" ? <Form onSubmit={(event) => { event.preventDefault(); void submitKnowledgeAction(); }}><label className="block text-sm">知识库名称<input autoFocus className="input mt-1" maxLength={128} value={knowledgeName} onChange={(event) => setKnowledgeName(event.target.value)} /></label><ErrorNotice error={knowledgeError} /><div className="flex justify-end gap-2"><button className="btn" type="button" disabled={knowledgeBusy} onClick={closeKnowledgeDialog}>取消</button><button className="btn btn-primary" disabled={knowledgeBusy || !knowledgeName.trim()}>保存</button></div></Form>
        : knowledgeDialog?.kind === "delete" ? <div className="space-y-4"><p className="text-sm">确定删除知识库“{knowledgeDialog.kb.name}”吗？文档及其处理结果将被删除，且无法恢复。</p>{deleteOperation && !["succeeded", "failed"].includes(deletion.data?.status ?? "") && <p role="status" className="text-sm text-sky-200">正在删除知识库（{deletion.data?.status ?? "提交中"}）…</p>}<ErrorNotice error={knowledgeError ?? deletion.error ?? (deletion.data?.status === "failed" ? new Error(deletion.data.error || "知识库删除失败。") : null)} />{deletion.isError && <button type="button" className="btn w-full" onClick={() => void deletion.refetch()}>重新查询删除进度</button>}<div className="flex justify-end gap-2"><button className="btn" disabled={knowledgeBusy} onClick={closeKnowledgeDialog}>{deleteOperation ? "关闭" : "取消"}</button>{!deleteOperation && <button className="btn btn-danger" disabled={knowledgeBusy} onClick={() => void submitKnowledgeAction()}>确认删除</button>}</div></div> : null}
    </Modal>
  </>;
}
function CollapsedInfo({ knowledge, hasSubagents, knowledgeTab, agentTab, onExpand, onKnowledgeTab, onAgentTab }: { knowledge: boolean; hasSubagents: boolean; knowledgeTab: KnowledgeTab; agentTab: AgentTab; onExpand: () => void; onKnowledgeTab: (tab: KnowledgeTab) => void; onAgentTab: (tab: AgentTab) => void }) {
  const knowledgeItems: Array<[KnowledgeTab, string, typeof Settings2]> = [["models", "模型设置", Settings2], ["graphs", "知识图谱", Network], ["advanced", "高级设置", SlidersHorizontal], ["retrieve", "检索实验", FlaskConical]];
  const agentItems: Array<[AgentTab, string, typeof Settings2]> = [["files", "工作区文件", FolderOpen], ["bindings", "Provider Bindings", Settings2], ...(hasSubagents ? [["subagents", "Subagent", Bot] as [AgentTab, string, typeof Settings2]] : []), ["runtime", "Runtime", Bot]];
  return <><div className="flex h-14 items-center justify-center border-b border-slate-800"><IconButton title="展开信息栏" onClick={onExpand}><PanelRightOpen size={18} /></IconButton></div><nav className="flex flex-col items-center gap-1 py-2">{knowledge ? knowledgeItems.map(([id, label, Icon]) => <IconButton key={id} title={label} onClick={() => onKnowledgeTab(id)}><Icon className={knowledgeTab === id ? "text-sky-300" : ""} size={18} /></IconButton>) : agentItems.map(([id, label, Icon]) => <IconButton key={id} title={label} onClick={() => onAgentTab(id)}><Icon className={agentTab === id ? "text-sky-300" : ""} size={18} /></IconButton>)}</nav></>;
}
type SessionAction = "rename" | "export" | "delete";
type KnowledgeAction = "rename" | "delete";

function DirectoryMenu({ label, actions, open, onToggle, onClose, onSelect }: { label: string; actions: ReadonlyArray<{ kind: SessionAction; text: string; disabled?: boolean }>; open: boolean; onToggle: () => void; onClose: () => void; onSelect: (kind: SessionAction) => void }) {
  const triggerRef = useRef<HTMLButtonElement>(null); const menuRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => { const target = event.target; if (target instanceof globalThis.Node && !triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) onClose(); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { onClose(); triggerRef.current?.focus(); } };
    const move = () => onClose();
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    window.addEventListener("scroll", move, true);
    window.addEventListener("resize", move);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", escape); window.removeEventListener("scroll", move, true); window.removeEventListener("resize", move); };
  }, [open, onClose]);
  const toggle = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    const height = actions.length * 36 + 12;
    if (rect) setPosition({ top: rect.bottom + height <= window.innerHeight ? rect.bottom + 4 : Math.max(8, rect.top - height), left: Math.max(8, Math.min(rect.right - 176, window.innerWidth - 184)) });
    onToggle();
  };
  return <><button ref={triggerRef} type="button" className="shrink-0 rounded p-1 text-slate-400 hover:bg-slate-700 hover:text-slate-100" aria-label={label} aria-haspopup="menu" aria-expanded={open} onClick={toggle}><MoreHorizontal size={18} /></button>
    {open && createPortal(<div ref={menuRef} role="menu" aria-label={label} className="fixed z-50 w-44 rounded-lg border border-slate-700 bg-slate-900 p-1 text-sm text-slate-100 shadow-xl" style={position}>
      {actions.map(({ kind, text, disabled }) => <button key={kind} type="button" role="menuitem" disabled={disabled} className={`block w-full rounded px-3 py-2 text-left hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50 ${kind === "delete" ? "text-rose-300" : ""}`} onClick={() => onSelect(kind)}>{text}</button>)}
    </div>, document.body)}
  </>;
}

function WorkspaceTree({ workspace, active, expanded, knowledgeBases, sessions, onOpen, onKnowledgeHome, onKnowledge, onCreateKnowledge, onSession, onAgent, onNewChat, openMenuId, onMenuToggle, onMenuClose, onSessionAction, onKnowledgeAction, knowledgeOperationPending }: { workspace: Workspace; active: boolean; expanded: boolean; knowledgeBases: KnowledgeBase[]; sessions: AgentSession[]; onOpen: () => void; onKnowledgeHome: () => void; onKnowledge: (id: string) => void; onCreateKnowledge: () => void; onSession: (id: string) => void; onAgent: () => void; onNewChat: () => void; openMenuId: string | null; onMenuToggle: (id: string) => void; onMenuClose: () => void; onSessionAction: (session: AgentSession, kind: SessionAction) => void; onKnowledgeAction: (kb: KnowledgeBase, kind: KnowledgeAction) => void; knowledgeOperationPending: boolean }) {
  return <div className="mb-1"><button className={`flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm ${active ? "bg-sky-500/15 text-sky-200" : "hover:bg-slate-800"}`} onClick={onOpen}>{expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}<span className="truncate">{workspace.name}</span></button>{expanded && <div className="ml-5 border-l border-slate-700 pl-2"><div className="mt-1 flex items-center gap-1 text-sm"><button className="flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1 hover:bg-slate-800" onClick={onKnowledgeHome}><FolderOpen size={15} />知识库</button><button className="btn p-1" title="创建知识库" onClick={onCreateKnowledge}><Plus size={14} /></button></div>{knowledgeBases.map((kb) => <div key={kb.id} className="flex min-w-0 items-center rounded hover:bg-slate-800"><button onClick={() => onKnowledge(kb.id)} className="min-w-0 flex-1 truncate rounded px-2 py-1 text-left text-sm muted hover:text-slate-100">{kb.name}</button><DirectoryMenu label={`知识库操作：${kb.name}`} actions={[{ kind: "rename", text: "重命名", disabled: kb.status !== "active" || knowledgeOperationPending }, { kind: "delete", text: "删除知识库", disabled: kb.status !== "active" || knowledgeOperationPending }]} open={openMenuId === `knowledge:${kb.id}`} onToggle={() => onMenuToggle(`knowledge:${kb.id}`)} onClose={onMenuClose} onSelect={(kind) => onKnowledgeAction(kb, kind as KnowledgeAction)} /></div>)}<div className="mt-1 flex items-center gap-1 text-sm"><button className="flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1 hover:bg-slate-800" onClick={onAgent}><MessageSquare size={15} />Agent</button><button className="btn p-1" title="新聊天" onClick={onNewChat}><Plus size={14} /></button></div>{sessions.map((session) => <div key={session.id} className="flex min-w-0 items-center rounded hover:bg-slate-800"><button onClick={() => onSession(session.id)} className="min-w-0 flex-1 truncate rounded px-2 py-1 text-left text-sm muted hover:text-slate-100">{session.title ?? "新聊天"}</button><DirectoryMenu label={`会话操作：${session.title ?? "新聊天"}`} actions={[{ kind: "rename", text: "重命名" }, { kind: "export", text: "导出会话" }, { kind: "delete", text: "删除会话" }]} open={openMenuId === `session:${session.id}`} onToggle={() => onMenuToggle(`session:${session.id}`)} onClose={onMenuClose} onSelect={(kind) => onSessionAction(session, kind)} /></div>)}</div>}</div>;
}
function CreateWorkspace({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (id: string) => void }) { const [name, setName] = useState(""); const mutation = useMutation({ mutationFn: () => api.createWorkspace(name), onSuccess: (item) => onCreated(item.id) }); return <Modal open={open} title="创建 workspace" onClose={onClose}><Form onSubmit={(event) => { event.preventDefault(); mutation.mutate(); }}><label className="block text-sm">名称<input autoFocus className="input mt-1" value={name} maxLength={128} onChange={(event) => setName(event.target.value)} /></label><ErrorNotice error={mutation.error} /><button className="btn btn-primary w-full" disabled={!name.trim() || mutation.isPending}>创建</button></Form></Modal>; }
function CreateKnowledge({ open, workspace, onClose, onCreated }: { open: boolean; workspace: Workspace; onClose: () => void; onCreated: (id: string) => void }) { const capabilities = useQuery({ queryKey: ["rag-capabilities"], queryFn: api.ragCapabilities, enabled: open }); const [name, setName] = useState(""); const [vector, setVector] = useState(""); useEffect(() => { if (!vector && capabilities.data?.vector_backends[0]) setVector(capabilities.data.vector_backends[0]); }, [capabilities.data, vector]); const mutation = useMutation({ mutationFn: () => api.createKnowledgeBase(workspace.id, { name, file_backend: capabilities.data!.file_backends[0], block_backend: capabilities.data!.block_backends[0], chunk_backend: capabilities.data!.chunk_backends[0], vector_backend: vector, graph_backend: capabilities.data!.graph_backends[0] }), onSuccess: (kb) => onCreated(kb.id) }); return <Modal open={open} title="创建知识库" onClose={onClose}><Form onSubmit={(event) => { event.preventDefault(); mutation.mutate(); }}><input autoFocus className="input" placeholder="知识库名称" value={name} onChange={(event) => setName(event.target.value)} /><select className="input" value={vector} onChange={(event) => setVector(event.target.value)}>{capabilities.data?.vector_backends.map((item) => <option key={item} value={item}>{item}</option>)}</select><ErrorNotice error={capabilities.error ?? mutation.error} /><button className="btn btn-primary w-full" disabled={!name.trim() || !vector || mutation.isPending}>创建</button></Form></Modal>; }

function KnowledgeLanding() {
  return <div className="grid h-screen place-items-center p-8 text-center"><div className="max-w-md"><FolderOpen className="mx-auto text-sky-300" size={40} /><h1 className="mt-4 text-xl font-semibold">知识库</h1><p className="mt-2 text-sm muted">从左侧选择已有知识库查看文档和设置，或点击“知识库”右侧的 + 创建知识库。</p></div></div>;
}

function AgentLanding() {
  return <div className="grid h-screen place-items-center p-8 text-center"><div className="max-w-md"><Bot className="mx-auto text-sky-300" size={40} /><h1 className="mt-4 text-xl font-semibold">Agent</h1><p className="mt-2 text-sm muted">从左侧选择已有会话继续对话，或点击 Agent 右侧的 + 新建聊天。</p></div></div>;
}

export function KnowledgeCenter({ workspace, kb, graphId, onBackFromGraph }: { workspace: Workspace; kb: KnowledgeBase; graphId: string | null; onBackFromGraph: () => void }) {
  const qc = useQueryClient(); const navigate = useNavigate(); const [picked, setPicked] = useState<string[]>([]); const [files, setFiles] = useState<File[]>([]);
  const [publishOpen, setPublishOpen] = useState(false); const [publishName, setPublishName] = useState("");
  const docs = useQuery({ queryKey: ["documents", workspace.id, kb.id], queryFn: () => api.documents(workspace.id, kb.id), refetchInterval: 2000 });
  const upload = useMutation({ mutationFn: () => api.uploadDocuments(workspace.id, kb.id, files), onSuccess: () => { setFiles([]); void qc.invalidateQueries({ queryKey: ["documents", workspace.id, kb.id] }); } });
  const submit = useMutation({ mutationFn: (kind: (typeof stages)[number][0]) => api.submitStageBatch(workspace.id, kb.id, kind, picked), onSuccess: () => void qc.invalidateQueries({ queryKey: ["documents", workspace.id, kb.id] }) });
  const remove = useMutation({ mutationFn: () => api.deleteDocuments(workspace.id, kb.id, picked), onSuccess: () => { setPicked([]); void qc.invalidateQueries({ queryKey: ["documents", workspace.id, kb.id] }); } });
  const publish = useMutation({ mutationFn: () => api.copyKnowledgeBase(workspace.id, kb.id, publishName.trim()), onSuccess: () => void qc.invalidateQueries({ queryKey: ["knowledge-bases", workspace.id] }) });
  const operation = useQuery({ queryKey: ["rag-operation", publish.data?.operation_id], queryFn: () => api.ragOperation(publish.data!.operation_id), enabled: Boolean(publish.data?.operation_id), refetchInterval: (query) => ["succeeded", "failed"].includes(query.state.data?.status ?? "") ? false : 2000 });
  useEffect(() => {
    if (operation.data?.status !== "succeeded" || !publish.data) return;
    void qc.invalidateQueries({ queryKey: ["knowledge-bases", workspace.id] }).then(() => { setPublishOpen(false); navigate(`/knowledge/${publish.data.target_knowledge_base_id}`); });
  }, [operation.data?.status, publish.data, qc, workspace.id, navigate]);
  useEffect(() => { if (operation.data?.status === "failed") void qc.invalidateQueries({ queryKey: ["knowledge-bases", workspace.id] }); }, [operation.data?.status, qc, workspace.id]);
  const items = docs.data?.items ?? []; const toggle = (id: string) => setPicked((old) => old.includes(id) ? old.filter((item) => item !== id) : [...old, id]);
  if (graphId) return <GraphCanvas workspace={workspace} kb={kb} graphId={graphId} onBack={onBackFromGraph} />;
  const publishing = publish.isPending || (Boolean(publish.data) && operation.data?.status !== "failed" && operation.data?.status !== "succeeded");
  return <><div className="flex h-screen min-w-0 flex-col"><header className="flex flex-wrap items-center gap-2 border-b border-slate-800 p-3"><h1 className="min-w-0 flex-1 truncate text-lg font-semibold">{kb.name}</h1>{stages.map(([kind, label]) => <button key={kind} className="btn" disabled={!picked.length || submit.isPending} onClick={() => submit.mutate(kind)}>{label}</button>)}<label className="btn"><Upload size={15} />上传文档<input className="hidden" type="file" multiple onChange={(event) => setFiles(Array.from(event.target.files ?? []))} /></label><button className="btn btn-danger" disabled={!picked.length || remove.isPending} onClick={() => remove.mutate()}>删除已选</button><button className="btn btn-primary" disabled={kb.status !== "active"} onClick={() => { setPublishName(""); publish.reset(); setPublishOpen(true); }}>版本发布</button></header>{files.length > 0 && <div className="flex items-center gap-3 border-b border-slate-800 p-2 text-sm"><span>{files.length} 个文件待上传</span><button className="btn btn-primary" onClick={() => upload.mutate()} disabled={upload.isPending}>提交上传</button></div>}<ErrorNotice error={docs.error ?? upload.error ?? submit.error ?? remove.error} /><div className="scrollbar min-w-0 flex-1 overflow-auto"><table className="min-w-[900px] w-full text-left text-sm"><thead className="sticky top-0 bg-slate-900 text-xs muted"><tr><th className="p-3"><input type="checkbox" checked={items.length > 0 && picked.length === items.length} onChange={() => setPicked(picked.length === items.length ? [] : items.map((item) => item.id))} /></th><th className="p-3">文件</th>{stages.map(([, label]) => <th key={label} className="p-3">{label}</th>)}</tr></thead><tbody>{items.map((document) => <DocumentRow key={document.id} workspace={workspace} kb={kb} document={document} checked={picked.includes(document.id)} onToggle={() => toggle(document.id)} />)}</tbody></table>{!items.length && <div className="grid min-h-60 place-items-center muted">上传文档后会显示四阶段状态。</div>}</div></div>
    <Modal open={publishOpen} title="版本发布" onClose={() => { if (!publishing) setPublishOpen(false); }}><Form onSubmit={(event) => { event.preventDefault(); if (!publishing) publish.mutate(); }}><label className="block text-sm">新知识库名称<input autoFocus className="input mt-1" required maxLength={128} value={publishName} onChange={(event) => setPublishName(event.target.value)} /></label><p className="text-xs muted">完整复制当前知识库。复制完成后，新知识库可继续编辑。</p>{publishing && <p role="status" className="text-sm text-sky-200">正在复制知识库（{operation.data?.status ?? "提交中"}）…</p>}<ErrorNotice error={publish.error ?? operation.error ?? (operation.data?.status === "failed" ? new Error(operation.data.error || "知识库复制失败。") : null)} />{operation.isError && publish.data && <button type="button" className="btn w-full" onClick={() => void operation.refetch()}>重新查询发布进度</button>}<button className="btn btn-primary w-full" disabled={!publishName.trim() || publishing}>确认发布</button></Form></Modal>
  </>;
}
function DocumentRow({ workspace, kb, document, checked, onToggle }: { workspace: Workspace; kb: KnowledgeBase; document: RagDocument; checked: boolean; onToggle: () => void }) {
  const [openEntries, setOpenEntries] = useState<"blocks" | "chunks" | null>(null);
  const visibleBlocks = openEntries === "blocks" && document.stages?.parsing?.status === "succeeded";
  const visibleChunks = openEntries === "chunks" && document.stages?.chunking?.status === "succeeded";
  return <><tr className="border-t border-slate-800"><td className="p-3"><input type="checkbox" checked={checked} onChange={onToggle} /></td><td className="p-3"><p className="text-xs muted">{document.id}</p><p className="mt-1 font-medium">{documentName(document)}</p></td>{stages.map(([kind, label]) => { const stage = document.stages?.[stageKey[kind]]; const expandable = kind === "parsing" || kind === "chunking"; const entryKind = kind === "parsing" ? "blocks" : "chunks"; const open = kind === "parsing" ? visibleBlocks : visibleChunks; return <td key={kind} className="p-3"><span className={stage?.status === "failed" ? "text-rose-300" : stage?.status === "succeeded" ? "text-emerald-300" : "text-sky-200"}>{stage?.status ?? "not_started"}</span><span className="ml-1 muted">{stage?.progress ?? 0}%</span>{expandable && stage?.status === "succeeded" && <button className="mt-1 block rounded p-1 hover:bg-slate-800" aria-label={`${open ? "收起" : "展开"}${label}条目：${documentName(document)}`} aria-expanded={open} onClick={() => setOpenEntries((current) => current === entryKind ? null : entryKind)}>{open ? <ChevronUp size={20} /> : <ChevronDown size={20} />}</button>}</td>; })}</tr>{(visibleBlocks || visibleChunks) && <tr className="border-t border-slate-800"><td colSpan={6} className="space-y-3 p-3 pl-10">{visibleBlocks && <DocumentEntries workspace={workspace} kb={kb} document={document} kind="blocks" />}{visibleChunks && <DocumentEntries workspace={workspace} kb={kb} document={document} kind="chunks" />}</td></tr>}</>;
}

function KnowledgeInfo({ workspace, kb, tab, setTab, onOpenGraph }: { workspace: Workspace; kb: KnowledgeBase; tab: KnowledgeTab; setTab: (tab: KnowledgeTab) => void; onOpenGraph: (id: string) => void }) { const docs = useQuery({ queryKey: ["documents", workspace.id, kb.id], queryFn: () => api.documents(workspace.id, kb.id) }); return <div className="p-3"><Tabs labels={[["models", "模型设置"], ["graphs", "知识图谱"], ["advanced", "高级设置"], ["retrieve", "检索实验"]]} value={tab} onChange={(value) => setTab(value as KnowledgeTab)} />{tab === "models" && <RagModelSettings workspace={workspace} kb={kb} />}{tab === "graphs" && <GraphList workspace={workspace} kb={kb} onOpen={onOpenGraph} />}{tab === "advanced" && <AdvancedSettings workspace={workspace} kb={kb} documents={docs.data?.items ?? []} />}{tab === "retrieve" && <RetrievalLab workspace={workspace} kb={kb} documents={docs.data?.items ?? []} />}</div>; }
function RagModelSettings({ workspace, kb }: { workspace: Workspace; kb: KnowledgeBase }) {
  const qc = useQueryClient(); const [kind, setKind] = useState<"embedding" | "llm" | "reranker">("embedding"); const [baseUrl, setBaseUrl] = useState(""); const [modelName, setModelName] = useState(""); const [apiKey, setApiKey] = useState(""); const [thinking, setThinking] = useState("");
  const models = useQuery({ queryKey: ["rag-models", workspace.id, kb.id], queryFn: () => api.ragModels(workspace.id, kb.id) });
  const save = useMutation({ mutationFn: () => api.setRagModel(workspace.id, kb.id, kind, kind === "reranker" ? { protocol: "vllm", base_url: baseUrl, model_name: modelName, api_key: apiKey } : { protocol: "openai", base_url: baseUrl, model_name: modelName, api_key: apiKey, thinking_effort: thinking || null }), onSuccess: () => { setApiKey(""); void qc.invalidateQueries({ queryKey: ["rag-models", workspace.id, kb.id] }); } });
  const remove = useMutation({ mutationFn: () => api.deleteReranker(workspace.id, kb.id), onSuccess: () => void qc.invalidateQueries({ queryKey: ["rag-models", workspace.id, kb.id] }) });
  return <section className="mt-4 space-y-4 text-sm"><div className="space-y-2">{models.data?.items.map((model) => <div key={model.kind} className="rounded bg-slate-900 p-3"><div className="flex items-center gap-2"><span className="min-w-0 flex-1 truncate">{model.kind} · {model.model_name}</span>{model.kind === "reranker" && <ConfirmButton label="删除" confirmText="确认删除" onConfirm={() => remove.mutate()} />}</div><p className="mt-1 truncate text-xs muted">{model.protocol} · {model.base_url} · {model.verified_at ? "已验证" : "未验证"}</p></div>)}{!models.data?.items.length && <p className="muted">尚未配置知识库模型。</p>}</div><Form onSubmit={(event) => { event.preventDefault(); save.mutate(); }}><label className="block text-sm">类型<select className="input mt-1" value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="embedding">embedding</option><option value="llm">LLM（图谱）</option><option value="reranker">reranker（可选）</option></select></label><label className="block text-sm">服务地址<input className="input mt-1" required type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="http://model-host:8000/v1" /></label><label className="block text-sm">模型名称<input className="input mt-1" required value={modelName} onChange={(event) => setModelName(event.target.value)} /></label>{kind === "llm" && <label className="block text-sm">Thinking effort（可选）<input className="input mt-1" value={thinking} onChange={(event) => setThinking(event.target.value)} /></label>}<label className="block text-sm">API Key<input className="input mt-1" required type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /></label><ErrorNotice error={models.error ?? save.error ?? remove.error} /><button className="btn btn-primary w-full" disabled={save.isPending}>验证并保存</button></Form></section>;
}
function GraphList({ workspace, kb, onOpen }: { workspace: Workspace; kb: KnowledgeBase; onOpen: (id: string) => void }) { const graphs = useQuery({ queryKey: ["graphs", workspace.id, kb.id], queryFn: () => api.graphs(workspace.id, kb.id) }); return <section className="mt-4 space-y-2 text-sm"><p className="text-xs muted">完成图谱提取后，在中央区域查看节点和边。</p>{graphs.data?.items.map((graph) => <button className="flex w-full items-center gap-2 rounded bg-slate-900 p-3 text-left hover:bg-slate-800" key={graph.id} onClick={() => onOpen(graph.id)}><Network size={16} className="text-sky-300" /><span className="min-w-0 flex-1 truncate">{graph.name}</span><ChevronRight size={15} /></button>)}{!graphs.data?.items.length && <p className="muted">尚无图谱。</p>}<ErrorNotice error={graphs.error} /></section>; }
function AdvancedSettings({ workspace, kb, documents }: { workspace: Workspace; kb: KnowledgeBase; documents: RagDocument[] }) {
  const qc = useQueryClient(); const [documentId, setDocumentId] = useState(""); const [strategy, setStrategy] = useState("fixed"); const [config, setConfig] = useState('{"max_token_size":512,"overlap_token_size":64,"split_by_character":"\\n\\n"}'); const [concurrency, setConcurrency] = useState(kb.concurrency);
  useEffect(() => setConcurrency(kb.concurrency), [kb.id, kb.concurrency]);
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["knowledge-bases", workspace.id] }); void qc.invalidateQueries({ queryKey: ["documents", workspace.id, kb.id] }); };
  const chunking = useMutation({ mutationFn: () => api.updateChunking(workspace.id, kb.id, documentId, strategy, JSON.parse(config) as Record<string, unknown>), onSuccess: refresh });
  const updateKb = useMutation({ mutationFn: () => api.updateKnowledgeBase(workspace.id, kb.id, { parsing_concurrency: concurrency.parsing, chunking_concurrency: concurrency.chunking, embedding_concurrency: concurrency.embedding, graph_concurrency: concurrency.graph }), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (kind: "blocks" | "chunks" | "vectors" | "graph") => api.deleteDerivedDocumentData(workspace.id, kb.id, documentId, kind), onSuccess: refresh });
  return <section className="mt-4 space-y-5 text-sm"><Form onSubmit={(event) => { event.preventDefault(); chunking.mutate(); }}><h3 className="font-medium">文档级切分配置</h3><label>文档<select className="input mt-1" required value={documentId} onChange={(event) => setDocumentId(event.target.value)}><option value="">请选择</option>{documents.map((document) => <option key={document.id} value={document.id}>{documentName(document)}</option>)}</select></label><label>策略<select className="input mt-1" value={strategy} onChange={(event) => setStrategy(event.target.value)}><option value="fixed">fixed</option><option value="regex">regex</option><option value="semantic">semantic</option></select></label><label>JSON 配置<textarea className="input mt-1 min-h-28 font-mono text-xs" value={config} onChange={(event) => setConfig(event.target.value)} /></label><ErrorNotice error={chunking.error} /><button className="btn btn-primary w-full" disabled={!documentId || chunking.isPending}>保存切分配置</button></Form><Form onSubmit={(event) => { event.preventDefault(); updateKb.mutate(); }}><h3 className="font-medium">并发上限</h3><div className="grid grid-cols-2 gap-2">{(["parsing", "chunking", "embedding", "graph"] as const).map((field) => <label key={field}>{stageLabels[field]}<input className="input mt-1" min={1} type="number" value={concurrency[field]} onChange={(event) => setConcurrency((old) => ({ ...old, [field]: Number(event.target.value) }))} /></label>)}</div><ErrorNotice error={updateKb.error} /><button className="btn btn-primary w-full" disabled={updateKb.isPending}>保存并发配置</button></Form><div><h3 className="font-medium">删除文档派生数据</h3><p className="mt-1 text-xs muted">任务 queued 或 running 时服务端会拒绝删除。</p><select className="input mt-2" value={documentId} onChange={(event) => setDocumentId(event.target.value)}><option value="">选择文档</option>{documents.map((document) => <option key={document.id} value={document.id}>{documentName(document)}</option>)}</select><div className="mt-2 flex flex-wrap gap-2">{(["blocks", "chunks", "vectors", "graph"] as const).map((kind) => <ConfirmButton key={kind} label={`删除 ${kind}`} confirmText="再次确认" onConfirm={() => remove.mutate(kind)} />)}</div><ErrorNotice error={remove.error} /></div></section>;
}
export function RetrievalLab({ workspace, kb, documents }: { workspace: Workspace; kb: KnowledgeBase; documents: RagDocument[] }) {
  const [query, setQuery] = useState(""); const [mode, setMode] = useState("hybrid"); const [rerank, setRerank] = useState(true); const [topK, setTopK] = useState(5); const [candidateK, setCandidateK] = useState(20); const [chosen, setChosen] = useState<string[]>([]);
  const retrieve = useMutation({ mutationFn: () => api.retrieve(workspace.id, kb.id, { query, mode, rerank: mode === "graph" ? undefined : rerank, top_k: topK, candidate_k: candidateK, document_ids: chosen.length ? chosen : undefined }) });
  const result = retrieve.data;
  return <section className="mt-4 space-y-4 text-sm">
    <Form onSubmit={(event) => { event.preventDefault(); retrieve.mutate(); }}>
      <label>查询<textarea className="input mt-1" required value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <label>模式<select className="input mt-1" value={mode} onChange={(event) => setMode(event.target.value)}><option value="vector">vector</option><option value="hybrid">hybrid</option><option value="graph">graph</option></select></label>
      {mode !== "graph" && <label className="flex items-center gap-2"><input type="checkbox" checked={rerank} onChange={(event) => setRerank(event.target.checked)} />本次启用重排</label>}
      <div className="grid grid-cols-2 gap-2"><label>top_k<input className="input mt-1" type="number" min={1} max={100} value={topK} onChange={(event) => setTopK(Number(event.target.value))} /></label><label>candidate_k<input className="input mt-1" type="number" min={1} max={500} value={candidateK} onChange={(event) => setCandidateK(Number(event.target.value))} /></label></div>
      <fieldset><legend>文档过滤（可选）</legend><div className="mt-1 max-h-28 overflow-auto">{documents.map((document) => <label key={document.id} className="flex gap-2 p-1"><input type="checkbox" checked={chosen.includes(document.id)} onChange={() => setChosen((old) => old.includes(document.id) ? old.filter((id) => id !== document.id) : [...old, document.id])} />{documentName(document)}</label>)}</div></fieldset>
      <button className="btn btn-primary w-full" disabled={retrieve.isPending}>运行检索</button>
    </Form>
    <ErrorNotice error={retrieve.error} />
    {result && <div className="space-y-2">
      <p className="text-xs muted">模式：{result.mode}</p>
      {result.mode !== "graph" && <p className="text-xs muted">重排：{result.rerank.applied ? "已应用" : result.rerank.error ? `未应用（${result.rerank.error}）` : result.rerank.configured ? "未应用" : "未配置"}</p>}
      {result.mode === "graph" ? <>
        {!result.nodes.length && !result.edges.length && <p className="muted">没有找到相关图谱结果。</p>}
        {result.nodes.map((node) => <article className="rounded bg-slate-900 p-3 text-xs" key={node.id}><span className="badge">节点 · {node.entity_type ?? "entity"}</span><p className="mt-2 text-sm">{node.name}</p>{node.description && <p className="mt-1 whitespace-pre-wrap muted">{node.description}</p>}</article>)}
        {result.edges.map((edge) => <article className="rounded bg-slate-900 p-3 text-xs" key={edge.id}><span className="badge">关系 · {edge.relation}</span><p className="mt-2 break-all muted">{edge.source_node_id} → {edge.target_node_id}</p>{edge.description && <p className="mt-1 whitespace-pre-wrap">{edge.description}</p>}</article>)}
        {result.evidence.length > 0 && <p className="text-xs muted">来源：{result.evidence.map((item) => `${item.document_id} / ${item.chunk_id}`).join("；")}</p>}
      </> : <>
        {!result.items.length && <p className="muted">没有找到相关文档。</p>}
        {result.items.map((item, index) => <article className="rounded bg-slate-900 p-3 text-xs" key={`${item.chunk_id ?? item.document_id ?? "item"}-${index}`}><div className="flex flex-wrap gap-1"><span className="badge">score {item.score?.toFixed(4) ?? "-"}</span>{item.retrieval_score !== undefined && <span className="badge">retrieval {item.retrieval_score.toFixed(4)}</span>}<span className="badge">{item.source ?? result.mode}</span></div><p className="mt-2 whitespace-pre-wrap text-sm">{item.text ?? JSON.stringify(item)}</p><p className="mt-2 break-all muted">document={item.document_id ?? "-"} · chunk={item.chunk_id ?? "-"}</p></article>)}
      </>}
    </div>}
  </section>;
}
function GraphCanvas({ workspace, kb, graphId, onBack }: { workspace: Workspace; kb: KnowledgeBase; graphId: string; onBack: () => void }) {
  const graph = useQuery({ queryKey: ["graph", workspace.id, kb.id, graphId], queryFn: () => api.graph(workspace.id, kb.id, graphId) });
  const nodes = useMemo<Node[]>(() => graph.data?.nodes.map((node, index) => ({ id: node.id, position: { x: 60 + (index % 4) * 220, y: 60 + Math.floor(index / 4) * 130 }, data: { label: <div><strong>{node.name}</strong><br /><small>{node.entity_type ?? "entity"}</small></div> }, style: { border: "1px solid #38bdf8", borderRadius: 8, padding: 8, background: "#0f172a", color: "#e2e8f0" } })) ?? [], [graph.data]);
  const edges = useMemo<Edge[]>(() => graph.data?.edges.map((edge) => ({ id: edge.id, source: edge.source_node_id, target: edge.target_node_id, label: edge.relation, style: { stroke: "#64748b" }, labelStyle: { fill: "#94a3b8" } })) ?? [], [graph.data]);
  return <div className="flex h-screen min-w-0 flex-col"><header className="flex items-center gap-3 border-b border-slate-800 p-3"><button className="btn" onClick={onBack}><ArrowLeft size={15} />返回文档</button><div className="min-w-0"><h1 className="truncate text-lg font-semibold">{graph.data?.name ?? "知识图谱"}</h1><p className="text-xs muted">只读图谱，可缩放、平移查看</p></div></header><ErrorNotice error={graph.error} /><div className="min-h-0 flex-1">{graph.data ? <ReactFlow nodes={nodes} edges={edges} fitView nodesDraggable={false} nodesConnectable={false} elementsSelectable><Background /><Controls /><MiniMap /></ReactFlow> : <div className="grid h-full place-items-center muted">正在读取图谱…</div>}</div></div>;
}

export function ChatCenter({ workspace, selected, onOpenBindings, onOpenSubagents }: { workspace?: Workspace; selected: AgentSession; onOpenBindings: () => void; onOpenSubagents?: (toolCallId: string) => void }) {
  const qc = useQueryClient(); const [text, setText] = useState(""); const [pending, setPending] = useState<PendingChat | null>(null); const [error, setError] = useState<unknown>(null); const [attachmentMenu, setAttachmentMenu] = useState(false); const [modelMenu, setModelMenu] = useState(false); const [uploadNotice, setUploadNotice] = useState(""); const fileRef = useRef<HTMLInputElement>(null); const bottomRef = useRef<HTMLDivElement>(null);
  const session = useQuery({
    queryKey: ["session", selected.id],
    queryFn: () => api.session(selected.id),
    refetchInterval: (query) => pending || (query.state.data ?? selected).latest_run?.status === "running" ? 5_000 : 30_000,
    refetchOnWindowFocus: true,
  });
  const active = session.data ?? selected;
  const messages = useQuery({ queryKey: ["messages", active?.id], queryFn: () => api.messages(active!.id), enabled: Boolean(active) });
  const models = useQuery({ queryKey: ["available-models", workspace?.id], queryFn: () => api.availableModels(workspace!.id), enabled: Boolean(workspace) });
  const readyModels = models.data?.items.filter((item) => item.status === undefined || item.status === "ready") ?? [];
  const selectedModel = readyModels.find((item) => item.provider_binding_id === active?.provider_binding_id && item.id === active?.model_id);
  const running = Boolean(pending) || active?.latest_run?.status === "running";
  useEffect(() => { bottomRef.current?.scrollIntoView({ block: "end" }); }, [messages.data?.items.length, pending?.response, pending?.content]);
  const setModel = useMutation({ mutationFn: (model: AvailableModel) => api.setSessionModelConfig(active!.id, { provider_binding_id: model.provider_binding_id, model_id: model.id, thinking_level: isLocalProvider(model.provider_id) ? null : model.thinking_levels.includes(active?.thinking_level ?? "") ? active!.thinking_level : model.thinking_levels[0] ?? null }), onSuccess: () => { setModelMenu(false); void qc.invalidateQueries({ queryKey: ["session", active?.id] }); } });
  const setThinking = useMutation({ mutationFn: (thinking: string) => api.setSessionModelConfig(active!.id, { provider_binding_id: active!.provider_binding_id!, model_id: active!.model_id!, thinking_level: thinking || null }), onSuccess: () => void qc.invalidateQueries({ queryKey: ["session", active?.id] }) });
  const uploadFile = async (file: File) => { if (!workspace) return; setError(null); try { await api.uploadWorkspaceFile(workspace.id, file.name, file, false); setUploadNotice(`已上传到工作区：${file.name}`); } catch (uploadError) { if (String(uploadError).includes("file_exists") && window.confirm("工作区已有同名文件，是否覆盖？")) { await api.uploadWorkspaceFile(workspace.id, file.name, file, true); setUploadNotice(`已覆盖工作区文件：${file.name}`); } else setError(uploadError); } finally { void qc.invalidateQueries({ queryKey: ["workspace-files", workspace.id] }); } };
  const send = async () => {
    const content = text.trim();
    if (!active || !content || running) return;
    if (!active.model_configured) { onOpenBindings(); setError(new Error("请先选择模型")); return; }
    setPending({ content, response: "", tools: [], compacting: false });
    setText(""); setError(null);
    try {
      await streamMessage(active.id, content, (event) => {
        if (event.name === "message.accepted") void qc.invalidateQueries({ queryKey: ["session", active.id] });
        if (event.name === "assistant.delta") setPending((value) => value ? { ...value, response: `${value.response}${String(event.data.delta ?? "")}` } : value);
        else if (event.name.startsWith("tool.")) {
          setPending((value) => value ? { ...value, tools: [...value.tools, event] } : value);
          if ((event.data.toolName ?? event.data.tool) === "call_subagents") void qc.invalidateQueries({ queryKey: ["subagent-sessions", active.id] });
        }
        else if (event.name === "compaction.started") setPending((value) => value ? { ...value, compacting: true } : value);
        else if (["compaction.ended", "message.completed", "message.failed", "done"].includes(event.name)) setPending((value) => value ? { ...value, compacting: false } : value);
      });
    } catch (sendError) {
      setError(sendError);
    } finally {
      setPending(null);
      void qc.invalidateQueries({ queryKey: ["messages", active.id] });
      void qc.invalidateQueries({ queryKey: ["subagent-sessions", active.id] });
      void qc.invalidateQueries({ queryKey: ["session", active.id] });
      void qc.invalidateQueries({ queryKey: ["sessions"] });
    }
  };
  if (!workspace) return <div className="grid h-screen place-items-center muted">正在读取 workspace…</div>;
  return <div className="flex h-screen min-w-0 flex-col">
    <header className="flex items-center gap-2 border-b border-slate-800 p-3">
      <h1 className="min-w-0 flex-1 truncate text-lg font-semibold" title={active.title ?? "新聊天"}>{active.title ?? "新聊天"}</h1>
      {active.knowledge_base_ids.length ? <span className="shrink-0 text-xs muted">已绑定 {active.knowledge_base_ids.length} 个知识库</span> : null}
      <div role="group" aria-label="会话 Token 统计" className="ml-auto flex shrink-0 items-center gap-3 whitespace-nowrap text-xs text-slate-400">
        <span>累计 {active.total_tokens.toLocaleString("en-US")} tokens</span>
        <span>上下文 {active.context_tokens.toLocaleString("en-US")} tokens</span>
      </div>
    </header>
    <div className="scrollbar flex-1 space-y-4 overflow-auto p-4"><ErrorNotice error={messages.error ?? error} />{active.latest_run?.error === "output_token_limit" && <p role="alert" className="rounded border border-amber-700 bg-amber-950/40 p-3 text-sm text-amber-200">上次回答达到最大输出 token，内容可能不完整。可发送“继续”让 Agent 接着回答，或要求分段输出。</p>}{messages.data?.items.map((message) => <MessageBubble key={message.id} message={message} onOpenSubagents={onOpenSubagents} />)}{pending && <PendingMessage value={pending} onOpenSubagents={onOpenSubagents} />}{uploadNotice && <p className="text-center text-xs text-emerald-300">{uploadNotice}</p>}<div ref={bottomRef} /></div>
    {active && <ChatComposer text={text} setText={setText} running={running} selectedModel={selectedModel} active={active} models={readyModels} attachmentMenu={attachmentMenu} setAttachmentMenu={setAttachmentMenu} modelMenu={modelMenu} setModelMenu={setModelMenu} fileRef={fileRef} uploadFile={uploadFile} onSend={send} onSelectModel={(model) => setModel.mutate(model)} onThinking={(thinking) => setThinking.mutate(thinking)} onOpenBindings={onOpenBindings} />}
  </div>;
}
function ChatComposer({ text, setText, running, selectedModel, active, models, attachmentMenu, setAttachmentMenu, modelMenu, setModelMenu, fileRef, uploadFile, onSend, onSelectModel, onThinking, onOpenBindings }: { text: string; setText: (value: string) => void; running: boolean; selectedModel?: AvailableModel; active: AgentSession; models: AvailableModel[]; attachmentMenu: boolean; setAttachmentMenu: (open: boolean) => void; modelMenu: boolean; setModelMenu: (open: boolean) => void; fileRef: React.RefObject<HTMLInputElement | null>; uploadFile: (file: File) => Promise<void>; onSend: () => Promise<void>; onSelectModel: (model: AvailableModel) => void; onThinking: (thinking: string) => void; onOpenBindings: () => void }) {
  return <form className="relative border-t border-slate-800 bg-slate-950 p-4" onSubmit={(event) => { event.preventDefault(); void onSend(); }}><div className="relative flex items-end gap-2 rounded-3xl border border-slate-700 bg-slate-900/80 p-2 shadow-lg shadow-slate-950/20"><button className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full hover:bg-slate-800" type="button" title="添加文件" aria-label="添加文件" disabled={running} onClick={() => { setAttachmentMenu(!attachmentMenu); setModelMenu(false); }}><Plus size={25} /></button><input ref={fileRef} className="hidden" type="file" onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadFile(file); event.currentTarget.value = ""; }} /><textarea className="min-h-10 max-h-48 min-w-0 flex-1 resize-none bg-transparent px-1 py-2 text-base outline-none placeholder:text-slate-500" value={text} onChange={(event) => { setText(event.target.value); event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 192)}px`; }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void onSend(); } }} placeholder="处理任何事务..." disabled={running} rows={1} /><div className="relative shrink-0"><button className="rounded-full bg-slate-800 px-3 py-2 text-sm hover:bg-slate-700 disabled:opacity-50" type="button" aria-label="选择模型" disabled={running} onClick={() => { setModelMenu(!modelMenu); setAttachmentMenu(false); }}>{selectedModel ? `${selectedModel.name}${active.thinking_level ? ` · ${active.thinking_level}` : ""}` : "选择模型"} <ChevronDown className="ml-1 inline" size={15} /></button>{modelMenu && <ModelMenu models={models} active={active} selected={selectedModel} onSelect={onSelectModel} onThinking={onThinking} onBindings={onOpenBindings} />}</div><button className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-sky-600 text-white hover:bg-sky-500 disabled:bg-slate-700" disabled={!text.trim() || running} title="发送"><Send size={17} /></button>{attachmentMenu && <div className="absolute bottom-[calc(100%+0.75rem)] left-0 z-30 w-72 rounded-2xl border border-slate-700 bg-slate-900 p-2 shadow-2xl"><button type="button" className="flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left hover:bg-slate-800" onClick={() => { setAttachmentMenu(false); fileRef.current?.click(); }}><Paperclip size={20} /><span><strong className="block">添加照片和文件</strong><small className="muted">从电脑上传到工作区</small></span></button></div>}</div></form>;
}
function ModelMenu({ models, active, selected, onSelect, onThinking, onBindings }: { models: AvailableModel[]; active: AgentSession; selected?: AvailableModel; onSelect: (model: AvailableModel) => void; onThinking: (thinking: string) => void; onBindings: () => void }) {
  if (!models.length) return <div className="absolute bottom-[calc(100%+0.75rem)] right-0 z-30 w-72 rounded-2xl border border-slate-700 bg-slate-900 p-3 shadow-2xl"><p className="text-sm muted">available models 为空</p><button className="btn btn-primary mt-3 w-full" type="button" onClick={onBindings}>配置 Provider Bindings</button></div>;
  const thinking = selected && !isLocalProvider(selected.provider_id) ? selected.thinking_levels : []; const activeThinking = active.thinking_level ?? thinking[0] ?? "";
  return <div className="absolute bottom-[calc(100%+0.75rem)] right-0 z-30 w-80 rounded-2xl border border-slate-700 bg-slate-900 p-2 shadow-2xl"><p className="px-2 py-1 text-xs muted">选择模型</p>{models.map((model) => <button type="button" key={`${model.provider_binding_id}:${model.id}`} className={`flex w-full items-center rounded-xl px-3 py-2 text-left hover:bg-slate-800 ${selected?.provider_binding_id === model.provider_binding_id && selected.id === model.id ? "bg-slate-800 text-sky-200" : ""}`} onClick={() => onSelect(model)}><span className="min-w-0 flex-1 truncate">{model.name}</span><small className="ml-2 muted">{model.binding_name}</small></button>)}{selected && thinking.length > 0 && <div className="mt-2 border-t border-slate-700 px-2 pt-3"><div className="flex items-center justify-between text-sm"><span>思考强度</span><span className="text-sky-200">{activeThinking}</span></div><input className="mt-3 w-full accent-sky-500" type="range" min={0} max={thinking.length - 1} step={1} value={Math.max(0, thinking.indexOf(activeThinking))} onChange={(event) => onThinking(thinking[Number(event.target.value)])} /><div className="flex justify-between text-[10px] muted">{thinking.map((level) => <span key={level}>{level}</span>)}</div></div>}</div>;
}
function MessageBubble({ message, onOpenSubagents }: { message: ChatMessage; onOpenSubagents?: (toolCallId: string) => void }) { const user = message.role === "user"; if (!user && message.role !== "assistant") return <details className="rounded bg-slate-900 p-3 text-xs"><summary>{message.tool_name ?? message.role}</summary><pre className="mt-2 overflow-auto">{JSON.stringify(message.arguments ?? message.result, null, 2)}</pre>{message.tool_name === "call_subagents" && message.tool_call_id && onOpenSubagents && <button type="button" className="btn mt-2" onClick={() => onOpenSubagents(message.tool_call_id!)}>查看 Subagent 轨迹</button>}</details>; return <article className={`flex ${user ? "justify-end" : "justify-start"}`}><div className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm ${user ? "bg-sky-700 text-white" : "bg-slate-800"}`}>{user ? <p className="whitespace-pre-wrap">{message.content}</p> : <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>}</div></article>; }
function PendingMessage({ value, onOpenSubagents }: { value: PendingChat; onOpenSubagents?: (toolCallId: string) => void }) {
  return <>
    <article className="flex justify-end"><div className="max-w-[85%] whitespace-pre-wrap rounded-2xl bg-sky-700 px-4 py-3 text-sm text-white">{value.content}</div></article>
    {value.tools.map((event, index) => <details key={`${event.name}-${index}`} className="rounded border border-sky-800/60 bg-sky-950/30 p-3 text-xs"><summary>{event.name === "tool.started" ? "正在调用" : "已完成"} {String(event.data.toolName ?? event.data.tool ?? "工具")}</summary><pre className="mt-2 overflow-auto">{JSON.stringify(event.data, null, 2)}</pre>{(event.data.toolName ?? event.data.tool) === "call_subagents" && typeof event.data.toolCallId === "string" && onOpenSubagents && <button type="button" className="btn mt-2" onClick={() => onOpenSubagents(event.data.toolCallId as string)}>查看 Subagent 轨迹</button>}</details>)}
    {(value.response || !value.compacting) && <article className="flex justify-start"><div className="max-w-[85%] rounded-2xl bg-slate-800 px-4 py-3 text-sm"><div className="markdown">{value.response ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{value.response}</ReactMarkdown> : <span className="muted">Agent 正在思考…</span>}</div></div></article>}
    {value.compacting && <p role="status" className="text-center text-xs muted">正在压缩…</p>}
  </>;
}

export function CreateSession({ open, workspace, onClose, onCreated }: { open: boolean; workspace: Workspace; onClose: () => void; onCreated: (id: string) => void }) {
  const [chosen, setChosen] = useState<string[]>([]);
  const [enableSubagents, setEnableSubagents] = useState(false);
  const kbs = useQuery({ queryKey: ["knowledge-bases", workspace.id], queryFn: () => api.knowledgeBases(workspace.id), enabled: open });
  const mutation = useMutation({
    mutationFn: () => api.createSession({
      workspace_id: workspace.id, knowledge_base_ids: chosen,
      ...(enableSubagents ? initialSubagentConfig(chosen.length > 0) : {}),
    }),
    onSuccess: (session) => { setChosen([]); setEnableSubagents(false); onCreated(session.id); },
  });
  const toggle = (id: string) => setChosen((old) => old.includes(id) ? old.filter((item) => item !== id) : [...old, id]);
  return <Modal open={open} title="新聊天" onClose={onClose}><Form onSubmit={(event) => { event.preventDefault(); mutation.mutate(); }}>
    <fieldset><legend className="text-sm">绑定知识库（可选）</legend><div className="mt-2 max-h-56 overflow-auto">{kbs.data?.items.filter((item) => item.status === "active").map((kb) => <label className="flex gap-2 p-2 text-sm" key={kb.id}><input type="checkbox" checked={chosen.includes(kb.id)} onChange={() => toggle(kb.id)} />{kb.name}</label>)}</div></fieldset>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={enableSubagents} onChange={(event) => setEnableSubagents(event.target.checked)} />启用 call_subagents 工具</label>
    {enableSubagents && <p className="rounded bg-slate-800/70 p-2 text-xs muted">将创建通用 Subagent（generalist）。创建后可在右侧 Subagent 栏修改定义。</p>}
    <ErrorNotice error={mutation.error} /><button className="btn btn-primary w-full" disabled={mutation.isPending}>创建聊天</button>
  </Form></Modal>;
}
export function AgentInfo({ workspace, session, focus, tab, setTab }: { workspace?: Workspace; session?: AgentSession; focus: SubagentFocus; tab: AgentTab; setTab: (value: AgentTab) => void }) {
  const hasSubagents = Boolean(session?.tools?.includes("call_subagents"));
  const labels: Array<[AgentTab, string]> = [["files", "工作区文件"], ["bindings", "Provider Bindings"], ...(hasSubagents ? [["subagents", "Subagent"] as [AgentTab, string]] : []), ["runtime", "Runtime"]];
  return <div className="p-3"><Tabs labels={labels} value={tab} onChange={(value) => setTab(value as AgentTab)} />{workspace && tab === "files" && <WorkspaceFiles workspace={workspace} />}{workspace && tab === "bindings" && <Bindings workspace={workspace} />}{session && hasSubagents && tab === "subagents" && <SubagentPanel key={session.id} session={session} focus={focus} />}{tab === "runtime" && <Runtime />}</div>;
}
function Tabs({ labels, value, onChange }: { labels: Array<[string, string]>; value: string; onChange: (value: string) => void }) { return <nav className="flex overflow-auto border-b border-slate-800">{labels.map(([id, label]) => <button key={id} className={`shrink-0 border-b-2 px-2 py-2 text-sm ${value === id ? "border-sky-400 text-sky-200" : "border-transparent muted"}`} onClick={() => onChange(id)}>{label}</button>)}</nav>; }
function WorkspaceFiles({ workspace }: { workspace: Workspace }) {
  const qc = useQueryClient(); const [file, setFile] = useState<File | null>(null);
  const files = useQuery({ queryKey: ["workspace-files", workspace.id], queryFn: () => api.workspaceFiles(workspace.id) });
  const refresh = () => void files.refetch();
  const upload = useMutation({ mutationFn: () => api.uploadWorkspaceFile(workspace.id, file!.name, file!, false), onSuccess: () => { setFile(null); void qc.invalidateQueries({ queryKey: ["workspace-files", workspace.id] }); } });
  const remove = useMutation({ mutationFn: (path: string) => api.deleteWorkspaceFile(workspace.id, path), onSuccess: () => void qc.invalidateQueries({ queryKey: ["workspace-files", workspace.id] }) });
  const download = useMutation({ mutationFn: async (path: string) => {
    const blob = await api.downloadWorkspaceFile(workspace.id, path);
    const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
    anchor.href = url; anchor.download = path.split("/").at(-1) || "download";
    document.body.append(anchor); anchor.click(); anchor.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 0);
  } });
  return <section className="mt-4"><div className="flex gap-2"><label className="btn min-w-0 flex-1"><FileUp size={15} />上传文件<input className="hidden" type="file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label><button className="btn" type="button" title="刷新文件" onClick={refresh} disabled={files.isFetching}><RefreshCw className={files.isFetching ? "animate-spin" : ""} size={15} />刷新</button></div>{file && <button className="btn btn-primary mt-2 w-full" onClick={() => upload.mutate()}>上传 {file.name}</button>}<ErrorNotice error={files.error ?? upload.error ?? remove.error ?? download.error} /><ul className="mt-3 space-y-1 text-sm">{files.data?.items.map((item) => <li key={item.path} className="flex items-center gap-2 rounded bg-slate-900 p-2"><span className="min-w-0 flex-1 break-all">{item.path}</span><button className="btn p-1" title={`下载 ${item.path}`} onClick={() => download.mutate(item.path)} disabled={download.isPending}><Download size={14} /></button><button className="btn p-1" title={`删除 ${item.path}`} onClick={() => remove.mutate(item.path)} disabled={remove.isPending}><Trash2 size={14} /></button></li>)}</ul></section>;
}
function isLocalProvider(providerId: string) { return providerId === "vllm" || providerId === "sglang"; }
function isLoopbackModelUrl(value: string) {
  try {
    const host = new URL(value.trim()).hostname.toLowerCase().replace(/^\[|\]$/g, "");
    return host === "localhost" || host === "::1" || /^127(?:\.\d{1,3}){3}$/.test(host);
  } catch { return false; }
}

export function Bindings({ workspace }: { workspace: Workspace }) {
  const qc = useQueryClient(); const [open, setOpen] = useState(false); const [expanded, setExpanded] = useState<string | null>(null);
  const bindings = useQuery({ queryKey: ["workspace-bindings", workspace.id], queryFn: () => api.workspaceBindings(workspace.id) });
  const remove = useMutation({ mutationFn: (id: string) => api.deleteWorkspaceBinding(workspace.id, id), onSuccess: (_, id) => { if (expanded === id) setExpanded(null); void qc.invalidateQueries({ queryKey: ["workspace-bindings", workspace.id] }); void qc.invalidateQueries({ queryKey: ["available-models", workspace.id] }); } });
  return <section className="mt-4"><button className="btn btn-primary w-full" onClick={() => setOpen(true)}><Plus size={15} />添加 Binding</button><ErrorNotice error={bindings.error ?? remove.error} /><ul className="mt-3 space-y-2">{bindings.data?.items.map((binding) => <li key={binding.id} className="rounded bg-slate-900 p-2 text-sm"><div className="flex items-center gap-2"><span className="min-w-0 flex-1 truncate">{binding.display_name}<small className="ml-1 muted">{binding.provider_id}</small></span>{isLocalProvider(binding.provider_id) && <button type="button" className="btn p-1 text-xs" aria-expanded={expanded === binding.id} onClick={() => setExpanded(expanded === binding.id ? null : binding.id)}>{expanded === binding.id ? "收起模型" : "配置模型"}</button>}<button type="button" className="btn p-1" title={`删除 ${binding.display_name}`} disabled={remove.isPending} onClick={() => remove.mutate(binding.id)}><Trash2 size={14} /></button></div>{isLocalProvider(binding.provider_id) && expanded === binding.id && <LocalBindingModels workspaceId={workspace.id} binding={binding} />}</li>)}</ul><BindingModal open={open} workspace={workspace} onClose={() => setOpen(false)} onSaved={(binding) => { setOpen(false); setExpanded(isLocalProvider(binding.provider_id) ? binding.id : null); void qc.invalidateQueries({ queryKey: ["workspace-bindings", workspace.id] }); void qc.invalidateQueries({ queryKey: ["available-models", workspace.id] }); }} /></section>;
}
function BindingModal({ open, workspace, onClose, onSaved }: { open: boolean; workspace: Workspace; onClose: () => void; onSaved: (binding: ProviderBinding) => void }) {
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers, enabled: open });
  const [provider, setProvider] = useState(""); const [name, setName] = useState(""); const [key, setKey] = useState(""); const [baseUrl, setBaseUrl] = useState("");
  const local = isLocalProvider(provider);
  const loopback = local && isLoopbackModelUrl(baseUrl);
  const mutation = useMutation({ mutationFn: () => api.createWorkspaceBinding(workspace.id, { provider_id: provider, display_name: name.trim(), api_key: key, ...(local ? { base_url: baseUrl.trim() } : {}) }), onSuccess: (binding) => { setProvider(""); setName(""); setKey(""); setBaseUrl(""); onSaved(binding); } });
  return <Modal open={open} title="Provider Binding" onClose={onClose}><Form onSubmit={(event) => { event.preventDefault(); if (!loopback) mutation.mutate(); }}><label className="block text-sm">Provider<select className="input mt-1" required value={provider} onChange={(event) => { setProvider(event.target.value); mutation.reset(); }}><option value="">选择 Provider</option>{providers.data?.providers.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}<option value="vllm">Local vLLM</option><option value="sglang">Local SGLang</option></select></label><label className="block text-sm">显示名称<input className="input mt-1" required maxLength={128} value={name} onChange={(event) => setName(event.target.value)} /></label>{local && <><label className="block text-sm">服务地址<input className="input mt-1" type="url" required placeholder="http://host.docker.internal:30018/v1" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label><p className="text-xs muted">填写服务根地址或 /v1 地址，须由 Runtime 容器访问。</p>{loopback && <p role="alert" className="text-xs text-amber-300">127.0.0.1/localhost 指向 Runtime 容器自身。请先让模型服务监听容器可达的私有地址，再填写该地址或 host.docker.internal。</p>}</>}<label className="block text-sm">API Key{local ? "（可选）" : ""}<input className="input mt-1" type="password" placeholder="API Key" autoComplete="off" required={!local} value={key} onChange={(event) => setKey(event.target.value)} /></label><ErrorNotice error={providers.error ?? mutation.error} /><button className="btn btn-primary w-full" disabled={!provider || !name.trim() || (local ? !baseUrl.trim() || loopback : !key) || mutation.isPending}>{mutation.isPending ? "正在发现模型…" : "保存"}</button></Form></Modal>;
}

function LocalBindingModels({ workspaceId, binding }: { workspaceId: string; binding: ProviderBinding }) {
  const qc = useQueryClient(); const queryKey = ["binding-models", workspaceId, binding.id];
  const models = useQuery({ queryKey, queryFn: () => api.workspaceBindingModels(workspaceId, binding.id) });
  const refresh = useMutation({ mutationFn: () => api.refreshWorkspaceBindingModels(workspaceId, binding.id), onSuccess: (result) => { qc.setQueryData(queryKey, result); void qc.invalidateQueries({ queryKey: ["available-models", workspaceId] }); } });
  return <div className="mt-3 space-y-3 border-t border-slate-700 pt-3"><p className="break-all text-xs muted">{binding.base_url}</p><button type="button" className="btn w-full text-xs" disabled={refresh.isPending} onClick={() => refresh.mutate()}><RefreshCw size={13} className={refresh.isPending ? "animate-spin" : ""} />刷新模型</button><ErrorNotice error={models.error ?? refresh.error} />{models.isPending && <p className="text-xs muted">正在读取模型目录…</p>}{models.data?.models.length === 0 && <p className="text-xs muted">服务未返回模型。</p>}{models.data?.models.map((model) => <LocalModelConfig key={model.id} workspaceId={workspaceId} bindingId={binding.id} model={model} />)}</div>;
}

function LocalModelConfig({ workspaceId, bindingId, model }: { workspaceId: string; bindingId: string; model: ProviderModel }) {
  const qc = useQueryClient(); const [contextWindow, setContextWindow] = useState(model.context_window?.toString() ?? ""); const [maxTokens, setMaxTokens] = useState(model.max_tokens?.toString() ?? ""); const [reasoning, setReasoning] = useState(model.reasoning ?? false);
  const context = Number(contextWindow); const max = Number(maxTokens);
  const valid = Number.isSafeInteger(context) && context > 1 && Number.isSafeInteger(max) && max > 0 && max < context;
  const save = useMutation({ mutationFn: () => api.configureWorkspaceBindingModel(workspaceId, bindingId, { model_id: model.id, context_window: context, max_tokens: max, reasoning }), onSuccess: () => { void qc.invalidateQueries({ queryKey: ["binding-models", workspaceId, bindingId] }); void qc.invalidateQueries({ queryKey: ["available-models", workspaceId] }); } });
  return <Form onSubmit={(event) => { event.preventDefault(); if (valid && model.status !== "unavailable") save.mutate(); }}><fieldset className="space-y-2 rounded border border-slate-700 p-2" disabled={model.status === "unavailable" || save.isPending}><legend className="px-1 text-xs">{model.name} <span className="muted">({model.id})</span></legend><span className="badge">{model.status === "ready" ? "已配置" : model.status === "unavailable" ? "不可用" : "待配置"}</span>{model.status === "unavailable" ? <p className="text-xs muted">模型已下线，保留原配置供查看。</p> : <><label className="block text-xs">上下文窗口<input className="input mt-1" type="number" min={2} step={1} value={contextWindow} onChange={(event) => setContextWindow(event.target.value)} /></label><label className="block text-xs">最大输出 token<input className="input mt-1" type="number" min={1} step={1} value={maxTokens} onChange={(event) => setMaxTokens(event.target.value)} /></label><label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={reasoning} onChange={(event) => setReasoning(event.target.checked)} />识别思考输出</label>{(contextWindow || maxTokens) && !valid && <p className="text-xs text-amber-300">请输入正整数，且最大输出 token 小于上下文窗口。</p>}<ErrorNotice error={save.error} /><button className="btn btn-primary w-full text-xs" disabled={!valid || save.isPending}>{save.isPending ? "保存中…" : "保存模型参数"}</button></>}</fieldset></Form>;
}
function Runtime() { const qc = useQueryClient(); const status = useQuery({ queryKey: ["runtime"], queryFn: api.runtime }); const recreate = useMutation({ mutationFn: api.recreateRuntime, onSuccess: () => void qc.invalidateQueries({ queryKey: ["runtime"] }) }); return <section className="mt-4 text-sm"><p className="muted">{status.data?.state ?? "读取中"}{status.data?.last_error ? ` · ${status.data.last_error}` : ""}</p><div className="mt-3 flex gap-2"><button className="btn" onClick={() => void api.startRuntime().then(() => qc.invalidateQueries({ queryKey: ["runtime"] }))}>启动</button><button className="btn" onClick={() => recreate.mutate()} disabled={recreate.isPending}>重建</button></div><ErrorNotice error={status.error ?? recreate.error} /></section>; }
