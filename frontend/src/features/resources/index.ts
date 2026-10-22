export interface ResourcesViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: ResourcesViewContext): never {
  // TODO: resources UI, loading/empty/error/permission states and API integration.
  throw new Error("resources view not implemented");
}
