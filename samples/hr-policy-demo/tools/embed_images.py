"""Inline every <img src="images/..."> of a rendered demo script as a data: URI and drop
the click-through <a href="images/..."> wrappers, so the HTML opens as a single file.

  backend/.venv/bin/python -I samples/hr-policy-demo/tools/build_html.py \
      samples/hr-policy-demo/demo-script.zh-CN.md samples/hr-policy-demo/demo-script.zh-CN.html
  backend/.venv/bin/python -I samples/hr-policy-demo/tools/embed_images.py \
      samples/hr-policy-demo/demo-script.zh-CN.html
"""
import base64, pathlib, re, sys
html = pathlib.Path(sys.argv[1]); root = html.parent; t = html.read_text()
mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
n = 0
def sub(m):
    global n
    p = root / m.group(2)
    if not p.exists(): return m.group(0)
    n += 1
    return f'{m.group(1)}data:{mime[p.suffix.lower()]};base64,{base64.b64encode(p.read_bytes()).decode()}{m.group(3)}'
t = re.sub(r'(<img[^>]*?\ssrc=")(images/[^"]+)(")', sub, t)
t = re.sub(r'<a href="images/[^"]+" target="_blank" rel="noopener">(<img [^>]*>)</a>', r'\1', t)
html.write_text(t); print(f'embedded {n} images; {html.stat().st_size/1e6:.1f} MB; left refs:', len(re.findall(r'src="images/', t)))
