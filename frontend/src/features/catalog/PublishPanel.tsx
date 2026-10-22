import { FormEvent, useState } from "react";
import { errorMessage, mutate } from "../../api/actions";
import { CatalogEntry, Project } from "../../api/types";

export function PublishPanel({ projectId, versionId, visibility, onVisibilityChange }: {
  projectId: string; versionId: string | null; visibility: "private" | "public";
  onVisibilityChange: (project: Project) => void;
}) {
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState("Productivity");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState<"public_project" | "template">("public_project");
  const [result, setResult] = useState<CatalogEntry | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function setVisibility(next: "private" | "public") {
    setBusy(true); setError("");
    try {
      const updated = await mutate<Project>(`/projects/${projectId}/visibility`, "PATCH", { visibility: next });
      setResult(null);
      onVisibilityChange(updated);
    }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function publish(event: FormEvent) {
    event.preventDefault(); if (!versionId) return;
    setBusy(true); setError("");
    try { setResult(await mutate<CatalogEntry>("/catalog", "POST", {
      project_id: projectId, version_id: versionId, kind, title, category, description,
    })); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  return <section className="card narrow"><h2>发布到发现目录</h2>
    <p>目录条目固定到当前成功版本。模板供其他用户创建副本，不包含项目成员、密钥或业务数据。</p>
    {visibility !== "public" && <div className="notice"><p>项目当前为私有，需要先改为公开。</p>
      <button onClick={() => setVisibility("public")} disabled={busy}>设为公开项目</button></div>}
    {visibility === "public" && <div className="notice"><p>项目当前为公开。设为私有后，目录中的作品和模板将不再展示。</p>
      <button onClick={() => setVisibility("private")} disabled={busy}>设为私有项目</button></div>}
    <form className="stack" onSubmit={publish}><label>名称<input required maxLength={120} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
      <label>类型<select value={kind} onChange={(event) => setKind(event.target.value as "public_project" | "template")}>
        <option value="public_project">公开作品</option><option value="template">模板</option></select></label>
      <label>分类（英文、数字、空格、下划线或连字符）<input required pattern="[A-Za-z0-9 _-]+" value={category} onChange={(event) => setCategory(event.target.value)} /></label>
      <label>简介<textarea rows={4} maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
      <button className="primary" disabled={!versionId || visibility !== "public" || busy}>发布固定版本</button></form>
    {error && <p role="alert" className="notice error">{error}</p>}
    {result && <p role="status" className="notice">目录条目已创建。<a href={`/works/${encodeURIComponent(result.id)}`}>查看作品</a></p>}
  </section>;
}
