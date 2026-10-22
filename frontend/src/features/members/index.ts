export interface MembersViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: MembersViewContext): never {
  // TODO: members UI, loading/empty/error/permission states and API integration.
  throw new Error("members view not implemented");
}
