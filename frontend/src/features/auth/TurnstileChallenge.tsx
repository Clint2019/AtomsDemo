import { useEffect, useRef, useState } from "react";

type TurnstileApi = {
  render: (element: HTMLElement, options: {
    sitekey: string;
    action: string;
    callback: (token: string) => void;
    "expired-callback": () => void;
    "error-callback": () => void;
    "timeout-callback": () => void;
  }) => string;
  reset: (id: string) => void;
  remove: (id: string) => void;
};

declare global {
  interface Window { turnstile?: TurnstileApi }
}

const scriptUrl = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

export function TurnstileChallenge({ siteKey, onToken, onError, resetVersion }: {
  siteKey: string;
  onToken: (token: string) => void;
  onError: (message: string) => void;
  resetVersion: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const widget = useRef<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let disposed = false;
    let script = document.querySelector<HTMLScriptElement>(`script[src="${scriptUrl}"]`);
    const render = () => {
      if (disposed || !container.current || !window.turnstile || widget.current !== null) return;
      try {
        setFailed(false);
        widget.current = window.turnstile.render(container.current, {
          sitekey: siteKey,
          action: "register",
          callback: (token) => {
            if (!disposed) { setFailed(false); onError(""); onToken(token); }
          },
          "expired-callback": () => {
            if (!disposed) { onToken(""); onError("验证码已过期，请重试。"); setFailed(true); }
          },
          "error-callback": () => {
            if (!disposed) { onToken(""); onError("验证码加载失败，请重试。"); setFailed(true); }
          },
          "timeout-callback": () => {
            if (!disposed) { onToken(""); onError("验证码操作超时，请重试。"); setFailed(true); }
          },
        });
      } catch {
        onToken("");
        onError("验证码加载失败，请重试。");
        setFailed(true);
      }
    };
    const loadFailed = () => {
      if (disposed) return;
      script?.remove();
      onToken("");
      onError("验证码服务不可用，请重试。");
      setFailed(true);
    };
    if (!script) {
      script = document.createElement("script");
      script.src = scriptUrl;
      script.async = true;
      document.head.appendChild(script);
    }
    script.addEventListener("load", render);
    script.addEventListener("error", loadFailed);
    render();
    return () => {
      disposed = true;
      script?.removeEventListener("load", render);
      script?.removeEventListener("error", loadFailed);
      if (widget.current !== null && window.turnstile) window.turnstile.remove(widget.current);
      widget.current = null;
    };
  }, [siteKey, onToken, onError, attempt]);

  useEffect(() => {
    if (resetVersion > 0 && widget.current !== null && window.turnstile) {
      window.turnstile.reset(widget.current);
    }
  }, [resetVersion]);

  return <><div ref={container} aria-label="注册验证码" />
    {failed && <button type="button" className="text-button" onClick={() => {
      onToken(""); setAttempt((value) => value + 1);
    }}>重试验证码</button>}</>;
}
