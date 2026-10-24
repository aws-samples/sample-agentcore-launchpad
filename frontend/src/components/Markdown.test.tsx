import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

const html = (text: string) => renderToStaticMarkup(<Markdown text={text} />);

describe("Markdown model-output repairs", () => {
  it("turns an in-band citation marker into a source chip", () => {
    const out = html("只申请编辑者。\uE200cite\uE20204-qdocs-roles.md\uE201");
    expect(out).toContain("<code>04-qdocs-roles.md</code>");
    expect(out).not.toMatch(/[\uE200-\uE2FF]|cite/);
  });

  it("names every source of a multi-source citation", () => {
    const out = html("审批后生效。\uE200cite\uE20203-access.md\uE20204-qdocs.md\uE201");
    expect(out).toContain("<code>03-access.md · 04-qdocs.md</code>");
  });

  it("drops non-cite markers and stray private-use characters", () => {
    expect(html("前\uE200navlist\uE202x\uE201后\uE203")).toBe('<div class="md"><p>前后</p></div>');
  });

  it("bolds ** runs that CommonMark leaves literal next to CJK punctuation", () => {
    const out = html("**结论：**在 AccessHub 申请「编辑者」。");
    expect(out).toContain("<strong>结论：</strong>在 AccessHub");
    expect(out).not.toContain("**");
  });

  it("keeps ordinary emphasis and never rewrites code", () => {
    expect(html("**ok** and `**raw**`")).toBe(
      '<div class="md"><p><strong>ok</strong> and <code>**raw**</code></p></div>',
    );
  });
});

describe("Markdown links", () => {
  it("routes a cited S3 source through the console instead of a new tab", () => {
    const out = html(
      "【来源：[QDocs 角色说明](https://b.s3.us-west-2.amazonaws.com/kb/KB1/04-qdocs-roles.md)】",
    );
    expect(out).toContain('data-testid="md-kb-source"');
    expect(out).toContain(">QDocs 角色说明</a>");
    expect(out).not.toContain('target="_blank"');
  });

  it("keeps every other link a plain new-tab link", () => {
    const out = html("[docs](https://example.com/a)");
    expect(out).toContain('target="_blank"');
    expect(out).not.toContain("md-kb-source");
  });
});

describe("Markdown footnotes", () => {
  const FN = "结论。[^roles]\n\n[^roles]: 知识库《QDocs 角色说明》。";

  it("keeps footnote links in the page instead of opening a new tab", () => {
    const out = html(FN);
    expect(out).toMatch(/<sup><a [^>]*href="#md-[A-Za-z0-9]*-fn-roles"/);
    expect(out).toMatch(/<li id="md-[A-Za-z0-9]*-fn-roles">/);
    expect(out).not.toContain('target="_blank"');
    expect(out).not.toContain("user-content-");
  });

  it("gives every rendered answer its own footnote ids", () => {
    const out = renderToStaticMarkup(
      <>
        <Markdown text={FN} />
        <Markdown text={FN} />
      </>,
    );
    const ids = [...out.matchAll(/<li id="([^"]+)"/g)].map((m) => m[1]);
    expect(ids).toHaveLength(2);
    expect(ids[0]).not.toBe(ids[1]);
  });
});

