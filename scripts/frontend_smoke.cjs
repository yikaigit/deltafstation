/**
 * 前端运行时冒烟测试：用 jsdom 加载真实页面，跑通 JS 初始化，
 * 捕获 console.error / 未捕获异常 / 渲染结果。
 *
 * 前置：本地已启动服务（默认 http://127.0.0.1:5173）
 * 运行：node scripts/frontend_smoke.cjs [baseUrl] [--data]
 *   --data  额外断言表格确实渲染出了数据行（要求已完成数据回填/打分）
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

  // 轮询而不是死等：抽屉现在要额外拉 K 线/估值/财务，
  // 首次（缓存冷启动）可能要 1s 量级，固定 800ms 会误判成「仍在加载」。
  const drawer = doc.getElementById('drawer');
  if (!drawer || !drawer.classList.contains('open')) return 'FAIL 抽屉未打开';
  let body = '';
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 100));
    body = doc.getElementById('drawer-body').textContent || '';
    if (body && !/加载中/.test(body)) break;
  }
  if (/加载中/.test(body)) return 'FAIL 抽屉仍在加载';
  if (!/维度得分/.test(body)) return 'FAIL 抽屉缺少维度区块';
  if (!doc.getElementById('drawer-chart')) return 'FAIL 历史图表容器未创建';

  const dimRows = doc.querySelectorAll('#drawer-body .dim-row').length;
  if (dimRows < 4) return `FAIL 维度条只有 ${dimRows} 条，期望 ≥ 4`;

  return `PASS 抽屉打开 ${code}，维度条 ${dimRows} 条`;
}

/** 交互检查：打开回测详情，验证七区块（指标卡/净值/年度/ISOOS/网格/持仓/交易）都渲染了 */
async function checkBacktestDetail(dom) {
  const win = dom.window;
  const doc = win.document;
  if (!win.BacktestPage || typeof win.BacktestPage.openDetail !== 'function') {
    return 'FAIL BacktestPage.openDetail 未导出';
  }
  // 挑一条「有参数网格」的记录来验证热力图：历史里有些记录是早期用的旧参数
  // （grid=0），拿第一条会误报成「热力图未渲染」。这里按 API 列出所有记录，
  // 找到第一条 grid 非空的，找不到就跳过该断言而不是判失败。
  let id = doc.querySelector('#bt-body > tr:first-child td:nth-child(2)')?.textContent?.trim();
  if (!id || !/^bt_/.test(id)) return 'FAIL 回测记录无数据行，无法打开详情';
  let expectGrid = null;
  try {
    const list = await (await win.fetch('/api/backtests')).json();
    const items = (list.data && list.data.items) || [];
    for (const it of items) {
      const detail = await (await win.fetch('/api/backtests/' + it.id)).json();
      const grid = (detail.data && detail.data.grid) || [];
      if (grid.length) {
        id = it.id;
        expectGrid = grid.length;
        break;
      }
    }
    if (expectGrid === null && items.length) expectGrid = 0;
  } catch (e) {
    expectGrid = null;
  }

  win.BacktestPage.openDetail(id);
  let box = null;
  for (let i = 0; i < 60; i += 1) {
    await new Promise((r) => setTimeout(r, 100));
    box = doc.getElementById('bt-detail');
    if (box && !box.hidden && doc.querySelectorAll('#bt-metrics .metric').length) break;
  }
  if (!box || box.hidden) return 'FAIL 详情面板未展开';

  const metrics = doc.querySelectorAll('#bt-metrics .metric').length;
  if (metrics < 6) return `FAIL 指标卡只有 ${metrics} 个，期望 ≥ 6`;
  if (!doc.querySelectorAll('#bt-isoos-body > tr').length) return 'FAIL IS/OOS 表为空';
  if (!doc.querySelectorAll('#bt-holdings-body > tr').length) return 'FAIL 持仓历史为空';
  if (!doc.querySelectorAll('#bt-trades-body > tr').length) return 'FAIL 交易明细为空';
  // 净值曲线：echarts 是桩，断言「喂进去的 series 有数据」而不是看 canvas
  //
  // ⚠️ 要取「数据最多的那次 setOption」而不是最后一次：画 OOS 分界线时会再调
  // 一次 setOption({series:[{},{},{markLine}]})，那是增量补丁，首条 series
  // 没有 data —— 拿最后一次会误判成「0 个数据点」。
  const navCalls = (win.__chartCalls || []).filter((c) => c.id === 'chart-nav');
  if (!navCalls.length) return 'FAIL 净值曲线未调用 setOption';
  const navPts = Math.max(
    ...navCalls.map((c) => {
      const head = (c.opt.series || [])[0];
      return head && head.data ? head.data.length : 0;
    })
  );
  if (navPts < 10) return `FAIL 净值曲线数据点只有 ${navPts}`;

  // 参数网格：32 组时应有 4×4×2 个格子
  // 热力图是纯 HTML 表格（不依赖 echarts）。有网格数据就必须画出格子；
  // 记录本身没有网格（早期旧参数的记录）时，应当给出降级说明而不是空白。
  const cells = doc.querySelectorAll('#bt-grid-heat table.heat td').length;
  const gridText = (doc.getElementById('bt-grid-heat').textContent || '').trim();
  if (expectGrid === null) {
    // 所有记录都没有网格：只能断言降级文案
    if (!gridText) return 'FAIL 参数网格区域完全空白';
  } else if (expectGrid > 0 && !cells) {
    return `FAIL 参数网格有 ${expectGrid} 组但热力图未渲染`;
  } else if (expectGrid === 0 && !gridText) {
    return 'FAIL 无网格时缺少降级说明';
  }

  // 对比：勾选两条后按钮可用
  const picks = [...doc.querySelectorAll('#bt-body input[type="checkbox"]')].slice(0, 2);
  if (picks.length < 2) return 'FAIL 回测记录不足 2 条，无法验证对比';
  picks.forEach((cb) => {
    cb.checked = true;
    win.BacktestPage.onPick(cb);
  });
  await new Promise((r) => setTimeout(r, 100));
  const btn = doc.getElementById('bt-compare-btn');
  if (!btn || btn.disabled) return 'FAIL 勾选 2 条后对比按钮仍不可用';

  return `PASS 回测详情 ${id}：指标 ${metrics} 张 · 网格 ${cells} 格 · 净值 ${navPts} 点`;
}

/** 交互检查：数据管理页的覆盖矩阵是否画出来了 */
async function checkCoverage(dom) {
  const doc = dom.window.document;
  let cells = 0;
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 100));
    cells = doc.querySelectorAll('#cover-body tr td').length;
    if (cells) break;
  }
  const years = doc.querySelectorAll('#cover-table thead th').length;
  if (!cells) return 'FAIL 覆盖矩阵未渲染';
  if (years < 5) return `FAIL 覆盖矩阵表头只有 ${years} 列`;
  return `PASS 覆盖矩阵：${doc.querySelectorAll('#cover-body tr').length} 只 × ${years - 2} 年`;
}

/** 交互检查：首页 Top/Bottom 与行业热力 */
function checkHomeRanks(dom) {
  const doc = dom.window.document;
  const top = doc.querySelectorAll('#top-body > tr').length;
  const bottom = doc.querySelectorAll('#bottom-body > tr').length;
  const heat = doc.querySelectorAll('#industry-heat .heat-row').length;
  if (!top || !bottom) return 'FAIL Top/Bottom 榜未渲染';
  if (!heat) return 'FAIL 行业热力条未渲染';
  return `PASS 首页：Top ${top} / Bottom ${bottom} / 行业 ${heat} 条`;
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
  // echarts 由 CDN 提供，用桩替代。桩会把每次 setOption 的参数记下来 ——
  // 这样能断言「图表确实被喂了数据」，而不是只能看 canvas 存不存在
  // （jsdom 里桩不画 canvas，纯看 DOM 会永远为空）。
  window.__chartCalls = [];
  window.echarts = {
    init: (dom) => {
      const id = (dom && dom.id) || '';
      return {
        setOption: (opt) => window.__chartCalls.push({ id, opt }),
        resize: () => {},
        dispose: () => {},
      };
    },
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
    const checks = [
      ['/scores', checkDrawer],
      ['/backtest', checkBacktestDetail],
      ['/data', checkCoverage],
      ['/', checkHomeRanks],
    ];
    for (const [path, fn] of checks) {
      const dom = rendered.get(path);
      if (!dom) continue;
      const result = await fn(dom);
      const ok = result.startsWith('PASS');
      if (!ok) errors.push(`[${path}] ${result.replace(/^FAIL\s*/, '')}`);
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
