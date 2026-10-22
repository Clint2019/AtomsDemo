import { FormEvent, useEffect, useState } from "react";
import { mutate, errorMessage } from "../../api/actions";
import { ResourceStatus } from "../../api/ResourceStatus";
import { request } from "../../api/client";
import { Page, Project } from "../../api/types";
import { useResource } from "../../api/useResource";
import { formatId } from "../../formatId";
import { classifyChatIntent } from "../../chatIntent";

async function loadFavoriteIds(): Promise<string[]> {
  const ids: string[] = [];
  let cursor: string | null = null;
  do {
    const page: Page<Project> = await request<Page<Project>>(
      `/projects?favorites_only=true&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
    ids.push(...page.items.map((item) => item.id));
    cursor = page.next_cursor;
  } while (cursor);
  return ids;
}

export function ProjectsPanel({ onOpen, mode = "projects", onNavigate, onProjectsChanged }: {
  onOpen: (id: string, prompt?: string, intent?: "answer" | "build") => void; mode?: "home" | "projects";
  onNavigate?: (view: "projects" | "catalog", kind?: "public_project" | "template") => void;
  onProjectsChanged?: () => void;
}) {
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<"private" | "public">("private");
  const [visibilityEdited, setVisibilityEdited] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [back, setBack] = useState<(string | null)[]>([]);
  const [editingId, setEditingId] = useState("");
  const [editingName, setEditingName] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const [homeIntent, setHomeIntent] = useState<"answer" | "clarify" | null>(null);
  const [questionProjectId, setQuestionProjectId] = useState("");
  const projects = useResource(`projects:${favoritesOnly}:${cursor}`, () => request<Page<Project>>(
    `/projects?limit=100&favorites_only=${favoritesOnly}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`));
  const favoriteIds = useResource("favorite-project-ids", loadFavoriteIds);
  const preferences = useResource("project-preferences", () => request<{ default_visibility: "private" | "public" }>("/preferences"));
  useEffect(() => {
    if (preferences.data && !visibilityEdited) setVisibility(preferences.data.default_visibility);
  }, [preferences.data, visibilityEdited]);

  async function createProject(intent?: "answer" | "build") {
    setError("");
    if (preferences.loading || preferences.error || !preferences.data) {
      setError("默认可见性尚未加载，请重试后创建项目。");
      return;
    }
    setBusy(true);
    try {
      const projectName = name.trim() || prompt.trim().slice(0, 40) || "未命名项目";
      const project = await mutate<Project>("/projects", "POST", { name: projectName, visibility });
      setName(""); projects.refresh(); onProjectsChanged?.(); onOpen(project.id, prompt.trim(), intent);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }

  function create(event: FormEvent) {
    event.preventDefault();
    if (mode === "home") {
      const intent = classifyChatIntent(prompt);
      if (intent !== "build") { setHomeIntent(intent); return; }
    }
    setHomeIntent(null);
    void createProject();
  }

  async function rename(event: FormEvent, project: Project) {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      await mutate(`/projects/${project.id}/name`, "PATCH", {
        name: editingName.trim(), expected_revision: project.revision,
      });
      setEditingId(""); projects.refresh(); onProjectsChanged?.();
    } catch (cause) { setError(errorMessage(cause)); projects.refresh(); }
    finally { setBusy(false); }
  }

  async function setFavorite(project: Project) {
    setError(""); setBusy(true);
    const favorite = !favoriteIds.data?.includes(project.id);
    try {
      await mutate(`/projects/${project.id}/favorite`, "PUT", { favorite });
      favoriteIds.refresh();
      if (favoritesOnly) projects.refresh();
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }

  if (mode === "home") return <section className="home-page">
    <div className="home-center">
      <div className="home-notice">✧ &nbsp; 从一个想法开始，创造你的下一个应用</div>
      <div className="home-mascots" aria-hidden="true"><span>✦</span><span>●</span><span>✳</span><span>◕</span><span>✧</span><span>●</span><span>✦</span></div>
      <h1>你今天想创造什么？</h1>
      <form className="home-composer" onSubmit={create}>
        <textarea aria-label="描述你的想法" value={prompt} onChange={(event) => {
          setPrompt(event.target.value); setHomeIntent(null);
        }}
          placeholder="描述你想创建的网站或应用…" rows={4} />
        {homeIntent === "clarify" && <div className="notice" role="group" aria-label="确认首页消息意图">
          <p>你想了解创建方法，还是现在开始创建应用？</p>
          <div className="inline-form"><button type="button" onClick={() => setHomeIntent("answer")}>了解方法</button>
            <button type="button" className="primary" disabled={busy} onClick={() => { setHomeIntent(null); void createProject("build"); }}>开始创建</button></div>
        </div>}
        {homeIntent === "answer" && <div className="notice" role="group" aria-label="选择问答项目">
          <p>这是一个问题。请选择保存对话的项目；提问不会生成应用。</p>
          {projects.error && <p role="alert">项目列表读取失败：{projects.error} <button type="button" onClick={projects.refresh}>重试</button></p>}
          {!!projects.data?.items.length && <div className="inline-form"><select aria-label="选择问答项目" value={questionProjectId}
            onChange={(event) => setQuestionProjectId(event.target.value)}><option value="">选择已有项目</option>
            {projects.data.items.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select>
            <button type="button" disabled={!questionProjectId} onClick={() => onOpen(questionProjectId, prompt.trim(), "answer")}>在此项目提问</button></div>}
          <button type="button" disabled={busy || preferences.loading || !!preferences.error || !preferences.data}
            onClick={() => void createProject("answer")}>新建空项目并提问</button>
        </div>}
        {showOptions && <div className="home-options">
          <label>项目名称<input value={name} onChange={(event) => setName(event.target.value)} placeholder="可选，默认使用需求开头" /></label>
          <label>可见性<select value={visibility} disabled={preferences.loading || !!preferences.error || !preferences.data}
            onChange={(event) => { setVisibilityEdited(true); setVisibility(event.target.value as "private" | "public"); }}>
            <option value="private">私有</option><option value="public">公开</option></select></label>
        </div>}
        <div className="home-composer-actions">
          <button className="round-button" type="button" aria-label="项目选项" aria-expanded={showOptions} onClick={() => setShowOptions(!showOptions)}>＋</button>
          <div><button className="build-mode" type="button" onClick={() => setShowOptions(!showOptions)} aria-expanded={showOptions}>构建⌄</button><button className="send-button" aria-label="创建项目" disabled={busy || !prompt.trim() || preferences.loading || !!preferences.error || !preferences.data}>{busy ? "…" : "↑"}</button></div>
        </div>
      </form>
      <div className="home-connectors">♧ &nbsp; 将你的工具连接到 Atoms <span>▣ &nbsp; ◈ &nbsp; ◉ &nbsp; ✦</span></div>
      {preferences.error && <p role="alert" className="notice error">默认可见性加载失败：{preferences.error} <button onClick={preferences.refresh}>重试</button></p>}
      {error && <p role="alert" className="notice error">{error}</p>}
    </div>
    <div className="home-bottom"><button onClick={() => onNavigate?.("catalog", "public_project")}>发现</button>
      <button onClick={() => onNavigate?.("projects")}>我的项目</button>
      <button onClick={() => onNavigate?.("catalog", "template")}>模板</button></div>
  </section>;

  return <section className="page projects-page"><div className="page-head"><h1>我的项目</h1>
    <button onClick={() => setShowCreate(!showCreate)}>{showCreate ? "收起" : "＋ 新建项目"}</button></div>
    <div className="project-tabs"><button className={!favoritesOnly ? "active" : ""} onClick={() => { setFavoritesOnly(false); setCursor(null); setBack([]); }}>全部</button>
      <button className={favoritesOnly ? "active" : ""} onClick={() => { setFavoritesOnly(true); setCursor(null); setBack([]); }}>已收藏</button></div>
    {showCreate && <form className="card inline-form create-project-form" onSubmit={create}>
      <label>项目名称<input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：客户反馈台" /></label>
      <label>可见性<select value={visibility} disabled={preferences.loading || !!preferences.error || !preferences.data}
        onChange={(event) => { setVisibilityEdited(true); setVisibility(event.target.value as "private" | "public"); }}>
        <option value="private">私有</option><option value="public">公开</option></select></label>
      <label className="wide">需求描述（可选）<textarea rows={3} value={prompt} onChange={(event) => setPrompt(event.target.value)}
        placeholder="例如：做一个求职进度管理工具，能新增公司、筛选状态并显示统计" /></label>
      <button className="primary" disabled={busy || preferences.loading || !!preferences.error || !preferences.data}>创建项目</button>
    </form>}
    {preferences.error && <p role="alert" className="notice error">默认可见性加载失败：{preferences.error} <button onClick={preferences.refresh}>重试</button></p>}
    {error && <p role="alert" className="notice error">{error}</p>}
    {favoriteIds.error && <p role="alert" className="notice error">收藏状态加载失败：{favoriteIds.error} <button onClick={favoriteIds.refresh}>重试</button></p>}
    <ResourceStatus loading={projects.loading} error={projects.error} retry={projects.refresh}>
      {projects.data?.items.length === 0 ? <div className="project-empty"><div className="empty-mascot" aria-hidden="true">◕</div><p>{favoritesOnly ? "还没有收藏的项目" : "还没有项目"}</p></div> :
      <div className="cards">{projects.data?.items.map((project) => <article className="card" key={project.id}>
        <span className="eyebrow">{project.visibility === "public" ? "公开" : "私有"}</span>
        {editingId === project.id ? <form className="inline-form" onSubmit={(event) => rename(event, project)}>
          <input aria-label="新项目名称" required maxLength={120} value={editingName} onChange={(event) => setEditingName(event.target.value)} />
          <button disabled={busy || !editingName.trim()}>保存</button>
          <button type="button" onClick={() => setEditingId("")}>取消</button>
        </form> : <h2>{project.name}</h2>}
        <p className="muted compact-id" title={`项目 ID：${project.id}`}>项目编号 {formatId(project.id)}</p>
        <div className="inline-form"><button className="primary" onClick={() => onOpen(project.id)}>打开项目</button>
          <button disabled={busy || favoriteIds.loading || !!favoriteIds.error} onClick={() => setFavorite(project)}>
            {favoriteIds.data?.includes(project.id) ? "取消收藏" : "收藏"}</button>
          <button disabled={busy} onClick={() => { setEditingId(project.id); setEditingName(project.name); }}>重命名</button></div>
      </article>)}</div>}
    </ResourceStatus>
    <div className="inline-form history-nav"><button disabled={projects.loading || back.length === 0} onClick={() => {
        setCursor(back[back.length - 1]); setBack(back.slice(0, -1));
      }}>上一页</button><button disabled={projects.loading || !projects.data?.next_cursor} onClick={() => {
        setBack([...back, cursor]); setCursor(projects.data?.next_cursor || null);
      }}>下一页</button></div>
  </section>;
}
