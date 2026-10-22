/** Keep opaque identifiers usable for support without making them dominate the UI. */
export function formatId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 6)}…${id.slice(-4)}` : id;
}
