import type * as TypeScript from "typescript";

export interface StaticTextSpan {
  kind: "jsx_text" | "string_literal";
  start: number;
  end: number;
  raw: string;
  label: string;
  tag: string;
}

export interface StaticTextResult {
  spans: StaticTextSpan[];
  error: string | null;
}

type Parser = typeof import("typescript");

const namedEntities: Record<string, string> = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0",
};

function decodeKnownEntities(value: string): string | null {
  let supported = true;
  const decoded = value.replace(/&(#(?:[xX][0-9a-fA-F]+|[0-9]+)|[a-zA-Z][a-zA-Z0-9]+);/g,
    (match, entity: string) => {
      if (entity.startsWith("#")) {
        const hex = entity[1]?.toLowerCase() === "x";
        const codepoint = Number.parseInt(entity.slice(hex ? 2 : 1), hex ? 16 : 10);
        if (codepoint > 0 && codepoint <= 0x10ffff &&
            !(codepoint >= 0xd800 && codepoint <= 0xdfff)) {
          return String.fromCodePoint(codepoint);
        }
      } else if (Object.hasOwn(namedEntities, entity)) {
        return namedEntities[entity];
      }
      supported = false;
      return match;
    });
  return supported ? decoded : null;
}

function parseError(file: TypeScript.SourceFile): boolean {
  return Boolean((file as TypeScript.SourceFile & {
    parseDiagnostics?: readonly TypeScript.Diagnostic[];
  }).parseDiagnostics?.length);
}

export function staticTsxText(source: string, parser: Parser): StaticTextResult {
  const file = parser.createSourceFile("visual.tsx", source, parser.ScriptTarget.Latest,
    true, parser.ScriptKind.TSX);
  if (parseError(file)) return { spans: [], error: "TSX 语法有误，请先在源码页修复。" };
  const spans: StaticTextSpan[] = [];
  function visit(node: TypeScript.Node) {
    if (parser.isJsxText(node)) {
      const start = node.getStart(file);
      const end = node.getEnd();
      const raw = source.slice(start, end);
      const label = decodeKnownEntities(raw.trim());
      if (label?.trim()) {
        const parent = node.parent;
        const tag = parser.isJsxElement(parent)
          ? parent.openingElement.tagName.getText(file)
          : "fragment";
        spans.push({ kind: "jsx_text", start, end, raw, label, tag });
      }
    }
    if (parser.isJsxExpression(node) && node.expression &&
        (parser.isJsxElement(node.parent) || parser.isJsxFragment(node.parent)) &&
        (parser.isStringLiteral(node.expression) ||
         parser.isNoSubstitutionTemplateLiteral(node.expression))) {
      const literal = node.expression;
      const start = literal.getStart(file);
      const end = literal.getEnd();
      const label = literal.text;
      if (label.trim()) {
        const parent = node.parent;
        const tag = parser.isJsxElement(parent)
          ? parent.openingElement.tagName.getText(file)
          : "fragment";
        spans.push({ kind: "string_literal", start, end,
          raw: source.slice(start, end), label, tag });
      }
    }
    parser.forEachChild(node, visit);
  }
  visit(file);
  return { spans, error: null };
}

export function replaceStaticTsxText(source: string, span: StaticTextSpan,
                                     value: string, parser: Parser): string {
  if (source.slice(span.start, span.end) !== span.raw || value.length > 300) {
    throw new Error("源码已变化或文字过长，请重新选择。");
  }
  if (value === span.label) return source;
  let replacement: string;
  if (span.kind === "string_literal") {
    replacement = JSON.stringify(value).replace(/\u2028/g, "\\u2028")
      .replace(/\u2029/g, "\\u2029");
  } else {
    const leading = span.raw.match(/^\s*/)?.[0] || "";
    const trailing = span.raw.match(/\s*$/)?.[0] || "";
    const escaped = value.replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/\{/g, "&#123;").replace(/\}/g, "&#125;")
      .replace(/\r/g, "&#13;").replace(/\n/g, "&#10;").replace(/\t/g, "&#9;");
    replacement = leading + escaped + trailing;
  }
  const updated = source.slice(0, span.start) + replacement + source.slice(span.end);
  if (staticTsxText(updated, parser).error) {
    throw new Error("修改后的 TSX 无法解析，请调整文字。");
  }
  return updated;
}
