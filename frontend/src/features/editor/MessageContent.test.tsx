import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MessageContent } from "./MessageContent";

function render(role: string, content: string) {
  return renderToStaticMarkup(<MessageContent role={role} content={content} />);
}

describe("MessageContent", () => {
  it("renders the answer format shown in the conversation", () => {
    const html = render("assistant", "当前项目中已经包含一个求职管理系统。\n\n1. **数据看板**：展示统计数据。\n2. **申请管理**：记录公司和职位。");

    expect(html).toContain("<ol>");
    expect(html).toContain("<li><strong>数据看板</strong>：展示统计数据。</li>");
    expect(html).toContain("<li><strong>申请管理</strong>：记录公司和职位。</li>");
    expect(html).not.toContain("**数据看板**");
  });

  it("renders common Markdown and GFM structures", () => {
    const html = render("assistant", [
      "## 标题", "", "> 引用", "", "- [x] 完成", "- [ ] 待办", "",
      "| 项目 | 状态 |", "| --- | --- |", "| A | ~~旧~~ 新 |", "",
      "`inline` 和 [链接](https://example.com)", "", "```ts", "const answer = 42", "```",
    ].join("\n"));

    expect(html).toContain("<h2>标题</h2>");
    expect(html).toContain("<blockquote>");
    expect(html).toContain('type="checkbox"');
    expect(html).toContain("<table>");
    expect(html).toContain("<del>旧</del>");
    expect(html).toContain("<code>inline</code>");
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain("<pre><code");
  });

  it("does not execute raw HTML or unsafe URLs", () => {
    const html = render("assistant", '<img src="x" onerror="alert(1)">\n\n[危险链接](javascript:alert(1))');

    expect(html).not.toContain("<img");
    expect(html).not.toContain("onerror");
    expect(html).not.toContain("javascript:");
  });

  it("keeps user and tool messages as literal text", () => {
    for (const role of ["user", "tool"]) {
      const html = render(role, "**原样**\n<script>alert(1)</script>");
      expect(html).toContain("**原样**");
      expect(html).toContain("&lt;script&gt;");
      expect(html).not.toContain("<strong>");
    }
  });
});
