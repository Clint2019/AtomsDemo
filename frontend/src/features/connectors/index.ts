export interface ConnectorsViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: ConnectorsViewContext): never {
  // TODO: connectors UI, loading/empty/error/permission states and API integration.
  throw new Error("connectors view not implemented");
}
