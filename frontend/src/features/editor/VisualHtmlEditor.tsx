import { useMemo, useRef, useState } from "react";

const omitted = new Set(["SCRIPT", "STYLE", "LINK", "META", "BASE", "IFRAME", "OBJECT", "EMBED",
  "FORM", "INPUT", "TEXTAREA", "SELECT", "VIDEO", "AUDIO", "SVG", "CANVAS", "TEMPLATE", "NOSCRIPT"]);
const visible = new Set(["DIV", "SPAN", "P", "H1", "H2", "H3", "H4", "H5", "H6", "MAIN", "SECTION",
  "ARTICLE", "HEADER", "FOOTER", "ASIDE", "NAV", "UL", "OL", "LI", "A", "BUTTON", "LABEL", "B",
  "I", "EM", "STRONG", "SMALL", "BLOCKQUOTE", "PRE", "CODE", "BR", "HR"]);

export function supportsVisualHtml(source: string): boolean {
  // Detached HTML parsers can still fetch embedded resources. Keep this mode to inert static pages.
  return !(/<\s*(?:script|style|link|img|iframe|object|embed|video|audio|source|track|picture|svg|form|input|meta|base)\b/i.test(source) ||
    /(?:src|href|action|background)\s*=|url\s*\(|@import/i.test(source));
}

function sourceDocument(source: string) {
  return new DOMParser().parseFromString(source, "text/html");
}

function editableTextNodes(root: Node): Text[] {
  const nodes: Text[] = [];
  function walk(node: Node) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.textContent?.trim()) nodes.push(node as Text);
      return;
    }
    if (node.nodeType === Node.ELEMENT_NODE && omitted.has((node as Element).tagName)) return;
    node.childNodes.forEach(walk);
  }
  walk(root);
  return nodes;
}

function canvasHtml(source: string): string {
  const original = sourceDocument(source);
  const safe = document.implementation.createHTMLDocument("HTML 文字编辑");
  const policy = safe.createElement("meta");
  policy.httpEquiv = "Content-Security-Policy";
  policy.content = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; frame-src 'none'; base-uri 'none'";
  safe.head.prepend(policy);
  const style = safe.createElement("style");
  style.textContent = "body{font:16px system-ui;padding:24px;color:#1c2440} [data-text-index]{cursor:text} [data-text-index]:hover{outline:2px solid #565cf5}";
  safe.head.append(style);
  let index = 0;
  function append(node: Node, parent: Node) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (!node.textContent?.trim()) { parent.appendChild(safe.createTextNode(node.textContent || "")); return; }
      const wrapper = safe.createElement("span");
      wrapper.dataset.textIndex = String(index++);
      wrapper.textContent = node.textContent;
      parent.appendChild(wrapper);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const element = node as Element;
    if (omitted.has(element.tagName)) return;
    const destination = visible.has(element.tagName) ? safe.createElement(element.tagName.toLowerCase()) : parent;
    if (destination !== parent) parent.appendChild(destination);
    element.childNodes.forEach((child) => append(child, destination));
  }
  original.body.childNodes.forEach((child) => append(child, safe.body));
  return `<!DOCTYPE html>\n${safe.documentElement.outerHTML}`;
}

/** Edits only a text node; the iframe receives an inert copy of source HTML. */
export function VisualHtmlEditor({ source, onChange, disabled = false }: {
  source: string; onChange: (value: string) => void; disabled?: boolean;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [index, setIndex] = useState<number | null>(null);
  const [text, setText] = useState("");
  const canvas = useMemo(() => canvasHtml(source), [source]);

  function loaded() {
    setIndex(null);
    frame.current?.contentDocument?.addEventListener("click", (event) => {
      event.preventDefault();
      const target = event.target as Element | null;
      const wrapper = target?.closest("[data-text-index]");
      if (!wrapper) return;
      setIndex(Number((wrapper as HTMLElement).dataset.textIndex));
      setText(wrapper.textContent || "");
    });
  }

  function apply() {
    if (index === null || disabled) return;
    const original = sourceDocument(source);
    const node = editableTextNodes(original.body)[index];
    if (!node) return;
    node.textContent = text;
    onChange(`<!DOCTYPE html>\n${original.documentElement.outerHTML}`);
    setIndex(null);
  }

  return <div className="stack">
    <p className="muted">点击画布文字后修改。画布不加载原页面脚本、样式或网络资源；保存会经正常构建流程生成新版本。</p>
    <iframe ref={frame} title="HTML 可视化文字编辑" className="visual-frame" sandbox="allow-same-origin"
      srcDoc={canvas} onLoad={loaded} style={{ pointerEvents: disabled ? "none" : undefined }} />
    {index !== null ? <div className="inline-form"><label>选中的文字<input value={text} disabled={disabled}
      onChange={(event) => setText(event.target.value)} /></label>
      <button type="button" onClick={apply} disabled={disabled}>应用文字</button></div> : <p className="muted">尚未选中文字。</p>}
  </div>;
}
