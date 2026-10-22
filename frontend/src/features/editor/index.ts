export interface EditorViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: EditorViewContext): never {
  // TODO: editor UI, loading/empty/error/permission states and API integration.
  throw new Error("editor view not implemented");
}
