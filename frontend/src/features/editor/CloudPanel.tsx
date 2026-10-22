import { request } from "../../api/client";
import { Page } from "../../api/types";
import { useResource } from "../../api/useResource";
import "./CloudPanel.css";

interface AppResource { id: string; kind: string; name: string; status: string }
interface ModelOption { key: string; label: string; available: boolean }
type Feature = "database" | "model" | "secret" | "identity";

function countKind(resources: AppResource[], kind: string): number {
  return resources.filter((resource) => resource.kind === kind && resource.status === "configured").length;
}

function AtomsGlyph() {
  return <svg className="atoms-cloud-glyph" width="36" height="36" viewBox="0 0 36 36" aria-hidden="true">
    <ellipse cx="12" cy="11" rx="6.5" ry="10" transform="rotate(23 12 11)" fill="currentColor" />
    <ellipse cx="13" cy="27" rx="5" ry="6.5" transform="rotate(35 13 27)" fill="currentColor" />
    <ellipse cx="27" cy="14" rx="4.5" ry="7" transform="rotate(-20 27 14)" fill="currentColor" />
    <circle cx="27" cy="28" r="2.2" fill="currentColor" />
  </svg>;
}

function FeatureIcon({ kind }: { kind: Feature }) {
  return <span className="atoms-cloud-feature-icon" aria-hidden="true"><svg width="19" height="19" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    {kind === "database" && <><ellipse cx="12" cy="5" rx="7" ry="3" /><path d="M5 5v14c0 1.7 3.1 3 7 3s7-1.3 7-3V5M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" /></>}
    {kind === "model" && <><path d="m10 3 1.4 5.6L17 10l-5.6 1.4L10 17l-1.4-5.6L3 10l5.6-1.4L10 3ZM18 15l.8 2.2L21 18l-2.2.8L18 21l-.8-2.2L15 18l2.2-.8L18 15Z" /></>}
    {kind === "secret" && <><path d="M12 2 20 5v6c0 5-3.3 8.6-8 11-4.7-2.4-8-6V5l8-3Z" /><path d="m8.4 11.7 2.4 2.4 4.8-4.8" /></>}
    {kind === "identity" && <><circle cx="9" cy="8" r="3" /><path d="M2.5 19c.6-3 3.1-5 6.5-5s5.9 2 6.5 5M16.5 5.4a3 3 0 0 1 0 5.2M17 14c2.7.3 4.2 2.1 4.5 5" /></>}
  </svg></span>;
}

export function CloudPanel({ projectId, onBack, onConnect }: {
  projectId: string;
  onBack: () => void;
  onConnect: () => void;
}) {
  const development = useResource(`cloud-development:${projectId}`,
    () => request<AppResource[]>(`/projects/${projectId}/resources?environment=development`));
  const production = useResource(`cloud-production:${projectId}`,
    () => request<AppResource[]>(`/projects/${projectId}/resources?environment=production`));
  const models = useResource("cloud-models", () => request<ModelOption[]>("/models"));
  const users = useResource(`cloud-app-users:${projectId}`,
    () => request<Page<string>>(`/projects/${projectId}/app-users?limit=100`));
  const resources = [...(development.data || []), ...(production.data || [])];
  const modelCount = (models.data || []).filter((model) => model.available).length;
  const statuses: Record<Feature, string> = {
    database: `已配置数据库 ${countKind(resources, "database")} 项，存储 ${countKind(resources, "storage")} 项`,
    model: `可用模型 ${modelCount} 项`,
    secret: `已添加密钥配置 ${countKind(resources, "secret")} 项`,
    identity: `认证配置 ${countKind(resources, "authentication")} 项，应用用户 ${users.data?.items.length || 0} 位`,
  };
  const features: { kind: Feature; text: string }[] = [
    { kind: "database", text: "强大的数据库和存储" },
    { kind: "model", text: "前沿的 AI 和模型集成" },
    { kind: "secret", text: "安全可靠的密钥管理" },
    { kind: "identity", text: "安全灵活的用户认证" },
  ];

  return <section className="atoms-cloud-panel" aria-label="Atoms云">
    <div className="atoms-cloud-content">
      <div className="atoms-cloud-title-card"><AtomsGlyph /><strong>Atoms云</strong>
        <span>构建功能齐全的应用</span></div>
      <div className="atoms-cloud-features">
        {features.map((feature) => <div className="atoms-cloud-feature" key={feature.kind} title={statuses[feature.kind]}>
          <FeatureIcon kind={feature.kind} /><span>{feature.text}</span></div>)}
      </div>
      <div className="atoms-cloud-actions"><button type="button" onClick={onBack}>↗&nbsp; 返回应用查看器</button>
        <button type="button" className="atoms-cloud-connect" onClick={onConnect}>连接</button></div>
    </div>
  </section>;
}
