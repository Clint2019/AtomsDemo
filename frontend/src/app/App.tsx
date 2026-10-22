import { useEffect, useState } from "react";
import { errorMessage, mutate } from "../api/actions";
import { request } from "../api/client";
import { Page, Project } from "../api/types";
import { useResource } from "../api/useResource";
import { AuthPanel } from "../features/auth/AuthPanel";
import { ProjectsPanel } from "../features/projects/ProjectsPanel";
import { CatalogPanel } from "../features/catalog/CatalogPanel";
import { PublishPanel } from "../features/catalog/PublishPanel";
import { EditorPanel } from "../features/editor/EditorPanel";
import { AccountPanel } from "../features/settings/AccountPanel";
import { PublicationPanel } from "../features/publication/PublicationPanel";
import { ResourcesPanel } from "../features/resources/ResourcesPanel";

interface Identity { user_id: string; workspace_id: string | null }
interface Workspace { id: string; name: string; revision: number }
type View = "home" | "projects" | "catalog" | "account" | "editor";

function chatProjectFromPath(): string {
  return /^\/chat\/([A-Za-z0-9_-]{1,128})$/.exec(window.location.pathname)?.[1] || "";
}

function workspaceViewFromPath(): View {
  if (chatProjectFromPath()) return "editor";
  if (window.location.pathname === "/projects") return "projects";
  if (window.location.pathname === "/catalog") return "catalog";
  if (window.location.pathname === "/account") return "account";
  return "home";
}

function publicCatalogRoute(): { authorId: string; entryId: string } | null {
  const path = window.location.pathname;
  if (path === "/discover") return { authorId: "", entryId: "" };
  const author = /^\/authors\/([A-Za-z0-9_-]{1,128})$/.exec(path);
  if (author) return { authorId: author[1], entryId: "" };
  const work = /^\/works\/([A-Za-z0-9_-]{1,128})$/.exec(path);
  return work ? { authorId: "", entryId: work[1] } : null;
}

function verificationTokenFromUrl(): string {
  if (window.location.pathname !== "/verify") return "";
  return new URLSearchParams(window.location.hash.slice(1)).get("token") ||
    new URLSearchParams(window.location.search).get("token") || "";
}

function invitationTokenFromUrl(): string {
  if (window.location.pathname !== "/invitations/accept") return "";
  return new URLSearchParams(window.location.hash.slice(1)).get("token") ||
    new URLSearchParams(window.location.search).get("token") || "";
}

export function App() {
  const publicRoute = publicCatalogRoute();
  const [verificationToken, setVerificationToken] = useState(verificationTokenFromUrl);
  const [invitationToken, setInvitationToken] = useState(invitationTokenFromUrl);
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [checking, setChecking] = useState(true);
  const [workspaceList, setWorkspaceList] = useState<Workspace[]>([]);
  const [view, setView] = useState<View>(workspaceViewFromPath);
  const [catalogKind, setCatalogKind] = useState<"public_project" | "template">("public_project");
  const [projectId, setProjectId] = useState(chatProjectFromPath);
  const [initialPrompt, setInitialPrompt] = useState("");
  const [initialIntent, setInitialIntent] = useState<"answer" | "build" | undefined>();
  const [workspaceName, setWorkspaceName] = useState("");
  const [error, setError] = useState("");
  const sidebarProjects = useResource(`sidebar-projects:${identity?.workspace_id ?? ""}:${view}`,
    () => identity?.workspace_id
      ? request<Page<Project>>("/projects?limit=5")
      : Promise.resolve({ items: [], next_cursor: null }));

  async function refreshIdentity() {
    setChecking(true);
    try {
      const me = await request<Identity>("/identity/me");
      setIdentity(me);
      const workspaces = await request<Page<Workspace>>("/workspaces?limit=100");
      setWorkspaceList(workspaces.items);
    } catch (cause) {
      setIdentity(null);
      if (cause instanceof Error && !("status" in cause && cause.status === 401)) setError(errorMessage(cause));
    } finally { setChecking(false); }
  }
  useEffect(() => { if (!publicRoute) void refreshIdentity(); }, []);
  useEffect(() => {
    const syncRoute = () => {
      const chatId = chatProjectFromPath();
      setProjectId(chatId);
      setInitialPrompt("");
      setInitialIntent(undefined);
      setView(workspaceViewFromPath());
    };
    window.addEventListener("popstate", syncRoute);
    return () => window.removeEventListener("popstate", syncRoute);
  }, []);
  useEffect(() => {
    if (verificationToken || invitationToken) window.history.replaceState(null, "", "/");
  }, [verificationToken, invitationToken]);

  async function chooseWorkspace(id: string) {
    setError("");
    try { await mutate(`/workspaces/${id}/select`, "POST"); await refreshIdentity(); }
    catch (cause) { setError(errorMessage(cause)); }
  }
  async function createWorkspace() {
    if (!workspaceName.trim()) return;
    setError("");
    try {
      const workspace = await mutate<Workspace>("/workspaces", "POST", { name: workspaceName.trim() });
      setWorkspaceName(""); await chooseWorkspace(workspace.id);
    } catch (cause) { setError(errorMessage(cause)); }
  }
  async function acceptInvitation() {
    setError("");
    try {
      const membership = await mutate<{ workspace_id: string }>("/workspaces/invitations/accept", "POST",
        { token: invitationToken });
      setInvitationToken("");
      await chooseWorkspace(membership.workspace_id);
    } catch (cause) { setError(errorMessage(cause)); }
  }
  async function logout() {
    setError("");
    try {
      await request("/identity/logout", { method: "POST", body: "{}" });
      setIdentity(null); setProjectId(""); setView("home");
      window.history.replaceState(null, "", "/");
    } catch (cause) { setError(errorMessage(cause)); }
  }
  function navigate(next: View) {
    const path = next === "projects" ? "/projects" : next === "catalog" ? "/catalog" :
      next === "account" ? "/account" : next === "editor" && projectId ? `/chat/${projectId}` : "/";
    if (window.location.pathname !== path) window.history.pushState(null, "", path);
    setView(next);
  }
  function openProject(id: string, prompt = "", intent?: "answer" | "build") {
    window.history.pushState(null, "", `/chat/${id}`);
    setProjectId(id); setInitialPrompt(prompt); setInitialIntent(intent); setView("editor");
  }

  if (verificationToken) return <AuthPanel initialVerificationToken={verificationToken}
    onAuthenticated={() => { setVerificationToken(""); void refreshIdentity(); }} />;
  if (publicRoute) return <main className="main-panel public-gallery">
    <nav className="inline-form" aria-label="公开导航"><a href="/discover">公开作品</a><a href="/">登录与工作台</a></nav>
    <CatalogPanel publicView initialAuthorId={publicRoute.authorId} initialEntryId={publicRoute.entryId} />
  </main>;
  if (checking) return <main className="center-page"><p role="status">正在恢复会话…</p></main>;
  if (!identity) return <><AuthPanel onAuthenticated={refreshIdentity} /><a className="public-entry" href="/discover">浏览公开作品</a>{error && <p className="notice error">{error}</p>}</>;
  if (invitationToken) return <main className="center-page"><section className="card setup-card">
    <span className="eyebrow">工作区邀请</span><h1>接受邀请</h1>
    <p>将当前账户加入邀请对应的工作区。</p>
    <button className="primary" onClick={acceptInvitation}>接受并进入工作区</button>
    <button className="text-button" onClick={logout}>切换账户</button>
    {error && <p className="notice error" role="alert">{error}</p>}
  </section></main>;
  if (!identity.workspace_id) return <main className="center-page"><section className="card setup-card">
    <span className="eyebrow">工作区</span><h1>选择工作区</h1>
    {workspaceList.map((workspace) => <button key={workspace.id} onClick={() => chooseWorkspace(workspace.id)}>{workspace.name}</button>)}
    <div className="inline-form"><input value={workspaceName} onChange={(event) => setWorkspaceName(event.target.value)} placeholder="新工作区名称" />
      <button className="primary" onClick={createWorkspace}>创建并进入</button></div>
    {error && <p className="notice error" role="alert">{error}</p>}
  </section></main>;

  return <div className={`layout layout-${view}`}>
    <aside className="sidebar"><div className="brand" aria-label="Atoms Demo"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>Atoms</div>
      <select className="workspace-select" aria-label="工作区" value={identity.workspace_id}
        onChange={(event) => chooseWorkspace(event.target.value)}>
        {workspaceList.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}
      </select>
      <nav aria-label="主导航">
        <button className={view === "home" ? "selected" : ""} onClick={() => navigate("home")}><span aria-hidden="true">⌂</span>首页</button>
        <button className={view === "catalog" ? "selected" : ""} onClick={() => { setCatalogKind("public_project"); navigate("catalog"); }}><span aria-hidden="true">◉</span>资源</button>
        <button className={view === "projects" ? "selected" : ""} onClick={() => navigate("projects")}><span aria-hidden="true">▣</span>我的项目</button>
        {projectId && <button className={view === "editor" ? "selected" : ""} onClick={() => navigate("editor")}><span aria-hidden="true">▤</span>当前项目</button>}
      </nav>
      <div className="sidebar-projects">
        {sidebarProjects.data && sidebarProjects.data.items.length > 0 ? <>
          <div className="sidebar-projects-label">项目</div>
          {sidebarProjects.data.items.filter((project) => project.workspace_id === identity.workspace_id)
            .map((project) => <button key={project.id} title={project.name} onClick={() => openProject(project.id)}>
              <span aria-hidden="true">▤</span><span>{project.name}</span>
            </button>)}
          {sidebarProjects.data.next_cursor && <button onClick={() => navigate("projects")}>查看全部</button>}
        </> : !sidebarProjects.loading && !sidebarProjects.error && <div className="sidebar-empty">还没有项目<br />点击“首页”开始。</div>}
      </div>
      <div className="sidebar-bottom"><button className={view === "account" ? "selected" : ""} onClick={() => navigate("account")}><span className="account-avatar" aria-hidden="true">A</span><span>账户设置</span></button><button className="sidebar-logout" onClick={logout}>退出登录</button></div>
    </aside>
    <main className="main-panel">
      {error && <p role="alert" className="notice error">{error}</p>}
      {view === "home" && <ProjectsPanel mode="home" onOpen={openProject} onProjectsChanged={sidebarProjects.refresh}
        onNavigate={(next, kind) => { if (kind) setCatalogKind(kind); navigate(next); }} />}
      {view === "projects" && <ProjectsPanel mode="projects" onOpen={openProject} onProjectsChanged={sidebarProjects.refresh} />}
      {view === "catalog" && <CatalogPanel key={catalogKind} initialKind={catalogKind} onOpen={openProject} />}
      {view === "account" && <><button className="account-back" onClick={() => navigate("home")}>‹　返回</button>
        <AccountPanel key={`${identity.user_id}:${identity.workspace_id}`} userId={identity.user_id} /></>}
      {view === "editor" && projectId && <EditorPanel key={`${projectId}:${identity.workspace_id}`} projectId={projectId}
        actorId={identity.user_id} workspaceId={identity.workspace_id} initialPrompt={initialPrompt}
        initialIntent={initialIntent} onInitialPromptConsumed={() => { setInitialPrompt(""); setInitialIntent(undefined); }}
        onBack={() => navigate("projects")}
        publication={(versionId) => <PublicationPanel projectId={projectId} versionId={versionId} />}
        resources={<ResourcesPanel projectId={projectId} />}
        catalog={(versionId, visibility, onVisibilityChange) => <PublishPanel projectId={projectId} versionId={versionId}
          visibility={visibility} onVisibilityChange={onVisibilityChange} />} />}
    </main>
  </div>;
}
