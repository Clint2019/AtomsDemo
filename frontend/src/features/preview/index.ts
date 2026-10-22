export interface PreviewViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: PreviewViewContext): never {
  // TODO: preview UI, loading/empty/error/permission states and API integration.
  throw new Error("preview view not implemented");
}
