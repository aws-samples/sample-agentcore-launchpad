import { describe, expect, it } from "vitest";

import { memoryRecordPlainText, memoryRecordSections } from "./memory-record";

// the shape the AgentCore summarization strategy wrote on prod (2026-11-02 QA)
const SUMMARY =
  '<topic name="用户信息"> 用户自我介绍名叫小蓝，偏好用表格形式获取回答。 </topic>\n' +
  '<topic name="Amazon Bedrock AgentCore 简介"> 助手以表格回答：\n\n| 维度 | 内容 |\n|---|---|\n| 是什么 | 托管服务 |\n</topic>';

describe("memoryRecordSections", () => {
  it("keeps a plain fact as one untitled section", () => {
    expect(memoryRecordSections("用户名叫小蓝。")).toEqual([{ title: null, body: "用户名叫小蓝。" }]);
  });

  it("splits summarization topics into titled markdown sections", () => {
    const sections = memoryRecordSections(SUMMARY);
    expect(sections.map((s) => s.title)).toEqual(["用户信息", "Amazon Bedrock AgentCore 简介"]);
    expect(sections[0].body).toBe("用户自我介绍名叫小蓝，偏好用表格形式获取回答。");
    expect(sections[1].body).toContain("| 维度 | 内容 |");
    expect(sections.some((s) => s.body.includes("<topic"))).toBe(false);
  });

  it("keeps text around topics, single-quoted names and escaped entities", () => {
    expect(memoryRecordSections("前言 <topic name='A &amp; B'>正文</topic> 结尾")).toEqual([
      { title: null, body: "前言" },
      { title: "A & B", body: "正文" },
      { title: null, body: "结尾" },
    ]);
  });

  it("leaves an unterminated tag as text", () => {
    expect(memoryRecordSections('<topic name="x">没有闭合')).toEqual([
      { title: null, body: '<topic name="x">没有闭合' },
    ]);
  });

  it("returns nothing for blank text", () => {
    expect(memoryRecordSections("  \n ")).toEqual([]);
  });
});

describe("memoryRecordPlainText", () => {
  it("flattens topics into one line for a clamped cell", () => {
    expect(memoryRecordPlainText('<topic name="用户信息">名叫\n小蓝</topic>')).toBe("用户信息：名叫 小蓝");
    expect(memoryRecordPlainText("plain fact")).toBe("plain fact");
  });
});
