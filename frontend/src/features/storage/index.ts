export interface StorageViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: StorageViewContext): never {
  // TODO: storage UI, loading/empty/error/permission states and API integration.
  throw new Error("storage view not implemented");
}
