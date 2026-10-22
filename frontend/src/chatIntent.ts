export type ChatIntent = "answer" | "build" | "clarify";

// Only route high-confidence instructions automatically. A question must never
// start a build merely because it contains words such as “创建” or “build”.
export function classifyChatIntent(input: string): ChatIntent {
  const prompt = input.trim();
  if (!prompt) return "clarify";

  const asksForGuidance = /如何|怎么|怎样|为什么|是什么|有哪些|步骤|教程|教我|解释|说明|请问|能否|可不可以|是否|\bhow\s+(?:to|do|can)\b|\bwhat\s+(?:is|are)\b|\bwhy\b|\bexplain\b|[?？]/i.test(prompt);
  const asksForAction = /(?:^|[，,。；;\s])(?:请|再|现在|直接|马上|立刻|帮我|给我|替我|为我|我要|我想|需要你){0,2}\s*(?:帮我|给我|替我|为我)?\s*(?:创建|生成|制作|做|搭建|开发|实现|修改|新增|添加|删除|修复|重构|部署|构建|更新)|(?:^|[，,。；;\s])把.{1,40}(?:改成|改为)|\b(?:build|create|generate|implement|fix|change|add|remove|deploy)\b/i.test(prompt);
  const mixedQuestion = /[，,。；;]\s*(?:再|现在|直接|马上|立刻|帮我|给我|替我|为我).{0,12}(?:创建|生成|制作|做|搭建|开发|实现|修改|新增|添加|删除|修复|重构|部署|构建|更新)/.test(prompt);
  const guidanceOpening = /^(?:如何|怎么|怎样|请问|我想知道|想了解|教我|解释|说明|how\s|what\s|why\s)/i.test(prompt);

  if (asksForGuidance) return mixedQuestion || (asksForAction && !guidanceOpening && /[?？]/.test(prompt))
    ? "clarify" : "answer";
  return asksForAction ? "build" : "clarify";
}
