import { FormEvent, useEffect, useRef, useState } from "react";
import { errorMessage, fileBase64, mutate } from "../../api/actions";
import { apiUrl, request } from "../../api/client";
import { Page } from "../../api/types";
import "./AssetsPanel.css";

interface StoredAsset {
  id: string;
  purpose: string;
  display_name: string;
  created_at: string;
  blob: { id: string; size_bytes: number; media_type: string };
}

type AssetCategory = "all" | "image" | "video" | "audio" | "file";

function categoryOf(asset: StoredAsset): Exclude<AssetCategory, "all"> {
  const mediaType = asset.blob.media_type.toLowerCase();
  if (mediaType.startsWith("image/")) return "image";
  if (mediaType.startsWith("video/")) return "video";
  if (mediaType.startsWith("audio/")) return "audio";
  return "file";
}

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const labels: Record<AssetCategory, string> = {
  all: "全部", image: "图片", video: "视频", audio: "音频", file: "文件",
};
const symbols: Record<Exclude<AssetCategory, "all">, string> = {
  image: "▧", video: "▷", audio: "♫", file: "▤",
};
const isLibraryAsset = (item: StoredAsset) => item.purpose === "asset" || item.purpose === "attachment";

export function AssetsPanel({ projectId }: { projectId: string }) {
  const [items, setItems] = useState<StoredAsset[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<AssetCategory>("all");
  const [view, setView] = useState<"cards" | "tree">("cards");
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newContent, setNewContent] = useState("");
  const uploadInput = useRef<HTMLInputElement>(null);
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    setLoading(true);
    setError("");
    setItems([]);
    setNextCursor(null);
    void request<Page<StoredAsset>>(`/storage/objects?project_id=${encodeURIComponent(projectId)}&limit=100`)
      .then((page) => {
        if (generation.current !== current) return;
        setItems(page.items.filter(isLibraryAsset));
        setNextCursor(page.next_cursor);
      })
      .catch((cause) => { if (generation.current === current) setError(errorMessage(cause)); })
      .finally(() => { if (generation.current === current) setLoading(false); });
    return () => { generation.current += 1; };
  }, [projectId, reload]);

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    const current = generation.current;
    setLoadingMore(true);
    setError("");
    try {
      const page = await request<Page<StoredAsset>>(
        `/storage/objects?project_id=${encodeURIComponent(projectId)}&limit=100&cursor=${encodeURIComponent(nextCursor)}`);
      if (generation.current !== current) return;
      setItems((previous) => {
        const known = new Set(previous.map((item) => item.id));
        return [...previous, ...page.items.filter((item) => isLibraryAsset(item) && !known.has(item.id))];
      });
      setNextCursor(page.next_cursor);
    } catch (cause) { if (generation.current === current) setError(errorMessage(cause)); }
    finally { if (generation.current === current) setLoadingMore(false); }
  }

  async function upload(file: File): Promise<boolean> {
    setBusy(true);
    setError("");
    try {
      await mutate<StoredAsset>("/storage/objects", "POST", {
        project_id: projectId,
        purpose: "asset",
        display_name: file.name,
        media_type: file.type || "application/octet-stream",
        content_base64: await fileBase64(file),
      });
      setPendingFile(null);
      if (uploadInput.current) uploadInput.current.value = "";
      setReload((value) => value + 1);
      return true;
    } catch (cause) { setError(errorMessage(cause)); return false; }
    finally { setBusy(false); }
  }

  async function createText(event: FormEvent) {
    event.preventDefault();
    const name = newName.trim();
    if (!name || !newContent.trim()) return;
    if (await upload(new File([newContent], name.endsWith(".txt") ? name : `${name}.txt`,
      { type: "text/plain" }))) {
      setCreating(false);
      setNewName("");
      setNewContent("");
    }
  }

  const visible = items.filter((item) =>
    (category === "all" || categoryOf(item) === category) &&
    item.display_name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));

  return <section className="assets-panel" aria-label="项目素材">
    <div className="assets-toolbar"><div className="assets-view-toggle" aria-label="查看方式">
      <button type="button" className={view === "cards" ? "active" : ""} aria-pressed={view === "cards"}
        onClick={() => setView("cards")}>▦ <span>卡片</span></button>
      <button type="button" className={view === "tree" ? "active" : ""} aria-pressed={view === "tree"}
        onClick={() => setView("tree")}>☷ <span>树</span></button></div></div>
    <div className="assets-filters"><label className="sr-only" htmlFor="asset-search">搜索素材</label>
      <input id="asset-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)}
        placeholder="搜索素材" />
      <label className="sr-only" htmlFor="asset-category">素材类别</label>
      <select id="asset-category" value={category} onChange={(event) => setCategory(event.target.value as AssetCategory)}>
        {(Object.keys(labels) as AssetCategory[]).map((key) => <option key={key} value={key}>{labels[key]}</option>)}
      </select></div>
    {error && <p className="assets-error" role="alert">{error} <button type="button" onClick={() => setReload((value) => value + 1)}>重试读取</button></p>}
    <div className="assets-content">
      {loading && <p className="assets-empty" role="status">正在读取素材…</p>}
      {!loading && visible.length === 0 && <p className="assets-empty">{items.length === 0 ? "暂无资源" : "没有匹配的素材"}</p>}
      {!loading && visible.length > 0 && <div className={`assets-list assets-list-${view}`}>
        {visible.map((item) => <a key={item.id} href={apiUrl(`/storage/objects/${encodeURIComponent(item.id)}/download`)}
          download={item.display_name} className="assets-item" title={`下载 ${item.display_name}`}>
          <span className="assets-icon" aria-hidden="true">{symbols[categoryOf(item)]}</span>
          <span className="assets-item-details"><strong>{item.display_name}</strong>
            <small>{labels[categoryOf(item)]} · {fileSize(item.blob.size_bytes)}</small></span>
        </a>)}</div>}
      {!loading && nextCursor && <button type="button" className="assets-more" disabled={loadingMore}
        onClick={() => void loadMore()}>{loadingMore ? "正在加载…" : "加载更多素材"}</button>}
    </div>
    {pendingFile && <div className="assets-pending" role="group" aria-label="待上传素材">
      <span title={pendingFile.name}>{pendingFile.name}</span>
      <button type="button" disabled={busy} onClick={() => void upload(pendingFile)}>重试上传</button>
      <button type="button" disabled={busy} onClick={() => setPendingFile(null)}>移除</button>
    </div>}
    {creating && <form className="assets-create" onSubmit={(event) => void createText(event)}>
      <strong>创建文本素材</strong>
      <label>名称<input required maxLength={251} value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="例如：需求说明" /></label>
      <label>内容<textarea required value={newContent} onChange={(event) => setNewContent(event.target.value)} rows={4} /></label>
      <div><button type="submit" disabled={busy || !newName.trim() || !newContent.trim()}>保存素材</button>
        <button type="button" disabled={busy} onClick={() => setCreating(false)}>取消</button></div>
    </form>}
    <div className="assets-actions"><button type="button" disabled={busy} onClick={() => uploadInput.current?.click()}>上传</button>
      <button type="button" disabled={busy} onClick={() => setCreating((value) => !value)}>创建</button>
      <input ref={uploadInput} type="file" className="sr-only" aria-label="选择要上传的素材"
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file) { setPendingFile(file); void upload(file); }
        }} /></div>
  </section>;
}
