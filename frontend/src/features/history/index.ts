export interface HistoryViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: HistoryViewContext): never {
  // TODO: history UI, loading/empty/error/permission states and API integration.
  throw new Error("history view not implemented");
}
