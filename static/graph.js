// SplitSync — Graph drawing via SVG and accessible DOM elements
// Per SPEC §10: Members on a circle, quadratic curve edges, arrowhead markers,
// amount label on midpoint rect, red/dashed for pairwise, green/solid for plan.
// Screen-reader accessible with title, desc, and hidden table.

(function (global) {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function getInitials(name) {
    if (!name) return '';
    const parts = name.trim().split(/\s+/);
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  function formatMoneySimple(paise) {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
    }).format((paise || 0) / 100);
  }

  // DECISION: Draw graph using createElementNS with pure CSSOM and attributes without string HTML parsing.
  function renderSvgGraph(container, members, edges, isPlan, titleText, descText) {
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }

    const memberMap = new Map();
    members.forEach(m => memberMap.set(m.id, m));

    const svg = document.createElementNS(SVG_NS, 'svg');
    // DECISION: Expanded viewBox and adjusted circle radius ensure member name labels are never clipped.
    svg.setAttribute('viewBox', '-35 -25 470 450');
    svg.setAttribute('class', 'graph-svg');
    svg.setAttribute('role', 'img');

    const titleEl = document.createElementNS(SVG_NS, 'title');
    titleEl.textContent = titleText;
    svg.appendChild(titleEl);

    const descEl = document.createElementNS(SVG_NS, 'desc');
    descEl.textContent = descText;
    svg.appendChild(descEl);

    // Defs for arrowhead markers
    const defs = document.createElementNS(SVG_NS, 'defs');

    const markerId = isPlan ? 'arrow-plan' : 'arrow-pairwise';
    const markerColor = isPlan ? '#16a34a' : '#dc2626';

    const marker = document.createElementNS(SVG_NS, 'marker');
    marker.setAttribute('id', markerId);
    marker.setAttribute('viewBox', '0 0 10 10');
    marker.setAttribute('refX', '7');
    marker.setAttribute('refY', '5');
    marker.setAttribute('markerWidth', '6');
    marker.setAttribute('markerHeight', '6');
    marker.setAttribute('orient', 'auto-start-reverse');

    const markerPath = document.createElementNS(SVG_NS, 'path');
    markerPath.setAttribute('d', 'M 0 1 L 10 5 L 0 9 z');
    markerPath.setAttribute('fill', markerColor);
    marker.appendChild(markerPath);
    defs.appendChild(marker);
    svg.appendChild(defs);

    const cx = 200;
    const cy = 200;
    const r = 115;
    const nodeR = 14;
    const n = members.length;

    const positions = new Map();
    members.forEach((m, idx) => {
      const angle = (2 * Math.PI * idx) / Math.max(n, 1) - Math.PI / 2;
      const x = cx + r * Math.cos(angle);
      const y = cy + r * Math.sin(angle);
      positions.set(m.id, { x, y, angle, member: m });
    });

    const edgesGroup = document.createElementNS(SVG_NS, 'g');
    edgesGroup.setAttribute('class', 'graph-edges');

    const labelsGroup = document.createElementNS(SVG_NS, 'g');
    labelsGroup.setAttribute('class', 'graph-labels');

    edges.forEach(edge => {
      const fromPos = positions.get(edge.from);
      const toPos = positions.get(edge.to);
      if (!fromPos || !toPos) return;

      const x1 = fromPos.x;
      const y1 = fromPos.y;
      const x2 = toPos.x;
      const y2 = toPos.y;

      let midX = (x1 + x2) / 2;
      let midY = (y1 + y2) / 2;

      // Curve slightly towards center or normal
      let cpX = midX * 0.7 + cx * 0.3;
      let cpY = midY * 0.7 + cy * 0.3;

      const dx = x2 - x1;
      const dy = y2 - y1;
      const dist = Math.hypot(dx, dy);

      if (dist > 0 && Math.abs(midX - cx) < 5 && Math.abs(midY - cy) < 5) {
        const nx = -dy / dist;
        const ny = dx / dist;
        cpX = cx + nx * 28;
        cpY = cy + ny * 28;
      }

      // Back off curve endpoints so markers don't overlap node circles
      let startX = x1;
      let startY = y1;
      const sDx = cpX - x1;
      const sDy = cpY - y1;
      const sDist = Math.hypot(sDx, sDy);
      if (sDist > 0) {
        startX = x1 + (sDx / sDist) * (nodeR + 2);
        startY = y1 + (sDy / sDist) * (nodeR + 2);
      }

      let targetX = x2;
      let targetY = y2;
      const tDx = x2 - cpX;
      const tDy = x2 - cpY;
      const tDist = Math.hypot(tDx, tDy);
      if (tDist > 0) {
        targetX = x2 - (tDx / tDist) * (nodeR + 6);
        targetY = y2 - (tDy / tDist) * (nodeR + 6);
      }

      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', 'M ' + startX + ' ' + startY + ' Q ' + cpX + ' ' + cpY + ' ' + targetX + ' ' + targetY);
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', markerColor);
      path.setAttribute('stroke-width', isPlan ? '2.5' : '1.5');
      if (!isPlan) {
        path.setAttribute('stroke-dasharray', '4,4');
      }
      path.setAttribute('marker-end', 'url(#' + markerId + ')');
      edgesGroup.appendChild(path);

      // Label at midpoint t=0.5
      const lx = 0.25 * startX + 0.5 * cpX + 0.25 * targetX;
      const ly = 0.25 * startY + 0.5 * cpY + 0.25 * targetY;
      const amountStr = formatMoneySimple(edge.amount_paise);

      const labelG = document.createElementNS(SVG_NS, 'g');

      const bgRect = document.createElementNS(SVG_NS, 'rect');
      const boxWidth = Math.max(52, amountStr.length * 6.5);
      bgRect.setAttribute('x', (lx - boxWidth / 2).toString());
      bgRect.setAttribute('y', (ly - 8).toString());
      bgRect.setAttribute('width', boxWidth.toString());
      bgRect.setAttribute('height', '16');
      bgRect.setAttribute('rx', '3');
      bgRect.setAttribute('ry', '3');
      bgRect.setAttribute('fill', '#ffffff');
      bgRect.setAttribute('stroke', '#cbd5e1');
      bgRect.setAttribute('stroke-width', '1');
      labelG.appendChild(bgRect);

      const textEl = document.createElementNS(SVG_NS, 'text');
      textEl.setAttribute('x', lx.toString());
      textEl.setAttribute('y', ly.toString());
      textEl.setAttribute('text-anchor', 'middle');
      textEl.setAttribute('dominant-baseline', 'central');
      textEl.setAttribute('font-size', '9');
      textEl.setAttribute('font-weight', '600');
      textEl.setAttribute('font-family', 'system-ui, sans-serif');
      textEl.setAttribute('fill', '#0f172a');
      textEl.textContent = amountStr;
      labelG.appendChild(textEl);

      labelsGroup.appendChild(labelG);
    });

    svg.appendChild(edgesGroup);
    svg.appendChild(labelsGroup);

    // Nodes group
    const nodesGroup = document.createElementNS(SVG_NS, 'g');
    nodesGroup.setAttribute('class', 'graph-nodes');

    members.forEach(m => {
      const pos = positions.get(m.id);
      if (!pos) return;

      const nodeG = document.createElementNS(SVG_NS, 'g');

      const circle = document.createElementNS(SVG_NS, 'circle');
      circle.setAttribute('cx', pos.x.toString());
      circle.setAttribute('cy', pos.y.toString());
      circle.setAttribute('r', nodeR.toString());
      // DECISION: Member colors applied directly via SVG fill attribute, adhering to CSP style-src 'self'.
      circle.setAttribute('fill', m.color || '#2563eb');
      nodeG.appendChild(circle);

      const initialsText = document.createElementNS(SVG_NS, 'text');
      initialsText.setAttribute('x', pos.x.toString());
      initialsText.setAttribute('y', pos.y.toString());
      initialsText.setAttribute('text-anchor', 'middle');
      initialsText.setAttribute('dominant-baseline', 'central');
      initialsText.setAttribute('font-size', '10');
      initialsText.setAttribute('font-weight', '700');
      initialsText.setAttribute('fill', '#ffffff');
      initialsText.textContent = getInitials(m.name);
      nodeG.appendChild(initialsText);

      // Name label offset outwards with anchor adjustment to prevent clipping
      const cosA = Math.cos(pos.angle);
      const sinA = Math.sin(pos.angle);
      let anchor = 'middle';
      let baseline = 'central';
      let lx = pos.x;
      let ly = pos.y;

      if (cosA > 0.25) {
        anchor = 'start';
        lx = pos.x + nodeR + 6;
        ly = pos.y + sinA * 6;
      } else if (cosA < -0.25) {
        anchor = 'end';
        lx = pos.x - (nodeR + 6);
        ly = pos.y + sinA * 6;
      } else {
        anchor = 'middle';
        lx = pos.x;
        if (sinA < 0) {
          baseline = 'auto';
          ly = pos.y - (nodeR + 6);
        } else {
          baseline = 'hanging';
          ly = pos.y + (nodeR + 6);
        }
      }

      const nameText = document.createElementNS(SVG_NS, 'text');
      nameText.setAttribute('x', lx.toString());
      nameText.setAttribute('y', ly.toString());
      nameText.setAttribute('text-anchor', anchor);
      nameText.setAttribute('dominant-baseline', baseline);
      nameText.setAttribute('font-size', '10');
      nameText.setAttribute('font-weight', '500');
      nameText.setAttribute('fill', '#64748b');
      nameText.textContent = m.name;
      nodeG.appendChild(nameText);

      nodesGroup.appendChild(nodeG);
    });

    svg.appendChild(nodesGroup);
    container.appendChild(svg);

    // Accessible hidden table of edges
    // A table ignores width: 1px, so the visually hidden box is a div around it.
    const hidden = document.createElement('div');
    hidden.className = 'sr-only';
    const table = document.createElement('table');

    const caption = document.createElement('caption');
    caption.textContent = titleText + ' — ' + descText;
    table.appendChild(caption);

    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['From', 'To', 'Amount'].forEach(col => {
      const th = document.createElement('th');
      th.textContent = col;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    edges.forEach(edge => {
      const fromMember = memberMap.get(edge.from);
      const toMember = memberMap.get(edge.to);
      const tr = document.createElement('tr');

      const tdFrom = document.createElement('td');
      tdFrom.textContent = fromMember ? fromMember.name : ('#' + edge.from);
      tr.appendChild(tdFrom);

      const tdTo = document.createElement('td');
      tdTo.textContent = toMember ? toMember.name : ('#' + edge.to);
      tr.appendChild(tdTo);

      const tdAmount = document.createElement('td');
      tdAmount.textContent = formatMoneySimple(edge.amount_paise);
      tr.appendChild(tdAmount);

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    hidden.appendChild(table);
    container.appendChild(hidden);
  }

  global.SplitSyncGraph = {
    renderSvgGraph,
    getInitials,
  };
})(window);
