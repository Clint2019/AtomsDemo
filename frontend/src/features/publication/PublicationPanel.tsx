import { FormEvent, useEffect, useRef, useState } from "react";
import { errorMessage, fileBase64, mutate } from "../../api/actions";
import { request } from "../../api/client";
import { ResourceStatus } from "../../api/ResourceStatus";
import { Publication } from "../../api/types";
import { useResource } from "../../api/useResource";

interface Uploaded { blob: { id: string; sha256: string; size_bytes: number; media_type: string } }

export function PublicationPanel({ projectId, versionId }: { projectId: string; versionId: string | null }) {
  const publication = useResource(`publication:${projectId}`, () => request<Publication | null>(`/projects/${projectId}/publication`));
  const [domain, setDomain] = useState("");
  const [syncDb, setSyncDb] = useState(false);
  const [publicUrl, setPublicUrl] = useState("");
  const [evidenceFile, setEvidenceFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const draftDirty = useRef(false);

  useEffect(() => {
    if (publication.loading || draftDirty.current) return;
    setDomain(publication.data?.domain || "");
    setSyncDb(Boolean(publication.data?.sync_db));
    setPublicUrl(publication.data?.public_url || "");
  }, [publication.loading, publication.data?.id, publication.data?.revision]);

  async function run(action: () => Promise<unknown>, success: string): Promise<boolean> {
    setBusy(true); setError(""); setNotice("");
    try { await action(); publication.refresh(); setNotice(success); return true; }
    catch (cause) { setError(errorMessage(cause)); return false; }
    finally { setBusy(false); }
  }
  async function prepare(event: FormEvent) {
    event.preventDefault();
    if (!versionId) return;
    const saved = await run(() => mutate(`/projects/${projectId}/publication`, "PUT", {
      version_id: versionId, environment: "production", domain: domain.trim() || null,
      sync_db: syncDb, migrate_data: false,
    }), "发布配置已保存。请先完成真实人工部署，再提交核验结果。");
    if (saved) draftDirty.current = false;
  }
  async function verify(event: FormEvent) {
    event.preventDefault();
    if (!publication.data || !evidenceFile) return;
    await run(async () => {
      const uploaded = await mutate<Uploaded>("/storage/objects", "POST", {
        project_id: projectId, purpose: "deployment_evidence", display_name: evidenceFile.name,
        media_type: evidenceFile.type || "application/octet-stream", content_base64: await fileBase64(evidenceFile),
      });
      return mutate(`/publications/${publication.data!.id}/manual-result`, "POST", {
        public_url: publicUrl, evidence: uploaded.blob,
      });
    }, "部署结果已通过服务端核验；请查看最新状态。");
  }
  const item = publication.data;
  return <div className="workspace-grid"><section className="card"><h2>发布配置</h2>
    <p>预览版本不会自动发布。此处只保存配置，部署需要在目标环境人工完成。</p>
    <form className="stack" onSubmit={prepare}><label>域名（可选）<input value={domain} disabled={busy || publication.loading || !!publication.error} onChange={(event) => { draftDirty.current = true; setDomain(event.target.value); }} placeholder="app.example.com" /></label>
      <label className="checkbox"><input type="checkbox" checked={syncDb} disabled={busy || publication.loading || !!publication.error} onChange={(event) => { draftDirty.current = true; setSyncDb(event.target.checked); }} /> 同步生产数据库资源</label>
      <p className="muted">业务数据迁移尚未实现；版本恢复也不会迁移或回滚业务数据。</p>
      <button className="primary" disabled={!versionId || busy || publication.loading || !!publication.error}>保存发布配置</button></form>
    <ResourceStatus loading={publication.loading} error={publication.error} retry={publication.refresh}>
      {item ? <div className="status-block"><strong>状态：{item.state}</strong>
        <p>目标版本：{item.version_id}</p><p>线上版本：{item.live_version_id || "无"}</p>
        <p>线上地址：{item.public_url || "尚未核验"}</p>
        {item.pending_action && <p className="notice">等待人工操作：{item.pending_action}</p>}</div>
        : <p className="muted">尚未配置发布。</p>}
    </ResourceStatus>
    {item?.state === "draft" && <button onClick={() => run(() => mutate(`/publications/${item.id}/manual-request`, "POST"), "状态已变更为 awaiting_manual。")}>申请人工部署</button>}
    {(item?.state === "live" || item?.state === "paused") && <div className="inline-form">
      {item.state === "live" && <button onClick={() => run(() => mutate(`/publications/${item.id}/state`, "PATCH", { target: "paused" }), "等待人工暂停与核验。")}>暂停</button>}
      {item.state === "paused" && <button onClick={() => run(() => mutate(`/publications/${item.id}/state`, "PATCH", { target: "live" }), "等待人工恢复与核验。")}>恢复</button>}
      <button onClick={() => run(() => mutate(`/publications/${item.id}/state`, "PATCH", { target: "unpublished" }), "等待人工撤回与核验。")}>撤回</button>
    </div>}
  </section>
  <section className="card"><h2>人工结果核验</h2><p>先部署实际产物，再提交 HTTPS 地址与凭据文件。服务端会检查站点状态和版本标记。</p>
    <form className="stack" onSubmit={verify}><label>公开地址<input required type="url" value={publicUrl} onChange={(event) => setPublicUrl(event.target.value)} placeholder="https://app.example.com" /></label>
      <label>人工操作凭据<input required type="file" onChange={(event) => setEvidenceFile(event.target.files?.[0] || null)} /></label>
      <button className="primary" disabled={item?.state !== "awaiting_manual" || busy || !evidenceFile}>提交核验</button></form>
    {error && <p className="notice error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
  </section></div>;
}
