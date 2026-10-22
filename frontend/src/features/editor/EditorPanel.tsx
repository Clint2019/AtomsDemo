import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { errorMessage, fileBase64, mutate, subscribeTaskEvents } from "../../api/actions";
import { sha256Hex } from "../../api/crypto";
import { ApiRequestError, apiUrl, request } from "../../api/client";
import { ResourceStatus } from "../../api/ResourceStatus";
import { newKey, Page, PreviewTicket, Project, ProjectHead, ShareLink, Task, TaskEvent, Version } from "../../api/types";
import { useResource } from "../../api/useResource";
import { formatId } from "../../formatId";
import { classifyChatIntent } from "../../chatIntent";
import { AssetsPanel } from "./AssetsPanel";
import { CloudPanel } from "./CloudPanel";
import { GrowthPanel } from "./GrowthPanel";
import { MessageContent } from "./MessageContent";
import { MessageSearchPanel } from "./MessageSearchPanel";
import { OverviewPanel } from "./OverviewPanel";
import { supportsVisualHtml, VisualHtmlEditor } from "./VisualHtmlEditor";
import { StaticTsxEditor } from "./StaticTsxEditor";
import { AuthenticatedPreviewFrame } from "./AuthenticatedPreviewFrame";

interface ModelOption { key: string; label: string; available: boolean }
interface Message { id: string; role: string; content: string; attachment_ids: string[]; created_at: string; task_id: string | null }
interface TaskApproval { id: string; task_id: string; items: string[]; selected_indices: number[] | null; additional_instructions: string; revision: number }
interface ApprovalAttempt { selectedIndices: number[]; instructions: string; key: string }
interface GenerationRequest {
  prompt: string; attachment_ids: string[]; model: string; agent_mode: string; expected_head_revision: number;
  confirmed_build_intent?: boolean;
}
interface PendingGeneration { request: GenerationRequest; idempotencyKey: string; attachmentFingerprints: string[] }
type Tab = "conversation" | "overview" | "code" | "visual" | "history" | "preview" | "assets" | "catalog" | "resources" | "publication" | "cloud" | "growth";
const done = new Set(["succeeded", "failed", "interrupted", "cancelled"]);
const taskProgressFallback: Record<string, string> = {
  waiting_for_approval: "任务正在等待确认…",
  queued: "任务已提交，正在等待执行…",
  answering: "正在整理回答…",
  generating: "模型正在生成应用…",
  building: "正在构建应用…",
  repairing: "正在修复构建问题…",
  committing: "正在保存生成结果…",
};
const taskPhaseLabel: Record<string, string> = {
  queued: "排队中", answering: "回答中", generating: "生成中",
  building: "构建中", repairing: "修复中", committing: "保存中",
};

function elapsedLabel(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes ? `${minutes} 分 ${remainder} 秒` : `${remainder} 秒`;
}

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

export function EditorPanel({ projectId, actorId, workspaceId, initialPrompt, initialIntent, onInitialPromptConsumed, onBack, publication, resources, catalog }: {
  projectId: string; actorId: string; workspaceId: string; initialPrompt?: string; initialIntent?: "answer" | "build";
  onInitialPromptConsumed?: () => void; onBack: () => void;
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
  const [growthView, setGrowthView] = useState<"analytics" | "seo" | "ads">("analytics");
  const [messageSearchOpen, setMessageSearchOpen] = useState(false);
  const [prompt, setPrompt] = useState(initialPrompt || "");
  const [model, setModel] = useState("");
  const [agentMode, setAgentMode] = useState<"approval" | "default" | "answer">("approval");
  const [attachments, setAttachments] = useState<File[]>([]);
  const pendingStorageKey = `atoms-pending-generation:${actorId}:${workspaceId}:${projectId}`;
  const taskStorageKey = `atoms-confirmed-task:${actorId}:${workspaceId}:${projectId}`;
  const [pending, setPending] = useState<PendingGeneration | null>(() => readPending(pendingStorageKey));
  const [abandonChoice, setAbandonChoice] = useState<PendingGeneration | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const [approval, setApproval] = useState<TaskApproval | null>(null);
  const [approvalSelection, setApprovalSelection] = useState<number[]>([]);
  const [approvalInstructions, setApprovalInstructions] = useState("");
  const [approvalLoading, setApprovalLoading] = useState(false);
  const [approvalError, setApprovalError] = useState("");
  const [approvalAttempt, setApprovalAttempt] = useState<ApprovalAttempt | null>(null);
  const taskRunning = !!task && !done.has(task.state);
  const recoveringAnswer = !!task && task.state === "interrupted" &&
    task.request.agent_mode === "answer" &&
    (task.error_code === "worker_unavailable" || task.error_code === "process_restart");
  const [recoveringTask, setRecoveringTask] = useState(true);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [eventStreamError, setEventStreamError] = useState(false);
  const [taskObserved, setTaskObserved] = useState<{ id: string; at: number } | null>(null);
  const [clockNow, setClockNow] = useState(Date.now());
  const eventCursor = useRef(0);
  const chatThreadRef = useRef<HTMLDivElement>(null);
  const draftRevision = useRef(0);
  const initialSubmissionStarted = useRef(false);
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
  const [intentClarification, setIntentClarification] = useState("");
  const [selectedFile, setSelectedFile] = useState("");
  const [fileContent, setFileContent] = useState("");
  const [originalFileContent, setOriginalFileContent] = useState("");
  const [fileLoading, setFileLoading] = useState(false);
  const [loadedFile, setLoadedFile] = useState<{ path: string; versionId: string } | null>(null);
  const [restoringHead, setRestoringHead] = useState<ProjectHead | null>(null);
  const [restoreChoice, setRestoreChoice] = useState<{ version: Version; expectedRevision: number } | null>(null);
  const [editSummary, setEditSummary] = useState("");
  const [previewTicket, setPreviewTicket] = useState<PreviewTicket | null>(null);
  const [previewVersionId, setPreviewVersionId] = useState<string | null>(null);
  const displayedPreviewVersion = previewVersionId || head.data?.version_id;
  const previewRequest = useRef(0);
  const autoPreviewVersion = useRef("");
  const [share, setShare] = useState<ShareLink | null>(null);
  const [device, setDevice] = useState<"desktop" | "tablet" | "mobile">("desktop");

  useEffect(() => { if (restoreChoice) restoreConfirmButton.current?.focus(); }, [restoreChoice]);

  function showLatestMessages() {
    setMessageCursor(null); setMessageBack([]); messages.refresh();
  }

  useEffect(() => {
    if (messageCursor === null && chatThreadRef.current) {
      chatThreadRef.current.scrollTop = chatThreadRef.current.scrollHeight;
    }
  }, [messages.data, messageCursor, task?.state, events.length, tab]);

  useEffect(() => {
    setApprovalAttempt(null);
    if (!task || task.state !== "waiting_for_approval") {
      setApproval(null); setApprovalError(""); return;
    }
    let active = true;
    setApprovalLoading(true);
    void request<TaskApproval>(`/tasks/${task.id}/approval`).then((plan) => {
      if (!active) return;
      setApproval(plan);
      setApprovalSelection(plan.items.map((_, index) => index));
      setApprovalInstructions(plan.additional_instructions || "");
      setApprovalError("");
    }).catch((cause) => { if (active) setApprovalError(errorMessage(cause)); })
      .finally(() => { if (active) setApprovalLoading(false); });
    return () => { active = false; };
  }, [task?.id, task?.state]);

  async function approveTask() {
    if (!task || task.state !== "waiting_for_approval" || !approval || approvalSelection.length === 0) return;
    setApprovalLoading(true); setApprovalError("");
    const attempted = approvalAttempt || {
      selectedIndices: approvalSelection.slice().sort((a, b) => a - b),
      instructions: approvalInstructions,
      key: newKey(),
    };
    setApprovalAttempt(attempted);
    try {
      const updated = await mutate<Task>(`/tasks/${task.id}/approval`, "POST", {
        selected_indices: attempted.selectedIndices,
        additional_instructions: attempted.instructions,
      }, attempted.key);
      setTask(updated); setApproval(null); setApprovalAttempt(null);
      eventCursor.current = 0; setEvents([]);
      showLatestMessages();
    } catch (cause) { setApprovalError(errorMessage(cause)); }
    finally { setApprovalLoading(false); }
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
  useEffect(() => {
    previewRequest.current += 1;
    setPreviewVersionId(null);
    setPreviewTicket(null);
  }, [head.data?.version_id]);
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
    let streamErrorTimer: number | undefined;
    setEventStreamError(false);
    const closeEvents = subscribeTaskEvents(task.id, eventCursor.current, (item) => {
      if (item.sequence <= eventCursor.current) return;
      eventCursor.current = item.sequence;
      setEvents((prior) => prior.some((old) => old.sequence === item.sequence) ? prior : [...prior, item]);
      if (item.kind === "succeeded" || item.kind === "failed") closeEventsRef.current?.();
    }, () => {
      closeEventsRef.current?.();
      request<Task>(`/tasks/${task.id}`).then((updated) => {
        acceptTaskUpdate(updated);
        head.refresh(); versions.refresh(); showLatestMessages();
      }).catch((cause) => setError(errorMessage(cause)));
    }, (connected) => {
      if (connected) {
        window.clearTimeout(streamErrorTimer);
        streamErrorTimer = undefined;
        setEventStreamError(false);
      } else if (streamErrorTimer === undefined) {
        // Normal server stream rotation reconnects quickly; report sustained loss only.
        streamErrorTimer = window.setTimeout(() => {
          streamErrorTimer = undefined;
          setEventStreamError(true);
        }, 10000);
      }
    });
    closeEventsRef.current = closeEvents;
    return () => {
      window.clearTimeout(streamErrorTimer);
      closeEvents();
      if (closeEventsRef.current === closeEvents) closeEventsRef.current = null;
    };
  }, [task?.id]);
  useEffect(() => {
    if (!task || (done.has(task.state) && !recoveringAnswer)) {
      if (task?.state === "cancelled" || task?.state === "interrupted") closeEventsRef.current?.();
      return;
    }
    const interval = window.setInterval(() => {
      request<Task>(`/tasks/${task.id}`).then((updated) => {
        acceptTaskUpdate(updated);
        if (done.has(updated.state)) { head.refresh(); versions.refresh(); showLatestMessages(); }
      }).catch((cause) => setError(errorMessage(cause)));
    }, recoveringAnswer ? 5000 : 1500);
    return () => window.clearInterval(interval);
  }, [task?.id, task?.state, task?.error_code, recoveringAnswer]);
  useEffect(() => {
    if (!task || !taskRunning || task.state === "waiting_for_approval") return;
    const now = Date.now();
    setTaskObserved((current) => current?.id === task.id ? current : { id: task.id, at: now });
    setClockNow(now);
    const timer = window.setInterval(() => setClockNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [task?.id, task?.state, taskRunning]);

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
  function acceptTaskUpdate(updated: Task) {
    setTask((current) => {
      if (current && current.id !== updated.id) return current;
      if (current && ((typeof updated.revision === "number" && typeof current.revision === "number" &&
          updated.revision < current.revision) ||
          (done.has(current.state) && !done.has(updated.state)))) return current;
      return updated;
    });
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
  async function generate(event?: FormEvent, chosenIntent?: "answer" | "build") {
    event?.preventDefault(); setError(""); setRetryNotice("");
    const intent = chosenIntent || (agentMode === "answer" ? "answer" : classifyChatIntent(prompt));
    if (intent === "clarify") {
      setIntentClarification(prompt);
      return;
    }
    setIntentClarification(""); setBusy(true);
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
        prompt, attachment_ids: attachmentIds, model,
        agent_mode: intent === "answer" ? "answer" : agentMode === "answer" ? "approval" : agentMode,
        expected_head_revision: head.data.revision,
        ...(chosenIntent === "build" ? { confirmed_build_intent: true } : {}),
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
  useEffect(() => {
    if (!initialPrompt?.trim() || initialSubmissionStarted.current || recoveringTask || task || pending || busy ||
        !head.data || head.loading || !model || !models.data?.some((option) => option.key === model && option.available) ||
        preferences.loading || preferences.error || !preferences.data) return;
    initialSubmissionStarted.current = true;
    onInitialPromptConsumed?.();
    void generate(undefined, initialIntent);
  }, [initialPrompt, recoveringTask, task, pending, busy, head.data, head.loading,
    model, models.data, preferences.loading, preferences.error, preferences.data, initialIntent]);
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
      setAgentMode(task.request.agent_mode === "answer" ? "answer"
        : task.request.agent_mode === "default" ? "default" : "approval");
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
  async function openVersionPreview(versionId: string | null | undefined, showConversation = false) {
    if (restoringHead || head.loading || !versionId) return;
    setBusy(true); setError("");
    const requestId = ++previewRequest.current;
    try {
      const result = await mutate<PreviewTicket>(`/projects/${projectId}/preview`, "POST", { version_id: versionId });
      if (requestId !== previewRequest.current) return;
      setPreviewVersionId(versionId === head.data?.version_id ? null : versionId);
      setPreviewTicket(result);
      if (showConversation) setTab("conversation");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function openPreview() { await openVersionPreview(displayedPreviewVersion); }
  useEffect(() => {
    const versionId = head.data?.version_id;
    if (tab !== "conversation" || previewVersionId || !versionId || autoPreviewVersion.current === versionId) return;
    autoPreviewVersion.current = versionId;
    const requestId = ++previewRequest.current;
    void mutate<PreviewTicket>(`/projects/${projectId}/preview`, "POST", { version_id: versionId })
      .then((ticket) => { if (requestId === previewRequest.current) setPreviewTicket(ticket); })
      .catch((cause) => setError(errorMessage(cause)));
  }, [tab, projectId, head.data?.version_id, previewVersionId]);
  useEffect(() => {
    if (!previewTicket || previewTicket.version_id !== displayedPreviewVersion) return;
    const expiry = Date.parse(previewTicket.expires_at);
    if (!Number.isFinite(expiry)) return;
    const timer = window.setTimeout(() => {
      const requestId = ++previewRequest.current;
      void mutate<PreviewTicket>(`/projects/${projectId}/preview`, "POST", { version_id: previewTicket.version_id })
        .then((ticket) => { if (requestId === previewRequest.current) setPreviewTicket(ticket); })
        .catch((cause) => setError(errorMessage(cause)));
    }, Math.max(1000, expiry - Date.now() - 60_000));
    return () => window.clearTimeout(timer);
  }, [projectId, displayedPreviewVersion, previewTicket]);
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
  const answering = task?.request.agent_mode === "answer";
  const elapsedSeconds = task && taskObserved?.id === task.id
    ? Math.max(0, Math.floor((clockNow - taskObserved.at) / 1000)) : 0;

  return <section className="page editor-page"><div className="page-head"><div><button className="text-button" onClick={onBack} aria-label="返回我的项目">◉</button>
    <h1 title={project.data?.name || "项目工作台"}>{project.data?.name || "项目工作台"}</h1></div>
    <div className="segmented">{(["history", "conversation", "overview", "code", "visual", "assets", "preview", "catalog", "resources", "publication"] as Exclude<Tab, "cloud" | "growth">[]).map((item) => {
      const label = ({ conversation: "应用查看器", overview: "项目概览", code: "代码", visual: "可视化", history: "历史", preview: "预览", assets: "素材", catalog: "目录", resources: "资源", publication: "发布" })[item];
      const icon = ({ conversation: "▣", overview: "◈", code: "⌘", visual: "✎", history: "↶", preview: "↗", assets: "▧", catalog: "⊞", resources: "◇", publication: "↑" })[item];
      return <button key={item} type="button" aria-label={label} title={label}
        className={tab === item ? "active" : ""} onClick={() => { tabRevision.current += 1; setTab(item); }}>{icon}</button>;
    })}</div>
    <div className="editor-aux-tools"><button type="button" className={tab === "cloud" ? "active" : ""}
      aria-label="Atoms云" title="Atoms云" onClick={() => setTab("cloud")}>☁</button>
      <button type="button" className={tab === "growth" ? "active" : ""}
        aria-label="增长" title="增长" onClick={() => { setGrowthView("analytics"); setTab("growth"); }}>▥</button></div>
    <div className="editor-share-tools"><button type="button" onClick={() => setTab("preview")}>分享</button>
      <button type="button" className="editor-update-button" onClick={() => setTab("publication")}>更新</button></div></div>
    {error && <p role="alert" className="notice error">{error}</p>}
    {retryNotice && <p role="status" className="notice">{retryNotice}</p>}
    {head.error && <p role="alert" className="notice error">版本状态读取失败：{head.error} <button onClick={head.refresh}>重试</button></p>}
    {restoringHead && <p role="status" className="notice">正在同步恢复后的版本… <button onClick={head.refresh}>重试读取</button></p>}
    <div className="chat-workspace"><div className="card chat-panel">
      <div className="chat-panel-toolbar"><span>对话记录</span><button type="button" aria-expanded={messageSearchOpen}
        aria-label="搜索对话" onClick={() => setMessageSearchOpen((open) => !open)}>⌕ 搜索</button></div>
      {messageSearchOpen && <MessageSearchPanel projectId={projectId} onClose={() => setMessageSearchOpen(false)} />}
      <div className="chat-thread" ref={chatThreadRef} aria-label="项目对话记录"><ResourceStatus loading={messages.loading} error={messages.error} retry={messages.refresh}>
        {messages.data?.items.length === 0 && <div className="chat-empty"><span aria-hidden="true">✦</span><h3>从一条消息开始</h3><p>可以先询问创建方法，也可以明确要求 Atoms 创建或修改应用。</p></div>}
        {messages.data?.items.filter((message) => !(task?.state === "waiting_for_approval" &&
          message.task_id === task.id && message.role === "assistant" &&
          message.content.startsWith("待确认的任务："))).map((message) => <article className={`chat-turn chat-turn-${message.role}`} key={message.id}>
          <div className="chat-avatar" aria-hidden="true">{message.role === "user" ? "我" : "✦"}</div>
          <div className="chat-bubble"><strong>{message.role === "user" ? "你" : message.role === "assistant" ? "Atoms" : "工具"}</strong>
            <MessageContent role={message.role} content={message.content} />{message.attachment_ids.length > 0 && <small>附件 {message.attachment_ids.length} 个</small>}</div>
        </article>)}
      </ResourceStatus>
        {task?.state === "waiting_for_approval" && <div className="chat-turn chat-turn-assistant chat-approval-turn" role="group" aria-label="Agent 任务确认">
          <div className="chat-avatar" aria-hidden="true">✦</div><div className="chat-approval-card">
            <strong>批准任务</strong><p>选择要执行的任务，并补充要求。</p>
            {approvalLoading && !approval && <p role="status">正在读取任务清单…</p>}
            {approval && <label className="chat-approval-select-all"><input type="checkbox"
              checked={approvalSelection.length === approval.items.length && approval.items.length > 0}
              disabled={approvalLoading || !!approvalAttempt}
              onChange={(event) => setApprovalSelection(event.target.checked ? approval.items.map((_, index) => index) : [])} />全选</label>}
            {approval?.items.map((item, index) => <label className="chat-approval-item" key={index}>
              <input type="checkbox" checked={approvalSelection.includes(index)} disabled={approvalLoading || !!approvalAttempt}
                onChange={(event) => setApprovalSelection((current) => event.target.checked
                  ? [...current, index] : current.filter((value) => value !== index))} />
              <span>{item}</span></label>)}
            {approval && <><label className="chat-approval-other">其他要求<input value={approvalInstructions}
              disabled={approvalLoading || !!approvalAttempt} maxLength={2000} onChange={(event) => setApprovalInstructions(event.target.value)}
              placeholder="可选：补充或调整任务要求" /></label>
              <button className="primary" type="button" disabled={approvalLoading || approvalSelection.length === 0}
                onClick={() => void approveTask()}>{approvalAttempt ? "重试原批准" : "批准并开始构建"}</button></>}
            {approvalAttempt && approvalError && <p className="muted">网络结果未确认，重试会提交相同的任务选择。</p>}
            {approvalError && <p role="alert" className="notice error">{approvalError}</p>}
          </div></div>}
        {taskRunning && task?.state !== "waiting_for_approval" && <div className="chat-turn chat-turn-assistant"><div className="chat-avatar" aria-hidden="true">✦</div>
          <div className="chat-bubble"><strong>Atoms · {answering && task.state === "generating" ? "回答中" : taskPhaseLabel[task.state] || "处理中"}</strong>
            <p role="status">{answering && task.state === "generating" ? "正在生成文字回答…" : events.at(-1)?.kind === task.state
              ? events.at(-1)?.message : taskProgressFallback[task.state] || "任务正在处理…"}</p>
            <progress aria-label="任务正在执行" style={{ width: "100%" }} />
            <small aria-live="off">本页已等待 {elapsedLabel(elapsedSeconds)}；任务状态会自动刷新。</small>
          </div></div>}
      </div>
      {(messageBack.length > 0 || !!messages.data?.next_cursor) && <div className="inline-form history-nav"><button disabled={messages.loading || messageBack.length === 0} onClick={() => {
        setMessageCursor(messageBack[messageBack.length - 1]); setMessageBack(messageBack.slice(0, -1));
      }}>较新消息</button><button disabled={messages.loading || !messages.data?.next_cursor} onClick={() => {
        setMessageBack([...messageBack, messageCursor]); setMessageCursor(messages.data?.next_cursor || null);
      }}>更早消息</button>{messageCursor && <button onClick={showLatestMessages}>回到最新</button>}</div>}
      {pending && <div className="notice"><strong>上次消息提交结果尚未确认</strong><p>{pending.request.prompt}</p>
        <div className="inline-form"><button onClick={retryPending} disabled={busy}>查找或重试原任务</button>
          <button onClick={() => setAbandonChoice(pending)} disabled={busy}>放弃追踪</button></div>
        {abandonChoice?.idempotencyKey === pending.idempotencyKey && <div role="group" aria-label="确认放弃追踪" className="notice">
          <p>可能已有任务正在执行。放弃追踪不会取消已提交的任务。确定放弃追踪并允许新的提交？</p>
          <div className="inline-form"><button className="primary" disabled={busy} onClick={() => {
            if (pending.idempotencyKey === abandonChoice.idempotencyKey) clearPending(pending);
          }}>确认放弃追踪</button>
            <button disabled={busy} onClick={() => setAbandonChoice(null)}>取消</button></div>
        </div>}</div>}
      {task && <details className="chat-status" open={taskRunning || recoveringAnswer || task.state === "failed"}><summary>Agent 执行 · {task.state}</summary>
        <p className="muted compact-id" title={`任务 ID：${task.id}`}>任务编号：{formatId(task.id)}</p>
        {recoveringAnswer && <p role="status" className="notice">任务已中断，正在核对是否有可恢复的回答；状态会继续自动刷新。</p>}
        {task.error_code && <p className="notice error">{task.error_code}</p>}
        {taskRunning && eventStreamError && <p role="status" className="notice">进度事件连接仍未恢复，正在重连；任务状态仍会自动刷新。</p>}
        <div className="event-list">{events.map((event) => <p key={event.sequence}><span>{event.sequence}</span> {event.message}</p>)}</div>
        {task.state === "failed" && task.request.agent_mode !== "manual_edit" && task.error_code !== "usage_reconciliation_required" &&
          <button type="button" onClick={prepareFailedTaskRetry} disabled={busy || !!pending}>用原需求重试</button>}
        {task.state === "failed" && task.request.agent_mode === "manual_edit" &&
          <button type="button" onClick={prepareFailedManualEdit}
            disabled={busy || fileLoading || head.loading || !head.data?.version_id || !!restoringHead}>继续修改失败候选</button>}
        {task.state === "failed" && task.error_code === "usage_reconciliation_required" &&
          <p className="notice">本次模型用量待核对，原任务暂不可直接重试，以免重复调用。</p>}
        {!done.has(task.state) && <button onClick={async () => { try { setTask(await mutate<Task>(`/tasks/${task.id}/cancel`, "POST")); } catch (cause) { setError(errorMessage(cause)); } }}>取消任务</button>}
      </details>}
      {intentClarification && <div className="notice" role="group" aria-label="确认消息意图">
        <p>这条消息是想了解创建方法，还是现在开始创建？</p>
        <div className="inline-form"><button type="button" disabled={busy} onClick={() => void generate(undefined, "answer")}>了解方法</button>
          <button type="button" className="primary" disabled={busy} onClick={() => void generate(undefined, "build")}>开始创建</button></div>
      </div>}
      <form onSubmit={generate} className="chat-composer"><label className="sr-only" htmlFor="chat-prompt">给 Atoms 发送消息</label>
        <textarea id="chat-prompt" required rows={3} value={prompt} placeholder="提问，或描述要创建和修改的应用…" onChange={(event) => {
          draftRevision.current += 1; setPrompt(event.target.value); setIntentClarification("");
        }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
          event.preventDefault(); if (prompt.trim()) event.currentTarget.form?.requestSubmit();
        } }} />
        {attachments.length > 0 && <p className="muted">已选择：{attachments.map((file) => file.name).join("、")}</p>}
        <div className="chat-composer-actions"><label className="chat-attachment" title="添加附件（最多 10 个）" aria-label="添加附件（最多 10 个）">＋<input ref={attachmentInput} type="file" multiple onChange={(event) => {
          draftRevision.current += 1; setAttachments(Array.from(event.target.files || []));
        }} /></label><label className="chat-model-label">模型<select value={model}
          disabled={models.loading || preferences.loading || !!preferences.error || !preferences.data}
          onChange={(event) => { draftRevision.current += 1; setModel(event.target.value); }}>
          {models.data?.map((option) => <option value={option.key} key={option.key} disabled={!option.available}>{option.label}{!option.available && "（未配置）"}</option>)}</select></label>
          <label className="chat-mode-label">模式<select value={agentMode} onChange={(event) => setAgentMode(event.target.value as "approval" | "default" | "answer")}>
            <option value="approval">自动判断，创建前确认</option><option value="default">直接构建明确需求</option>
            <option value="answer">仅回答问题</option></select></label>
          <button className="primary" aria-label="发送消息" title="发送消息" disabled={busy || recoveringTask || taskRunning || !!pending || !!restoringHead || head.loading || preferences.loading || !!preferences.error || !preferences.data || !model || !prompt.trim() || !models.data?.some((option) => option.available)}>↑</button></div>
      </form>{models.data?.length === 0 && <p className="notice">模型服务尚未配置。</p>}
      {models.error && <p role="alert" className="notice error">模型列表加载失败：{models.error} <button onClick={models.refresh}>重试</button></p>}
      {preferences.error && <p role="alert" className="notice error">默认模型偏好加载失败：{preferences.error} <button onClick={preferences.refresh}>重试</button></p>}</div>
      <div className="editor-right-panel">{tab === "conversation" && <div className="chat-preview" aria-label="应用实时预览"><div className="chat-preview-toolbar">
        <span>{previewVersionId ? `历史版本 ${formatId(previewVersionId)}` : "应用预览"}</span><div>
          {previewVersionId && <button type="button" title="返回当前版本" onClick={() => void openVersionPreview(head.data?.version_id)} disabled={busy}>当前</button>}
          <button type="button" title="切换手机视口" aria-pressed={device === "mobile"}
            onClick={() => setDevice(device === "mobile" ? "desktop" : "mobile")}>▯</button>
          <button type="button" title="刷新预览" onClick={() => void openPreview()} disabled={!displayedPreviewVersion || busy}>↻</button>
          <button type="button" title="打开完整预览" onClick={() => setTab("preview")}>↗</button></div></div>
        {previewTicket && previewTicket.version_id === displayedPreviewVersion
          ? <AuthenticatedPreviewFrame key={`${actorId}:${previewTicket.url}`} ticket={previewTicket} title="应用实时预览"
              className={`chat-preview-frame ${device}`} />
          : <div className="chat-preview-empty"><span aria-hidden="true">◌</span><p>{head.data?.version_id ? "正在准备预览…" : "生成应用后，在这里实时预览"}</p></div>}
      </div>}
    {tab === "overview" && <OverviewPanel projectId={projectId}
      onOpenAppInfo={() => setTab("catalog")}
      onOpenPublication={() => setTab("publication")}
      onOpenGrowth={(view) => { setGrowthView(view); setTab("growth"); }}
      onOpenAssets={() => setTab("assets")} />}
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
        <div><strong>{version.summary}</strong><p className="muted compact-id" title={`版本 ID：${version.id}`}>{formatId(version.id)} · {version.build_status} · {version.created_at}</p></div>
        <button disabled={busy || version.build_status !== "success"} onClick={() => void openVersionPreview(version.id, true)}>预览</button>
        <button disabled={busy || !!restoringHead || head.loading || version.build_status !== "success" || version.id === head.data?.version_id} onClick={() => {
          if (head.data) setRestoreChoice({ version, expectedRevision: head.data.revision });
        }}>恢复</button>
      </div>)}{restoreChoice && <div className="notice" role="group" aria-label="确认恢复版本">
        <p title={`版本 ID：${restoreChoice.version.id}`}>恢复版本 {formatId(restoreChoice.version.id)}？历史和业务数据会保留。</p>
        <div className="inline-form"><button ref={restoreConfirmButton} className="primary" disabled={busy || !!restoringHead || head.loading}
          onClick={() => void restore(restoreChoice.version, restoreChoice.expectedRevision)}>确认恢复</button>
          <button disabled={busy} onClick={() => setRestoreChoice(null)}>取消</button></div>
      </div>}<div className="inline-form history-nav"><button disabled={historyBack.length === 0} onClick={() => {
        setHistoryCursor(historyBack[historyBack.length - 1]); setHistoryBack(historyBack.slice(0, -1));
      }}>上一页</button><button disabled={!versions.data?.next_cursor} onClick={() => {
        setHistoryBack([...historyBack, historyCursor]); setHistoryCursor(versions.data?.next_cursor || null);
      }}>下一页</button></div></ResourceStatus></div>}
    {tab === "preview" && <div className="card"><div className="page-head"><div><h2>应用预览</h2><p>宽度切换只改变浏览器视口，不重新构建。</p></div>
      <div className="inline-form"><button className="primary" onClick={() => void openVersionPreview(head.data?.version_id)} disabled={busy || !!restoringHead || head.loading || !head.data?.version_id}>打开当前版本</button>
        <button onClick={createShare} disabled={busy || !!restoringHead || head.loading || !head.data?.version_id}>创建固定版本分享</button></div></div>
      <div className="segmented">{(["desktop", "tablet", "mobile"] as const).map((size) => <button key={size} className={device === size ? "active" : ""} onClick={() => setDevice(size)}>{size}</button>)}</div>
      {previewTicket && previewTicket.version_id === displayedPreviewVersion ? <AuthenticatedPreviewFrame key={`${actorId}:${previewTicket.url}`} ticket={previewTicket} title="应用预览" className={`preview-frame ${device}`} />
        : <p className="muted">打开当前成功版本后显示预览。</p>}
      {share && <div className="notice"><a href={share.url} target="_blank" rel="noreferrer">打开分享链接</a>
        <span className="muted compact-id" title={`版本 ID：${share.version_id}`}> 固定版本：{formatId(share.version_id)}</span> <button onClick={revokeShare} disabled={busy}>撤销分享</button></div>}</div>}
    {tab === "publication" && publication(head.data?.version_id || null)}
    {tab === "assets" && <AssetsPanel projectId={projectId} />}
    {tab === "cloud" && <CloudPanel projectId={projectId} onBack={() => setTab("conversation")} onConnect={() => setTab("resources")} />}
    {tab === "growth" && <GrowthPanel key={growthView} initialView={growthView} onOpenResources={() => setTab("resources")} />}
    {tab === "resources" && resources}
    {tab === "catalog" && catalog(head.data?.version_id || null, project.data?.visibility || "private", project.replace)}
    </div></div>
  </section>;
}
