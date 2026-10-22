export interface BillingViewContext {
  workspaceId: string | null;
  projectId: string | null;
}

export function createView(context: BillingViewContext): never {
  // TODO: billing UI, loading/empty/error/permission states and API integration.
  throw new Error("billing view not implemented");
}
