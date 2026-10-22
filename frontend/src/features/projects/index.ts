export interface ProjectsViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: ProjectsViewContext): never {
  // TODO: projects UI, loading/empty/error/permission states and API integration.
  throw new Error("projects view not implemented");
}
