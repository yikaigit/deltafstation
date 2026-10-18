/* 首页：状态汇总 + Top/Bottom 榜 + 行业热力 + 评级分布 + 最近任务 */
const Index = (() => {
  let gradeChart = null;

  const STATUS_LABEL = {
    pending: '等待中',
    running: '运行中',
    success: '成功',
    failed: '失败',
    partial: '部分成功',
    cancelled: '已取消',
  };

  const el = (id) => document.getElementById(id);

  function renderStats(health, status) {
    const bars = (health.datasets && health.datasets.bars) || {};
    const score = health.score || {};
    el('stat-symbols').textContent = health.universe_count || 0;
    el('stat-symbols-hint').textContent = health.universe_count
      ? '标的表已建立'
      : '标的表尚未建立';
    el('stat-latest').textContent = bars.latest_date || '—';
    el('stat-latest-hint').textContent = bars.exists ? 'bars 数据集' : '尚未回填数据';
    const versions = score.versions || [];
    el('stat-version').textContent = versions.length ? versions[versions.length - 1] : '—';
    el('stat-version-hint').textContent = score.latest_date
      ? '最新快照 ' + score.latest_date
      : versions.length
      ? '尚无快照'
      : '尚无打分版本';

    const q = (status && status.quality) || {};
    const qNode = el('stat-quality');
    const qHint = el('stat-quality-hint');
    if (!q.available) {
      qNode.textContent = '—';
      qNode.className = 'stat-value faint';
      qHint.textContent = '尚未生成校验报告';
    } else if (q.error_symbols) {
      qNode.textContent = `ERROR ${q.error_symbols}`;
      qNode.className = 'stat-value up';
      qHint.textContent = `WARN ${q.warn_symbols || 0} / ${q.symbols || 0} 只 · ${q.trade_date || ''}`;
    } else {
      qNode.textContent = `WARN ${q.warn_symbols || 0}`;
      qNode.className = 'stat-value';
      qHint.textContent = `ERROR 0 / ${q.symbols || 0} 只 · ${q.trade_date || ''}`;
    }
  }

  function renderDatasets(health) {
    const names = {
      bars: '日线',
      valuation: '估值',
      adj_factor: '复权因子',
      fundamental: '财务',
      universe: '标的表',
      calendar: '交易日历',
    };
    const html = Object.entries(health.datasets || {})
      .map(([key, info]) => {
        const label = names[key] || key;
        if (info.exists) {
          const extra = info.latest_date ? ` · ${info.latest_date}` : '';
          return `<span class="chip ok">${App.esc(label)}${App.esc(extra)}</span>`;
        }
        return `<span class="chip miss">${App.esc(label)} · 未建</span>`;
      })
      .join('');
    el('dataset-list').innerHTML = `<div class="chips">${html}</div>`;
  }

  function rankRows(items) {
    return items
      .map(
        (row, idx) => `<tr class="clickable" onclick="location.href='/scores'">
          <td class="right mono faint">${idx + 1}</td>
          <td class="mono">${App.esc(row.code)}</td>
          <td>${App.esc(row.name || '—')}</td>
          <td class="right mono strong">${
            row.score_pct === null || row.score_pct === undefined
              ? '—'
              : Number(row.score_pct).toFixed(1)
          }</td>
          <td>${App.badge(row.grade)}</td>
        </tr>`
      )
      .join('');
  }

  function renderRanks(items, date) {
    const topBody = el('top-body');
    const bottomBody = el('bottom-body');
    const sorted = items
      .slice()
      .sort((a, b) => Number(b.score_pct) - Number(a.score_pct))
      .filter((r) => r.score_pct !== null && r.score_pct !== undefined);
    if (!sorted.length) {
      const empty = `<tr><td colspan="5" style="padding:0">${App.empty(
        '尚无打分数据',
        '执行每日任务后这里会显示榜单'
      )}</td></tr>`;
      topBody.innerHTML = empty;
      bottomBody.innerHTML = empty;
      return;
    }
    el('top-date').textContent = date || '—';
    el('bottom-date').textContent = date || '—';
    topBody.innerHTML = rankRows(sorted.slice(0, 10));
    bottomBody.innerHTML = rankRows(sorted.slice(-10).reverse());
  }

  function heatColor(value, lo, hi) {
    if (value === null || value === undefined) return '#f3f4f6';
    const t = Math.max(0, Math.min(1, (Number(value) - lo) / ((hi - lo) || 1)));
    const mid = [243, 244, 246];
    const edge = t < 0.5 ? [46, 158, 107] : [214, 69, 69];
    const k = t < 0.5 ? t / 0.5 : (t - 0.5) / 0.5;
    const rgb = mid.map((m, i) => Math.round(m + (edge[i] - m) * k));
    return `rgb(${rgb.join(',')})`;
  }

  function renderIndustryHeat(items) {
    const box = el('industry-heat');
    if (!items || !items.length) {
      box.innerHTML = App.empty('暂无行业数据', '需要至少一只标的完成打分');
      return;
    }
    const values = items.map((r) => Number(r.avg_score)).filter((v) => !Number.isNaN(v));
    const lo = values.length ? Math.min(...values) : 0;
    const hi = values.length ? Math.max(...values) : 100;
    box.innerHTML = items
      .slice(0, 20)
      .map((row) => {
        const avg = Number(row.avg_score);
        const width = Math.max(2, Math.min(100, ((avg - lo) / ((hi - lo) || 1)) * 100));
        return `<div class="heat-row" title="${App.esc(row.industry)} 均分 ${avg.toFixed(2)} · ${row.count} 只">
          <span class="small muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${App.esc(
            row.industry || '—'
          )}</span>
          <span class="heat-track"><i style="width:${width}%;background:${heatColor(avg, lo, hi)}"></i></span>
          <span class="small mono right">${avg.toFixed(1)}</span>
        </div>`;
      })
      .join('');
  }

  function renderGradeChart(distribution, date) {
    const dom = el('chart-grade');
    const entries = Object.entries(distribution || {});
    el('dist-date').textContent = date || '—';
    if (!entries.length) {
      if (gradeChart) {
        gradeChart.dispose();
        gradeChart = null;
      }
      dom.innerHTML = App.empty('尚无打分数据', '执行每日任务后，这里会显示评级分布');
      return;
    }
    dom.innerHTML = '';
    if (!gradeChart) gradeChart = echarts.init(dom);
    const order = ['强烈看多', '看多', '中性', '看空', '强烈看空'];
    const color = {
      强烈看多: '#d64545',
      看多: '#e08a8a',
      中性: '#b4b2a9',
      看空: '#7fb79a',
      强烈看空: '#2e9e6b',
    };
    const values = order.map((g) => distribution[g] || 0);
    gradeChart.setOption({
      grid: { left: 40, right: 16, top: 16, bottom: 32 },
      xAxis: {
        type: 'category',
        data: order,
        axisLine: { lineStyle: { color: '#e5e7eb' } },
        axisLabel: { color: '#6b7280', fontSize: 12 },
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: '#eef0f3' } },
        axisLabel: { color: '#9ca3af', fontSize: 11 },
      },
      tooltip: { trigger: 'axis' },
      series: [
        {
          type: 'bar',
          data: values.map((v, i) => ({ value: v, itemStyle: { color: color[order[i]] } })),
          barWidth: 32,
          label: { show: true, position: 'top', color: '#6b7280', fontSize: 11 },
        },
      ],
    });
  }

  async function renderJobs() {
    const body = el('job-body');
    try {
      const data = await App.get('/api/jobs', { limit: 8 });
      if (!data.items.length) {
        body.innerHTML = `<tr><td colspan="6">${App.empty('暂无任务记录', '在数据管理页可以手动触发更新')}</td></tr>`;
        return;
      }
      body.innerHTML = data.items
        .map((job) => {
          const status = STATUS_LABEL[job.status] || job.status;
          const cls = job.status === 'success' ? 'ok' : job.status === 'failed' ? 'warn' : 'muted';
          return `<tr>
            <td class="mono">${App.esc(job.id)}</td>
            <td>${App.esc(job.kind)}</td>
            <td><span class="badge ${cls}">${App.esc(status)}</span></td>
            <td class="num">${Math.round((job.progress || 0) * 100)}%</td>
            <td class="muted small">${App.esc(job.created_at || '—')}</td>
            <td class="muted small">${App.esc(job.finished_at || '—')}</td>
          </tr>`;
        })
        .join('');
    } catch (err) {
      body.innerHTML = `<tr><td colspan="6" class="muted">${App.esc(err.message)}</td></tr>`;
    }
  }

  async function quickUpdate() {
    const btn = el('quick-update');
    const hint = el('quick-hint');
    if (btn) btn.disabled = true;
    try {
      const res = await App.post('/api/jobs', { kind: 'update' });
      App.toast('已提交数据更新任务：' + (res.job_id || ''));
      if (hint) hint.textContent =
        '已提交今日行情刷新（' + (res.job_id || '') + '）；补历史缺口请用上方「补齐并打分」';
      await renderJobs();
    } catch (err) {
      App.toast('提交失败：' + err.message, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /* ---------------- 收盘后补齐（P6） ---------------- */

  let catchupTimer = null;

  const GAP_LABEL = {
    bars: '行情',
    index_bars: '指数',
    valuation: '估值',
  };

  function renderCatchup(data) {
    const gap = data.gap || {};
    const badge = el('cu-badge');
    const gapNode = el('cu-gap');
    const scoreNode = el('cu-score');
    const lastNode = el('cu-last');
    const sub = el('cu-sub');

    if (!badge) return;
    el('cu-target').textContent = gap.target || '—';

    if (gap.complete) {
      badge.className = 'badge ok';
      badge.textContent = '数据已齐';
      gapNode.innerHTML = `<span class="up">${App.esc(gap.note || '')}</span>`;
    } else {
      badge.className = 'badge warn';
      badge.textContent = `缺 ${gap.days_to_fill_count} 天`;
      const detail = Object.entries(gap.datasets || {})
        .filter(([, g]) => g.missing_count > 0)
        .map(([name, g]) => `${GAP_LABEL[name] || name} 缺 ${g.missing_count} 天`)
        .join(' · ');
      gapNode.innerHTML = `<span class="down">${App.esc(detail || '')}</span>
        <span class="faint small">（${App.esc((gap.days_to_fill || []).slice(0, 5).join(', '))}${
          gap.days_to_fill_count > 5 ? ' …' : ''
        }）</span>`;
    }

    const score = data.score || {};
    if (score.latest_date) {
      const behind = score.latest_date !== gap.target;
      scoreNode.innerHTML = `<span class="mono">${App.esc(score.version)}</span> ·
        <span class="mono ${behind ? 'down' : ''}">${App.esc(score.latest_date)}</span>
        <span class="faint small">（${score.dates} 个交易日）</span>`;
    } else {
      scoreNode.textContent = '尚无打分快照';
    }

    const last = data.last_run || {};
    if (last.job_id) {
      const tone =
        last.status === 'success' ? 'up' : last.status === 'failed' ? 'down' : 'muted';
      lastNode.innerHTML = `<span class="${tone}">${App.esc(last.status)}</span>
        <span class="faint small">${App.esc(last.message || '')}</span>
        <span class="faint small mono">${App.esc((last.created_at || '').slice(0, 19))}</span>`;
    } else {
      lastNode.innerHTML = '<span class="faint small">尚未运行过</span>';
    }

    if (sub) {
      sub.textContent = gap.complete
        ? `回看 ${data.lookback_days} 个交易日，无缺口`
        : `回看 ${data.lookback_days} 个交易日，发现缺口`;
    }

    // 有任务在跑就接上进度
    if (last.job_id && (last.status === 'pending' || last.status === 'running')) {
      watchJob(last.job_id);
    }
  }

  async function loadCatchup() {
    try {
      const data = await App.get('/api/catchup/status');
      renderCatchup(data);
    } catch (err) {
      const badge = el('cu-badge');
      if (badge) {
        badge.className = 'badge muted';
        badge.textContent = '检查失败';
      }
    }
  }

  function watchJob(jobId) {
    const box = el('cu-progress');
    const bar = el('cu-bar');
    const msg = el('cu-msg');
    if (box) box.hidden = false;
    if (catchupTimer) clearTimeout(catchupTimer);

    const tick = async () => {
      try {
        const job = await App.get('/api/jobs/' + encodeURIComponent(jobId));
        const ratio = Math.max(0, Math.min(1, Number(job.progress) || 0));
        if (bar) bar.style.width = (ratio * 100).toFixed(0) + '%';
        if (msg) msg.textContent = `${job.message || ''}  ${(ratio * 100).toFixed(0)}%`;

        if (job.status === 'success' || job.status === 'failed' || job.status === 'cancelled') {
          if (box) box.hidden = true;
          if (bar) bar.style.width = '0%';
          catchupTimer = null;
          // 完成后刷新卡片与榜单
          await loadCatchup();
          await reload();
          if (job.status === 'success') {
            App.toast('补齐完成：' + (job.message || ''));
          } else {
            App.toast('补齐失败：' + (job.message || ''), 'error');
          }
          return;
        }
        catchupTimer = setTimeout(tick, 1500);
      } catch (err) {
        catchupTimer = setTimeout(tick, 3000);
      }
    };
    tick();
  }

  async function runCatchup() {
    const btn = el('cu-run');
    const hint = el('cu-hint');
    const withScore = el('cu-with-score');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '运行中…';
    }
    try {
      const res = await App.post('/api/catchup/run', {
        score: withScore ? withScore.checked : true,
      });
      if (res.already_running) {
        App.toast(res.message || '已有一轮在跑');
        watchJob(res.job_id);
        return;
      }
      App.toast('已开始补齐：' + res.job_id);
      if (hint) hint.textContent = '任务 ' + res.job_id + ' 运行中…';
      watchJob(res.job_id);
    } catch (err) {
      App.toast('提交失败：' + err.message, 'error');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '补齐并打分';
      }
    }
  }

  async function reload() {
    try {
      const health = await App.get('/api/health');
      let status = null;
      try {
        status = await App.get('/api/data/status');
      } catch (e) {
        status = null;
      }
      renderStats(health, status);
      renderDatasets(health);
      loadCatchup();

      const query = { date: 'latest' };
      if (health.score && health.score.versions && health.score.versions.length) {
        query.version = health.score.versions[health.score.versions.length - 1];
      }
      const [scoreData, industries] = await Promise.all([
        App.get('/api/scores', query),
        App.get('/api/scores/industries', query).catch(() => ({ items: [] })),
      ]);
      const date = (scoreData.meta || {}).date;
      renderGradeChart((scoreData.meta || {}).distribution, date);
      renderRanks(scoreData.items || [], date);
      renderIndustryHeat(industries.items || []);
    } catch (err) {
      App.toast(err.message, 'error');
    }
    renderJobs();
  }

  window.addEventListener('resize', () => gradeChart && gradeChart.resize());
  document.addEventListener('DOMContentLoaded', reload);

  return { reload, quickUpdate, runCatchup, loadCatchup };
})();
