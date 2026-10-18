/* 公共工具：请求封装、格式化、提示、抽屉、空状态 */
const App = (() => {
  const GRADE_LABEL = {
    strong_buy: '强烈看多',
    buy: '看多',
    neutral: '中性',
    sell: '看空',
    strong_sell: '强烈看空',
  };
  const GRADE_KEY = {
    强烈看多: 'strong_buy',
    看多: 'buy',
    中性: 'neutral',
    看空: 'sell',
    强烈看空: 'strong_sell',
  };

  async function request(path, options = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    let payload = null;
    try {
      payload = await res.json();
    } catch (e) {
      throw new Error(`接口返回异常（HTTP ${res.status}）`);
    }
    if (!res.ok || payload.ok === false) {
      const message = (payload && payload.error && payload.error.message) || `HTTP ${res.status}`;
      const err = new Error(message);
      err.code = payload && payload.error ? payload.error.code : 'http_error';
      err.status = res.status;
      throw err;
    }
    return payload.data;
  }

  const get = (path, params) => {
    const query = params ? '?' + new URLSearchParams(params).toString() : '';
    return request(path + query);
  };

  const post = (path, body) =>
    request(path, { method: 'POST', body: JSON.stringify(body || {}) });

  const del = (path) => request(path, { method: 'DELETE' });

  const el = (id) => document.getElementById(id);

  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function num(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    return Number(value).toLocaleString('zh-CN', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function pct(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    return Number(value).toFixed(digits) + '%';
  }

  function shortDate(value) {
    return value ? String(value).slice(0, 10) : '—';
  }

  function toast(message, type = 'info') {
    const node = el('toast');
    if (!node) return;
    node.textContent = message;
    node.className = 'toast show' + (type === 'error' ? ' error' : '');
    clearTimeout(node._timer);
    node._timer = setTimeout(() => {
      node.className = 'toast';
    }, 3200);
  }

  function empty(title, hint) {
    return `<div class="empty">
      <div class="empty-icon">∅</div>
      <div class="empty-title">${esc(title)}</div>
      <div class="empty-hint">${esc(hint || '')}</div>
    </div>`;
  }

  function loading(cols) {
    return `<tr><td colspan="${cols}" class="muted" style="padding:24px 14px;text-align:center">加载中…</td></tr>`;
  }

  function badge(grade) {
    const key = GRADE_KEY[grade] || grade || 'neutral';
    return `<span class="badge ${esc(key)}">${esc(grade || '—')}</span>`;
  }

  function dimBars(item) {
    const dims = [
      ['trend', '趋势'],
      ['momentum', '动量'],
      ['volume', '量能'],
      ['risk', '风险'],
      ['valuation', '估值'],
      ['quality', '质量'],
    ];
    const parts = dims
      .filter(([key]) => item['dim_' + key] !== undefined && item['dim_' + key] !== null)
      .map(([key, label]) => {
        const value = Math.max(0, Math.min(100, Number(item['dim_' + key]) || 0));
        return `<span class="dim" title="${label} ${value.toFixed(1)}"><i style="width:${value}%"></i></span>`;
      })
      .join('');
    return `<div class="dims">${parts || '<span class="faint small">—</span>'}</div>`;
  }

  function openDrawer(title, html) {
    el('drawer-title').textContent = title;
    el('drawer-body').innerHTML = html;
    el('drawer').classList.add('open');
    el('drawer-mask').classList.add('open');
    el('drawer').setAttribute('aria-hidden', 'false');
  }

  function closeDrawer() {
    el('drawer').classList.remove('open');
    el('drawer-mask').classList.remove('open');
    el('drawer').setAttribute('aria-hidden', 'true');
  }

  function chartColors() {
    return {
      axis: '#9ca3af',
      split: '#eef0f3',
      text: '#6b7280',
      primary: '#185fa5',
      up: '#d64545',
      down: '#2e9e6b',
    };
  }

  async function loadTopbar() {
    try {
      const data = await get('/api/health');
      const node = el('topbar-status');
      const latest = data.datasets && data.datasets.bars ? data.datasets.bars.latest_date : null;
      const scoreDate = data.score ? data.score.latest_date : null;
      if (!latest) {
        node.innerHTML = '<span class="dot warn"></span>数据未初始化';
      } else {
        node.innerHTML = `<span class="dot ok"></span>数据截至 ${esc(latest)}${
          scoreDate ? ' · 打分 ' + esc(scoreDate) : ''
        }`;
      }
      return data;
    } catch (err) {
      const node = el('topbar-status');
      if (node) node.innerHTML = '<span class="dot warn"></span>服务异常';
      return null;
    }
  }

  window.closeDrawer = closeDrawer;

  return {
    get, post, del, el, esc, num, pct, shortDate, toast, empty, loading,
    badge, dimBars, openDrawer, closeDrawer, chartColors, loadTopbar, GRADE_LABEL,
  };
})();

document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('topbar-status')) App.loadTopbar();
});
