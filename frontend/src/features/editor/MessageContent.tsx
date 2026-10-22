import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./message-content.css";

export function MessageContent({ role, content }: { role: string; content: string }) {
  if (role !== "assistant") return <p className="message-plain">{content}</p>;

  return <div className="markdown-body">
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{
      a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
      img: ({ node: _node, ...props }) => <img {...props} loading="lazy" referrerPolicy="no-referrer" />,
      table: ({ node: _node, ...props }) => <div className="markdown-table-scroll"><table {...props} /></div>,
    }}>{content}</ReactMarkdown>
  </div>;
}
