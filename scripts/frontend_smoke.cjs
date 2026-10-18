/**
 * 前端运行时冒烟测试：用 jsdom 加载真实页面，跑通 JS 初始化，
 * 捕获 console.error / 未捕获异常 / 渲染结果。
 *
 * 前置：本地已启动服务（默认 http://127.0.0.1:5173）
 * 运行：node scripts/frontend_smoke.cjs [baseUrl]
 * 依赖：jsdom（装在受管 node workspace，通过 NODE_PATH 引入）
 */
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.argv[2] || 'http://127.0.0.1:5173';
const CHECK_DATA = process.argv.includes('--data');

const PAGES = [
  { path: '/', name: '首页', expect: ['stat-symbols', 'chart-grade', 'dataset-list'] },
  { path: '/data', name: '数据管理', expect: ['dataset-chips', 'symbol-body', 'job-body'] },
  { path: '/scores', name: '每日打分', expect: ['score-body', 'industry-body', 's-count', 's-avg'] },
  { path: '/backtest', name: '回测中心', expect: ['bt-form', 'bt-body'] },
];

// 传 --data 时额外断言：数据行确实渲染（需配合 preview_with_sample.py）
const DATA_ROWS = [
  { path: '/data', table: 'symbol-body', min: 12 },
  { path: '/scores', table: 'score-body', min: 12 },
  { path: '/scores', table: 'industry-body', min: 1 },
  { path: '/backtest', table: 'bt-body', min: 1 },
];

/** 交互检查：打开个股抽屉，验证六维与历史图表挂载 */
async function checkDrawer(dom) {
  const win = dom.window;
  const doc = win.document;
  if (!win.ScorePage || typeof win.ScorePage.openDetail !== 'function') {
    return 'FAIL ScorePage.openDetail 未导出';
  }
  const code = doc.querySelector('#score-body > tr:first-child td:nth-child(2)')?.textContent?.trim();
  if (!code) return 'FAIL 榜单无数据行，无法打开抽屉';

  win.ScorePage.openDetail(code);
  await new Promise((r) => setTimeout(r, 800));

  const drawer = doc.getElementById('drawer');
  if (!drawer || !drawer.classList.contains('open')) return 'FAIL 抽屉未打开';

  const body = doc.getElementById('drawer-body').textContent || '';
  if (/加载中/.test(body)) return 'FAIL 抽屉仍在加载';
  if (!/维度得分/.test(body)) return 'FAIL 抽屉缺少维度区块';
  if (!doc.getElementById('drawer-chart')) return 'FAIL 历史图表容器未创建';

  const dimRows = doc.querySelectorAll('#drawer-body .dim-row').length;
  if (dimRows < 4) return `FAIL 维度条只有 ${dimRows} 条，期望 ≥ 4`;

  return `PASS 抽屉打开 ${code}，维度条 ${dimRows} 条`;
}

const errors = [];

/**
 * jsdom 的间接 eval 不会把顶层 let/const 泄漏到全局作用域（真实浏览器里
 * <script> 的顶层声明是跨脚本共享的）。这里把顶层声明显式补挂到 window。
 */
function hoistGlobals(code) {
  const names = new Set();
  const declRe = /^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/gm;
  const fnRe = /^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm;
  let m;
  while ((m = declRe.exec(code))) names.add(m[1]);
  while ((m = fnRe.exec(code))) names.add(m[1]);
  if (!names.size) return code;
  const tail = [...names].map((n) => `try{window.${n}=${n}}catch(e){}`).join(';');
  return code + '\n;' + tail;
}

function makeDom(html, url) {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => {
    const msg = String((e && e.message) || e);
    if (/Could not load script|resource loading|Could not parse CSS/i.test(msg)) return;
    errors.push(`[${url}] jsdomError: ${msg}`);
  });
  virtualConsole.on('error', (...args) => {
    errors.push(`[${url}] console.error: ${args.join(' ')}`);
  });

  const dom = new JSDOM(html, {
    url: BASE + url,
    runScripts: 'outside-only',
    virtualConsole,
    pretendToBeVisual: true,
  });

  const { window } = dom;
  // echarts 由 CDN 提供，用桩替代，只验证调用链不报错
  window.echarts = {
    init: () => ({ setOption: () => {}, resize: () => {}, dispose: () => {} }),
  };
  window.fetch = async (input, init) => {
    const target = typeof input === 'string' ? input : input.url;
    const abs = target.startsWith('http') ? target : BASE + target;
    const res = await globalThis.fetch(abs, init);
    const text = await res.text();
    return {
      ok: res.ok,
      status: res.status,
      json: async () => JSON.parse(text),
      text: async () => text,
    };
  };
  window.confirm = () => true;
  window.alert = () => {};
  return dom;
}

async function loadPage(page) {
  const res = await fetch(BASE + page.path);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const html = await res.text();
  const dom = makeDom(html, page.path);
  const { window } = dom;

  const scripts = [...window.document.querySelectorAll('script[src]')]
    .map((s) => s.getAttribute('src'))
    .filter((src) => src.startsWith('/static/'));

  for (const src of scripts) {
    const code = await (await fetch(BASE + src)).text();
    try {
      window.eval(hoistGlobals(code));
    } catch (err) {
      errors.push(`[${page.path}] 脚本 ${src} 执行失败: ${err.message}`);
    }
  }

  try {
    window.document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));
  } catch (err) {
    errors.push(`[${page.path}] 初始化抛异常: ${err.message}`);
  }
  await new Promise((r) => setTimeout(r, 1000));

  return dom;
}

function checkRender(page, dom) {
  const doc = dom.window.document;
  const missing = page.expect.filter((id) => !doc.getElementById(id));
  if (missing.length) {
    errors.push(`[${page.path}] 缺少节点: ${missing.join(', ')}`);
    return 'FAIL';
  }

  const stillLoading = [...doc.querySelectorAll('tbody')].filter((tb) => /加载中/.test(tb.textContent));
  if (stillLoading.length) {
    errors.push(`[${page.path}] 仍有 ${stillLoading.length} 个表格停留在「加载中」`);
    return 'FAIL';
  }

  const bodyText = doc.body.textContent || '';
  if (/undefined|NaN|\[object Object\]/.test(bodyText)) {
    errors.push(`[${page.path}] 页面文本出现 undefined / NaN / [object Object]`);
    return 'FAIL';
  }
  return 'PASS';
}

async function main() {
  try {
    const ping = await fetch(BASE + '/api/health');
    if (!ping.ok) throw new Error(`HTTP ${ping.status}`);
  } catch (err) {
    console.error(`服务不可达 ${BASE}：${err.message}`);
    console.error('请先启动：DELTAF_PORT=5173 .venv/Scripts/python.exe run.py');
    process.exit(2);
  }

  console.log('='.repeat(64));
  console.log(`前端运行时冒烟 · ${BASE}`);
  console.log('='.repeat(64));

  let failed = 0;
  const rendered = new Map();
  for (const page of PAGES) {
    try {
      const dom = await loadPage(page);
      const status = checkRender(page, dom);
      if (status === 'FAIL') failed += 1;
      rendered.set(page.path, dom);
      console.log(`  ${status}  ${page.name.padEnd(8)} ${page.path.padEnd(11)} title="${dom.window.document.title}"`);
    } catch (err) {
      failed += 1;
      errors.push(`[${page.path}] ${err.message}`);
      console.log(`  FAIL  ${page.name.padEnd(8)} ${page.path.padEnd(11)} ${err.message}`);
    }
  }

  if (CHECK_DATA) {
    console.log('-'.repeat(64));
    console.log('数据渲染');
    for (const rule of DATA_ROWS) {
      const dom = rendered.get(rule.path);
      if (!dom) {
        errors.push(`[${rule.path}] 页面未成功加载，跳过 ${rule.table} 校验`);
        continue;
      }
      const rows = dom.window.document.querySelectorAll(`#${rule.table} > tr`).length;
      const ok = rows >= rule.min;
      if (!ok) errors.push(`[${rule.path}] #${rule.table} 只有 ${rows} 行，期望 ≥ ${rule.min}`);
      console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${rule.path.padEnd(11)} #${rule.table.padEnd(14)} ${rows} 行`);
    }

    console.log('-'.repeat(64));
    console.log('交互');
    const scoreDom = rendered.get('/scores');
    if (scoreDom) {
      const result = await checkDrawer(scoreDom);
      const ok = result.startsWith('PASS');
      if (!ok) errors.push(`[/scores] ${result.replace(/^FAIL\s*/, '')}`);
      console.log(`  ${result}`);
    }
  }

  for (const dom of rendered.values()) dom.window.close();

  console.log('='.repeat(64));
  if (errors.length) {
    console.log(`发现 ${errors.length} 个问题：`);
    for (const e of errors) console.log('  -', e);
    process.exit(1);
  }
  console.log(`全部通过：${PAGES.length} 个页面无 JS 运行时错误`);
}

main();
