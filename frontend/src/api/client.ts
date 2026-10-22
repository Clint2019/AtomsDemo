export interface ApiError {
  code: string;
  message: string;
  request_id: string;
  retryable: boolean;
}

export class ApiRequestError extends Error implements ApiError {
  constructor(
    public code: string,
    message: string,
    public request_id: string,
    public retryable: boolean,
    public status: number,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

const apiBase = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, "");

export function apiUrl(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//")) throw new Error("API path must be absolute and local");
  return `${apiBase}${path}`;
}

function csrfToken(): string | undefined {
  const pair = document.cookie.split("; ").find((item) => item.startsWith("csrf_token="));
  return pair ? decodeURIComponent(pair.slice("csrf_token=".length)) : undefined;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("API path must be absolute and local");
  }
  const method = (init?.method || "GET").toUpperCase();
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD"].includes(method)) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  let response: Response;
  try {
    response = await fetch(apiUrl(path), { ...init, method, headers, credentials: "include" });
  } catch {
    throw new ApiRequestError("network_error", "无法连接服务，请检查网络后重试。", "", true, 0);
  }
  if (response.status === 204) return undefined as T;
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = payload as Partial<ApiError> | null;
    throw new ApiRequestError(
      error?.code || "http_error", error?.message || `请求失败 (${response.status})`,
      error?.request_id || response.headers.get("X-Request-ID") || "",
      error?.retryable || false, response.status,
    );
  }
  return payload as T;
}
