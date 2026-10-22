import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { errorMessage, fileBase64, mutate, subscribeTaskEvents } from "../../api/actions";
import { sha256Hex } from "../../api/crypto";
import { ApiRequestError, apiUrl, request } from "../../api/client";
import { ResourceStatus } from "../../api/ResourceStatus";
import { newKey, Page, PreviewTicket, Project, ProjectHead, ShareLink, Task, TaskEvent, Version } from "../../api/types";
import { useResource } from "../../api/useResource";
import { supportsVisualHtml, VisualHtmlEditor } from "./VisualHtmlEditor";
import { StaticTsxEditor } from "./StaticTsxEditor";

interface ModelOption { key: string; label: string; available: boolean }
interface Message { id: string; role: string; content: string; attachment_ids: string[]; created_at: string }
interface GenerationRequest {
  prompt: string; attachment_ids: string[]; model: string; agent_mode: string; expected_head_revision: number;
}
interface PendingGeneration { request: GenerationRequest; idempotencyKey: string; attachmentFingerprints: string[] }
type Tab = "conversation" | "code" | "visual" | "history" | "preview" | "catalog" | "resources" | "publication";
const done = new Set(["succeeded", "failed", "interrupted", "cancelled"]);

function readPending(key: string): PendingGeneration | null {
  try {
    const value = sessionStorage.getItem(key);
    if (!value) return null;
    const parsed = JSON.parse(value) as PendingGeneration;
    return parsed?.idempotencyKey && typeof parsed.request?.prompt === "string" &&
      Number.isInteger(parsed.request.expected_head_revision) &&
      Array.isArray(parsed.attachmentFingerprints) ? parsed : null;
  } catch { return null; }
}

export function EditorPanel({ projectId, actorId, workspaceId, initialPrompt, onBack, publication, resources, catalog }: {
  projectId: string; actorId: string; workspaceId: string; initialPrompt?: string; onBack: () => void;
  publication: (versionId: string | null) => ReactNode; resources: ReactNode;
  catalog: (versionId: string | null, visibility: "private" | "public", onVisibilityChange: (project: Project) => void) => ReactNode;
}) {
  const project = useResource(`project:${projectId}`, () => request<Project>(`/projects/${projectId}`));
  const head = useResource(`head:${projectId}`, () => request<ProjectHead>(`/projects/${projectId}/head`));
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyBack, setHistoryBack] = useState<(string | null)[]>([]);
  const versions = useResource(`versions:${projectId}:${historyCursor}`, () => request<Page<Version>>(
    `/projects/${projectId}/versions?limit=100${historyCursor ? `&cursor=${encodeURIComponent(historyCursor)}` : ""}`));
  const currentVersion = useResource(`version:${head.data?.version_id || "none"}`, () => head.data?.version_id
    ? request<Version>(`/versions/${head.data.version_id}`) : Promise.resolve(null));
  const [messageCursor, setMessageCursor] = useState<string | null>(null);
  const [messageBack, setMessageBack] = useState<(string | null)[]>([]);
  const messages = useResource(`messages:${projectId}:${messageCursor}`, () => request<Page<Message>>(
    `/projects/${projectId}/messages?recent=true&limit=100${messageCursor ? `&cursor=${encodeURIComponent(messageCursor)}` : ""}`));
  const models = useResource("models", () => request<ModelOption[]>("/models"));
  const preferences = useResource("editor-preferences", () => request<{ default_model: string }>("/preferences"));
  const [tab, setTab] = useState<Tab>("conversation");
  const [prompt, setPrompt] = useState(initialPrompt || "");
  const [model, setModel] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const pendingStorageKey = `atoms-pending-generation:${actorId}:${workspaceId}:${projectId}`;
  const taskStorageKey = `atoms-confirmed-task:${actorId}:${workspaceId}:${projectId}`;
  const [pending, setPending] = useState<PendingGeneration | null>(() => readPending(pendingStorageKey));
  const [abandonChoice, setAbandonChoice] = useState<PendingGeneration | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const taskRunning = !!task && !done.has(task.state);
  const [recoveringTask, setRecoveringTask] = useState(true);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const eventCursor = useRef(0);
  const draftRevision = useRef(0);
  const tabRevision = useRef(0);
  const codeDraftRevision = useRef(0);
  const closeEventsRef = useRef<(() => void) | null>(null);
  const fileRequest = useRef(0);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const restoreConfirmButton = useRef<HTMLButtonElement>(null);
  const uploadedAttachmentIds = useRef<Map<string, string>>(new Map());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retryNotice, setRetryNotice] = useState("");
  const [selectedFile, setSelectedFile] = useState("");
  const [fileContent, setFileContent] = useState("");
  const [originalFileContent, setOriginalFileContent] = useState("");
  const [fileLoading, setFileLoading] = useState(false);
  const [loadedFile, setLoadedFile] = useState<{ path: string; versionId: string } | null>(null);
  const [restoringHead, setRestoringHead] = useState<ProjectHead | null>(null);
  const [restoreChoice, setRestoreChoice] = useState<{ version: Version; expectedRevision: number } | null>(null);
  const [editSummary, setEditSummary] = useState("");
  const [previewTicket, setPreviewTicket] = useState<PreviewTicket | null>(null);
  const [share, setShare] = useState<ShareLink | null>(null);
  const [device, setDevice] = useState<"desktop" | "tablet" | "mobile">("desktop");

  useEffect(() => { if (restoreChoice) restoreConfirmButton.current?.focus(); }, [restoreChoice]);

  function showLatestMessages() {
    setMessageCursor(null); setMessageBack([]); messages.refresh();
  }

  useEffect(() => {
    const options = models.data;
    if (!options?.length || preferences.loading || preferences.error || !preferences.data) return;
    const preferred = options.find((item) => item.key === preferences.data?.default_model && item.available);
    setModel((current) => current || preferred?.key ||
      options.find((item) => item.available)?.key || options[0].key);
  }, [models.data, preferences.data, preferences.loading, preferences.error]);
  useEffect(() => {
    if (loadedFile && loadedFile.versionId !== head.data?.version_id) {
      ++fileRequest.current; codeDraftRevision.current += 1;
      setLoadedFile(null); setSelectedFile(""); setFileContent(""); setOriginalFileContent(""); setFileLoading(false);
      setEditSummary("");
      setError("当前版本已变化，请重新打开文件后编辑。");
    }
  }, [head.data?.version_id, loadedFile]);
  useEffect(() => {
    if (restoringHead && !head.loading && head.data && head.data.revision >= restoringHead.revision) {
      setRestoringHead(null);
      if (head.data.version_id !== restoringHead.version_id) {
        setError("恢复成功，但当前版本随后被其他操作更新；请核对版本历史。");
      }
    }
  }, [restoringHead, head.loading, head.data]);
  useEffect(() => { setPreviewTicket(null); }, [head.data?.version_id]);
  useEffect(() => {
    let mounted = true;
    // The latest confirmed task is shared across tabs; a tab-local copy is a fallback
    // when browser storage is restricted. Pending submissions remain tab-local.
    function savedTaskId() {
      try {
        const shared = localStorage.getItem(taskStorageKey);
        if (shared) return shared;
      } catch { /* Storage may be disabled. */ }
      try { return sessionStorage.getItem(taskStorageKey) || ""; }
      catch { return ""; }
    }
    const initialSavedId = savedTaskId();
    async function recoverTask() {
      try {
        const active = await request<Task | null>(`/projects/${projectId}/active-task`);
        const latest = active || await request<Task | null>(`/projects/${projectId}/latest-task`);
        // A different tab may have submitted a newer task while this request was pending.
        let found = latest && savedTaskId() === initialSavedId ? latest : null;
        for (let attempt = 0; !found && mounted && attempt < 4; attempt += 1) {
          const savedId = savedTaskId();
          if (!savedId) break;
          try {
            const saved = await request<Task>(`/tasks/${savedId}`);
            if (!mounted) break;
            if (savedTaskId() !== savedId) continue;
            if (saved.project_id === projectId && saved.workspace_id === workspaceId &&
                saved.actor_id === actorId) found = saved;
          }
          catch (cause) {
            if (!mounted) break;
            if (savedTaskId() !== savedId) continue;
            if (!(cause instanceof ApiRequestError && [403, 404].includes(cause.status))) throw cause;
          }
          if (!found && savedTaskId() === savedId) {
            try { if (sessionStorage.getItem(taskStorageKey) === savedId) sessionStorage.removeItem(taskStorageKey); }
            catch { /* Storage may be disabled. */ }
            try { if (localStorage.getItem(taskStorageKey) === savedId) localStorage.removeItem(taskStorageKey); }
            catch { /* Storage may be disabled. */ }
          }
        }
        if (mounted && found) {
          setTask(found);
          if (found.actor_id === actorId &&
              (savedTaskId() === initialSavedId || savedTaskId() === found.id)) {
            try { sessionStorage.setItem(taskStorageKey, found.id); } catch { /* Storage may be disabled. */ }
            try { localStorage.setItem(taskStorageKey, found.id); } catch { /* Storage may be disabled. */ }
          }
        }
      } catch (cause) { if (mounted) setError(errorMessage(cause)); }
      finally { if (mounted) setRecoveringTask(false); }
    }
    void recoverTask();
    return () => { mounted = false; };
  }, [actorId, projectId, taskStorageKey, workspaceId]);
  useEffect(() => {
    if (!task) return;
    const closeEvents = subscribeTaskEvents(task.id, eventCursor.current, (item) => {
      if (item.sequence <= eventCursor.current) return;
      eventCursor.current = item.sequence;
      setEvents((prior) => prior.some((old) => old.sequence === item.sequence) ? prior : [...prior, item]);
      if (item.kind === "succeeded" || item.kind === "failed") closeEventsRef.current?.();
    }, () => {
      closeEventsRef.current?.();
      request<Task>(`/tasks/${task.id}`).then((updated) => {
        setTask(updated);
        head.refresh(); versions.refresh(); showLatestMessages();
      }).catch((cause) => setError(errorMessage(cause)));
    });
    closeEventsRef.current = closeEvents;
    return () => {
      closeEvents();
      if (closeEventsRef.current === closeEvents) closeEventsRef.current = null;
    };
  }, [task?.id]);
  useEffect(() => {
    if (!task || done.has(task.state)) {
      if (task?.state === "cancelled" || task?.state === "interrupted") closeEventsRef.current?.();
      return;
    }
    const interval = window.setInterval(() => {
      request<Task>(`/tasks/${task.id}`).then((updated) => {
        setTask(updated);
        if (done.has(updated.state)) { head.refresh(); versions.refresh(); showLatestMessages(); }
      }).catch((cause) => setError(errorMessage(cause)));
    }, 1500);
    return () => window.clearInterval(interval);
  }, [task?.id, task?.state]);

  function clearPending(record: PendingGeneration) {
    setPending(null);
    setAbandonChoice(null);
    try { sessionStorage.removeItem(pendingStorageKey); } catch { /* Storage may be disabled. */ }
    for (const fingerprint of record.attachmentFingerprints) {
      uploadedAttachmentIds.current.delete(fingerprint);
      try { sessionStorage.removeItem(`atoms-attachment:${projectId}:${fingerprint}`); } catch { /* Storage may be disabled. */ }
    }
  }
  function rememberTask(created: Task) {
    try { sessionStorage.setItem(taskStorageKey, created.id); } catch { /* Active task query still recovers running work. */ }
    try { localStorage.setItem(taskStorageKey, created.id); } catch { /* Active task query still recovers running work. */ }
    setTask(created);
  }
  async function submitPending(record: PendingGeneration) {
    const created = await mutate<Task>(`/projects/${projectId}/generate`, "POST", record.request, record.idempotencyKey);
    rememberTask(created);
    clearPending(record);
    eventCursor.current = 0; setEvents([]); setPrompt(""); setAttachments([]);
    if (attachmentInput.current) attachmentInput.current.value = "";
    head.refresh(); versions.refresh(); showLatestMessages();
  }
  async function retryPending() {
    if (!pending) return;
    setBusy(true); setError("");
    try { await submitPending(pending); }
    catch (cause) {
      if (cause instanceof ApiRequestError && cause.status === 409) clearPending(pending);
      setError(errorMessage(cause));
    } finally { setBusy(false); }
  }
  async function generate(event: FormEvent) {
    event.preventDefault(); setError(""); setRetryNotice(""); setBusy(true);
    let submittedRecord: PendingGeneration | null = null;
    try {
      if (pending) throw new Error("请先查找或处理上一次未确认的生成任务");
      if (taskRunning) throw new Error("当前任务仍在执行，请等待完成后再提交。");
      if (recoveringTask || !head.data || preferences.loading || preferences.error ||
          !preferences.data || !model) throw new Error("任务、版本或模型尚未就绪");
      if (attachments.length > 10) throw new Error("每次最多附加 10 个文件");
      const attachmentIds: string[] = [];
      for (const file of attachments) {
        const contentBase64 = await fileBase64(file);
        const fingerprint = await sha256Hex(`${file.name}:${file.type}:${contentBase64}`);
        const cacheKey = `atoms-attachment:${projectId}:${fingerprint}`;
        let objectId = uploadedAttachmentIds.current.get(fingerprint);
        if (!objectId) {
          try { objectId = sessionStorage.getItem(cacheKey) || undefined; } catch { /* Storage may be disabled. */ }
        }
        if (!objectId) {
          const stored = await mutate<{ id: string }>("/storage/objects", "POST", {
            project_id: projectId, purpose: "attachment", display_name: file.name,
            media_type: file.type || "application/octet-stream", content_base64: contentBase64,
          });
          objectId = stored.id;
          try { sessionStorage.setItem(cacheKey, objectId); } catch { /* Keep this page's cache. */ }
        }
        uploadedAttachmentIds.current.set(fingerprint, objectId);
        if (!attachmentIds.includes(objectId)) attachmentIds.push(objectId);
      }
      const record: PendingGeneration = { request: {
        prompt, attachment_ids: attachmentIds, model, agent_mode: "default",
        expected_head_revision: head.data.revision,
      }, idempotencyKey: newKey(), attachmentFingerprints: Array.from(uploadedAttachmentIds.current.keys()) };
      submittedRecord = record;
      setPending(record);
      try { sessionStorage.setItem(pendingStorageKey, JSON.stringify(record)); } catch { /* Keep this page's record. */ }
      await submitPending(record);
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.status === 409 && submittedRecord) clearPending(submittedRecord);
      setError(errorMessage(cause));
    }
    finally { setBusy(false); }
  }
  async function prepareFailedTaskRetry() {
    if (!task || task.state !== "failed" || task.request.agent_mode === "manual_edit" ||
        task.error_code === "usage_reconciliation_required" || pending || busy) return;
    const startingRevision = draftRevision.current;
    setBusy(true); setError(""); setRetryNotice("");
    try {
      let cursor: string | null = null;
      const seen = new Set<string>();
      let original: Message | undefined;
      while (true) {
        const page: Page<Message> = await request(`/projects/${projectId}/messages?recent=true&limit=100${
          cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
        original = page.items.find((item) => item.id === task.request.message_id);
        if (original || !page.next_cursor) break;
        if (seen.has(page.next_cursor)) throw new Error("消息分页状态异常，请刷新后重试。");
        seen.add(page.next_cursor);
        cursor = page.next_cursor;
      }
      if (!original) throw new Error("原需求已不在消息记录中，无法自动填入。");
      if (draftRevision.current !== startingRevision) {
        throw new Error("当前草稿已修改，未覆盖你的输入；如需回填请再次点击重试。");
      }
      setPrompt(original.content);
      setAttachments([]);
      if (attachmentInput.current) attachmentInput.current.value = "";
      if (models.data?.some((item) => item.key === task.request.model && item.available)) {
        setModel(task.request.model);
      }
      setRetryNotice(original.attachment_ids.length > 0
        ? "已填入原需求；原任务包含附件，请重新选择附件并检查模型后提交。"
        : "已填入原需求；检查内容和模型后，点击「提交任务」重试。");
      setTab("conversation");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function prepareFailedManualEdit() {
    if (!task || task.state !== "failed" || task.request.agent_mode !== "manual_edit" ||
        !head.data?.version_id || head.loading || restoringHead || busy || fileLoading) return;
    if (fileContent !== originalFileContent || editSummary.trim()) {
      setError("当前源码草稿尚未保存，未覆盖你的修改；请先保存或重新打开文件。");
      return;
    }
    const startingTabRevision = tabRevision.current;
    const startingCodeRevision = codeDraftRevision.current;
    const expectedHead = head.data;
    const requestNumber = ++fileRequest.current;
    setBusy(true); setError(""); setRetryNotice("");
    try {
      const candidate = await request<Version>(`/versions/${task.request.message_id}`);
      if (candidate.project_id !== projectId || candidate.build_status !== "failed" ||
          candidate.parent_id !== expectedHead.version_id) {
        throw new Error("失败候选与当前成功版本不一致，请先核对版本历史。");
      }
      const base = await request<Version>(`/versions/${expectedHead.version_id}`);
      if (base.id !== expectedHead.version_id || base.project_id !== projectId ||
          base.build_status !== "success") {
        throw new Error("当前成功版本不可读取，请刷新后重试。");
      }
      const baseFiles = new Map(base.files.map((file) => [file.path, file.blob.sha256]));
      const changed = candidate.files.filter((file) =>
        baseFiles.get(file.path) !== file.blob.sha256);
      if (changed.length !== 1 || changed[0].path.startsWith("/") ||
          !baseFiles.has(changed[0].path) ||
          base.files.some((file) => !candidate.files.some((item) => item.path === file.path))) {
        throw new Error("失败候选不是单文件修改，无法自动回填，请核对版本历史。");
      }
      const path = changed[0].path;
      const [original, failed, latestHead] = await Promise.all([
        request<{ content: string }>(`/versions/${base.id}/files?path=${encodeURIComponent(path)}`),
        request<{ content: string }>(`/versions/${candidate.id}/files?path=${encodeURIComponent(path)}`),
        request<ProjectHead>(`/projects/${projectId}/head`),
      ]);
      if (fileRequest.current !== requestNumber || tabRevision.current !== startingTabRevision ||
          codeDraftRevision.current !== startingCodeRevision) {
        throw new Error("页面已切换，未覆盖当前源码草稿；如需回填请再次点击。");
      }
      if (latestHead.version_id !== expectedHead.version_id ||
          latestHead.revision !== expectedHead.revision) {
        throw new Error("当前版本已变化，未回填失败候选；请刷新后核对版本历史。");
      }
      setSelectedFile(path);
      setOriginalFileContent(original.content);
      setFileContent(failed.content);
      setLoadedFile({ path, versionId: base.id });
      setEditSummary(`修正失败候选：${candidate.summary}`);
      codeDraftRevision.current += 1;
      setRetryNotice(`已载入失败候选的 ${path}。修正后再点击「保存并构建候选版本」；当前成功版本未变。`);
      setTab("code");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function openFile(path: string) {
    if (busy || restoringHead || head.loading || !head.data?.version_id) return;
    const versionId = head.data.version_id;
    const requestNumber = ++fileRequest.current;
    codeDraftRevision.current += 1;
    setSelectedFile(path); setLoadedFile(null); setFileContent(""); setOriginalFileContent("");
    setEditSummary(""); setFileLoading(true); setError("");
    try {
      const result = await request<{ content: string }>(`/versions/${versionId}/files?path=${encodeURIComponent(path)}`);
      if (requestNumber === fileRequest.current) {
        setFileContent(result.content); setOriginalFileContent(result.content); setLoadedFile({ path, versionId });
      }
    } catch (cause) { if (requestNumber === fileRequest.current) setError(errorMessage(cause)); }
    finally { if (requestNumber === fileRequest.current) setFileLoading(false); }
  }
  async function submitEdit() {
    if (recoveringTask || restoringHead || head.loading || !head.data?.version_id || !selectedFile || fileLoading ||
        loadedFile?.path !== selectedFile || loadedFile.versionId !== head.data.version_id) {
      setError("文件或版本已变化，请重新打开后编辑。"); return;
    }
    setBusy(true); setError("");
    try {
      const result = await mutate<Task>(`/projects/${projectId}/edits`, "POST", {
        base_version_id: head.data.version_id, edits: [{ path: selectedFile, content: fileContent }],
        summary: editSummary.trim() || `编辑 ${selectedFile}`,
      });
      eventCursor.current = 0; setEvents([]); rememberTask(result); showLatestMessages(); setTab("conversation");
      ++fileRequest.current; codeDraftRevision.current += 1;
      setLoadedFile(null); setSelectedFile(""); setFileContent(""); setOriginalFileContent("");
      setEditSummary("");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function restore(version: Version, expectedRevision: number) {
    if (busy || restoringHead || head.loading || !head.data) return;
    if (head.data.revision !== expectedRevision) {
      setRestoreChoice(null);
      setError("当前版本已变化，请重新选择要恢复的版本。");
      return;
    }
    setBusy(true); setError("");
    try {
      const restored = await mutate<ProjectHead>(`/projects/${projectId}/restore`, "POST", {
        version_id: version.id, expected_head_revision: expectedRevision,
      });
      setRestoreChoice(null);
      setRestoringHead(restored);
      ++fileRequest.current;
      codeDraftRevision.current += 1;
      setLoadedFile(null); setSelectedFile(""); setFileContent(""); setOriginalFileContent(""); setFileLoading(false);
      head.refresh(); versions.refresh();
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function openPreview() {
    if (restoringHead || head.loading || !head.data?.version_id) return;
    setBusy(true); setError("");
    try {
      const result = await mutate<PreviewTicket>(`/projects/${projectId}/preview`, "POST", { version_id: head.data.version_id });
      setPreviewTicket(result);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function createShare() {
    if (restoringHead || head.loading || !head.data?.version_id) return;
    setBusy(true); setError("");
    try { setShare(await mutate<ShareLink>(`/projects/${projectId}/shares`, "POST", { version_id: head.data.version_id })); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function revokeShare() {
    if (!share) return;
    setBusy(true); setError("");
    try { await mutate(`/shares/${share.id}`, "DELETE"); setShare(null); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  const current = currentVersion.data;

  return <section className="page"><div className="page-head"><div><button className="text-button" onClick={onBack}>← 我的项目</button>
    <h1>{project.data?.name || "项目工作台"}</h1><p className="muted">{head.data?.version_id ? `当前版本 ${head.data.version_id}` : "尚无成功版本"}</p></div>
    <div className="segmented">{(["conversation", "code", "visual", "history", "preview", "catalog", "resources", "publication"] as Tab[]).map((item) =>
      <button key={item} className={tab === item ? "active" : ""} onClick={() => { tabRevision.current += 1; setTab(item); }}>{({ conversation: "对话", code: "代码", visual: "可视化", history: "历史", preview: "预览", catalog: "目录", resources: "资源", publication: "发布" })[item]}</button>)}</div></div>
    {error && <p role="alert" className="notice error">{error}</p>}
    {retryNotice && <p role="status" className="notice">{retryNotice}</p>}
    {head.error && <p role="alert" className="notice error">版本状态读取失败：{head.error} <button onClick={head.refresh}>重试</button></p>}
    {restoringHead && <p role="status" className="notice">正在同步恢复后的版本… <button onClick={head.refresh}>重试读取</button></p>}
    {tab === "conversation" && <div className="workspace-grid"><div className="card">
      <h2>需求与修改</h2><ResourceStatus loading={messages.loading} error={messages.error} retry={messages.refresh}
        empty={messages.data?.items.length === 0}><div className="message-list">{messages.data?.items.map((message) =>
          <div className="message" key={message.id}><strong>{message.role}</strong><p>{message.content}</p></div>)}</div></ResourceStatus>
      <div className="inline-form history-nav"><button disabled={messages.loading || messageBack.length === 0} onClick={() => {
        setMessageCursor(messageBack[messageBack.length - 1]); setMessageBack(messageBack.slice(0, -1));
      }}>较新消息</button><button disabled={messages.loading || !messages.data?.next_cursor} onClick={() => {
        setMessageBack([...messageBack, messageCursor]); setMessageCursor(messages.data?.next_cursor || null);
      }}>更早消息</button>{messageCursor && <button onClick={showLatestMessages}>回到最新</button>}</div>
      {pending && <div className="notice"><strong>上次生成结果尚未确认</strong><p>{pending.request.prompt}</p>
        <div className="inline-form"><button onClick={retryPending} disabled={busy}>查找或重试原任务</button>
          <button onClick={() => setAbandonChoice(pending)} disabled={busy}>放弃追踪</button></div>
        {abandonChoice?.idempotencyKey === pending.idempotencyKey && <div role="group" aria-label="确认放弃追踪" className="notice">
          <p>可能已有任务正在执行。放弃追踪不会取消已提交的任务。确定放弃追踪并允许新的提交？</p>
          <div className="inline-form"><button className="primary" disabled={busy} onClick={() => {
            if (pending.idempotencyKey === abandonChoice.idempotencyKey) clearPending(pending);
          }}>确认放弃追踪</button>
            <button disabled={busy} onClick={() => setAbandonChoice(null)}>取消</button></div>
        </div>}</div>}
      <form onSubmit={generate} className="stack"><label>描述你要创建或修改的应用
        <textarea required rows={5} value={prompt} onChange={(event) => {
          draftRevision.current += 1; setPrompt(event.target.value);
        }} /></label>
        <label>需求附件（最多 10 个）<input ref={attachmentInput} type="file" multiple onChange={(event) => {
          draftRevision.current += 1; setAttachments(Array.from(event.target.files || []));
        }} /></label>
        {attachments.length > 0 && <p className="muted">已选择：{attachments.map((file) => file.name).join("、")}</p>}
        <div className="inline-form"><label>模型<select value={model}
          disabled={models.loading || preferences.loading || !!preferences.error || !preferences.data}
          onChange={(event) => { draftRevision.current += 1; setModel(event.target.value); }}>
          {models.data?.map((option) => <option value={option.key} key={option.key} disabled={!option.available}>{option.label}{!option.available && "（未配置）"}</option>)}</select></label>
          <button className="primary" disabled={busy || recoveringTask || taskRunning || !!pending || !!restoringHead || head.loading || preferences.loading || !!preferences.error || !preferences.data || !model || !models.data?.some((option) => option.available)}>提交任务</button></div>
      </form>{models.data?.length === 0 && <p className="notice">模型服务尚未配置。</p>}
      {models.error && <p role="alert" className="notice error">模型列表加载失败：{models.error} <button onClick={models.refresh}>重试</button></p>}
      {preferences.error && <p role="alert" className="notice error">默认模型偏好加载失败：{preferences.error} <button onClick={preferences.refresh}>重试</button></p>}</div>
      <div className="card"><h2>执行状态</h2>{task ? <><strong>{task.state}</strong><p>任务 ID：{task.id}</p>
        {task.error_code && <p className="notice error">{task.error_code}</p>}
        <div className="event-list">{events.map((event) => <p key={event.sequence}><span>{event.sequence}</span> {event.message}</p>)}</div>
        {task.state === "failed" && task.request.agent_mode !== "manual_edit" &&
          task.error_code !== "usage_reconciliation_required" &&
          <button type="button" onClick={prepareFailedTaskRetry} disabled={busy || !!pending}>用原需求重试</button>}
        {task.state === "failed" && task.request.agent_mode === "manual_edit" &&
          <button type="button" onClick={prepareFailedManualEdit}
            disabled={busy || fileLoading || head.loading || !head.data?.version_id || !!restoringHead}>继续修改失败候选</button>}
        {task.state === "failed" && task.error_code === "usage_reconciliation_required" &&
          <p className="notice">本次模型用量待核对，原任务暂不可直接重试，以免重复调用。</p>}
        {!done.has(task.state) && <button onClick={async () => { try { setTask(await mutate<Task>(`/tasks/${task.id}/cancel`, "POST")); } catch (cause) { setError(errorMessage(cause)); } }}>取消任务</button>}</>
        : <p className="muted">提交需求后在此查看真实任务状态。</p>}</div></div>}
    {tab === "code" && <div className="workspace-grid"><div className="card"><h2>源码文件</h2>
      <ResourceStatus loading={currentVersion.loading} error={currentVersion.error} retry={currentVersion.refresh}
        empty={!current}><div>
      {current?.files.map((file) => <button className="list-button" key={file.path} disabled={busy || !!restoringHead || head.loading} onClick={() => openFile(file.path)}>{file.path}</button>)}
      {current && <a className="button-link" href={apiUrl(`/versions/${current.id}/export`)}>导出源码 ZIP</a>}</div></ResourceStatus></div>
      <div className="card"><h2>{selectedFile || "选择文件"}</h2>{fileLoading ? <p>读取中…</p> : selectedFile && <>
        <textarea className="code-editor" aria-label="文件内容" value={fileContent} disabled={busy}
          onChange={(event) => {
          codeDraftRevision.current += 1; setFileContent(event.target.value);
        }} />
        <label>修改说明<input value={editSummary} disabled={busy} onChange={(event) => {
          codeDraftRevision.current += 1; setEditSummary(event.target.value);
        }} /></label>
        <button className="primary" onClick={submitEdit} disabled={busy || !loadedFile || loadedFile.versionId !== head.data?.version_id}>保存并构建候选版本</button></>}</div></div>}
    {tab === "visual" && <div className="workspace-grid"><div className="card"><h2>可视化文字文件</h2>
      <ResourceStatus loading={currentVersion.loading} error={currentVersion.error} retry={currentVersion.refresh}
        empty={!current}><div>{current?.files.filter((file) => /\.(html|tsx|jsx)$/i.test(file.path)).map((file) =>
          <button className="list-button" key={file.path} disabled={busy || !!restoringHead || head.loading} onClick={() => openFile(file.path)}>{file.path}</button>)}
          {current && !current.files.some((file) => /\.(html|tsx|jsx)$/i.test(file.path)) &&
            <p className="notice">当前版本没有 HTML、TSX 或 JSX 文件；请使用源码编辑。</p>}</div></ResourceStatus></div>
      <div className="card"><h2>{loadedFile?.path === selectedFile ? selectedFile : "选择文件"}</h2>
        {fileLoading ? <p>读取中…</p> : loadedFile?.path === selectedFile && selectedFile.toLowerCase().endsWith(".html") && <>
          {!supportsVisualHtml(fileContent) ? <p className="notice">此 HTML 包含脚本、样式或外部资源引用，无法在隔离画布中安全映射；请使用源码编辑。</p> : <>
            <VisualHtmlEditor source={fileContent} disabled={busy} onChange={(value) => {
              codeDraftRevision.current += 1; setFileContent(value);
            }} />
            <label>修改说明<input value={editSummary} disabled={busy} onChange={(event) => {
              codeDraftRevision.current += 1; setEditSummary(event.target.value);
            }} /></label>
            <button className="primary" onClick={submitEdit} disabled={busy || fileContent === originalFileContent || loadedFile.versionId !== head.data?.version_id}>保存并构建候选版本</button>
          </>}
        </>}
        {!fileLoading && loadedFile?.path === selectedFile && /\.(tsx|jsx)$/i.test(selectedFile) && <>
          <StaticTsxEditor key={`${loadedFile.versionId}:${selectedFile}`} source={fileContent}
            disabled={busy} onChange={(value) => {
            codeDraftRevision.current += 1; setFileContent(value);
          }} />
          <label>修改说明<input value={editSummary} disabled={busy} onChange={(event) => {
            codeDraftRevision.current += 1; setEditSummary(event.target.value);
          }} /></label>
          <button className="primary" onClick={submitEdit} disabled={busy || fileContent === originalFileContent || loadedFile.versionId !== head.data?.version_id}>保存并构建候选版本</button>
        </>}
      </div></div>}
    {tab === "history" && <div className="card"><h2>版本历史</h2><ResourceStatus loading={versions.loading} error={versions.error} retry={versions.refresh}
      empty={versions.data?.items.length === 0}>{versions.data?.items.map((version) => <div className="history-row" key={version.id}>
        <div><strong>{version.summary}</strong><p className="muted">{version.id} · {version.build_status} · {version.created_at}</p></div>
        <button disabled={busy || !!restoringHead || head.loading || version.build_status !== "success" || version.id === head.data?.version_id} onClick={() => {
          if (head.data) setRestoreChoice({ version, expectedRevision: head.data.revision });
        }}>恢复</button>
      </div>)}{restoreChoice && <div className="notice" role="group" aria-label="确认恢复版本">
        <p>恢复版本 {restoreChoice.version.id}？历史和业务数据会保留。</p>
        <div className="inline-form"><button ref={restoreConfirmButton} className="primary" disabled={busy || !!restoringHead || head.loading}
          onClick={() => void restore(restoreChoice.version, restoreChoice.expectedRevision)}>确认恢复</button>
          <button disabled={busy} onClick={() => setRestoreChoice(null)}>取消</button></div>
      </div>}<div className="inline-form history-nav"><button disabled={historyBack.length === 0} onClick={() => {
        setHistoryCursor(historyBack[historyBack.length - 1]); setHistoryBack(historyBack.slice(0, -1));
      }}>上一页</button><button disabled={!versions.data?.next_cursor} onClick={() => {
        setHistoryBack([...historyBack, historyCursor]); setHistoryCursor(versions.data?.next_cursor || null);
      }}>下一页</button></div></ResourceStatus></div>}
    {tab === "preview" && <div className="card"><div className="page-head"><div><h2>应用预览</h2><p>宽度切换只改变浏览器视口，不重新构建。</p></div>
      <div className="inline-form"><button className="primary" onClick={openPreview} disabled={busy || !!restoringHead || head.loading || !head.data?.version_id}>打开当前版本</button>
        <button onClick={createShare} disabled={busy || !!restoringHead || head.loading || !head.data?.version_id}>创建固定版本分享</button></div></div>
      <div className="segmented">{(["desktop", "tablet", "mobile"] as const).map((size) => <button key={size} className={device === size ? "active" : ""} onClick={() => setDevice(size)}>{size}</button>)}</div>
      {previewTicket && previewTicket.version_id === head.data?.version_id ? <iframe title="应用预览" className={`preview-frame ${device}`} src={previewTicket.url} sandbox="allow-scripts allow-forms" />
        : <p className="muted">打开当前成功版本后显示预览。</p>}
      {share && <div className="notice"><a href={share.url} target="_blank" rel="noreferrer">打开分享链接</a>
        <span className="muted"> 固定版本：{share.version_id}</span> <button onClick={revokeShare} disabled={busy}>撤销分享</button></div>}</div>}
    {tab === "publication" && publication(head.data?.version_id || null)}
    {tab === "resources" && resources}
    {tab === "catalog" && catalog(head.data?.version_id || null, project.data?.visibility || "private", project.replace)}
  </section>;
}
