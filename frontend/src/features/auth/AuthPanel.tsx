import { FormEvent, useEffect, useRef, useState } from "react";
import { ApiRequestError, request } from "../../api/client";
import { newKey } from "../../api/types";
import { TurnstileChallenge } from "./TurnstileChallenge";

function authErrorMessage(cause: unknown): string {
  if (cause instanceof ApiRequestError) {
    if (cause.message === "Captcha verification failed") return "验证码校验失败，请重新完成验证码后重试。";
    if (cause.message === "Email verification required") return "邮箱尚未验证，请先完成邮箱验证。";
    if (["Verification token is invalid or expired", "Verification token is no longer current"]
      .includes(cause.message)) return "验证链接无效或已过期，请重新生成验证链接。";
  }
  return cause instanceof Error ? cause.message : "请求失败";
}

export function AuthPanel({ onAuthenticated, initialVerificationToken = "" }: {
  onAuthenticated: () => void; initialVerificationToken?: string;
}) {
  const [authConfig, setAuthConfig] = useState<{ captcha_mode: string; mail_mode: string; captcha_site_key?: string } | null>(null);
  const [mode, setMode] = useState<"login" | "register" | "verify">(initialVerificationToken ? "verify" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState(initialVerificationToken);
  const [busy, setBusy] = useState(false);
  const [resendUntil, setResendUntil] = useState(0);
  const [clockNow, setClockNow] = useState(Date.now());
  const [message, setMessage] = useState("");
  const [captchaToken, setCaptchaToken] = useState("");
  const [captchaError, setCaptchaError] = useState("");
  const [captchaReset, setCaptchaReset] = useState(0);
  const resendKey = useRef<string | null>(null);

  useEffect(() => {
    void request<{ captcha_mode: string; mail_mode: string; captcha_site_key?: string }>("/identity/auth-config")
      .then(setAuthConfig)
      .catch(() => setAuthConfig({ captcha_mode: "unavailable", mail_mode: "unavailable" }));
  }, []);
  useEffect(() => {
    if (resendUntil <= Date.now()) return;
    const timer = window.setInterval(() => {
      const now = Date.now();
      setClockNow(now);
      if (now >= resendUntil) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [resendUntil]);

  const privateTest = authConfig?.captcha_mode === "private_test" && authConfig.mail_mode === "local_capture";
  const localCapture = authConfig?.mail_mode === "local_capture";
  const turnstile = authConfig?.captcha_mode === "turnstile" && !!authConfig.captcha_site_key;
  const registrationReady = (privateTest || turnstile && !!captchaToken) && authConfig?.mail_mode !== "unavailable";
  useEffect(() => { if (captchaToken) setCaptchaError(""); }, [captchaToken]);
  const switchMode = (next: "login" | "register" | "verify") => {
    setCaptchaToken(""); setCaptchaError(""); setMode(next);
  };
  const resendSeconds = Math.max(0, Math.ceil((resendUntil - clockNow) / 1000));
  const startResendCooldown = () => { const now = Date.now(); setClockNow(now); setResendUntil(now + 60_000); };

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      if (mode === "verify") {
        await request("/identity/verify", { method: "POST", body: JSON.stringify({ token }) });
        onAuthenticated();
      } else if (mode === "register") {
        await request("/identity/register", { method: "POST", body: JSON.stringify({
          email, password, captcha_token: privateTest ? null : captchaToken, accepted_terms: true,
        }) });
        setMode("verify");
        try {
          const requestKey = resendKey.current ?? newKey();
          resendKey.current = requestKey;
          await request("/identity/verification", { method: "POST",
            headers: { "Idempotency-Key": requestKey }, body: JSON.stringify({ email }) });
          resendKey.current = null;
          startResendCooldown();
          setMessage(localCapture
            ? "私有测试：若该邮箱尚未验证，链接会保存到服务器受限 outbox，不会发送邮件。"
            : "若该邮箱尚未验证，验证邮件请求已提交。请检查邮箱。");
        } catch (cause) {
          setMessage(`账户已创建，但验证邮件请求失败：${authErrorMessage(cause)}`);
        }
      } else {
        await request("/identity/login", { method: "POST", body: JSON.stringify({
          email, password, captcha_token: null, accepted_terms: true,
        }) });
        onAuthenticated();
      }
    } catch (error) {
      setMessage(authErrorMessage(error));
    } finally {
      if (mode === "register" && turnstile) {
        setCaptchaToken("");
        setCaptchaReset((value) => value + 1);
      }
      setBusy(false);
    }
  }

  async function resend() {
    if (busy || resendSeconds > 0 || !email.trim()) return;
    setBusy(true);
    try {
      const requestKey = resendKey.current ?? newKey();
      resendKey.current = requestKey;
      await request("/identity/verification", { method: "POST",
        headers: { "Idempotency-Key": requestKey }, body: JSON.stringify({ email }) });
      resendKey.current = null;
      startResendCooldown();
      setMessage(localCapture
        ? "私有测试：若该邮箱存在且尚未验证，链接会保存到服务器受限 outbox，不会发送邮件。"
        : "若该邮箱存在且尚未验证，验证邮件请求已提交。请检查邮箱。");
    } catch (error) {
      if (error instanceof ApiRequestError && [409, 429].includes(error.status)) startResendCooldown();
      setMessage(authErrorMessage(error));
    }
    finally { setBusy(false); }
  }

  return <main className="auth-layout">
    <div className="auth-form-side"><a className="auth-back" href="/discover" aria-label="浏览公开作品">←</a>
    <form className="auth-card" onSubmit={submit}>
      <div className="segmented">
        <button type="button" className={mode === "login" ? "active" : ""} onClick={() => switchMode("login")}>登录</button>
        <button type="button" className={mode === "register" ? "active" : ""} onClick={() => switchMode("register")}>注册</button>
        <button type="button" className={mode === "verify" ? "active" : ""} onClick={() => switchMode("verify")}>验证</button>
      </div>
      <h1>{mode === "verify" ? "验证邮箱" : mode === "register" ? "创建你的账户" : "欢迎来到 Atoms"}</h1>
      <p className="auth-subtitle">{mode === "verify" ? "输入验证链接中的代码以继续" : mode === "register" ? "设置你的密码以继续" : "登录并继续创建你的应用"}</p>
      {privateTest && <p className="notice" role="status">私有测试模式：注册不使用 CAPTCHA，邮件只保存在服务器受限 outbox，不会发送到邮箱。</p>}
      {mode === "register" && turnstile && localCapture &&
        <p className="notice" role="status">私有测试模式：使用测试验证码，验证链接只保存在服务器受限 outbox，不会发送到邮箱。</p>}
      {mode === "register" && authConfig?.captcha_mode === "unavailable" &&
        <p className="notice" role="status">当前环境未配置 CAPTCHA，注册暂不可用。</p>}
      {mode === "register" && authConfig?.mail_mode === "unavailable" &&
        <p className="notice" role="status">当前环境未配置邮件服务，注册暂不可用。</p>}
      <label>邮箱地址{mode === "verify" ? "（仅重发时需要）" : ""}<input type="email" required={mode !== "verify"}
        value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" /></label>
      {mode !== "verify" && <label>密码<input type="password" required minLength={12} value={password}
        onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "register" ? "new-password" : "current-password"} /></label>}
      {mode === "register" && turnstile &&
        <div className="auth-captcha"><TurnstileChallenge siteKey={authConfig.captcha_site_key!} onToken={setCaptchaToken}
          onError={setCaptchaError} resetVersion={captchaReset} /></div>}
      {mode === "register" && captchaError && <p className="notice error" role="alert">{captchaError}</p>}
      {mode === "verify" && <label>邮件验证码<input required value={token} onChange={(event) => setToken(event.target.value)} /></label>}
      {message && <p className="notice" role="status">{message}</p>}
      <button className="primary" disabled={busy || (mode === "register" && !registrationReady)}>{busy ? "处理中…" : mode === "verify" ? "完成验证" : mode === "register" ? "继续" : "登录"}</button>
      {mode === "verify" && <button type="button" className="text-button" onClick={resend}
        disabled={busy || resendSeconds > 0 || !email.trim()}>
        {localCapture ? "重新生成验证链接" : "重发邮件"}{resendSeconds > 0 ? `（${resendSeconds} 秒）` : ""}
      </button>}
    </form></div>
    <div className="auth-brand-panel"><div className="auth-brand"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>Atoms</div>
      <div className="auth-brand-content"><h2>把想法变成可销售的产品</h2>
        <ul><li>几分钟而不是几周</li><li>真实应用，不只是演示</li><li>自动化你的运营</li><li>获得付费客户并创造收入</li><li>始终拥有完整所有权</li></ul></div>
      <div className="auth-brand-orbs" aria-hidden="true"><span>✦</span><span>✦</span></div>
    </div>
  </main>;
}
