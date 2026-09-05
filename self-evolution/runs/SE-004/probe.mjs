// SE-004 probe: page-level horizontal overflow on every route at 390x844 and 1440x900.
// usage: node probe.mjs [port] [--shots]
import { chromium } from '/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs';
const port = process.argv[2] || '5197';
const shots = process.argv.includes('--shots');
const outDir = '/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-004';
const routes = ['/', '/create', '/create/studio', '/registry', '/registry?view=register', '/registry?view=a2a-demo',
  '/knowledge-bases', '/knowledge-bases?view=create', '/memory', '/memory?view=short-term', '/memory?view=long-term',
  '/chat', '/observability', '/observability?tab=sessions', '/observability?tab=traces',
  '/evaluation', '/evaluation?view=experiment', '/evaluation?view=evaluators', '/evaluation?view=datasets', '/evaluation?view=online',
  '/skill-lab', '/skill-lab?view=eval', '/skill-lab?view=train',
  '/governance', '/governance?view=policy', '/governance?view=decisions', '/governance?view=audit',
  '/users', '/workspaces', '/workspaces?view=create'];
const shotRoutes = { '/create':'create', '/registry':'registry', '/knowledge-bases':'knowledge_bases',
  '/memory?view=long-term':'memory_long_term', '/chat':'chat', '/users':'users' };
const browser = await chromium.launch({ args:['--no-sandbox'] });
let fails = 0;
for (const vp of [{width:390,height:844},{width:1440,height:900}]) {
  const ctx = await browser.newContext({ viewport: vp });
  const locale = process.env.LOCALE || 'en'; await ctx.addInitScript((l) => localStorage.setItem('i18nextLng', l), locale);
  const page = await ctx.newPage();
  console.log(`\n=== viewport ${vp.width}x${vp.height} ===`);
  for (const r of routes) {
    try { await page.goto(`http://localhost:${port}${r}`, {waitUntil:'networkidle', timeout:25000}); } catch {}
    await page.waitForTimeout(800);
    const info = await page.evaluate(()=>{
      const W = window.innerWidth; const out = [];
      for (const el of document.querySelectorAll('main *')) {
        const r = el.getBoundingClientRect();
        if (r.width>0 && r.right > W+2) {
          const p = el.parentElement; const pr = p ? p.getBoundingClientRect() : null;
          if (pr && pr.right > W+2 && Math.abs(pr.right - r.right) < 4) continue;
          const cs = getComputedStyle(el);
          out.push({ tag: el.tagName.toLowerCase(), cls: (typeof el.className==='string'?el.className:'').split(' ').slice(0,3).join('.'), right: Math.round(r.right), w: Math.round(r.width), minW: cs.minWidth, ws: cs.whiteSpace, text: (el.innerText||'').trim().slice(0,30) });
        }
      }
      out.sort((a,b)=>b.right-a.right);
      return { sw: document.documentElement.scrollWidth, top: out.slice(0,5) };
    });
    const ok = info.sw === vp.width;
    if (!ok) fails++;
    console.log(`${ok?'OK  ':'FAIL'} [${r}] scrollWidth=${info.sw}`);
    for (const o of info.top) console.log('      ', JSON.stringify(o));
    if (shots && shotRoutes[r]) {
      const tag = vp.width === 390 ? 'narrow' : 'desktop';
      if (vp.width === 390) await page.evaluate(() => { const el = document.querySelector('.table-scroll table, .chat-grid .code'); if (el && el.getBoundingClientRect().top > window.innerHeight - 200) el.scrollIntoView({ block: 'center' }); });
      await page.waitForTimeout(200);
      await page.screenshot({ path: `${outDir}/${tag}_${shotRoutes[r]}.png`, fullPage: false });
    }
  }
  await ctx.close();
}
await browser.close();
console.log(`\nRESULT: ${fails === 0 ? 'PASS' : 'FAIL'} (${fails} route/viewport failures)`);
