/* renderFlow.js — 数据驱动模块流程图渲染器（零依赖，双击可看）
   用法（页面底部）：
   <div id="mflow"></div>
   <script src="../assets/renderFlow.js"></script>
   <script>
     renderFlow("mflow", {
       small: false,                       // true = 迷你尺寸（"你在哪"位置指示用）
       rows: [ ["a","b"], ["c","d"] ],     // 每行一条横向链，节点 id 按序
       nodes: {
         a: { label:"节点A", sub:"副标", color:"green", href:"page.html", current:true }
       },
       edges: [ {from:"a", to:"b", label:"≥0.85", dashed:true, color:"orange"} ]
     });
   </script>
   颜色语义（与导览图例一致）：green 主链路 | orange 快速通道 | blue 数据/模型 | red 防御降级 | gray 辅助
*/
(function (global) {
  "use strict";
  var C = {
    green:  { stroke: "#07b464", fill: "#f4fbf7" },
    orange: { stroke: "#faad14", fill: "#fff8e6" },
    blue:   { stroke: "#1890ff", fill: "#f0f6ff" },
    red:    { stroke: "#e64340", fill: "#fff1f0" },
    gray:   { stroke: "#c9d2da", fill: "#f6f8fa" }
  };
  var BIG   = { w: 172, h: 48, gap: 42, vg: 60, f1: 12,  f2: 9.5, pad: 22 };
  var SMALL = { w: 112, h: 38, gap: 24, vg: 46, f1: 10,  f2: 8.5, pad: 16 };

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  global.renderFlow = function (containerId, data) {
    var el = document.getElementById(containerId);
    if (!el || !data || !data.rows) return;
    var S = data.small ? SMALL : BIG;
    var rows = data.rows, nodes = data.nodes || {};

    var maxN = 0;
    rows.forEach(function (r) { maxN = Math.max(maxN, r.length); });
    var width  = Math.round(S.pad * 2 + maxN * S.w + (maxN - 1) * S.gap);
    var height = Math.round(S.pad * 2 + rows.length * S.h + (rows.length - 1) * S.vg);

    var pos = {}, rowOf = {};
    rows.forEach(function (row, ri) {
      var rowW = row.length * S.w + (row.length - 1) * S.gap;
      var sx = Math.round((width - rowW) / 2);
      var y = Math.round(S.pad + ri * (S.h + S.vg));
      row.forEach(function (id, ci) {
        var x = Math.round(sx + ci * (S.w + S.gap));
        pos[id] = { x: x, y: y, cx: x + S.w / 2, cy: y + S.h / 2,
                    top: y, bottom: y + S.h, left: x, right: x + S.w, row: ri };
        rowOf[id] = ri;
      });
    });

    var out = [];
    out.push('<svg viewBox="0 0 ' + width + ' ' + height + '" width="100%" xmlns="http://www.w3.org/2000/svg" style="background:#fbfcfd;border:1px solid #e6ebf0;border-radius:12px">');
    Object.keys(C).forEach(function (k) {
      out.push('<marker id="rf-' + containerId + '-' + k + '" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="' + C[k].stroke + '"/></marker>');
    });

    (data.edges || []).forEach(function (e) {
      var a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      var cname = (e.color && C[e.color]) ? e.color : "green";
      var color = C[cname].stroke;
      var mk = "url(#rf-" + containerId + "-" + cname + ")";
      var dash = e.dashed ? ' stroke-dasharray="5,4"' : "";
      if (a.row === b.row) {
        var x1 = a.cx < b.cx ? a.right : a.left;
        var x2 = a.cx < b.cx ? b.left : b.right;
        out.push('<line x1="' + x1 + '" y1="' + a.cy + '" x2="' + x2 + '" y2="' + b.cy +
          '" stroke="' + color + '" stroke-width="2"' + dash + ' marker-end="' + mk + '"/>');
        if (e.label) out.push('<text x="' + Math.round((a.cx + b.cx) / 2) + '" y="' + (a.cy - 8) +
          '" font-size="9" fill="#8a97a5" text-anchor="middle">' + esc(e.label) + '</text>');
      } else {
        var midY = Math.round((Math.max(a.bottom, b.top) + Math.min(a.bottom, b.top)) / 2);
        var pts = (b.row > a.row)
          ? a.cx + "," + a.bottom + " " + a.cx + "," + midY + " " + b.cx + "," + midY + " " + b.cx + "," + b.top
          : a.cx + "," + a.top + " " + a.cx + "," + midY + " " + b.cx + "," + midY + " " + b.cx + "," + b.bottom;
        out.push('<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.8"' + dash + ' marker-end="' + mk + '"/>');
        if (e.label) out.push('<text x="' + Math.round((a.cx + b.cx) / 2) + '" y="' + (midY - 5) +
          '" font-size="9" fill="#8a97a5" text-anchor="middle">' + esc(e.label) + '</text>');
      }
    });

    rows.forEach(function (row) {
      row.forEach(function (id) {
        var n = nodes[id] || {}, p = pos[id];
        var c = C[n.color || "green"];
        var sw = n.current ? ' stroke-width="3.5"' : ' stroke-width="1.5"';
        var labelY = n.sub ? p.y + S.h / 2 - 2 : p.y + S.h / 2 + 4;
        var inner =
          '<rect x="' + p.x + '" y="' + p.y + '" width="' + S.w + '" height="' + S.h + '" rx="8" fill="' + c.fill + '" stroke="' + c.stroke + '"' + sw + '/>' +
          '<text x="' + p.cx + '" y="' + labelY + '" font-size="' + S.f1 + '" fill="#26313d" text-anchor="middle">' + esc(n.label) + '</text>' +
          (n.sub ? '<text x="' + p.cx + '" y="' + (p.y + S.h - 9) + '" font-size="' + S.f2 + '" fill="#8a97a5" text-anchor="middle">' + esc(n.sub) + '</text>' : '') +
          (n.current ? '<text x="' + (p.x + S.w - 2) + '" y="' + (p.y + S.h + 13) + '" font-size="9" fill="#1890ff" text-anchor="end">◉ 本页</text>' : '');
        var title = '<title>' + esc(n.sub || n.label) + '</title>';
        var g = n.href ? '<a href="' + n.href + '">' + title + inner + '</a>' : '<g>' + title + inner + '</g>';
        out.push('<g id="rf-' + containerId + '-' + id + '">' + g + '</g>');
      });
    });

    out.push('</svg>');
    el.innerHTML = out.join("");
  };
})(window);
