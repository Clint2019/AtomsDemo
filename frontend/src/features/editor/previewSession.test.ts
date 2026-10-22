import { describe, expect, it, vi } from "vitest";
import { isPreviewReady, pingPreview, previewMessageOrigin, previewToken,
  sendPreviewSession } from "./previewSession";

const token = "preview_token_123";
const ipUrl = `https://8.137.197.104:18443/p/${token}/`;
const namedUrl = `https://v-123.p-456.preview.example.com/preview/${token}`;

describe("authenticated preview bridge", () => {
  it("extracts only preview ticket paths and keeps an IP iframe opaque", () => {
    expect(previewToken(ipUrl)).toBe(token);
    expect(previewToken(namedUrl)).toBe(token);
    expect(previewToken(`https://example.com/share/${token}`)).toBeNull();
    expect(previewMessageOrigin(ipUrl, "8.137.197.104")).toBe("null");
    expect(previewMessageOrigin(namedUrl, "app.example.com"))
      .toBe("https://v-123.p-456.preview.example.com");
  });

  it("rejects ready messages from another frame, origin, or ticket", () => {
    const frame = {} as Window;
    const ready = { source: frame, origin: "null",
      data: { type: "atoms:app-session-ready", previewToken: token } };
    expect(isPreviewReady(ready, frame, "null", token)).toBe(true);
    expect(isPreviewReady({ ...ready, source: {} as Window }, frame, "null", token)).toBe(false);
    expect(isPreviewReady({ ...ready, origin: "https://evil.example" }, frame, "null", token)).toBe(false);
    expect(isPreviewReady(ready, frame, "null", "different-ticket")).toBe(false);
    expect(isPreviewReady({ ...ready, data: { type: "wrong", previewToken: token } },
      frame, "null", token)).toBe(false);
  });

  it("pings without a credential and sends the app session only to the accepted frame", () => {
    const postMessage = vi.fn();
    const frame = { postMessage } as unknown as Window;
    pingPreview(frame, "null");
    expect(postMessage).toHaveBeenCalledWith({ type: "atoms:app-session-ping" }, "*");
    sendPreviewSession(frame, "null", token, "scoped-app-session");
    expect(postMessage).toHaveBeenLastCalledWith({ type: "atoms:app-session",
      previewToken: token, token: "scoped-app-session" }, "*");
    postMessage.mockClear();
    pingPreview(frame, "https://preview.example.com");
    expect(postMessage).toHaveBeenCalledWith({ type: "atoms:app-session-ping" },
      "https://preview.example.com");
  });
});
