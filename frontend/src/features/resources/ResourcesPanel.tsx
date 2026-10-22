import { FormEvent, useState } from "react";
import { errorMessage, mutate } from "../../api/actions";
import { request } from "../../api/client";
import { ResourceStatus } from "../../api/ResourceStatus";
import { useResource } from "../../api/useResource";
import { newKey, Page } from "../../api/types";

interface AppResource { id: string; environment: string; kind: string; name: string; status: string; revision: number }

export function ResourcesPanel({ projectId }: { projectId: string }) {
  const [environment, setEnvironment] = useState<"development" | "production">("development");
  const [kind, setKind] = useState("database");
  const [name, setName] = useState("");
  const [draftId, setDraftId] = useState(newKey);
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const resources = useResource(`resources:${projectId}:${environment}`,
    () => request<AppResource[]>(`/projects/${projectId}/resources?environment=${environment}`));
  const users = useResource(`app-users:${projectId}`,
    () => request<Page<string>>(`/projects/${projectId}/app-users?limit=100`));

  async function create(event: FormEvent) {
    event.preventDefault(); setError(""); setNotice(""); setBusy(true);
    try { await mutate(`/projects/${projectId}/resources`, "PUT", {
      id: draftId, environment, kind, name, status: "configured", revision: 0,
    }); setName(""); setDraftId(newKey()); resources.refresh(); setNotice("资源配置已保存。"); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }
  async function update(resource: AppResource, status: string) {
    setError("");
    try { await mutate(`/projects/${projectId}/resources`, "PUT", { ...resource, status }); resources.refresh(); }
    catch (cause) { setError(errorMessage(cause)); }
  }
  async function setResourceSecret(resource: AppResource) {
    if (!secret) return;
    setError(""); setBusy(true);
    try { await mutate(`/resources/${resource.id}/secret`, "PUT", { secret }); setSecret(""); setNotice("密钥已保存；不会在页面回显。"); resources.refresh(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  }

  return <div className="workspace-grid"><section className="card"><div className="page-head"><h2>应用资源</h2>
    <select aria-label="资源环境" value={environment} onChange={(event) => {
      setEnvironment(event.target.value as "development" | "production"); setDraftId(newKey());
    }}>
      <option value="development">开发</option><option value="production">生产</option></select></div>
    {error && <p className="notice error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    <ResourceStatus loading={resources.loading} error={resources.error} retry={resources.refresh} empty={resources.data?.length === 0}>
      {resources.data?.map((resource) => <div className="history-row" key={resource.id}><div><strong>{resource.name}</strong><p>{resource.kind} · {resource.status}</p>
        {resource.kind === "secret" && <div className="inline-form"><input type="password" aria-label="新密钥" value={secret} onChange={(event) => setSecret(event.target.value)} />
          <button disabled={busy || !secret} onClick={() => setResourceSecret(resource)}>设置密钥</button></div>}</div>
        <button onClick={() => update(resource, resource.status === "configured" ? "disabled" : "configured")}>{resource.status === "configured" ? "禁用" : "启用"}</button></div>)}</ResourceStatus>
  </section>
  <section className="card"><h2>添加资源</h2><form className="stack" onSubmit={create}><label>名称<input required value={name} onChange={(event) => {
      setName(event.target.value); setDraftId(newKey());
    }} /></label>
    <label>类型<select value={kind} onChange={(event) => { setKind(event.target.value); setDraftId(newKey()); }}>
      <option value="database">数据库</option><option value="authentication">身份</option><option value="storage">存储</option><option value="secret">密钥</option></select></label>
    <button className="primary" disabled={busy}>添加配置</button></form>
    <h2>应用用户</h2><ResourceStatus loading={users.loading} error={users.error} retry={users.refresh} empty={users.data?.items.length === 0}>
      {users.data?.items.map((id) => <p key={id} className="muted">{id}</p>)}</ResourceStatus></section></div>;
}
