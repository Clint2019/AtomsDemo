export interface CatalogViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: CatalogViewContext): never {
  // TODO: catalog UI, loading/empty/error/permission states and API integration.
  throw new Error("catalog view not implemented");
}
