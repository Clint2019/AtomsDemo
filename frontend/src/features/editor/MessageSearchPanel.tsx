import { FormEvent, useEffect, useRef, useState } from "react";
import { errorMessage } from "../../api/actions";
import { request } from "../../api/client";
import { Page } from "../../api/types";
import "./message-search.css";

interface SearchMessage {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  created_at: string;
  task_id: string | null;
}

export function MessageSearchPanel({ projectId, onClose }: {
  projectId: string;
  onClose: () => void;
}) {
  const [term, setTerm] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [matches, setMatches] = useState<SearchMessage[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestSequence = useRef(0);

  useEffect(() => {
    requestSequence.current += 1;
    setTerm(""); setSubmitted(""); setMatches([]); setCursor(null); setError("");
    return () => { requestSequence.current += 1; };
  }, [projectId]);

  async function search(query: string, nextCursor: string | null) {
    const clean = query.trim();
    if (!clean || clean.length > 200) {
      setError("请输入 1–200 个字符的搜索词。");
      return;
    }
    const sequence = ++requestSequence.current;
    setLoading(true); setError("");
    if (!nextCursor) {
      setSubmitted(clean);
      setMatches([]);
      setCursor(null);
    }
    try {
      const path = `/projects/${encodeURIComponent(projectId)}/messages/search?q=${encodeURIComponent(clean)}&limit=20` +
        (nextCursor ? `&cursor=${encodeURIComponent(nextCursor)}` : "");
      const page = await request<Page<SearchMessage>>(path);
      if (sequence !== requestSequence.current) return;
      setMatches((current) => nextCursor ? [...current, ...page.items] : page.items);
      setCursor(page.next_cursor);
    } catch (cause) {
      if (sequence === requestSequence.current) setError(errorMessage(cause));
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void search(term, null);
  }

  return <section className="message-search-panel" aria-label="搜索对话消息">
    <div className="message-search-head"><h2>搜索对话</h2><button type="button" onClick={onClose} aria-label="关闭消息搜索">×</button></div>
    <form onSubmit={submit} className="message-search-form">
      <label className="sr-only" htmlFor="message-search-query">搜索当前项目的消息</label>
      <input id="message-search-query" type="search" value={term} maxLength={200}
        autoFocus placeholder="搜索当前项目的消息" onChange={(event) => setTerm(event.target.value)} />
      <button type="submit" disabled={loading || !term.trim()}>搜索</button>
    </form>
    {error && <p role="alert" className="message-search-error">{error}</p>}
    {loading && <p role="status" className="message-search-hint">正在搜索…</p>}
    {!loading && submitted && matches.length === 0 && !error &&
      <p className="message-search-hint">没有找到包含“{submitted}”的消息。</p>}
    <div className="message-search-results" aria-live="polite">
      {matches.map((item) => <article key={item.id} className="message-search-result">
        <div><strong>{item.role === "user" ? "你" : item.role === "assistant" ? "Atoms" : "工具"}</strong>
          <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString("zh-CN")}</time></div>
        <p>{item.content}</p>
      </article>)}
    </div>
    {cursor && <button type="button" className="message-search-more" disabled={loading}
      onClick={() => void search(submitted, cursor)}>加载更早的匹配消息</button>}
  </section>;
}
