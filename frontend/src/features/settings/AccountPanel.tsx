import { FormEvent, useEffect, useRef, useState } from "react";
import { errorMessage, fileBase64, mutate } from "../../api/actions";
import { ApiRequestError, apiUrl, request } from "../../api/client";
import { formatMoney, parseCnyMinorUnits } from "../../api/money";
import { ResourceStatus } from "../../api/ResourceStatus";
import { Page, Project } from "../../api/types";
import { useResource } from "../../api/useResource";

interface Profile { user_id: string; display_name: string; public_slug: string; avatar: { id: string; media_type: string } | null; revision: number }
interface Preferences { locale: string; default_model: string; default_visibility: string; credit_reminders: boolean; sound: boolean; email_notifications: boolean; branding: boolean; revision: number }
interface Member { id: string; user_id: string; role: string }
interface Balance { account: string; available: number; reserved: number; unit: string }
interface Plan { id: string; name: string; interval: string; price: { minor_units: number; currency: string }; entitlements: string[] }
interface Subscription { plan_id: string; status: string }
interface LedgerEntry { id: string; account: string; units: number; unit: string; recorded_at: string }
interface Invoice { id: string; status: string; amount: { minor_units: number; currency: string }; download_url: string | null }
interface Connection { id: string; provider: string; status: string; project_ids: string[] }
interface StorageUsage { used_bytes: number; quota_bytes: number; pay_as_you_go: boolean }
interface StoredObject { id: string; display_name: string; purpose: string; blob: { size_bytes: number; media_type: string } }
type Tab = "profile" | "preferences" | "members" | "billing" | "connectors" | "storage";
type ConfirmAction = { kind: "storage" | "member" | "connector"; id: string; label: string; identity?: string };
const PROFILE_VALIDATION_MESSAGE = "显示名称需为 1–80 个字符；公开路径需为 3–40 位英文字母、数字或连字符，首位须为字母或数字。";
const CONNECTOR_PROVIDER_MESSAGE = "提供方标识需以小写字母开头，长度 2–31 位，仅能使用小写字母、数字、下划线或连字符。";

function accountLabel(account: string): string {
  return account === "build_credits" ? "生成积分" : account === "cloud_wallet" ? "云钱包" : account;
}

function formatAccountUnits(units: number, account: string, unit: string): string {
  if (account === "build_credits") return `${new Intl.NumberFormat("zh-CN").format(units)} 积分`;
  if (account === "cloud_wallet") return formatMoney(units, "CNY");
  return `${new Intl.NumberFormat("zh-CN").format(units)} ${unit}`;
}

function formatLedgerAmount(item: LedgerEntry): string {
  const prefix = item.units > 0 ? "+" : item.units < 0 ? "−" : "";
  return `${prefix}${formatAccountUnits(Math.abs(item.units), item.account, item.unit)}`;
}

function ledgerDescription(item: LedgerEntry): string {
  const subject = item.account === "build_credits" ? "积分" : item.account === "cloud_wallet" ? "钱包" : "账户";
  return `${subject}${item.units > 0 ? "入账" : item.units < 0 ? "支出" : "记录"}`;
}

function formatLedgerDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function shortUserId(id: string): string {
  return `${id.slice(0, 6)}…${id.slice(-4)}`;
}

function formatStorageMb(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  const mb = bytes / (1024 * 1024);
  if (bytes > 0 && mb < 0.01) return "<0.01 MB";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(mb)} MB`;
}

function providerError(cause: unknown, unavailableMessage: string): string {
  return cause instanceof ApiRequestError && cause.code === "provider_unavailable"
    ? unavailableMessage : errorMessage(cause);
}

async function allProjects(): Promise<Project[]> {
  const projects: Project[] = [];
  let cursor: string | null = null;
  do {
    const page: Page<Project> = await request<Page<Project>>(
      `/projects?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
    projects.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return projects;
}

export function AccountPanel({ userId }: { userId: string }) {
  const [tab, setTab] = useState<Tab>("profile");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const profile = useResource(`profile:${userId}`, () => request<Profile>(`/profiles/${userId}`));
  const preferences = useResource(`preferences:${userId}`, () => request<Preferences>("/preferences"));
  const members = useResource("members", () => request<Page<Member>>( "/workspaces/current/members?limit=100"));
  const memberIds = members.data?.items.map((member) => member.user_id).join(":") ?? "";
  const memberNames = useResource(`member-names:${memberIds}`, async () => Object.fromEntries(
    await Promise.all((members.data?.items ?? []).map(async (member) => {
      try {
        const result = await request<Profile>(`/profiles/${member.user_id}`);
        return [member.user_id, result.display_name.trim()] as const;
      } catch {
        return [member.user_id, ""] as const;
      }
    })),
  ) as Record<string, string>);
  const balances = useResource("balances", () => request<Balance[]>("/billing/balances"));
  const plans = useResource("plans", () => request<Plan[]>("/billing/plans"));
  const subscription = useResource("subscription", () => request<Subscription | null>("/billing/subscription"));
  const [ledgerAccount, setLedgerAccount] = useState<"build_credits" | "cloud_wallet">("build_credits");
  const [ledgerProject, setLedgerProject] = useState("");
  const [ledgerCursor, setLedgerCursor] = useState<string | null>(null);
  const [ledgerBack, setLedgerBack] = useState<(string | null)[]>([]);
  const ledger = useResource(`ledger:${ledgerAccount}:${ledgerProject}:${ledgerCursor}`, () => request<Page<LedgerEntry>>(
    `/billing/ledger?limit=50&account=${ledgerAccount}${ledgerProject ? `&project_id=${encodeURIComponent(ledgerProject)}` : ""}${ledgerCursor ? `&cursor=${encodeURIComponent(ledgerCursor)}` : ""}`));
  const invoices = useResource("invoices", () => request<Page<Invoice>>( "/billing/invoices?limit=50"));
  const connections = useResource("connections", () => request<Page<Connection>>( "/connectors?limit=100"));
  const projects = useResource("account-projects", allProjects);
  const storage = useResource("storage-usage", () => request<StorageUsage>("/storage/usage"));
  const objects = useResource("storage-objects", () => request<Page<StoredObject>>( "/storage/objects?limit=100"));
  const authConfig = useResource("auth-config", () => request<{ mail_mode: string }>("/identity/auth-config"));
  const [name, setName] = useState<string | null>(null);
  const [slug, setSlug] = useState<string | null>(null);
  const [profileSaving, setProfileSaving] = useState(false);
  const profileSavingRef = useRef(false);
  const [newEmail, setNewEmail] = useState("");
  const [emailSubmitting, setEmailSubmitting] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [prefDraft, setPrefDraft] = useState<Preferences | null>(null);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [provider, setProvider] = useState("");
  const [connectorBusy, setConnectorBusy] = useState(false);
  const connectorBusyRef = useRef(false);
  const [topUpAmount, setTopUpAmount] = useState("");
  const [bindingProjects, setBindingProjects] = useState<Record<string, string>>({});
  const [billingBusy, setBillingBusy] = useState(false);
  const [storageBusy, setStorageBusy] = useState(false);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const confirmBusyRef = useRef(false);
  const confirmButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (preferences.data) setPrefDraft(preferences.data); }, [preferences.data]);
  useEffect(() => { if (confirmAction) confirmButton.current?.focus(); }, [confirmAction]);

  function askConfirmation(action: ConfirmAction) {
    if (confirmBusyRef.current) return;
    setError(""); setNotice(""); setConfirmAction(action);
  }

  async function saveProfile(event: FormEvent) {
    event.preventDefault(); if (profileSavingRef.current) return;
    const displayName = (name ?? profile.data?.display_name ?? "").trim();
    const publicSlug = (slug ?? profile.data?.public_slug ?? "").trim();
    if (Array.from(displayName).length < 1 || Array.from(displayName).length > 80 ||
        !/^[a-z0-9][a-z0-9-]{2,39}$/i.test(publicSlug)) {
      setError(PROFILE_VALIDATION_MESSAGE); setNotice(""); return;
    }
    profileSavingRef.current = true; setProfileSaving(true); setError(""); setNotice("");
    try { const saved = await mutate<Profile>("/profile", "PATCH", {
      display_name: displayName, public_slug: publicSlug,
      revision: profile.data?.revision || 0,
    }); setName(saved.display_name); setSlug(saved.public_slug); profile.refresh(); setNotice("资料已保存。"); }
    catch (cause) { setError(cause instanceof ApiRequestError && cause.code === "invalid_input"
      ? PROFILE_VALIDATION_MESSAGE : errorMessage(cause)); }
    finally { profileSavingRef.current = false; setProfileSaving(false); }
  }
  async function savePreferences(event: FormEvent) {
    event.preventDefault(); if (!prefDraft) return;
    setError(""); setNotice("");
    try { await mutate("/preferences", "PUT", prefDraft); preferences.refresh(); setNotice("偏好已保存。"); }
    catch (cause) { setError(errorMessage(cause)); }
  }
  async function changeEmail(event: FormEvent) {
    event.preventDefault();
    if (emailSubmitting) return;
    setError(""); setNotice("");
    setEmailSubmitting(true);
    try {
      await mutate("/identity/email", "PATCH", { email: newEmail.trim() });
      setNewEmail("");
      setNotice(authConfig.data?.mail_mode === "local_capture"
        ? "验证链接仅保存在服务器受限 outbox，不会发送邮件。验证完成前，原邮箱仍可用于登录。"
        : "邮箱变更验证请求已提交。验证完成前，原邮箱仍可用于登录。");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setEmailSubmitting(false); }
  }
  async function invite(event: FormEvent) {
    event.preventDefault(); setError(""); setNotice("");
    try { await mutate("/workspaces/current/invitations", "POST", { email: inviteEmail, role: inviteRole });
      setInviteEmail(""); setNotice("邀请已创建。"); members.refresh(); }
    catch (cause) { setError(errorMessage(cause)); }
  }
  async function upload(event: FormEvent) {
    event.preventDefault(); if (!uploadFile) return;
    setError(""); setNotice("");
    try { await mutate("/storage/objects", "POST", { project_id: null, purpose: "user_file",
      display_name: uploadFile.name, media_type: uploadFile.type || "application/octet-stream",
      content_base64: await fileBase64(uploadFile),
    }); setUploadFile(null); objects.refresh(); storage.refresh(); setNotice("文件已上传。"); }
    catch (cause) { setError(errorMessage(cause)); }
  }
  async function uploadAvatar(event: FormEvent) {
    event.preventDefault(); if (!avatarFile) return;
    setError(""); setNotice("");
    try {
      const stored = await mutate<{ id: string }>("/storage/objects", "POST", {
        project_id: null, purpose: "avatar", display_name: avatarFile.name,
        media_type: avatarFile.type, content_base64: await fileBase64(avatarFile),
      });
      await mutate("/profile/avatar", "PUT", { object_id: stored.id });
      setAvatarFile(null); profile.refresh(); setNotice("头像已更新。");
    } catch (cause) { setError(errorMessage(cause)); }
  }
  async function confirmRemoval() {
    if (!confirmAction || confirmBusyRef.current) return;
    const action = confirmAction;
    const expectedTab = { storage: "storage", member: "members", connector: "connectors" }[action.kind];
    const current = action.kind === "storage" ? objects.data?.items.find((item) => item.id === action.id)?.display_name
      : action.kind === "member" ? members.data?.items.find((item) => item.id === action.id)?.user_id
        : connections.data?.items.find((item) => item.id === action.id)?.provider;
    if (tab !== expectedTab || current !== (action.identity ?? action.label)) {
      setConfirmAction(null); setError("待处理项目已变化，请刷新后重试。"); return;
    }
    confirmBusyRef.current = true; setConfirmBusy(true); setError(""); setNotice("");
    try {
      const path = action.kind === "storage" ? `/storage/objects/${action.id}`
        : action.kind === "member" ? `/workspaces/current/members/${action.id}`
          : `/connectors/${action.id}`;
      await mutate(path, "DELETE");
      setConfirmAction(null);
      if (action.kind === "storage") { objects.refresh(); storage.refresh(); }
      else if (action.kind === "member") members.refresh();
      else connections.refresh();
      setNotice(action.kind === "storage" ? "文件已删除。" : action.kind === "member" ? "成员已移除。" : "连接已撤销。");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { confirmBusyRef.current = false; setConfirmBusy(false); }
  }
  async function changeRole(member: Member, role: string) {
    setError("");
    try { await mutate(`/workspaces/current/members/${member.id}`, "PATCH", { role }); members.refresh(); }
    catch (cause) { setError(errorMessage(cause)); }
  }
  async function startConnector(event: FormEvent) {
    event.preventDefault();
    if (connectorBusyRef.current) return;
    const providerId = provider.trim();
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(providerId)) {
      setError(CONNECTOR_PROVIDER_MESSAGE); return;
    }
    connectorBusyRef.current = true; setConnectorBusy(true); setError("");
    try {
      const result = await mutate<{ url: string }>(`/connectors/${encodeURIComponent(providerId)}/authorize`, "POST");
      window.location.assign(result.url);
    } catch (cause) { setError(cause instanceof ApiRequestError && cause.code === "invalid_input"
      ? CONNECTOR_PROVIDER_MESSAGE : providerError(cause, "连接器服务当前不可用，暂时无法授权。")); }
    finally { connectorBusyRef.current = false; setConnectorBusy(false); }
  }
  async function startCheckout(plan: Plan) {
    setError("");
    try { const result = await mutate<{ url: string }>("/billing/checkout", "POST", { plan_id: plan.id });
      window.location.assign(result.url); }
    catch (cause) { setError(providerError(cause, "支付服务当前不可用，暂时无法前往支付。")); }
  }

  async function topUp(event: FormEvent) {
    event.preventDefault(); setError(""); setNotice("");
    const minorUnits = parseCnyMinorUnits(topUpAmount);
    if (minorUnits === null) {
      setError("请输入大于 0 的金额，最多两位小数。"); return;
    }
    setBillingBusy(true);
    try {
      const result = await mutate<{ url: string }>("/billing/top-ups", "POST", {
        minor_units: minorUnits, currency: "CNY",
      });
      window.location.assign(result.url);
    } catch (cause) { setError(providerError(cause, "支付服务当前不可用，暂时无法充值。")); }
    finally { setBillingBusy(false); }
  }
  async function bindConnector(connection: Connection) {
    const projectId = bindingProjects[connection.id];
    if (!projectId) return;
    setError(""); setNotice("");
    try {
      await mutate(`/connectors/${connection.id}/projects/${projectId}`, "PUT");
      setBindingProjects((current) => ({ ...current, [connection.id]: "" }));
      connections.refresh(); setNotice("连接器已绑定到项目。");
    } catch (cause) { setError(errorMessage(cause)); }
  }
  async function setStorageBilling(enabled: boolean) {
    setError(""); setNotice(""); setStorageBusy(true);
    try {
      await mutate("/storage/pay-as-you-go", "PUT", { enabled });
      storage.refresh(); setNotice(enabled ? "已开启超额按量存储。" : "已关闭超额按量存储。");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setStorageBusy(false); }
  }

  return <section className="page account-page"><div className="page-head"><div><h1>账户设置</h1><p>管理你的个人资料并个性化你的账户体验</p></div></div>
    <div className="segmented wrap">{(["profile", "preferences", "members", "billing", "connectors", "storage"] as Tab[]).map((item) =>
      <button className={tab === item ? "active" : ""} key={item} disabled={confirmBusy} onClick={() => { setTab(item); setConfirmAction(null); setError(""); setNotice(""); }}>
        {({ profile: "你的账户", preferences: "偏好设置", members: "成员", billing: "套餐与积分", connectors: "连接器", storage: "磁盘空间" })[item]}</button>)}</div>
    {error && <p className="notice error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    {confirmAction && <div className="notice" role="group" aria-label="确认移除操作">
      <p>{confirmAction.kind === "storage" ? "删除文件" : confirmAction.kind === "member" ? "移除成员" : "撤销连接"}“{confirmAction.label}”？</p>
      <div className="inline-form"><button ref={confirmButton} className="primary" disabled={confirmBusy} onClick={() => void confirmRemoval()}>
        {confirmBusy ? "处理中…" : "确认移除"}</button>
        <button disabled={confirmBusy} onClick={() => setConfirmAction(null)}>取消</button></div></div>}
    {tab === "profile" && <div className="card account-settings-card"><ResourceStatus loading={profile.loading} error={profile.error} retry={profile.refresh}>
      <div className="setting-row"><div><h2>语言</h2><p>更改用户界面使用的语言</p></div>
        <button onClick={() => setTab("preferences")}>{preferences.data?.locale === "zh" ? "中文" : preferences.data?.locale || "语言设置"}　⌄</button></div>
      <div className="setting-row"><div><h2>个人主页</h2><p>管理你的公开个人资料</p></div>
        <a href={`/authors/${encodeURIComponent(userId)}`}>查看个人主页　↗</a></div>
      <div className="setting-row"><div><h2>头像</h2><p>上传或更换你的头像</p></div>
        <form className="setting-row-control" onSubmit={uploadAvatar}>
          {profile.data?.avatar ? <img className="setting-avatar" src={apiUrl(`/profiles/${userId}/avatar`)} alt="个人头像" /> :
            <span className="setting-avatar setting-avatar-placeholder" aria-hidden="true">A</span>}
          <input type="file" aria-label="选择头像" accept="image/png,image/jpeg,image/gif,image/webp" onChange={(event) => setAvatarFile(event.target.files?.[0] || null)} />
          <button disabled={!avatarFile || !profile.data?.display_name || !profile.data?.public_slug}>上传头像</button>
        </form></div>
      <div className="setting-row"><div><h2>用户名</h2><p>选择你的名字在 Atoms 上的显示方式</p></div>
        <form className="setting-row-control" onSubmit={saveProfile}>
          <input aria-label="显示名称" disabled={profileSaving} value={name ?? profile.data?.display_name ?? ""} onChange={(event) => setName(event.target.value)} placeholder="显示名称" />
          <input aria-label="公开路径" disabled={profileSaving} value={slug ?? profile.data?.public_slug ?? ""} onChange={(event) => setSlug(event.target.value)} placeholder="公开路径" />
          <button disabled={profileSaving}>保存资料</button>
        </form></div>
      <div className="setting-row"><div><h2>邮箱地址</h2><p>管理你的账户邮箱</p></div>
        <form className="setting-row-control" onSubmit={changeEmail}>
          <input type="email" aria-label="新邮箱地址" required value={newEmail} onChange={(event) => setNewEmail(event.target.value)} autoComplete="email" placeholder="新邮箱地址" />
          <button disabled={!newEmail.trim() || emailSubmitting}>{emailSubmitting ? "处理中…" : "发送验证请求"}</button>
          {authConfig.data?.mail_mode === "local_capture" &&
            <p className="muted">私有测试：验证链接保存在服务器受限 outbox。</p>}
        </form></div></ResourceStatus></div>}
    {tab === "preferences" && <div className="card narrow"><h2>默认偏好</h2><ResourceStatus loading={preferences.loading} error={preferences.error} retry={preferences.refresh}>
      {prefDraft && <form className="stack" onSubmit={savePreferences}>
        <label>语言<input value={prefDraft.locale} onChange={(event) => setPrefDraft({ ...prefDraft, locale: event.target.value })} /></label>
        <label>默认模型<input value={prefDraft.default_model} onChange={(event) => setPrefDraft({ ...prefDraft, default_model: event.target.value })} /></label>
        <label>默认可见性<select value={prefDraft.default_visibility} onChange={(event) => setPrefDraft({ ...prefDraft, default_visibility: event.target.value })}>
          <option value="private">私有</option><option value="public">公开</option></select></label>
        <label className="checkbox"><input type="checkbox" checked={prefDraft.credit_reminders} onChange={(event) => setPrefDraft({ ...prefDraft, credit_reminders: event.target.checked })} /> 积分提醒</label>
        <label className="checkbox"><input type="checkbox" checked={prefDraft.sound} onChange={(event) => setPrefDraft({ ...prefDraft, sound: event.target.checked })} /> 声音</label>
        <label className="checkbox"><input type="checkbox" checked={prefDraft.email_notifications} onChange={(event) => setPrefDraft({ ...prefDraft, email_notifications: event.target.checked })} /> 邮件通知</label>
        <label className="checkbox"><input type="checkbox" checked={prefDraft.branding} onChange={(event) => setPrefDraft({ ...prefDraft, branding: event.target.checked })} /> 品牌展示</label>
        <button className="primary">保存偏好</button></form>}</ResourceStatus></div>}
    {tab === "members" && <div className="workspace-grid"><div className="card"><h2>成员</h2><ResourceStatus loading={members.loading} error={members.error} retry={members.refresh}
      empty={members.data?.items.length === 0}>{members.data?.items.map((member) => {
        const displayName = (member.user_id === userId ? profile.data?.display_name : memberNames.data?.[member.user_id])?.trim()
          || memberNames.data?.[member.user_id] || "未设置用户名";
        const shortId = shortUserId(member.user_id);
        return <div className="history-row member-row" key={member.id}>
          <div className="member-identity"><span className="member-avatar" aria-hidden="true">{displayName === "未设置用户名" ? "?" : displayName[0]}</span>
            <div><strong>{displayName}</strong><p className="muted" title={`用户 ID：${member.user_id}`}>
              {member.user_id === userId ? "当前账号" : "成员"} · {shortId}</p></div></div>
          <div className="inline-form"><select aria-label={`${displayName}（${shortId}）的角色`} value={member.role} onChange={(event) => changeRole(member, event.target.value)}>
            <option value="owner">所有者</option><option value="admin">管理员</option><option value="member">成员</option><option value="viewer">访客</option></select>
            <button disabled={confirmBusy} onClick={() => askConfirmation({ kind: "member", id: member.id, label: `${displayName}（${shortId}）`, identity: member.user_id })}>移除</button></div>
        </div>;
      })}</ResourceStatus></div>
      <form className="card stack" onSubmit={invite}><h2>邀请成员</h2><label>邮箱<input type="email" required value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} /></label>
        <label>角色<select value={inviteRole} onChange={(event) => setInviteRole(event.target.value)}><option value="member">成员</option><option value="viewer">访客</option><option value="admin">管理员</option></select></label>
        <button className="primary">创建邀请</button></form></div>}
    {tab === "billing" && <div className="workspace-grid"><div className="card"><h2>账户余额</h2><ResourceStatus loading={balances.loading} error={balances.error} retry={balances.refresh}
      empty={balances.data?.length === 0}><div className="balance-list">{balances.data?.map((balance) => <div className="balance-item" key={balance.account}>
        <span>{accountLabel(balance.account)}</span><strong>{formatAccountUnits(balance.available, balance.account, balance.unit)}</strong>
        <small>已预留 {formatAccountUnits(balance.reserved, balance.account, balance.unit)}</small>
      </div>)}</div></ResourceStatus>
      <h2>当前订阅</h2><ResourceStatus loading={subscription.loading} error={subscription.error} retry={subscription.refresh}>
        <p>{subscription.data ? `${subscription.data.plan_id} · ${subscription.data.status}` : "暂无订阅"}</p></ResourceStatus>
      <h2>账目与项目费用</h2><div className="inline-form"><label>账户<select value={ledgerAccount} onChange={(event) => {
        setLedgerAccount(event.target.value as "build_credits" | "cloud_wallet"); setLedgerCursor(null); setLedgerBack([]);
      }}><option value="build_credits">生成积分</option><option value="cloud_wallet">云钱包</option></select></label>
      <label>项目<select value={ledgerProject} onChange={(event) => {
        setLedgerProject(event.target.value); setLedgerCursor(null); setLedgerBack([]);
      }}><option value="">全部项目</option>{projects.data?.map((project) =>
        <option key={project.id} value={project.id}>{project.name}</option>)}</select></label></div>
      <ResourceStatus loading={ledger.loading} error={ledger.error} retry={ledger.refresh}
        empty={ledger.data?.items.length === 0}><ol className="ledger-list" aria-label="账目流水">{ledger.data?.items.map((item) => <li className="ledger-item" key={item.id}>
          <span className={`ledger-icon ${item.units > 0 ? "income" : item.units < 0 ? "expense" : "neutral"}`} aria-hidden="true">
            {item.units > 0 ? "＋" : item.units < 0 ? "−" : "·"}</span>
          <div className="ledger-detail"><strong>{ledgerDescription(item)}</strong><time dateTime={item.recorded_at}>{formatLedgerDate(item.recorded_at)}</time></div>
          <strong className={`ledger-amount ${item.units > 0 ? "income" : item.units === 0 ? "neutral" : ""}`}>{formatLedgerAmount(item)}</strong>
        </li>)}</ol></ResourceStatus>
      <div className="inline-form history-nav"><button disabled={ledger.loading || ledgerBack.length === 0} onClick={() => {
        setLedgerCursor(ledgerBack[ledgerBack.length - 1]); setLedgerBack(ledgerBack.slice(0, -1));
      }}>上一页账目</button><button disabled={ledger.loading || !ledger.data?.next_cursor} onClick={() => {
        setLedgerBack([...ledgerBack, ledgerCursor]); setLedgerCursor(ledger.data?.next_cursor || null);
      }}>下一页账目</button></div></div>
      <div className="card"><h2>可用套餐</h2><ResourceStatus loading={plans.loading} error={plans.error} retry={plans.refresh}
      empty={plans.data?.length === 0}>{plans.data?.map((plan) => <div className="history-row" key={plan.id}><span>{plan.name}：{formatMoney(plan.price.minor_units, plan.price.currency)} / {plan.interval}</span>
        <button onClick={() => startCheckout(plan)}>前往支付</button></div>)}</ResourceStatus>
        <p className="muted">只有支付方 webhook 核验成功后余额或订阅才会更新。</p>
        <form className="stack" onSubmit={topUp}><h2>云钱包充值</h2>
          <label>充值金额（元）<input inputMode="decimal" value={topUpAmount} onChange={(event) => setTopUpAmount(event.target.value)} placeholder="例如 10.00" required /></label>
          <button disabled={billingBusy}>{billingBusy ? "处理中…" : "前往充值"}</button></form>
        <h2>发票</h2><ResourceStatus loading={invoices.loading} error={invoices.error} retry={invoices.refresh}
          empty={invoices.data?.items.length === 0}>{invoices.data?.items.map((invoice) => <p key={invoice.id}>{invoice.status} · {formatMoney(invoice.amount.minor_units, invoice.amount.currency)}
            {invoice.download_url && <a href={invoice.download_url}> 下载</a>}</p>)}</ResourceStatus></div></div>}
    {tab === "connectors" && <div className="workspace-grid"><div className="card"><h2>资源连接</h2><ResourceStatus loading={connections.loading} error={connections.error} retry={connections.refresh}
      empty={connections.data?.items.length === 0}>{connections.data?.items.map((connection) => <div className="history-row" key={connection.id}>
        <div><strong>{connection.provider}</strong> · {connection.status}
          <p className="muted">已绑定 {connection.project_ids?.length || 0} 个项目</p></div>
        <div className="inline-form"><select aria-label={`将 ${connection.provider} 绑定到项目`} value={bindingProjects[connection.id] || ""}
          onChange={(event) => setBindingProjects({ ...bindingProjects, [connection.id]: event.target.value })}>
          <option value="">选择项目</option>{projects.data?.filter((project) => !connection.project_ids?.includes(project.id)).map((project) =>
            <option key={project.id} value={project.id}>{project.name}</option>)}</select>
          <button disabled={connection.status !== "active" || !bindingProjects[connection.id]} onClick={() => bindConnector(connection)}>绑定</button>
          <button disabled={confirmBusy} onClick={() => askConfirmation({ kind: "connector", id: connection.id, label: connection.provider })}>撤销</button></div></div>)}</ResourceStatus></div>
      <form className="card stack" onSubmit={startConnector}><h2>连接提供方</h2><label>提供方标识<input required disabled={connectorBusy} value={provider} onChange={(event) => setProvider(event.target.value)} /></label>
        <button className="primary" disabled={connectorBusy}>{connectorBusy ? "处理中…" : "开始授权"}</button><p className="muted">可用提供方以本机服务配置为准。</p></form></div>}
    {tab === "storage" && <div className="workspace-grid"><div className="card"><h2>磁盘空间</h2><ResourceStatus loading={storage.loading} error={storage.error} retry={storage.refresh}>
      <p>已用 {formatStorageMb(storage.data?.used_bytes)} / 配额 {formatStorageMb(storage.data?.quota_bytes)}</p>
      <label className="checkbox"><input type="checkbox" checked={storage.data?.pay_as_you_go || false} disabled={storageBusy}
        onChange={(event) => setStorageBilling(event.target.checked)} /> 超额后按量计费</label></ResourceStatus>
      <form className="stack" onSubmit={upload}><label>上传文件<input type="file" required onChange={(event) => setUploadFile(event.target.files?.[0] || null)} /></label>
        <button className="primary" disabled={!uploadFile}>上传</button></form></div>
      <div className="card"><h2>文件</h2><ResourceStatus loading={objects.loading} error={objects.error} retry={objects.refresh}
        empty={objects.data?.items.length === 0}>{objects.data?.items.map((object) => <div className="history-row" key={object.id}>
          <div><strong>{object.display_name}</strong><p className="muted">{object.purpose} · {formatStorageMb(object.blob.size_bytes)}</p></div>
          <div className="inline-form"><a className="button-link" href={apiUrl(`/storage/objects/${object.id}/download`)}>下载</a>
            <button disabled={confirmBusy} onClick={() => askConfirmation({ kind: "storage", id: object.id, label: object.display_name })}>删除</button></div></div>)}</ResourceStatus></div></div>}
  </section>;
}
