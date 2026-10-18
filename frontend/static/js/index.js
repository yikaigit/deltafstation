/* 首页：状态汇总 + 评级分布 + 最近任务 */
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

  function renderStats(health) {
    const bars = (health.datasets && health.datasets.bars) || {};
    const score = health.score || {};
    document.getElementById('stat-symbols').textContent = health.universe_count || 0;
    document.getElementById('stat-symbols-hint').textContent = health.universe_count
      ? '标的表已建立'
      : '标的表尚未建立';
    document.getElementById('stat-latest').textContent = bars.latest_date || '—';
    document.getElementById('stat-latest-hint').textContent = bars.exists ? 'bars 数据集' : '尚未回填数据';
    const versions = score.versions || [];
    document.getElementById('stat-version').textContent = versions.length ? versions[versions.length - 1] : '—';
    document.getElementById('stat-version-hint').textContent = versions.length ? '打分快照可用' : '尚无打分版本';
    document.getElementById('stat-score-date').textContent = score.latest_date || '—';
    document.getElementById('stat-score-hint').textContent = score.latest_date ? '快照已落盘' : '等待首次打分';
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
    document.getElementById('dataset-list').innerHTML = `<div class="chips">${html}</div>`;
  }

  function renderGradeChart(distribution, date) {
    const dom = document.getElementById('chart-grade');
    const entries = Object.entries(distribution || {});
    document.getElementById('dist-date').textContent = date || '—';
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
    const body = document.getElementById('job-body');
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

  async function reload() {
    try {
      const health = await App.get('/api/health');
      renderStats(health);
      renderDatasets(health);
      const scoreData = await App.get('/api/scores', { date: 'latest' });
      renderGradeChart(scoreData.meta.distribution, scoreData.meta.date);
    } catch (err) {
      App.toast(err.message, 'error');
    }
    renderJobs();
  }

  window.addEventListener('resize', () => gradeChart && gradeChart.resize());
  document.addEventListener('DOMContentLoaded', reload);

  return { reload };
})();
