import { describe, expect, it } from "vitest";
import * as ts from "typescript";
import { replaceStaticTsxText, staticTsxText } from "./tsxStaticText";

describe("static JSX text mapping", () => {
  const source = `const secret = "do not touch";
export default function App() {
  const count = 3;
  return <main>
    <h1>  我的阅读清单  </h1>
    <p>已有 {count} 本书</p>
  </main>;
}`;

  it("only locates literal JSX text and preserves expressions", () => {
    const result = staticTsxText(source, ts);
    expect(result.error).toBeNull();
    expect(result.spans.map((span) => span.label)).toEqual(["我的阅读清单", "已有", "本书"]);
    expect(result.spans[0].tag).toBe("h1");
    expect(source.slice(result.spans[0].start, result.spans[0].end)).toBe(result.spans[0].raw);
    const edited = replaceStaticTsxText(source, result.spans[0], "A & B < {C}", ts);
    expect(edited).toContain("<h1>  A &amp; B &lt; &#123;C&#125;  </h1>");
    expect(edited).toContain("{count}");
    expect(edited).toContain('"do not touch"');
    expect(staticTsxText(edited, ts).error).toBeNull();
  });

  it("rejects stale ranges, long values, and invalid TSX", () => {
    const span = staticTsxText(source, ts).spans[0];
    expect(() => replaceStaticTsxText(source + "\n", span, "ok", ts)).not.toThrow();
    expect(() => replaceStaticTsxText(source.replace("我的", "你的"), span, "ok", ts)).toThrow();
    expect(() => replaceStaticTsxText(source, span, "x".repeat(301), ts)).toThrow();
    expect(staticTsxText("return <main><h1></main>", ts).error).not.toBeNull();
  });

  it("round trips entities and significant internal whitespace", () => {
    const entitySource = "const UI=()=> <><h1>A &amp; B &#123;C&#125;</h1><pre>A  B</pre><p>&copy;</p></>";
    const result = staticTsxText(entitySource, ts);
    expect(result.spans.map((span) => span.label)).toEqual(["A & B {C}", "A  B"]);
    expect(replaceStaticTsxText(entitySource, result.spans[0], result.spans[0].label, ts)).toBe(entitySource);
    expect(replaceStaticTsxText(entitySource, result.spans[1], result.spans[1].label, ts)).toBe(entitySource);
    const changed = replaceStaticTsxText(entitySource, result.spans[0], "X & Y", ts);
    expect(changed).toContain("<h1>X &amp; Y</h1>");
    expect(changed).toContain("<pre>A  B</pre>");
  });

  it("keeps edited entity newlines as JSX text rather than source formatting", () => {
    const sourceWithNewline = "const UI=()=> <pre>A&#10;B</pre>";
    const span = staticTsxText(sourceWithNewline, ts).spans[0];
    expect(span.label).toBe("A\nB");
    const edited = replaceStaticTsxText(sourceWithNewline, span, "X\nB", ts);
    expect(edited).toContain("<pre>X&#10;B</pre>");
    expect(ts.transpileModule(edited, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX } })
      .outputText).toContain('children: "X\\nB"');
  });

  it("edits direct string expressions without touching props or dynamic expressions", () => {
    const expressionSource = `const title = "variable";
const UI = () => <><h1>{"原始标题"}</h1><p>{\`说明文字\`}</p><label title={"属性文字"}>{title}</label><i>{\`插值 ${"${title}"}\`}</i></>`;
    const result = staticTsxText(expressionSource, ts);
    expect(result.error).toBeNull();
    expect(result.spans.map((span) => [span.kind, span.label, span.tag])).toEqual([
      ["string_literal", "原始标题", "h1"],
      ["string_literal", "说明文字", "p"],
    ]);
    const edited = replaceStaticTsxText(expressionSource, result.spans[0], 'A "quote"\n{brace}', ts);
    expect(edited).toContain('{"A \\"quote\\"\\n{brace}"}');
    expect(edited).toContain('title={"属性文字"}');
    expect(edited).toContain('{title}');
    expect(ts.transpileModule(edited, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX } })
      .outputText).toContain('A \\"quote\\"\\n{brace}');
    expect(staticTsxText(edited, ts).spans[0].label).toBe('A "quote"\n{brace}');
  });

  it("round trips a plain template literal containing escape-sensitive text", () => {
    const sourceWithTemplate = 'const UI=()=> <p>{`old`}</p>';
    const span = staticTsxText(sourceWithTemplate, ts).spans[0];
    const value = 'slash \\ tick ` and ${value}\u2028\u2029';
    const edited = replaceStaticTsxText(sourceWithTemplate, span, value, ts);
    expect(staticTsxText(edited, ts).spans[0].label).toBe(value);
    expect(edited).toContain('\\u2028\\u2029');
    expect(edited).toContain('${value}');
  });
});
