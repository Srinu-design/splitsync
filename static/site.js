(function () {
  'use strict';

  const BASE = new URL('..', document.currentScript.src).pathname;
  if (!document.getElementById('landing-page')) return;
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function showHashPanel(scroll) {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (_) { id = 'about'; }
    const target = document.getElementById(id || 'about');
    const panel = target && target.closest('[data-tab]');
    const active = panel ? panel.dataset.tab : 'about';
    document.querySelectorAll('[data-tab]').forEach(node => {
      node.hidden = node.dataset.tab !== active;
    });
    document.querySelectorAll('[data-tab-link]').forEach(link => {
      if (link.dataset.tabLink === active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    document.body.dataset.activeTab = active;
    if (!scroll) return;
    requestAnimationFrame(() => {
      if (target && target !== panel) target.scrollIntoView({ behavior: 'auto', block: 'start' });
      else window.scrollTo({ top: 0, behavior: 'auto' });
    });
  }

  window.addEventListener('hashchange', () => showHashPanel(true));
  showHashPanel(Boolean(location.hash));

  const sectionLinks = new Map();
  document.querySelectorAll('.section-chips a, .how-nav a').forEach(link => {
    const id = link.hash.slice(1);
    if (document.getElementById(id)) sectionLinks.set(id, link);
  });
  if ('IntersectionObserver' in window) {
    const visible = new Map();
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) visible.set(entry.target.id, entry.target);
        else visible.delete(entry.target.id);
      });
      const current = Array.from(visible.values())
        .filter(node => !node.closest('[data-tab]').hidden)
        .sort((a, b) => Math.abs(a.getBoundingClientRect().top - 180) - Math.abs(b.getBoundingClientRect().top - 180))[0];
      if (!current) return;
      sectionLinks.forEach((link, id) => {
        if (id === current.id) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      });
    }, { rootMargin: '-150px 0px -45% 0px', threshold: 0 });
    sectionLinks.forEach((_, id) => observer.observe(document.getElementById(id)));

    if (!reducedMotion.matches) {
      const reveal = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add('is-revealed');
          reveal.unobserve(entry.target);
        });
      }, { rootMargin: '0px 0px 24px 0px', threshold: 0.03 });
      document.querySelectorAll('[data-reveal]').forEach(node => {
        node.classList.add('reveal-pending');
        reveal.observe(node);
      });
    }
  }

  let checkingStatus = false;
  async function checkStatus() {
    if (document.hidden || checkingStatus) return;
    checkingStatus = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    const pill = document.getElementById('site-status');
    let state = 'down';
    let label = 'Down';
    let detail = 'The server is not ready.';
    try {
      const response = await fetch(BASE + 'readyz', { cache: 'no-store', signal: controller.signal });
      const body = await response.json();
      if (response.ok && body.db === 'ok' && body.redis === 'ok') {
        state = 'ok'; label = 'Live'; detail = 'PostgreSQL and Redis are ready.';
      } else if (response.ok && body.db === 'ok' && body.redis === 'degraded') {
        state = 'degraded'; label = 'Degraded'; detail = 'PostgreSQL is ready; Redis is degraded. Writes remain protected.';
      }
    } catch (_) {
      detail = 'Readiness could not be checked. Try again shortly.';
    } finally {
      clearTimeout(timeout);
      checkingStatus = false;
    }
    pill.dataset.state = state;
    pill.title = detail;
    document.getElementById('site-status-text').textContent = label;
  }
  checkStatus();
  setInterval(checkStatus, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkStatus(); });

  const members = [
    { id: 1, name: 'Aarav', color: '#E4572E' },
    { id: 2, name: 'Priya', color: '#17BEBB' },
    { id: 3, name: 'Rohan', color: '#FFC914' },
    { id: 4, name: 'Sneha', color: '#76B041' },
    { id: 5, name: 'Karthik', color: '#7E52A0' },
  ];
  // The reference seed is a read-only illustration; opening the landing page creates no room.
  const incurred = [
    [4, 1, 288000], [5, 1, 238000], [5, 2, 196000], [4, 3, 136000], [4, 2, 126000],
    [5, 3, 116000], [4, 5, 50000], [3, 2, 40000], [1, 3, 38000], [2, 1, 12000],
  ];
  const optimal = [[4, 2, 350000], [4, 3, 250000], [5, 1, 500000]];
  const graph = document.getElementById('hero-graph');
  const iouButton = document.getElementById('hero-ious-btn');
  const planButton = document.getElementById('hero-plan-btn');
  let graphTimer;
  function renderHero(plan, animate) {
    clearTimeout(graphTimer);
    iouButton.setAttribute('aria-pressed', String(!plan));
    planButton.setAttribute('aria-pressed', String(plan));
    function draw() {
      window.SplitSyncGraph.renderSvgGraph(
        graph, members,
        (plan ? optimal : incurred).map(([from, to, amount_paise]) => ({ from, to, amount_paise })),
        plan,
        plan ? 'Goa trip: three optimal transfers' : 'Goa trip: ten IOUs as incurred',
        plan ? 'Sneha pays Priya ₹3,500 and Rohan ₹2,500. Karthik pays Aarav ₹5,000.'
          : 'Ten red dashed arrows show pairwise debts from eight trip expenses. The adjacent transfer toggle shows the exact simplified plan.'
      );
      document.getElementById('hero-graph-caption').textContent = plan
        ? 'Three solid transfers settle the same balances. Greedy needs four.'
        : 'Ten dashed IOUs as incurred. Choose 3 transfers to see the exact plan.';
      requestAnimationFrame(() => graph.classList.remove('is-switching'));
    }
    if (animate && !reducedMotion.matches) {
      graph.classList.add('is-switching');
      graphTimer = setTimeout(draw, 130);
    } else draw();
  }
  if (window.SplitSyncGraph) {
    renderHero(false, false);
    iouButton.addEventListener('click', () => renderHero(false, true));
    planButton.addEventListener('click', () => renderHero(true, true));
  }

  const origin = location.origin + BASE;
  const websocket = new URL(BASE + 'ws/rooms/ROOM_SLUG', location.href);
  websocket.protocol = websocket.protocol === 'https:' ? 'wss:' : 'ws:';
  const settlement = `curl -i -X POST "${origin}api/rooms/$SLUG/settlements" \\
  -H 'Content-Type: application/json' \\
  -H 'Idempotency-Key: settlement-demo-0001' \\
  --data "{\\"from_member\\":$FROM_MEMBER,\\"to_member\\":$TO_MEMBER,\\"amount\\":\\"5000\\",\\"expected_graph_version\\":$GRAPH_VERSION}"`;
  const examples = {
    create: `curl -sS -X POST '${origin}api/rooms' \\
  -H 'Content-Type: application/json' \\
  --data '{"kind":"DEMO"}'

# Copy the slug from the response, then read this room's member ids and version.
SLUG='paste-your-room-slug'
curl -sS "${origin}api/rooms/$SLUG/state"`,
    expense: `# Replace these example ids with Rohan, Sneha and Karthik from your state.
ROHAN_ID=3
SNEHA_ID=4
KARTHIK_ID=5
curl -i -X POST "${origin}api/rooms/$SLUG/expenses" \\
  -H 'Content-Type: application/json' \\
  -H 'Idempotency-Key: expense-example-0001' \\
  --data "{\\"title\\":\\"Airport auto\\",\\"paid_by\\":$ROHAN_ID,\\"amount\\":\\"100\\",\\"split\\":{\\"type\\":\\"EQUAL\\",\\"members\\":[{\\"id\\":$ROHAN_ID},{\\"id\\":$SNEHA_ID},{\\"id\\":$KARTHIK_ID}]}}"`,
    settlement: `# Use Karthik and Aarav's current ids, and the latest graph_version.
FROM_MEMBER=5
TO_MEMBER=1
GRAPH_VERSION=10
${settlement}`,
    replay: `# Reuse the same key and payment; the committed receipt is replayed.
${settlement}`,
    websocket: websocket.href,
  };
  document.querySelectorAll('[data-api-example]').forEach(code => {
    code.textContent = examples[code.dataset.apiExample];
  });

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) {}
    // DECISION: Keep copyable examples usable over plain HTTP with the existing textarea fallback.
    const active = document.activeElement;
    const field = document.createElement('textarea');
    field.className = 'sr-only';
    field.value = text;
    field.setAttribute('aria-label', 'Text to copy');
    document.body.appendChild(field);
    field.focus();
    field.select();
    let copied = false;
    try { copied = document.execCommand('copy'); } catch (_) {}
    field.remove();
    if (active) active.focus();
    return copied;
  }
  document.querySelectorAll('[data-copy-target]').forEach(button => {
    button.addEventListener('click', async () => {
      const text = document.getElementById(button.dataset.copyTarget).textContent;
      const copied = await copyText(text);
      button.textContent = copied ? 'Copied' : 'Select the code to copy';
      document.getElementById('copy-status').textContent = copied ? 'Code copied.' : 'Copy unavailable. Select and copy the code manually.';
      setTimeout(() => { button.textContent = 'Copy'; }, 1800);
    });
  });
})();
