export interface DashboardViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: DashboardViewContext): never {
  // TODO: dashboard UI, loading/empty/error/permission states and API integration.
  throw new Error("dashboard view not implemented");
}
