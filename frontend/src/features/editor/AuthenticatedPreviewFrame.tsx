import { useEffect, useRef, useState } from "react";
import { request } from "../../api/client";
import { PreviewTicket } from "../../api/types";
import { isPreviewReady, pingPreview, previewMessageOrigin, previewToken,
  sendPreviewSession } from "./previewSession";

interface AppSession {
  id: string;
  project_id: string;
  environment: string;
}

/** The platform account obtains the app session before untrusted code is loaded. */
export function AuthenticatedPreviewFrame({ ticket, title, className }: {
  ticket: PreviewTicket; title: string; className: string;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [session, setSession] = useState<{ url: string; id: string } | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const token = previewToken(ticket.url);
  const origin = previewMessageOrigin(ticket.url, window.location.hostname);

  useEffect(() => {
    let active = true;
    setSession(null);
    setError("");
    if (!token) {
      setError("预览链接无效，请重新打开预览。");
      return () => { active = false; };
    }
    void request<AppSession>("/app/sessions", {
      method: "POST", body: JSON.stringify({ preview_token: token }),
    }).then((result) => {
      if (!active) return;
      if (!result.id || result.project_id !== ticket.project_id || result.environment !== "development") {
        throw new Error("应用数据会话与预览不匹配");
      }
      setSession({ url: ticket.url, id: result.id });
    }).catch((cause: unknown) => {
      if (active) setError(cause instanceof Error ? cause.message : "预览连接失败，请重试。");
    });
    return () => { active = false; };
  }, [ticket.url, ticket.project_id, token, attempt]);

  useEffect(() => {
    if (!session || session.url !== ticket.url || !token) return;
    const onMessage = (event: MessageEvent) => {
      if (!isPreviewReady(event, frame.current?.contentWindow || null, origin, token)) return;
      sendPreviewSession(frame.current?.contentWindow || null, origin, token, session.id);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [session, ticket.url, origin, token]);

  if (error) return <div className="chat-preview-empty" role="alert">
    <p>{error}</p><button type="button" onClick={() => setAttempt((value) => value + 1)}>重试预览</button>
  </div>;
  if (!session || session.url !== ticket.url || !token)
    return <div className="chat-preview-empty"><p>正在连接应用数据…</p></div>;
  return <iframe ref={frame} title={title} src={ticket.url} className={className}
    sandbox={origin === "null" ? "allow-scripts allow-forms" : "allow-scripts allow-forms allow-same-origin"}
    onLoad={() => pingPreview(frame.current?.contentWindow || null, origin)} />;
}
