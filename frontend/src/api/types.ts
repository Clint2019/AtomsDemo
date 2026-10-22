export interface Page<T> { items: T[]; next_cursor: string | null }
export interface Project {
  id: string; workspace_id: string; owner_id: string; name: string;
  visibility: "private" | "public"; revision: number; source_version_id: string | null;
}
export interface ProjectHead { project_id: string; version_id: string | null; revision: number }
export interface FileEntry { path: string; blob: { id: string; sha256: string; size_bytes: number; media_type: string } }
export interface Version {
  id: string; project_id: string; parent_id: string | null; files: FileEntry[];
  artifact: unknown; build_status: string; summary: string; created_at: string;
}
export interface Task {
  id: string; project_id: string; state: string; result_version_id: string | null;
  error_code: string | null;
  request: { message_id: string; model: string; agent_mode: string };
  [key: string]: unknown;
}
export interface TaskEvent { sequence: number; kind: string; message: string; created_at: string }
export interface CatalogEntry {
  id: string; kind: "template" | "public_project"; project_id: string; version_id: string;
  author_id: string; title: string; category: string; description: string;
    views: number; saves: number; revision: number; saved: boolean;
}
export interface PreviewTicket { id: string; project_id: string; version_id: string; url: string; expires_at: string }
export interface ShareLink { id: string; project_id: string; version_id: string; url: string; revoked: boolean }
export interface Publication {
  id: string; project_id: string; version_id: string; live_version_id: string | null;
  environment: string; domain: string | null; sync_db: boolean; migrate_data: boolean;
  state: string; public_url: string | null; pending_action: string | null; revision: number;
}

export { newKey } from "./crypto";
