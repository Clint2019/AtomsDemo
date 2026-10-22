import { afterEach, describe, expect, it, vi } from "vitest";
import { mutate } from "./actions";

afterEach(() => vi.unstubAllGlobals());

describe("mutation retry", () => {
  it("reuses the same key after a response is lost and rotates it after success", async () => {
    vi.stubGlobal("document", { cookie: "" });
    let attempt = 0;
    const fetch = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/identity/me")) return Promise.resolve(new Response(JSON.stringify({ user_id: "u", workspace_id: "a" })));
      if (++attempt === 1) return Promise.reject(new TypeError("connection lost"));
      return Promise.resolve(new Response(JSON.stringify({ id: "project" }), { status: 201 }));
    });
    vi.stubGlobal("fetch", fetch);
    await expect(mutate("/projects", "POST", { name: "Example" })).rejects.toThrow();
    await expect(mutate("/projects", "POST", { name: "Example" })).resolves.toEqual({ id: "project" });
    await mutate("/projects", "POST", { name: "Example" });
    const keys = fetch.mock.calls.filter((call) => !call[0].endsWith("/identity/me"))
      .map((call) => call[1].headers.get("Idempotency-Key"));
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[1]);
  });

  it("separates unresolved keys by workspace", async () => {
    vi.stubGlobal("document", { cookie: "" });
    let workspace = "a";
    const fetch = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/identity/me")) return Promise.resolve(new Response(JSON.stringify({ user_id: "u", workspace_id: workspace })));
      return Promise.reject(new TypeError("response lost"));
    });
    vi.stubGlobal("fetch", fetch);
    await expect(mutate("/projects", "POST", { name: "Same" })).rejects.toThrow();
    workspace = "b";
    await expect(mutate("/projects", "POST", { name: "Same" })).rejects.toThrow();
    workspace = "a";
    await expect(mutate("/projects", "POST", { name: "Same" })).rejects.toThrow();
    const keys = fetch.mock.calls.filter((call) => !call[0].endsWith("/identity/me"))
      .map((call) => call[1].headers.get("Idempotency-Key"));
    expect(keys[0]).not.toBe(keys[1]);
    expect(keys[0]).toBe(keys[2]);
  });

  it("uses a caller-owned key for an unresolved generation submission", async () => {
    vi.stubGlobal("document", { cookie: "" });
    let attempt = 0;
    const fetch = vi.fn().mockImplementation(() => {
      if (++attempt === 1) return Promise.reject(new TypeError("response lost"));
      return Promise.resolve(new Response(JSON.stringify({ id: "original-task" }), { status: 202 }));
    });
    vi.stubGlobal("fetch", fetch);
    const request = { prompt: "Same idea", expected_head_revision: 0 };
    await expect(mutate("/projects/p/generate", "POST", request, "saved-generation-key")).rejects.toThrow();
    await expect(mutate("/projects/p/generate", "POST", request, "saved-generation-key"))
      .resolves.toEqual({ id: "original-task" });
    expect(fetch.mock.calls.map((call) => call[1].headers.get("Idempotency-Key")))
      .toEqual(["saved-generation-key", "saved-generation-key"]);
  });
});
