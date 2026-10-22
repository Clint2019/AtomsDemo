export interface PublicationViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: PublicationViewContext): never {
  // TODO: publication UI, loading/empty/error/permission states and API integration.
  throw new Error("publication view not implemented");
}
