export interface AuthViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: AuthViewContext): never {
  // TODO: auth UI, loading/empty/error/permission states and API integration.
  throw new Error("auth view not implemented");
}
