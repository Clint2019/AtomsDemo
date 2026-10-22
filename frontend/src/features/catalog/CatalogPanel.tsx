import { FormEvent, useEffect, useRef, useState } from "react";
import { errorMessage, mutate } from "../../api/actions";
import { ApiRequestError, apiUrl, request } from "../../api/client";
import { ResourceStatus } from "../../api/ResourceStatus";
import { CatalogEntry, Page, PreviewTicket, Project } from "../../api/types";
import { useResource } from "../../api/useResource";
import { formatId } from "../../formatId";

interface PublicProfile { user_id: string; display_name: string; public_slug: string; avatar: { id: string } | null }

export function CatalogPanel({ onOpen, publicView = false, initialAuthorId = "", initialEntryId = "", initialKind = "public_project" }: {
  onOpen?: (id: string) => void; publicView?: boolean; initialAuthorId?: string; initialEntryId?: string;
  initialKind?: "public_project" | "template";
}) {
  const [kind, setKind] = useState(initialKind);
  const [category, setCategory] = useState("");
  const [sort, setSort] = useState("latest");
  const [savedOnly, setSavedOnly] = useState(false);
  const [authorId, setAuthorId] = useState(initialAuthorId);
  const [cursor, setCursor] = useState<string | null>(null);
  const [back, setBack] = useState<(string | null)[]>([]);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState("");
  const [cloneChoice, setCloneChoice] = useState<{ entry: CatalogEntry; name: string } | null>(null);
  const cloning = useRef(false);
  const entries = useResource(`catalog:${kind}:${category}:${sort}:${savedOnly}:${authorId}:${cursor}`, () => request<Page<CatalogEntry>>(
    `/catalog?kind=${kind}&category=${encodeURIComponent(category)}&sort=${sort}&saved_only=${savedOnly}&limit=100${authorId ? `&author_id=${encodeURIComponent(authorId)}` : ""}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`));
  const categories = useResource(`catalog-categories:${kind}`, () => request<string[]>(`/catalog/categories?kind=${kind}`));
  const author = useResource(`catalog-author:${authorId}`, () => authorId
    ? request<PublicProfile>(`/profiles/${authorId}`).catch((cause: unknown) => {
        if (cause instanceof ApiRequestError && cause.status === 404) return null;
        throw cause;
      }) : Promise.resolve(null));
  const [selected, setSelected] = useState<CatalogEntry | null>(null);
  const [selectedPreview, setSelectedPreview] = useState<PreviewTicket | null>(null);
  const detailRequest = useRef(0);
  function resetPage() { ++detailRequest.current; setSelected(null); setSelectedPreview(null); setCursor(null); setBack([]); }
  function chooseClone(entry: CatalogEntry) {
    if (cloning.current) return;
    setCloneChoice({ entry, name: `${entry.title} 副本` });
    setError("");
  }
  async function clone(event: FormEvent) {
    event.preventDefault();
    if (!cloneChoice || !cloneChoice.name.trim() || cloning.current) return;
    const { entry, name } = cloneChoice;
    cloning.current = true;
    setBusyId(entry.id); setError("");
    try {
      const project = await mutate<Project>("/projects/clone", "POST", {
        source_version_id: entry.version_id, source_entry_id: entry.id, target_name: name.trim(),
      });
      setCloneChoice(null);
      onOpen?.(project.id);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { cloning.current = false; setBusyId(""); }
  }
  async function openDetail(entryId: string) {
    setError("");
    const requestId = ++detailRequest.current;
    try {
      const detail = await request<CatalogEntry>(`/catalog/${entryId}`);
      if (requestId !== detailRequest.current) return;
      setSelectedPreview(null);
      setSelected(detail);
      await mutate(`/catalog/${entryId}/views`, "POST");
      entries.refresh();
    } catch (cause) { if (requestId === detailRequest.current) setError(errorMessage(cause)); }
  }
  useEffect(() => { if (initialEntryId) void openDetail(initialEntryId); }, [initialEntryId]);
  async function openPreview(entry: CatalogEntry) {
    const requestId = detailRequest.current;
    setError("");
    try {
      const ticket = await request<PreviewTicket>(`/catalog/${entry.id}/preview`);
      if (requestId === detailRequest.current && selected?.id === entry.id)
        setSelectedPreview(ticket);
    } catch (cause) { if (requestId === detailRequest.current) setError(errorMessage(cause)); }
  }
  async function save(entry: CatalogEntry) {
    setError("");
    const requestId = detailRequest.current;
    try {
      await mutate(`/catalog/${entry.id}/saved`, "PUT", { saved: !entry.saved });
      entries.refresh();
      if (selected?.id === entry.id && requestId === detailRequest.current) {
        const detail = await request<CatalogEntry>(`/catalog/${entry.id}`);
        if (requestId === detailRequest.current) setSelected(detail);
      }
    }
    catch (cause) { setError(errorMessage(cause)); }
  }
  function renderDetail() {
    if (!selected) return null;
    const entry = selected;
    return <section className="card detail-card"><button className="text-button" onClick={() => {
      ++detailRequest.current; setSelected(null); setSelectedPreview(null);
    }}>关闭详情</button>
      <h2>{entry.title}</h2><p>{entry.description}</p><p className="muted compact-id"><span title={`版本 ID：${entry.version_id}`}>版本 {formatId(entry.version_id)}</span> · <span title={`作者 ID：${entry.author_id}`}>作者 {formatId(entry.author_id)}</span></p>
      <div className="inline-form">{publicView
        ? <a href={`/authors/${encodeURIComponent(entry.author_id)}`}>查看作者主页</a>
        : <button onClick={() => { setAuthorId(entry.author_id); setSelected(null); resetPage(); }}>查看作者主页</button>}
        <button onClick={() => openPreview(entry)}>预览作品</button>
        {publicView ? <a href="/">登录后收藏或创建副本</a>
          : <button className="primary" onClick={() => chooseClone(entry)} disabled={!!busyId}>创建副本</button>}
        <a href={`/works/${encodeURIComponent(entry.id)}`}>作品链接</a></div>
      {selectedPreview?.version_id === entry.version_id &&
        <iframe title={`${entry.title} 预览`} className="preview-frame desktop" src={selectedPreview.url}
          sandbox={new URL(selectedPreview.url).hostname === window.location.hostname ? "allow-scripts allow-forms" : "allow-scripts allow-forms allow-same-origin"} />}</section>;
  }
  return <section className="page catalog-page"><div className="page-head"><h1>资源</h1></div>
    <div className="segmented catalog-tabs"><button className={kind === "public_project" ? "active" : ""} onClick={() => { setKind("public_project"); setCategory(""); resetPage(); }}>发现</button>
      <button className={kind === "template" ? "active" : ""} onClick={() => { setKind("template"); setCategory(""); resetPage(); }}>模板</button></div>
    {authorId && <div className="card"><button onClick={() => { setAuthorId(""); resetPage(); }}>查看全部作者</button>
      <ResourceStatus loading={author.loading} error={author.error} retry={author.refresh}>
      <div className="inline-form">{author.data?.avatar && <img className="avatar" src={apiUrl(`/profiles/${authorId}/avatar`)} alt="作者头像" />}
        <div><h2>{author.data?.display_name || (author.data ? "未设置名称的作者" : "作者资料不可用")}</h2><p className="muted compact-id" title={author.data?.public_slug ? undefined : `作者 ID：${authorId}`}>{author.data?.public_slug || formatId(authorId)}</p></div></div></ResourceStatus></div>}
    <div className="catalog-filters"><div className="catalog-categories">
      <button className={!category ? "active" : ""} onClick={() => { setCategory(""); resetPage(); }}>全部</button>
      {categories.data?.map((item) => <button key={item} className={category === item ? "active" : ""}
        onClick={() => { setCategory(item); resetPage(); }}>{item}</button>)}</div>
      <div className="catalog-filter-actions">{!publicView && <label className="checkbox"><input type="checkbox" checked={savedOnly} onChange={(event) => { setSavedOnly(event.target.checked); resetPage(); }} /> 已保存</label>}
        <label>排序<select value={sort} onChange={(event) => { setSort(event.target.value); resetPage(); }}><option value="latest">最新</option><option value="popular">热门</option></select></label></div></div>
    {error && <p role="alert" className="notice error">{error}</p>}
    {cloneChoice && <form className="card stack" onSubmit={clone} aria-label="创建项目副本">
      <h2>从“{cloneChoice.entry.title}”创建项目</h2>
      <label>新项目名称<input autoFocus required value={cloneChoice.name} disabled={!!busyId}
        onChange={(event) => setCloneChoice({ ...cloneChoice, name: event.target.value })} /></label>
      <div className="inline-form"><button className="primary" disabled={!!busyId || !cloneChoice.name.trim()}>确认创建</button>
        <button type="button" disabled={!!busyId} onClick={() => setCloneChoice(null)}>取消</button></div>
    </form>}
    {publicView && initialEntryId && renderDetail()}
    <ResourceStatus loading={entries.loading} error={entries.error} retry={entries.refresh}
      empty={entries.data?.items.length === 0}>
      <div className="cards">{entries.data?.items.map((entry) => <article className="card catalog-card" key={entry.id}>
        <button className="catalog-card-preview" onClick={() => openDetail(entry.id)} aria-label={`查看 ${entry.title}`}>
          <span>{entry.category || "作品"}</span><strong>{entry.title}</strong><small>{entry.description}</small>
        </button>
        <div className="catalog-card-body"><h2>{entry.title}</h2><p className="muted">浏览 {entry.views} · 收藏 {entry.saves}</p>
        <div className="inline-form"><button onClick={() => openDetail(entry.id)}>详情</button>
          {!publicView && <button onClick={() => save(entry)}>{entry.saved ? "取消收藏" : "收藏"}</button>}
          {!publicView && <button onClick={() => chooseClone(entry)} disabled={!!busyId}>从此版本创建项目</button>}</div></div>
      </article>)}</div>
    </ResourceStatus>
    <div className="inline-form history-nav"><button disabled={entries.loading || back.length === 0} onClick={() => {
      setCursor(back[back.length - 1]); setBack(back.slice(0, -1));
    }}>上一页作品</button><button disabled={entries.loading || !entries.data?.next_cursor} onClick={() => {
      setBack([...back, cursor]); setCursor(entries.data?.next_cursor || null);
    }}>下一页作品</button></div>
    {(!publicView || !initialEntryId) && renderDetail()}
  </section>;
}
