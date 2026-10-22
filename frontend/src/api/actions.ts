import { apiUrl, request } from "./client";
import { newKey, TaskEvent } from "./types";
import { sha256Hex } from "./crypto";

const pendingKeys = new Map<string, string>();

export async function mutate<T>(path: string, method: "POST" | "PUT" | "PATCH" | "DELETE", body: object = {},
                                idempotencyKey?: string): Promise<T> {
  if (idempotencyKey) {
    return request<T>(path, { method, headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(body) });
  }
  const identity = await request<{ user_id: string; workspace_id: string | null }>("/identity/me");
  const payload = JSON.stringify(body);
  const fingerprint = await sha256Hex(`${identity.user_id}:${identity.workspace_id || "none"}:${method}:${path}:${payload}`);
  const storageKey = `atoms-mutation:${fingerprint}`;
  let key = pendingKeys.get(storageKey);
  try { key ||= sessionStorage.getItem(storageKey) || undefined; } catch { /* Session storage may be disabled. */ }
  if (!key) key = newKey();
  pendingKeys.set(storageKey, key);
  try { sessionStorage.setItem(storageKey, key); } catch { /* Keep key for this page session. */ }
  const result = await request<T>(path, { method, headers: { "Idempotency-Key": key }, body: payload });
  pendingKeys.delete(storageKey);
  try { sessionStorage.removeItem(storageKey); } catch { /* Storage may be disabled. */ }
  return result;
}

export function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "请求失败";
}

export function subscribeTaskEvents(taskId: string, after: number,
                                    onEvent: (event: TaskEvent) => void,
                                    onTerminal?: (state: string) => void): () => void {
  const stream = new EventSource(apiUrl(`/tasks/${taskId}/events?after=${after}`), { withCredentials: true });
  const receive = (message: Event) => {
    try {
      const event = JSON.parse((message as MessageEvent<string>).data) as TaskEvent;
      if (Number.isInteger(event.sequence) && event.sequence > 0) onEvent(event);
    } catch { /* A malformed event must not stop future events. */ }
  };
  for (const kind of ["queued", "generating", "building", "repairing", "succeeded", "failed"])
    stream.addEventListener(kind, receive);
  stream.addEventListener("terminal", (message: Event) => {
    try {
      const control = JSON.parse((message as MessageEvent<string>).data) as { state?: string };
      if (control.state) {
        stream.close();
        onTerminal?.(control.state);
      }
    } catch { /* A malformed control event is ignored. */ }
  });
  return () => stream.close();
}

export async function fileBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 8192) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  }
  return btoa(binary);
}
