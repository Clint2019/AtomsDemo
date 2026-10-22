import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("API transport", () => {
  it("sends credentials and CSRF token on a mutation", async () => {
    vi.stubGlobal("document", { cookie: "csrf_token=local-test-token" });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "p1" }), { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    await expect(request<{ id: string }>("/projects", { method: "POST", body: "{}" }))
      .resolves.toEqual({ id: "p1" });
    const [, init] = fetch.mock.calls[0];
    expect(init.credentials).toBe("include");
    expect(init.headers.get("X-CSRF-Token")).toBe("local-test-token");
    expect(init.headers.get("Content-Type")).toBe("application/json");
  });

  it("preserves a structured server failure", async () => {
    vi.stubGlobal("document", { cookie: "" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ code: "not_implemented", message: "Unavailable", request_id: "r1", retryable: false }),
      { status: 501 },
    )));
    await expect(request("/projects")).rejects.toMatchObject({
      code: "not_implemented", status: 501, request_id: "r1",
    });
  });
});
