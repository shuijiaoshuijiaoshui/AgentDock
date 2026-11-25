/* AgentDock PRD — charts & diagrams
 * 图表：图 2-1 三层痛点分布 / 图 7-1 功能优先级构成
 * 流程图：图 6-1 多 Agent 协作链路（Mermaid，startOnLoad）
 */
(function () {
  'use strict';

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    v = (v || '').trim();
    return v || fallback;
  }

  function initCharts() {
    if (typeof echarts === 'undefined') return;

    var accent = cssVar('--accent', '#0f9d58');
    var accent2 = cssVar('--accent2', '#1a73e8');
    var ink = cssVar('--ink', '#14201a');
    var muted = cssVar('--muted', '#5c6b63');
    var rule = cssVar('--rule', '#dfe6e1');
    var font = cssVar('--font', 'sans-serif');

    var charts = [];

    /* ---------- 图 2-1：三层痛点数量与优先级分布 ---------- */
    var painEl = document.getElementById('chart-pain');
    if (painEl) {
      var pain = echarts.init(painEl, null, { renderer: 'svg' });
      pain.setOption({
        animation: false,
        textStyle: { fontFamily: font, color: ink },
        grid: { left: 8, right: 16, top: 48, bottom: 8, containLabel: true },
        tooltip: {
          trigger: 'axis',
          appendToBody: true,
          axisPointer: { type: 'shadow' }
        },
        legend: {
          top: 8,
          textStyle: { color: muted, fontFamily: font }
        },
        xAxis: {
          type: 'category',
          data: ['用户侧痛点', '商家侧痛点', '平台/工程侧痛点'],
          axisLine: { lineStyle: { color: rule } },
          axisLabel: { color: muted, fontFamily: font }
        },
        yAxis: {
          type: 'value',
          name: '数量',
          nameTextStyle: { color: muted, fontFamily: font },
          axisLine: { show: false },
          splitLine: { lineStyle: { color: rule } },
          axisLabel: { color: muted, fontFamily: font }
        },
        series: [
          {
            name: 'P0 高优先',
            type: 'bar',
            stack: 'total',
            barMaxWidth: 48,
            itemStyle: { color: accent },
            data: [2, 2, 2]
          },
          {
            name: 'P1 中优先',
            type: 'bar',
            stack: 'total',
            barMaxWidth: 48,
            itemStyle: { color: accent2 },
            data: [1, 1, 1]
          },
          {
            name: 'P2 一般',
            type: 'bar',
            stack: 'total',
            barMaxWidth: 48,
            itemStyle: { color: '#b7c3bc' },
            data: [1, 1, 1]
          }
        ]
      });
      charts.push(pain);
    }

    /* ---------- 图 7-1：核心功能模块的优先级构成 ---------- */
    var priEl = document.getElementById('chart-priority');
    if (priEl) {
      var pri = echarts.init(priEl, null, { renderer: 'svg' });
      pri.setOption({
        animation: false,
        textStyle: { fontFamily: font, color: ink },
        grid: { left: 8, right: 24, top: 48, bottom: 8, containLabel: true },
        tooltip: { trigger: 'axis', appendToBody: true, axisPointer: { type: 'shadow' } },
        legend: { top: 8, textStyle: { color: muted, fontFamily: font } },
        xAxis: {
          type: 'value',
          name: '功能条目数',
          nameTextStyle: { color: muted, fontFamily: font },
          axisLine: { show: false },
          splitLine: { lineStyle: { color: rule } },
          axisLabel: { color: muted, fontFamily: font }
        },
        yAxis: {
          type: 'category',
          data: ['智能导购问答', 'A2A 多 Agent 协作', '来源溯源与拒答', '企业级治理', '运营与可观测'],
          axisLine: { lineStyle: { color: rule } },
          axisLabel: { color: muted, fontFamily: font }
        },
        series: [
          {
            name: 'P0',
            type: 'bar',
            stack: 'total',
            barMaxWidth: 28,
            itemStyle: { color: accent },
            data: [3, 3, 2, 2, 1]
          },
          {
            name: 'P1',
            type: 'bar',
            stack: 'total',
            barMaxWidth: 28,
            itemStyle: { color: accent2 },
            data: [2, 1, 2, 2, 2]
          },
          {
            name: 'P2',
            type: 'bar',
            stack: 'total',
            barMaxWidth: 28,
            itemStyle: { color: '#b7c3bc' },
            data: [1, 1, 1, 1, 2]
          }
        ]
      });
      charts.push(pri);
    }

    window.addEventListener('resize', function () {
      charts.forEach(function (c) { c.resize(); });
    });
  }

  function initMermaid() {
    if (typeof mermaid === 'undefined') return;
    mermaid.initialize({
      startOnLoad: true,
      theme: 'neutral',
      securityLevel: 'loose',
      fontFamily: cssVar('--font', 'sans-serif')
    });
  }

  function boot() {
    initMermaid();
    initCharts();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
