"""Build an offline HTML manual; screenshots are embedded, no remote dependencies."""

import argparse
import base64
import html
import io
import json
import re

from bs4 import BeautifulSoup
from lab import PACKAGE, ROOT, STAFF
from markdown_it import MarkdownIt
from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument(
    "--final", action="store_true", help="Require complete evaluation and all screenshots"
)
args = parser.parse_args()
m = json.loads((ROOT / "manifest.json").read_text())
prompts = json.loads((ROOT / "lab-assets/prompts.json").read_text())
source = (ROOT / "handbook.md").read_text()
values = {
    "console": m["console"],
    "model_id": m["model_id"],
    "staff_accounts": "、".join(STAFF),
    "gateway_name": (m.get("gateway") or {}).get("name", "组织者提供的实验 Gateway"),
    "requester_policy": (ROOT / "policies/itsm_requester_is_self.cedar").read_text().strip(),
    **{"prompt" + str(n): prompts["step" + str(n)] for n in range(1, 4)},
}
summary = ROOT / "evidence/evaluation-summary.md"
values["eval_summary"] = (
    summary.read_text()
    if summary.exists()
    else "> 新部署尚未执行评测。请运行并保存本环境的真实结果；参考环境的结果不能作为本环境验收。"
)
if args.final and not summary.exists():
    raise RuntimeError("Final build requires the actual evaluation summary")
for key, value in values.items():
    source = source.replace("{{" + key + "}}", value)
missing = []
images = []
reference_images = []


def image(match):
    tag, caption = match.group(1), match.group(2)
    path = ROOT / "screenshots" / (tag + ".png")
    if not path.exists():
        missing.append(tag)
        reference = PACKAGE / "reference-screenshots" / (tag + ".webp")
        if not reference.exists():
            return f'<div class="pending">请在本环境核对：{html.escape(caption)}</div>'
        path = reference
        reference_images.append(tag)
        caption = "参考环境截图（2026-10-28）；请以本次部署结果为准。" + caption
    buf = io.BytesIO()
    with Image.open(path) as im:
        im.convert("RGB").save(buf, format="WEBP", quality=88, method=6)
    data = base64.b64encode(buf.getvalue()).decode()
    images.append(tag)
    return f'<figure><button class="image-zoom" aria-label="放大截图：{html.escape(caption)}"><img loading="lazy" src="data:image/webp;base64,{data}" alt="{html.escape(caption)}"></button><figcaption>{html.escape(caption)}</figcaption></figure>'


source = re.sub(r"{{shot:([^|}]+)\|([^}]+)}}", image, source)
if args.final and (missing or re.search(r"{{[^}]+}}", source)):
    raise RuntimeError(f"Incomplete final manual: {missing}")
md = MarkdownIt("commonmark", {"html": True}).enable("table")
soup = BeautifulSoup(md.render(source), "html.parser")
navigation = []
for heading in soup.select("h2"):
    match = re.search(r"\s*\{#([^}]+)\}\s*$", heading.get_text())
    if match:
        heading["id"] = match.group(1)
        title = heading.get_text()[: match.start()].strip()
        heading.clear()
        heading.string = title
        navigation.append((match.group(1), title))
for pre in soup.select("pre"):
    wrapper = soup.new_tag("div", attrs={"class": "code-block"})
    pre.wrap(wrapper)
    button = soup.new_tag("button", attrs={"class": "copy", "type": "button"})
    button.string = "复制"
    wrapper.insert(0, button)
for table in soup.select("table"):
    wrapper = soup.new_tag("div", attrs={"class": "table-scroll", "tabindex": "0"})
    table.wrap(wrapper)
component = ROOT / "doc-page.js"
docjs = (component if component.exists() else (PACKAGE / "doc-page.js")).read_text()
(ROOT / "doc-page.js").write_text(docjs)
docjs = re.sub(r"</script", r"<\\/script", docjs, flags=re.I)
nav = "".join(f'<a href="#{html.escape(i)}">{html.escape(t)}</a>' for i, t in navigation)
css = """
:root{--ink:#1f2937;--brand:#1e4fae;--blue:#1d4ed8;--muted:#64748b;--line:#e2e8f0}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:28px}
body{margin:0;color:var(--ink);font:15.5px/1.85 "PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
.side{position:fixed;inset:0 auto 0 0;width:266px;padding:30px 18px;overflow-y:auto;background:#fff;border-right:1px solid var(--line);z-index:2}
.eyebrow{font-size:11px;letter-spacing:.15em;color:var(--muted)}.side strong{display:block;font-size:19px;line-height:1.5;color:var(--brand);margin:10px 9px 25px}.side a{display:block;padding:10px 12px;font-size:13px;color:#334155;border-radius:6px;line-height:1.55}.side a.on{background:#e8efff;color:var(--brand);font-weight:700}.side .note{font-size:12px;color:var(--muted);padding:15px 12px}
.toolbar{display:flex;gap:8px;margin:14px 9px}.toolbar button,.copy,.mobile-nav summary{cursor:pointer;color:var(--brand);background:white;border:1px solid #ccd7e5;border-radius:5px;padding:6px 11px;font:inherit;font-size:12px}
doc-page{margin-left:266px;min-width:0!important;font:inherit!important}
h1{color:#143871;font-size:35px;line-height:1.45;letter-spacing:-.02em;margin:0 0 16px}
h2{font-size:26px;color:#173e7a;line-height:1.45;border-bottom:2px solid #2356b3;padding-bottom:14px;margin:62px 0 20px;scroll-margin-top:24px}
h3{font-size:19px;color:#263e60;margin:28px 0 10px}p{margin:13px 0}li{margin:5px 0}strong{font-weight:700}
code{font:13px/1.7 "SFMono-Regular",Consolas,"Noto Sans Mono CJK SC",monospace;background:#f0f4fa;padding:2px 5px;border-radius:4px;overflow-wrap:anywhere}
.code-block{position:relative;margin:20px 0}.copy{position:absolute;right:8px;top:8px;z-index:1}pre{margin:0;background:#f4f7fb;border:1px solid #dce5f0;border-left:3px solid #2563eb;border-radius:6px;padding:18px 66px 18px 18px;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8}pre code{background:none;padding:0}
.table-scroll{overflow-x:auto;margin:22px 0}table{width:100%;border-collapse:collapse;font-size:13.5px;line-height:1.75}th,td{border:1px solid #dce3ed;padding:10px 12px;text-align:left;vertical-align:top}th{background:#eff4fb;color:#233d63}tr:nth-child(even) td{background:#fafbfd}
figure{margin:26px 0}figure img{display:block;width:100%;height:auto;border:1px solid #dbe3ee;border-radius:7px}figcaption{font-size:12px;color:var(--muted);line-height:1.7;text-align:center;margin-top:8px}.image-zoom{display:block;background:none;border:0;padding:0;width:100%;cursor:zoom-in}
blockquote{border-left:3px solid #e3a526;background:#fff8e9;padding:12px 20px;margin:20px 0;color:#655123}.pending{border:1px dashed #b9781b;padding:16px;background:#fff9ed;color:#755618}
.mobile-nav{display:none}#zoom{padding:16px;border:0;max-width:96%;max-height:95%;background:white;border-radius:8px}#zoom::backdrop{background:#0f172ae8}#zoom img{max-width:100%;height:auto}#zoom button{font:inherit;cursor:pointer;border:1px solid #cbd5e1;background:#fff;border-radius:4px;padding:5px 18px;margin:0 0 10px}.footer{margin:50px 0 0;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:20px}
@media screen{doc-page{--doc-page-w:min(100%,1040px)!important;--doc-page-margin:48px!important;padding:28px!important}doc-page{background:#f6f8fb!important}}
@media(max-width:1000px){.side{display:none}doc-page{margin-left:0}.mobile-nav{display:block;padding:12px 22px;background:#fff}.mobile-nav nav{padding:10px 0}.mobile-nav a{display:block;padding:8px 0;font-size:13px}}
@media(max-width:600px){doc-page{padding:10px!important;--doc-page-margin:20px!important}h1{font-size:27px}h2{font-size:22px;margin-top:42px}body{font-size:14px}pre{padding:45px 13px 14px}table{font-size:12px}th,td{padding:8px}.mobile-nav{padding:10px 15px}}
@media print{.side,.mobile-nav,.copy,#zoom{display:none!important}doc-page{margin:0!important}h2{break-before:page;font-size:23px}h1{font-size:29px}.table-scroll{overflow:visible}figure img{max-height:8.1in;object-fit:contain}figure,blockquote,.code-block{break-inside:avoid}a{color:inherit}.pending{display:none}body{font-size:12px}pre code{font-size:11px}table{font-size:11px}}
"""
script = """
document.querySelectorAll('.copy').forEach(b=>b.addEventListener('click',async()=>{const t=b.parentElement.querySelector('code').textContent;try{await navigator.clipboard.writeText(t)}catch{const x=document.createElement('textarea');x.value=t;document.body.append(x);x.select();document.execCommand('copy');x.remove()}b.textContent='已复制';setTimeout(()=>b.textContent='复制',1300)}));
const z=document.getElementById('zoom');document.querySelectorAll('.image-zoom').forEach(b=>b.addEventListener('click',()=>{z.querySelector('img').src=b.querySelector('img').src;z.querySelector('img').alt=b.querySelector('img').alt;z.showModal()}));z.querySelector('button').onclick=()=>z.close();z.addEventListener('click',e=>{if(e.target===z)z.close()});
const heads=[...document.querySelectorAll('h2[id]')];new IntersectionObserver(es=>{es.filter(e=>e.isIntersecting).forEach(e=>{document.querySelectorAll('.side a').forEach(a=>a.classList.toggle('on',a.hash==='#'+e.target.id))})},{rootMargin:'-5% 0px -70% 0px'}).observe(heads[0]);const obs=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting)document.querySelectorAll('.side a').forEach(a=>a.classList.toggle('on',a.hash==='#'+e.target.id))}),{rootMargin:'-5% 0px -65% 0px'});heads.forEach(h=>obs.observe(h));
document.querySelectorAll('[data-print]').forEach(b=>b.onclick=()=>window.print());
"""
out = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Launchpad 中国区 IT 服务台八步实验：知识、工具、权限、记忆、评测与经验入库。"><title>IT服务台小助手 · 中国区实验手册</title><style>{css}</style></head><body>
<aside class="side"><div class="eyebrow">LAUNCHPAD · CHINA LAB</div><strong>IT 服务台小助手<br>中国区实验手册</strong><nav aria-label="章节目录">{nav}</nav><div class="toolbar"><button data-print>打印 / 保存 PDF</button></div><p class="note">2026-10-28<br>8 步实验 · 合成业务资料<br>点击截图可放大；代码可复制。</p></aside>
<details class="mobile-nav"><summary>查看章节目录</summary><nav>{nav}</nav></details>
<doc-page margin="0.55in">{soup}<p class="footer">实验资源、源码与验收证据详见交付包。凭据另行发放。部署版本请以本环境控制台显示和部署状态文件为准。</p></doc-page>
<dialog id="zoom" aria-label="截图放大"><button>关闭</button><img alt=""></dialog>
<script>{docjs}</script><script>{script}</script></body></html>"""
name = "IT服务台小助手-中国区实验手册.html"
(ROOT / name).write_text(out)
(ROOT / "evidence/manual-build.json").write_text(
    json.dumps(
        {
            "file": name,
            "embedded_screenshots": images,
            "missing_screenshots": missing,
            "reference_screenshots": reference_images,
            "chapters": navigation,
            "bytes": len(out.encode()),
        },
        ensure_ascii=False,
        indent=2,
    )
)
print("Built", name, "screenshots", len(images), "pending", missing)
