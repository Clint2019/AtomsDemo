import { useState } from "react";
import "./GrowthPanel.css";

type GrowthView = "seo" | "analytics" | "ads";

interface GrowthPanelProps {
  onOpenResources?: () => void;
  initialView?: GrowthView;
}

const views: { id: GrowthView; label: string; title: string; description: string }[] = [
  { id: "seo", label: "SEO", title: "SEO", description: "了解网站在搜索引擎中的展示情况" },
  { id: "analytics", label: "分析", title: "分析", description: "跟踪你站点上的用户行为和参与度" },
  { id: "ads", label: "广告", title: "广告", description: "了解广告投放和访问转化情况" },
];

function GrowthIcon({ view }: { view: GrowthView }) {
  if (view === "seo") return <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M4.5 18.5h15M6.5 15V9.5h3V15m2.5 0V5.5h3V15m2.5 0v-7h3V15" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
  if (view === "analytics") return <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M4.5 18.5h15M6.5 16V11m4 5V6m4 10V9m4 7V4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>;
  return <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M4.5 10.5v4h3l4.5 3V7.5l-4.5 3h-3Zm7.5-3 5-2v14l-5-2M5.5 14.5l1 4h3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
}

function AnalyticsMark() {
  return <svg className="growth-analytics-mark" viewBox="0 0 72 72" role="img" aria-label="Google Analytics">
    <circle cx="13" cy="59" r="7" fill="#e87200" />
    <rect x="25" y="32" width="14" height="34" rx="7" fill="#e87200" />
    <rect x="45" y="6" width="14" height="60" rx="7" fill="#fbbc04" />
  </svg>;
}

export function GrowthPanel({ onOpenResources, initialView = "analytics" }: GrowthPanelProps) {
  const [view, setView] = useState<GrowthView>(initialView);
  const [helpOpen, setHelpOpen] = useState(false);
  const [authorizationNotice, setAuthorizationNotice] = useState(false);
  const current = views.find((item) => item.id === view)!;

  return <section className="growth-panel" aria-label="增长工具">
    <nav className="growth-rail" aria-label="增长工具分类">
      {views.map((item) => <button key={item.id} type="button"
        className={`growth-rail-item${view === item.id ? " is-active" : ""}`}
        aria-current={view === item.id ? "page" : undefined}
        aria-label={item.label}
        onClick={() => { setView(item.id); setHelpOpen(false); setAuthorizationNotice(false); }}>
        <span className="growth-rail-icon"><GrowthIcon view={item.id} /></span>
        <span>{item.label}</span>
      </button>)}
    </nav>
    <div className="growth-main">
      <header className="growth-header">
        <div className="growth-heading"><h2>{current.title}</h2><p>{current.description}</p></div>
        <button type="button" className="growth-help-button" aria-expanded={helpOpen}
          aria-controls="growth-help" onClick={() => setHelpOpen((open) => !open)}>
          <span aria-hidden="true">ⓘ</span> 帮助中心
        </button>
      </header>
      {helpOpen && <aside id="growth-help" className="growth-help" aria-label={`${current.title}帮助`}>
        <strong>{current.title}帮助</strong>
        {view === "analytics" ? <p>连接 GA4 需要 Google OAuth 配置和你的账户授权。当前本地项目尚未配置 OAuth，因此暂时无法读取分析数据。</p>
          : <p>当前本地项目尚未连接{view === "seo" ? "搜索平台" : "广告平台"}。连接后才能查看真实数据。</p>}
        {onOpenResources && <button type="button" onClick={onOpenResources}>打开项目资源</button>}
      </aside>}
      <div className="growth-body">
        {view === "analytics" ? <div className="growth-empty growth-analytics-empty">
          <AnalyticsMark />
          <p className="growth-primary-copy">连接你的 Google Analytics 账户<br />并授权访问你现有的 GA4 属性。</p>
          <span className="growth-info-icon" aria-hidden="true">ⓘ</span>
          <p className="growth-secondary-copy">GA4 授权需要 OAuth。当前本地环境尚未配置 Google OAuth，无法访问你的分析数据。</p>
          <button type="button" className="growth-authorize-button"
            onClick={() => setAuthorizationNotice(true)}><span aria-hidden="true">↗</span> 授权 GA4</button>
          {authorizationNotice && <div className="growth-connection-notice" role="status">
            <strong>尚未配置 GA4 授权</strong>
            <p>本地暂未提供 GA4 OAuth 配置，此按钮目前不会打开授权流程。你可以在项目资源中查看已有连接能力。</p>
            {onOpenResources && <button type="button" onClick={onOpenResources}>打开项目资源</button>}
          </div>}
        </div> : <div className="growth-empty growth-other-empty">
          <span className="growth-other-icon"><GrowthIcon view={view} /></span>
          <h3>{view === "seo" ? "查看搜索表现" : "查看广告表现"}</h3>
          <p>{view === "seo" ? "当前项目尚未连接搜索平台，因此没有可显示的站点数据。" : "当前项目尚未连接广告平台，因此没有可显示的投放数据。"}</p>
          {onOpenResources && <button type="button" onClick={onOpenResources}>打开项目资源</button>}
        </div>}
      </div>
    </div>
  </section>;
}
