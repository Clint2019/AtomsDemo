import { describe, expect, it } from "vitest";
import { classifyChatIntent } from "../../chatIntent";

describe("classifyChatIntent", () => {
  it.each([
    "如何创建一个求职管理网页",
    "怎么创建一个日常管理任务？",
    "我想知道如何创建一个任务管理网站",
    "How to create a job tracker?",
    "这个应用需要哪些字段？",
  ])("answers a question instead of building: %s", (prompt) => {
    expect(classifyChatIntent(prompt)).toBe("answer");
  });

  it.each([
    "请帮我创建一个求职管理网页",
    "直接生成一个任务管理应用",
    "把首页标题改成求职管理",
    "Build a job tracker site",
  ])("recognizes an explicit build instruction: %s", (prompt) => {
    expect(classifyChatIntent(prompt)).toBe("build");
  });

  it.each([
    "求职管理网页",
    "帮我创建一个网页可以吗？",
    "怎么创建一个网页，再直接帮我做一个",
    "",
  ])("asks for clarification when intent is mixed or missing: %s", (prompt) => {
    expect(classifyChatIntent(prompt)).toBe("clarify");
  });
});
