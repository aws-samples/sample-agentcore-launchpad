"""Render samples/research-demo/demo-script.zh-CN.md into a self-contained HTML page.

Run with the backend venv (it has markdown-it-py):
  backend/.venv/bin/python -I build_html.py <in.md> <out.html>
"""
import html
import re
import sys

from markdown_it import MarkdownIt

src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()

md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
tokens = md.parse(text)

# heading ids + TOC
toc = []
n2 = n3 = 0
for i, t in enumerate(tokens):
    if t.type == "heading_open" and t.tag in ("h2", "h3"):
        title = tokens[i + 1].content
        if t.tag == "h2":
            n2 += 1
            n3 = 0
            hid = f"s{n2}"
        else:
            n3 += 1
            hid = f"s{n2}-{n3}"
        t.attrSet("id", hid)
        toc.append((t.tag, hid, title))

body = md.renderer.render(tokens, md.options, {})
h1 = re.search(r"<h1>(.*?)</h1>", body).group(1)

# task-list checkboxes
body = re.sub(r"<li>\[ \] ", '<li class="task"><input type="checkbox" aria-label="准备项"> ', body)
# tables scroll horizontally on narrow screens
body = body.replace("<table>", '<div class="table-scroll"><table>').replace("</table>", "</table></div>")
# image + italic caption paragraph -> figure
body = re.sub(
    r'<p><img src="([^"]+)" alt="([^"]*)" />\s*<em>(.*?)</em></p>',
    lambda m: (f'<figure><a href="{m.group(1)}" target="_blank" rel="noopener">'
               f'<img src="{m.group(1)}" alt="{m.group(2)}" loading="lazy"></a>'
               f'<figcaption>{m.group(3)}</figcaption></figure>'),
    body, flags=re.S)
# code blocks get a copy button
body = re.sub(r"<pre><code([^>]*)>",
              r'<div class="code"><button type="button" class="copy" aria-label="复制代码">复制</button><pre><code\1>',
              body)
body = body.replace("</code></pre>", "</code></pre></div>")
# external links open in a new tab
body = re.sub(r'<a href="(https?://[^"]+)">', r'<a href="\1" target="_blank" rel="noopener">', body)


def toc_html():
    out, open_sub = [], False
    for tag, hid, title in toc:
        label = html.escape(title)
        if tag == "h2":
            if open_sub:
                out.append("</ul></li>")
                open_sub = False
            out.append(f'<li><a href="#{hid}">{label}</a><ul>')
            open_sub = True
        else:
            out.append(f'<li class="sub"><a href="#{hid}">{label}</a></li>')
    if open_sub:
        out.append("</ul></li>")
    return "\n".join(out).replace("<ul></ul>", "")


CSS = r"""
:root {
  --bg: #f3f5f7; --surface: #ffffff; --text: #1f2a37; --muted: #5f6b7a; --heading: #142336;
  --border: #dce2e8; --accent: #175d9b; --accent-hover: #0f4271; --code-bg: #f5f7fa; --inline-code: #eaeff5;
  --th: #edf2f7; --stripe: #fafbfc; --quote-bg: #f1f6fb; --quote-border: #7897b3; --btn: #ffffff; --btn-hover: #e9eff6;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1419; --surface: #171d24; --text: #d7dee6; --muted: #93a0ae; --heading: #eef3f8;
    --border: #2b3540; --accent: #6fb2f2; --accent-hover: #9ccbf7; --code-bg: #11171d; --inline-code: #242d37;
    --th: #1f2730; --stripe: #1a2129; --quote-bg: #18222d; --quote-border: #4f7699; --btn: #1f2730; --btn-hover: #2a3542;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1419; --surface: #171d24; --text: #d7dee6; --muted: #93a0ae; --heading: #eef3f8;
  --border: #2b3540; --accent: #6fb2f2; --accent-hover: #9ccbf7; --code-bg: #11171d; --inline-code: #242d37;
  --th: #1f2730; --stripe: #1a2129; --quote-bg: #18222d; --quote-border: #4f7699; --btn: #1f2730; --btn-hover: #2a3542;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; background: var(--bg); color: var(--text); font-size: 16px; line-height: 1.85;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }
.page { max-width: 1440px; margin: 0 auto; padding: 32px 28px; display: grid; grid-template-columns: 260px minmax(0, 1fr); gap: 32px; align-items: start; }
nav.toc { position: sticky; top: 24px; max-height: calc(100vh - 48px); overflow-y: auto; font-size: 14px; line-height: 1.6;
  padding-right: 6px; scrollbar-width: thin; }
nav.toc summary { font-weight: 600; color: var(--muted); cursor: pointer; margin-bottom: 10px; list-style: none; }
nav.toc summary::-webkit-details-marker { display: none; }
nav.toc ul { list-style: none; margin: 0; padding: 0; }
nav.toc > details > ul > li { margin: 0 0 12px; }
nav.toc > details > ul > li > a { font-weight: 600; }
nav.toc li.sub { margin: 4px 0 0 12px; }
nav.toc li.sub a { color: var(--muted); }
nav.toc a { display: block; text-decoration: none; color: var(--accent); }
nav.toc a:hover { color: var(--accent-hover); }
nav.toc a.active { color: var(--heading); font-weight: 600; }
main { min-width: 0; background: var(--surface); padding: 40px 44px 64px; border: 1px solid var(--border); border-radius: 8px; }
h1, h2, h3 { line-height: 1.45; color: var(--heading); scroll-margin-top: 20px; overflow-wrap: anywhere; }
h1 { font-size: 30px; margin: 0 0 24px; }
h2 { font-size: 25px; margin: 56px 0 24px; padding-bottom: 12px; border-bottom: 2px solid var(--border); }
h3 { font-size: 20px; margin: 40px 0 16px; }
p { margin: 14px 0; overflow-wrap: anywhere; }
a { color: var(--accent); text-underline-offset: 3px; overflow-wrap: anywhere; }
a:hover { color: var(--accent-hover); }
a:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
ul, ol { padding-left: 1.6em; }
li { margin: 6px 0; overflow-wrap: anywhere; }
li.task { list-style: none; margin-left: -1.4em; }
li.task input { margin-right: 8px; }
blockquote { margin: 20px 0; padding: 4px 20px; border-left: 4px solid var(--quote-border); background: var(--quote-bg); color: var(--text); border-radius: 0 6px 6px 0; }
code { font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: .88em; background: var(--inline-code); border-radius: 3px; padding: .12em .32em; overflow-wrap: anywhere; }
.code { position: relative; margin: 18px 0; }
pre { margin: 0; padding: 18px 20px; padding-top: 38px; border: 1px solid var(--border); border-radius: 6px; background: var(--code-bg); white-space: pre-wrap; overflow-wrap: anywhere; tab-size: 4; }
pre code { padding: 0; background: none; font-size: 14px; line-height: 1.75; }
button.copy { position: absolute; top: 6px; right: 6px; font: inherit; font-size: 12px; line-height: 1; padding: 6px 10px; color: var(--text);
  background: var(--btn); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; }
button.copy:hover { background: var(--btn-hover); }
button.copy.done { color: #2e9b5f; border-color: #2e9b5f; }
.table-scroll { overflow-x: auto; margin: 20px 0; }
table { border-collapse: collapse; width: 100%; font-size: 14px; line-height: 1.7; }
th, td { border: 1px solid var(--border); padding: 9px 12px; text-align: left; vertical-align: top; }
th { background: var(--th); font-weight: 600; white-space: nowrap; }
tbody tr:nth-child(even) td { background: var(--stripe); }
figure { margin: 22px 0 28px; }
figure img { display: block; max-width: 100%; height: auto; margin: 0 auto; border: 1px solid var(--border); border-radius: 4px; }
figcaption { text-align: center; color: var(--muted); font-size: 14px; margin-top: 8px; }
.meta { font-size: 13px; color: var(--muted); margin-bottom: 14px; }
@media (max-width: 1000px) {
  .page { grid-template-columns: minmax(0, 1fr); gap: 16px; padding: 16px; }
  nav.toc { position: static; max-height: none; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; }
  nav.toc summary { margin: 0; }
  nav.toc details[open] summary { margin-bottom: 10px; }
  main { padding: 24px 16px 40px; }
  h1 { font-size: 24px; } h2 { font-size: 21px; } h3 { font-size: 17px; }
  pre { padding: 14px; padding-top: 36px; }
}
@media print {
  body { background: white; }
  .page { display: block; padding: 0; max-width: none; }
  nav.toc, button.copy { display: none; }
  main { border: none; padding: 0; }
  h2, h3 { break-after: avoid; }
  figure, pre, tr { break-inside: avoid; }
}
"""

JS = r"""
(function () {
  document.querySelectorAll('button.copy').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var text = btn.parentElement.querySelector('code').innerText.replace(/\n$/, '');
      var ok = function () { btn.textContent = '已复制'; btn.classList.add('done');
        setTimeout(function () { btn.textContent = '复制'; btn.classList.remove('done'); }, 1600); };
      var fallback = function () {
        var ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); ok(); } catch (e) { btn.textContent = '复制失败'; }
        document.body.removeChild(ta);
      };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(ok, fallback);
      else fallback();
    });
  });
  var det = document.querySelector('nav.toc details');
  if (det && window.matchMedia('(max-width: 1000px)').matches) det.removeAttribute('open');
  var links = Array.prototype.slice.call(document.querySelectorAll('nav.toc a'));
  var byId = {}; links.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting && byId[e.target.id]) {
          links.forEach(function (a) { a.classList.remove('active'); });
          byId[e.target.id].classList.add('active');
        }
      });
    }, { rootMargin: '0px 0px -75% 0px' });
    document.querySelectorAll('main h2[id], main h3[id]').forEach(function (h) { io.observe(h); });
  }
})();
"""

page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>投研分析助手演示脚本</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
<nav class="toc" aria-label="目录"><details open><summary>目录</summary><ul>
{toc_html()}
</ul></details></nav>
<main id="content">
<div class="meta">阅读版 · 图片位于同目录 images/，数据集位于 datasets/</div>
{body}
</main>
</div>
<script>{JS}</script>
</body>
</html>
"""
open(dst, "w", encoding="utf-8").write(page)
print("wrote", dst, len(page), "bytes;", len(toc), "toc entries; h1:", h1)
