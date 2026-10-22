import { afterEach, describe, expect, it, vi } from "vitest";
import { newKey, sha256Hex } from "./crypto";

afterEach(() => vi.unstubAllGlobals());

describe("HTTP IP browser fallback", () => {
  it("hashes standard vectors without crypto.subtle", async () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes });
    expect(await sha256Hex("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    expect(await sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    expect(await sha256Hex("a".repeat(1000))).toBe("41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3");
  });

  it("creates a version 4 key without crypto.randomUUID", () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => {
      for (let index = 0; index < bytes.length; index++) bytes[index] = index;
      return bytes;
    } });
    expect(newKey()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });
});
