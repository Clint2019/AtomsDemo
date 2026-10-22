/** The preview URL is a bearer capability; only these two runtime routes contain its token. */
export function previewToken(url: string): string | null {
  let path: string;
  try { path = new URL(url).pathname; }
  catch { return null; }
  return /^\/(?:p|preview)\/([A-Za-z0-9_-]{1,128})\/?$/.exec(path)?.[1] || null;
}

export function previewMessageOrigin(url: string, pageHostname: string): string {
  const parsed = new URL(url);
  return parsed.hostname === pageHostname ? "null" : parsed.origin;
}

export function isPreviewReady(event: Pick<MessageEvent, "source" | "origin" | "data">,
                               frameWindow: Window | null, origin: string, token: string): boolean {
  const message = event.data;
  return frameWindow !== null && event.source === frameWindow && event.origin === origin &&
    typeof message === "object" && message !== null &&
    message.type === "atoms:app-session-ready" && message.previewToken === token;
}

export function pingPreview(frameWindow: Window | null, origin: string): void {
  frameWindow?.postMessage({ type: "atoms:app-session-ping" },
    origin === "null" ? "*" : origin);
}

export function sendPreviewSession(frameWindow: Window | null, origin: string,
                                   token: string, sessionId: string): void {
  frameWindow?.postMessage({ type: "atoms:app-session", previewToken: token, token: sessionId },
    origin === "null" ? "*" : origin);
}
