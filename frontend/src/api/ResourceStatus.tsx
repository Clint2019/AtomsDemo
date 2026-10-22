import { ReactNode } from "react";

export function ResourceStatus({ loading, error, empty, children, retry }: {
  loading: boolean; error: string | null; empty?: boolean; children: ReactNode; retry?: () => void;
}) {
  if (loading) return <p role="status">正在加载…</p>;
  if (error) return <div className="notice error" role="alert">{error} {retry && <button onClick={retry}>重试</button>}</div>;
  if (empty) return <p className="muted">暂无内容。</p>;
  return <>{children}</>;
}
