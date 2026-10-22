import { useEffect, useMemo, useState } from "react";
import { replaceStaticTsxText, staticTsxText } from "./tsxStaticText";
import type { StaticTextSpan } from "./tsxStaticText";

type Parser = typeof import("typescript");

/** Maps JSX literal text to source offsets without running component code. */
export function StaticTsxEditor({ source, onChange, disabled = false }: {
  source: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const [parser, setParser] = useState<Parser | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [selected, setSelected] = useState<{
    index: number; source: string; span: StaticTextSpan;
  } | null>(null);
  const [text, setText] = useState("");
  const [editError, setEditError] = useState("");

  useEffect(() => {
    let active = true;
    import("typescript").then((module) => {
      if (active) setParser(module);
    }).catch(() => { if (active) setLoadError(true); });
    return () => { active = false; };
  }, []);

  const result = useMemo(() => parser ? staticTsxText(source, parser) : null,
    [parser, source]);

  function apply() {
    if (!selected || !parser || !result || disabled) return;
    if (source !== selected.source) {
      setEditError("源码已变化，请重新选择文字。");
      return;
    }
    try {
      onChange(replaceStaticTsxText(source, selected.span, text, parser));
      setSelected(null);
      setEditError("");
    } catch (cause) {
      setEditError(cause instanceof Error ? cause.message : "修改失败，请重新选择文字。");
    }
  }

  return <div className="stack">
    <p className="muted">这里列出 TSX 中可定位的静态 JSX 文字和直接写出的字符串表达式。选择文字并修改后，可按下方按钮保存并构建；组件代码、变量表达式和动态数据不会在编辑器中执行。含不支持实体的文字请用源码编辑。</p>
    {loadError && <p role="alert" className="notice error">TSX 解析器加载失败，请稍后重试。</p>}
    {!parser && !loadError && <p role="status">正在准备 TSX 文字映射…</p>}
    {result?.error && <p role="alert" className="notice error">{result.error}</p>}
    {result && !result.error && result.spans.length === 0 &&
      <p className="notice">此文件没有可编辑的静态 JSX 文字。动态内容请使用源码编辑。</p>}
    {result && !result.error && result.spans.length > 0 && <div className="tsx-text-canvas">
      {result.spans.map((span, index) => <button key={`${span.start}:${span.end}`}
        type="button" disabled={disabled} className={selected?.index === index && selected.source === source
          ? "tsx-text-item active" : "tsx-text-item"}
        onClick={() => { setSelected({ index, source, span }); setText(span.label); setEditError(""); }}>
        <small>{span.tag}</small><span>{span.label}</span>
      </button>)}
    </div>}
    {selected && <div className="inline-form">
      <label>选中的文字<textarea value={text} maxLength={300} rows={2} disabled={disabled}
        onChange={(event) => setText(event.target.value)} /></label>
      <button type="button" onClick={apply} disabled={disabled}>应用文字</button>
    </div>}
    {editError && <p role="alert" className="notice error">{editError}</p>}
  </div>;
}
