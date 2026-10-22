export interface ProfileViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: ProfileViewContext): never {
  // TODO: profile UI, loading/empty/error/permission states and API integration.
  throw new Error("profile view not implemented");
}
