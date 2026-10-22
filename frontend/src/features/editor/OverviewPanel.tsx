import { request } from "../../api/client";
import type { Project, ProjectHead, Publication } from "../../api/types";
import { useResource } from "../../api/useResource";
import "./OverviewPanel.css";

type GrowthView = "analytics" | "seo" | "ads";

interface ProjectOverview {
  project: Project;
  head: ProjectHead;
  balances: unknown[];
}

export interface OverviewPanelProps {
  projectId: string;
  onOpenAppInfo: () => void;
  onOpenPublication: () => void;
  onOpenGrowth: (view: GrowthView) => void;
  onOpenAssets: () => void;
}

function AtomsMark() {
  return <svg viewBox="0 0 40 40" aria-hidden="true" fill="currentColor">
    <path d="M20 4.5c3.9 0 6.7 2.7 6.7 6.4 0 4.2-3.3 7.1-6.7 9.2-3.4-2.1-6.7-5-6.7-9.2 0-3.7 2.8-6.4 6.7-6.4Zm-9.5 11.7c3.6 0 6.5 3 8.3 6.7-1.1 3.7-3.6 8.2-7.8 8.2-3.5 0-6.3-2.7-6.3-6.5 0-4.3 2.4-8.4 5.8-8.4Zm19 0c3.4 0 5.8 4.1 5.8 8.4 0 3.8-2.8 6.5-6.3 6.5-4.2 0-6.7-4.5-7.8-8.2 1.8-3.7 4.7-6.7 8.3-6.7Zm-9.5 9.3c3.9 0 6.8 2.5 6.8 5.6 0 3-2.9 4.7-6.8 4.7s-6.8-1.7-6.8-4.7c0-3.1 2.9-5.6 6.8-5.6Z" />
  </svg>;
}

function TileIcon({ kind }: { kind: "info" | "domain" | "video" | "poster" | "case" }) {
  if (kind === "info" || kind === "domain") return <span className="overview-tile-icon overview-tile-icon-dotted" aria-hidden="true">◌</span>;
  const symbol = { video: "▷", poster: "▧", case: "▣" }[kind];
  return <span className="overview-tile-icon" aria-hidden="true">{symbol}</span>;
}

function safePublicUrl(publication: Publication | null): URL | null {
  if (publication?.state !== "live" || !publication.public_url) return null;
  try {
    const url = new URL(publication.public_url);
    return url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

export function OverviewPanel({ projectId, onOpenAppInfo, onOpenPublication, onOpenGrowth, onOpenAssets }: OverviewPanelProps) {
  const overview = useResource(`overview:${projectId}`, () => request<ProjectOverview>(`/projects/${encodeURIComponent(projectId)}/overview`));
  const publication = useResource(`publication:${projectId}`, () => request<Publication | null>(`/projects/${encodeURIComponent(projectId)}/publication`));
  const project = overview.data?.project;
  const liveUrl = safePublicUrl(publication.data);
  const hasVersion = Boolean(overview.data?.head.version_id);
  const domain = liveUrl?.hostname || publication.data?.domain || null;
  const status = publication.loading ? "核对中" : liveUrl ? "在线" : publication.data?.state === "awaiting_manual" ? "待部署" : "未上线";

  return <section className="overview-panel" aria-label="项目概览">
    <div className="overview-panel-inner">
      <header className="overview-app-card">
        <div className="overview-app-logo"><AtomsMark /></div>
        <div className="overview-app-details">
          <div className="overview-app-name-line">
            <h2 title={project?.name}>{project?.name || (overview.loading ? "加载应用中…" : "应用信息暂不可用")}</h2>
            <span className={`overview-live-badge${liveUrl ? " is-live" : ""}`}><span aria-hidden="true">●</span> {status}</span>
          </div>
          {domain ? <p className="overview-app-domain" title={domain}>{domain}</p>
            : <p className="overview-app-domain is-empty">尚未设置公开域名</p>}
          <button type="button" className="overview-edit-url" onClick={onOpenPublication}>
            <span aria-hidden="true">✎</span> {domain ? "修改 URL" : "设置 URL"}
          </button>
        </div>
      </header>

      {overview.error && <div className="overview-load-error" role="alert">应用信息读取失败：{overview.error} <button type="button" onClick={overview.refresh}>重试</button></div>}
      {publication.error && <div className="overview-load-error" role="alert">发布状态读取失败：{publication.error} <button type="button" onClick={publication.refresh}>重试</button></div>}

      <section className="overview-section" aria-labelledby="overview-manage-title">
        <h3 id="overview-manage-title">管理</h3>
        <button type="button" className="overview-nav-tile" onClick={onOpenAppInfo}>
          <TileIcon kind="info" />
          <span className="overview-tile-text"><strong>应用信息</strong><small>{hasVersion ? "查看应用详情并预览" : "开始创建并预览应用"}</small></span>
          <span className="overview-chevron" aria-hidden="true">›</span>
        </button>
        <button type="button" className="overview-nav-tile" onClick={onOpenPublication}>
          <TileIcon kind="domain" />
          <span className="overview-tile-text"><strong>线上应用和域名</strong><small>管理状态和域名</small></span>
          <span className="overview-chevron" aria-hidden="true">›</span>
        </button>
      </section>

      <section className="overview-section overview-growth-section" aria-labelledby="overview-growth-title">
        <h3 id="overview-growth-title">增长</h3>
        <article className="overview-growth-card overview-analytics-card">
          <h4>分析</h4>
          <p>跟踪你站点上的用户行为和参与度指标。</p>
          <div className="overview-chart-placeholder" aria-label="尚未连接分析数据">
            <div className="overview-chart-bars" aria-hidden="true">
              <span /><span /><span /><span /><span /><span /><span /><span />
            </div>
            <span className="overview-pending-badge">待处理</span>
          </div>
          <button type="button" className="overview-black-button" onClick={() => onOpenGrowth("analytics")}>设置 Analytics</button>
        </article>
        <article className="overview-growth-card overview-compact-card">
          <h4>SEO</h4>
          <p>了解网站在搜索引擎中的展示情况。</p>
          <span className="overview-compact-pending">待处理</span>
          <button type="button" className="overview-black-button" onClick={() => onOpenGrowth("seo")}>优化 SEO</button>
        </article>
        <article className="overview-growth-card overview-compact-card">
          <h4>Google Ads</h4>
          <p>连接广告平台后查看投放和访问转化情况。</p>
          <span className="overview-compact-pending">待处理</span>
          <button type="button" className="overview-black-button" onClick={() => onOpenGrowth("ads")}>创建广告</button>
        </article>
      </section>

      <section className="overview-section overview-promo-section" aria-labelledby="overview-promo-title">
        <h3 id="overview-promo-title">推广素材</h3>
        <p className="overview-promo-description">从素材库查看或添加用于推广的内容。</p>
        <button type="button" className="overview-nav-tile" onClick={onOpenAssets}>
          <TileIcon kind="case" /><span className="overview-tile-text"><strong>案例素材</strong><small>查看项目素材</small></span><span className="overview-chevron" aria-hidden="true">›</span>
        </button>
        <button type="button" className="overview-nav-tile" onClick={onOpenAssets}>
          <TileIcon kind="video" /><span className="overview-tile-text"><strong>商业宣传视频</strong><small>查看或上传视频素材</small></span><span className="overview-chevron" aria-hidden="true">›</span>
        </button>
        <button type="button" className="overview-nav-tile" onClick={onOpenAssets}>
          <TileIcon kind="poster" /><span className="overview-tile-text"><strong>广告海报</strong><small>查看或上传图片素材</small></span><span className="overview-chevron" aria-hidden="true">›</span>
        </button>
      </section>

      {liveUrl && <a className="overview-open-live" href={liveUrl.toString()} target="_blank" rel="noopener noreferrer">打开线上应用 ↗</a>}
    </div>
  </section>;
}
