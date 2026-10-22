export interface SettingsViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: SettingsViewContext): never {
  // TODO: settings UI, loading/empty/error/permission states and API integration.
  throw new Error("settings view not implemented");
}
