// SplitSync — Frontend vanilla JavaScript
// Per SPEC §10, §8, §9 and deployment overrides.
// No build step, no framework, no CDN, strict DOM creation via createElement.

(function () {
  'use strict';

  // DECISION: The app may be served under a path prefix or at root. Compute BASE from document.currentScript.src.
  const BASE = new URL('..', document.currentScript.src).pathname;

  // DECISION: In plain HTTP environments standard random UUIDs may not exist. Use crypto.getRandomValues for 32 hex chars.
  function generateIdempotencyKey() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    let hex = '';
    for (let i = 0; i < bytes.length; i++) {
      hex += bytes[i].toString(16).padStart(2, '0');
    }
    return hex;
  }

  // DECISION: Construct WebSocket URL relative to BASE with http->ws and https->wss.
  function getWebSocketUrl(slug) {
    const wsUrl = new URL(BASE + 'ws/rooms/' + slug, location.href);
    wsUrl.protocol = wsUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    return wsUrl.href;
  }

  // DECISION: Format currency strictly through Intl.NumberFormat('en-IN', INR).
  const inrFormatter = new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
  });
  function formatMoney(paise) {
    return inrFormatter.format((paise || 0) / 100);
  }

  // DECISION: Format dates strictly through Intl.DateTimeFormat('en-IN').
  const dateFormatter = new Intl.DateTimeFormat('en-IN', {
    day: 'numeric',
    month: 'short',
  });
  function formatDate(dateStr) {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    return isNaN(d.getTime()) ? dateStr : dateFormatter.format(d);
  }

  function formatTime(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  function cleanRupeeInput(str) {
    return (str || '').replace(/[₹,\s]/g, '');
  }

  function parseRupeesToPaise(str) {
    const cleaned = cleanRupeeInput(str);
    const num = parseFloat(cleaned);
    return isNaN(num) ? 0 : Math.round(num * 100);
  }

  function getTodayIsoDate() {
    const d = new Date();
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return year + '-' + month + '-' + day;
  }

  // Toast System
  function showToast(title, detail, actionLabel, actionCallback, durationMs) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = 'toast';

    const titleEl = document.createElement('div');
    titleEl.className = 'toast-title';
    titleEl.textContent = title;
    toast.appendChild(titleEl);

    if (detail) {
      const detailEl = document.createElement('div');
      detailEl.className = 'toast-detail';
      detailEl.textContent = detail;
      toast.appendChild(detailEl);
    }

    if (actionLabel && actionCallback) {
      const actionBtn = document.createElement('button');
      actionBtn.type = 'button';
      actionBtn.className = 'btn btn-sm btn-primary toast-action';
      actionBtn.textContent = actionLabel;
      actionBtn.addEventListener('click', () => {
        actionCallback();
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      });
      toast.appendChild(actionBtn);
    }

    container.appendChild(toast);

    setTimeout(() => {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, durationMs || 5000);
  }

  // LocalStorage Helpers
  function getStoredRooms() {
    try {
      const raw = localStorage.getItem('splitsync.rooms');
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) {
      return [];
    }
  }

  function saveRoomToHistory(slug, name, kind) {
    try {
      const rooms = getStoredRooms();
      const filtered = rooms.filter(r => r.slug !== slug);
      filtered.unshift({
        slug,
        name,
        kind,
        last_opened: Date.now(),
      });
      const capped = filtered.slice(0, 50);
      localStorage.setItem('splitsync.rooms', JSON.stringify(capped));
    } catch (_) {}
  }

  function getStoredActingMember(slug) {
    try {
      return localStorage.getItem('splitsync.me.' + slug) || '';
    } catch (_) {
      return '';
    }
  }

  function setStoredActingMember(slug, memberId) {
    try {
      if (memberId) {
        localStorage.setItem('splitsync.me.' + slug, memberId.toString());
      } else {
        localStorage.removeItem('splitsync.me.' + slug);
      }
    } catch (_) {}
  }

  // DECISION: On plain HTTP, navigator.clipboard might be unavailable. Fall back to selecting an input with prompt.
  async function copyRoomLink() {
    const url = location.href;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(url);
        showToast('Link copied to clipboard');
        return;
      } catch (_) {}
    }
    const dialog = document.getElementById('copy-fallback-dialog');
    const input = document.getElementById('copy-link-input');
    if (dialog && input) {
      input.value = url;
      dialog.classList.remove('hidden');
      input.focus();
      input.select();
      showToast('Press Ctrl+C to copy');
    }
  }

  // ==========================================
  // LANDING PAGE CONTROLLER
  // ==========================================
  function initLandingPage() {
    const startDemoBtn = document.getElementById('start-demo-btn');
    const createGroupForm = document.getElementById('create-group-form');
    const myGroupsList = document.getElementById('my-groups-list');
    const noGroupsMsg = document.getElementById('no-groups-msg');

    if (startDemoBtn) {
      startDemoBtn.addEventListener('click', async () => {
        startDemoBtn.disabled = true;
        try {
          const res = await fetch(BASE + 'api/rooms', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind: 'DEMO' }),
          });
          if (res.ok) {
            const data = await res.json();
            saveRoomToHistory(data.slug, 'Goa Road Trip', 'DEMO');
            // DECISION: Build navigation URL from BASE + 'r/' + slug instead of url field verbatim.
            location.href = BASE + 'r/' + data.slug;
          } else {
            const err = await res.json().catch(() => ({}));
            showToast(err.title || 'Failed to create demo', err.detail || '');
            startDemoBtn.disabled = false;
          }
        } catch (err) {
          showToast('Network error', err.message);
          startDemoBtn.disabled = false;
        }
      });
    }

    if (createGroupForm) {
      createGroupForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const nameInput = document.getElementById('group-name-input');
        const membersInput = document.getElementById('group-members-input');
        const submitBtn = document.getElementById('create-group-btn');

        const groupName = nameInput.value.trim();
        const rawMembers = membersInput.value.split('\n').map(m => m.trim()).filter(Boolean);

        if (rawMembers.length < 2 || rawMembers.length > 50) {
          showToast('Invalid member count', 'Please enter between 2 and 50 members (one per line).');
          return;
        }

        submitBtn.disabled = true;
        try {
          const res = await fetch(BASE + 'api/rooms', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              kind: 'GROUP',
              name: groupName,
              members: rawMembers,
            }),
          });
          if (res.ok) {
            const data = await res.json();
            saveRoomToHistory(data.slug, groupName, 'GROUP');
            location.href = BASE + 'r/' + data.slug;
          } else {
            const err = await res.json().catch(() => ({}));
            showToast(err.title || 'Failed to create group', err.detail || '');
            submitBtn.disabled = false;
          }
        } catch (err) {
          showToast('Network error', err.message);
          submitBtn.disabled = false;
        }
      });
    }

    // Render My Groups
    if (myGroupsList && noGroupsMsg) {
      const rooms = getStoredRooms();
      if (rooms.length === 0) {
        noGroupsMsg.classList.remove('hidden');
      } else {
        noGroupsMsg.classList.add('hidden');
        rooms.forEach(r => {
          const li = document.createElement('li');
          const a = document.createElement('a');
          a.className = 'room-item-link';
          a.href = BASE + 'r/' + r.slug;

          const nameSpan = document.createElement('span');
          nameSpan.className = 'room-item-name';
          nameSpan.textContent = r.name || r.slug;
          a.appendChild(nameSpan);

          const metaSpan = document.createElement('span');
          metaSpan.className = 'room-item-meta';

          const badge = document.createElement('span');
          badge.className = 'badge';
          badge.textContent = r.kind || 'GROUP';
          metaSpan.appendChild(badge);

          if (r.last_opened) {
            const dateSpan = document.createElement('span');
            dateSpan.textContent = formatDate(r.last_opened);
            metaSpan.appendChild(dateSpan);
          }

          a.appendChild(metaSpan);
          li.appendChild(a);
          myGroupsList.appendChild(li);
        });
      }
    }
  }

  // ==========================================
  // ROOM PAGE CONTROLLER
  // ==========================================
  function initRoomPage() {
    const match = location.pathname.match(/\/r\/([^\/?#]+)/);
    const slug = match ? match[1] : null;

    if (!slug) {
      const notFound = document.getElementById('room-not-found-msg');
      const content = document.getElementById('room-content');
      if (notFound) notFound.classList.remove('hidden');
      if (content) content.classList.add('hidden');
      return;
    }

    let currentVersion = 0;
    let currentSnapshot = null;
    let activePlanType = 'OPTIMAL';
    let lastETag = null;
    let ws = null;
    let pingInterval = null;
    let reconnectTimeout = null;
    let reconnectAttempts = 0;
    let searchDebounceTimer = null;
    let searchAbortController = null;
    let searchNextCursor = null;
    let historyNextCursor = null;
    let historyLoadSeq = 0;
    let historyAbortController = null;
    let currentLineageFilter = null;
    let lastDeletedVersionId = null;

    const copyBtn = document.getElementById('copy-link-btn');
    if (copyBtn) {
      copyBtn.addEventListener('click', copyRoomLink);
    }
    const copyCloseBtn = document.getElementById('copy-fallback-close-btn');
    if (copyCloseBtn) {
      copyCloseBtn.addEventListener('click', () => {
        const dialog = document.getElementById('copy-fallback-dialog');
        if (dialog) dialog.classList.add('hidden');
      });
    }

    const actingPicker = document.getElementById('acting-member-picker');
    if (actingPicker) {
      actingPicker.addEventListener('change', () => {
        setStoredActingMember(slug, actingPicker.value);
      });
    }

    function getActingMemberHeader() {
      const val = actingPicker ? actingPicker.value : getStoredActingMember(slug);
      return val ? { 'X-Acting-Member': val } : {};
    }

    // Set connection status pill
    function setConnectionStatus(status) {
      const pill = document.getElementById('connection-pill');
      if (!pill) return;
      pill.className = 'pill';
      if (status === 'live') {
        pill.classList.add('pill-live');
        pill.textContent = 'Live';
      } else if (status === 'reconnecting') {
        pill.classList.add('pill-reconnecting');
        pill.textContent = 'Reconnecting';
      } else {
        pill.classList.add('pill-polling');
        pill.textContent = 'Polling';
      }
    }

    // Connect WebSocket
    function connectWs() {
      if (ws) {
        try { ws.close(); } catch (_) {}
      }
      clearInterval(pingInterval);

      setConnectionStatus('reconnecting');
      const wsUrl = getWebSocketUrl(slug);

      try {
        ws = new WebSocket(wsUrl);
      } catch (_) {
        setConnectionStatus('polling');
        scheduleReconnect();
        return;
      }

      ws.onopen = () => {
        setConnectionStatus('live');
        reconnectAttempts = 0;
        // Ping every 25 s
        pingInterval = setInterval(() => {
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, 25000);
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'snapshot' && msg.state) {
            if (msg.version > currentVersion) {
              currentVersion = msg.version;
              renderState(msg.state);
            }
          }
        } catch (_) {}
      };

      ws.onclose = () => {
        clearInterval(pingInterval);
        setConnectionStatus('reconnecting');
        scheduleReconnect();
      };

      ws.onerror = () => {
        try { ws.close(); } catch (_) {}
      };
    }

    // Reconnect with exponential backoff: 1, 2, 4, 8, 16, then 30 s + 0–500 ms jitter
    function scheduleReconnect() {
      clearTimeout(reconnectTimeout);
      const delays = [1000, 2000, 4000, 8000, 16000];
      const baseDelay = reconnectAttempts < delays.length ? delays[reconnectAttempts] : 30000;
      reconnectAttempts++;
      const jitter = Math.floor(Math.random() * 500);
      const delay = baseDelay + jitter;

      if (reconnectAttempts > 2) {
        setConnectionStatus('polling');
      }

      reconnectTimeout = setTimeout(connectWs, delay);
    }

    // Fetch state via HTTP GET with ETag
    async function fetchStateHttp() {
      try {
        const headers = {};
        if (lastETag) {
          headers['If-None-Match'] = lastETag;
        }
        const res = await fetch(BASE + 'api/rooms/' + slug + '/state', { headers });

        if (res.status === 304) {
          return; // No change
        }

        if (res.status === 404) {
          const notFound = document.getElementById('room-not-found-msg');
          const content = document.getElementById('room-content');
          if (notFound) notFound.classList.remove('hidden');
          if (content) content.classList.add('hidden');
          return;
        }

        if (res.ok) {
          lastETag = res.headers.get('ETag');
          const data = await res.json();
          if (data.room && data.room.graph_version > currentVersion) {
            currentVersion = data.room.graph_version;
            renderState(data);
          }
        }
      } catch (_) {}
    }

    // Polling every 30 s while visible + on visibility change
    setInterval(() => {
      if (!document.hidden) {
        fetchStateHttp();
      }
    }, 30000);

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        fetchStateHttp();
      }
    });

    // Main API Request Wrapper with 409 handlers
    async function apiRequest(path, options = {}) {
      const url = BASE + path;
      let retryCount = 0;

      while (true) {
        let response;
        try {
          response = await fetch(url, options);
        } catch (err) {
          showToast('Network error', err.message);
          throw err;
        }

        if (response.status === 409) {
          let errBody = null;
          try {
            errBody = await response.clone().json();
          } catch (_) {}

          // GROUP_BUSY: retry once after Retry-After
          if (errBody && errBody.code === 'GROUP_BUSY' && retryCount < 1) {
            retryCount++;
            const retryAfter = parseInt(response.headers.get('Retry-After') || '1', 10);
            await new Promise(r => setTimeout(r, retryAfter * 1000));
            continue;
          }

          // EXPENSE_CHANGED
          if (errBody && errBody.code === 'EXPENSE_CHANGED') {
            await fetchStateHttp();
            showToast('Someone changed this expense first — showing the latest version', errBody.detail || '');
            if (errBody.current_expense_id && !errBody.deleted) {
              populateExpenseEditor(errBody.current_expense_id);
            }
            return response;
          }

          // STALE_GRAPH_VERSION
          if (errBody && errBody.code === 'STALE_GRAPH_VERSION') {
            await fetchStateHttp();
            showToast('Someone just changed this room — check the plan and submit again.', errBody.detail || '');
            return response;
          }
        }

        if (!response.ok) {
          let errBody = null;
          try {
            errBody = await response.clone().json();
          } catch (_) {}
          if (errBody && errBody.title) {
            showToast(errBody.title, errBody.detail || '');
          } else {
            showToast('Error', response.statusText || ('HTTP ' + response.status));
          }
        } else {
          // Re-fetch state immediately after successful write
          fetchStateHttp();
        }

        return response;
      }
    }

    // ==========================================
    // RENDER STATE
    // ==========================================
    function renderState(snapshot) {
      currentSnapshot = snapshot;
      currentVersion = snapshot.room.graph_version;
      saveRoomToHistory(slug, snapshot.room.name, snapshot.room.kind);

      const memberMap = new Map();
      snapshot.members.forEach(m => memberMap.set(m.id, m));

      // 1. Status Bar
      const titleEl = document.getElementById('room-title');
      if (titleEl) titleEl.textContent = snapshot.room.name;

      const kindBadge = document.getElementById('room-kind-badge');
      if (kindBadge) {
        if (snapshot.room.kind === 'DEMO') {
          kindBadge.textContent = 'Demo · deleted after 24 h idle';
          kindBadge.className = 'badge badge-warning';
        } else {
          kindBadge.textContent = 'Group · kept while in use';
          kindBadge.className = 'badge';
        }
      }

      const versionBadge = document.getElementById('room-version-badge');
      if (versionBadge) {
        versionBadge.textContent = 'v' + snapshot.room.graph_version;
      }

      const sigmaBadge = document.getElementById('sigma-badge');
      if (sigmaBadge) {
        const zeroSum = snapshot.invariants.zero_sum_paise;
        if (zeroSum === 0) {
          sigmaBadge.className = 'badge badge-success';
          sigmaBadge.textContent = 'Σ ₹0.00';
        } else {
          sigmaBadge.className = 'badge badge-danger';
          sigmaBadge.textContent = 'Σ ' + formatMoney(zeroSum);
        }
      }

      const driftBadge = document.getElementById('drift-badge');
      if (driftBadge) {
        const driftTotal = snapshot.invariants.ledger_drift_total_paise;
        if (driftTotal === 0) {
          driftBadge.className = 'badge badge-success';
          driftBadge.textContent = 'Drift: ₹0.00';
        } else {
          driftBadge.className = 'badge badge-danger';
          const drifted = snapshot.invariants.drifted_members || [];
          if (drifted.length > 0) {
            driftBadge.textContent = drifted.map(d => {
              const sign = d.drift_paise > 0 ? '+' : '−';
              const memberName = memberMap.get(d.member_id)?.name || ('Member #' + d.member_id);
              return memberName + ' ' + sign + formatMoney(Math.abs(d.drift_paise));
            }).join(' · ');
          } else {
            driftBadge.textContent = 'Drift: ' + formatMoney(driftTotal);
          }
        }
      }

      // Populate Acting Member picker
      if (actingPicker) {
        const storedVal = getStoredActingMember(slug);
        while (actingPicker.firstChild) {
          actingPicker.removeChild(actingPicker.firstChild);
        }
        const anonOpt = document.createElement('option');
        anonOpt.value = '';
        anonOpt.textContent = 'I am: Someone';
        actingPicker.appendChild(anonOpt);

        snapshot.members.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m.id.toString();
          opt.textContent = 'I am: ' + m.name;
          if (m.id.toString() === storedVal) {
            opt.selected = true;
          }
          actingPicker.appendChild(opt);
        });
      }

      // Reset Room button (demo only)
      const resetBtn = document.getElementById('reset-room-btn');
      if (resetBtn) {
        if (snapshot.room.kind === 'DEMO') {
          resetBtn.classList.remove('hidden');
          resetBtn.onclick = async () => {
            resetBtn.disabled = true;
            try {
              const res = await apiRequest('api/rooms/' + slug + '/reset', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  ...getActingMemberHeader(),
                },
              });
              if (res.ok) {
                const newSnap = await res.json();
                renderState(newSnap);
                showToast('Room reset', 'Reset back to demo seed balances.');
              }
            } finally {
              resetBtn.disabled = false;
            }
          };
        } else {
          resetBtn.classList.add('hidden');
        }
      }

      // 2. Graphs Section
      renderGraphsSection(snapshot, memberMap);

      // 3. Balances
      renderBalancesSection(snapshot);

      // 4. Expenses List
      renderExpensesList(snapshot, memberMap);

      // 5. Add / Edit Expense Form member select and split options
      populateExpenseFormDropdowns(snapshot);

      // 6. Settlements Section
      renderSettlementsSection(snapshot, memberMap);

      // 7. Activity Feed: reload from top on snapshot updates so items are newest-first and never duplicated
      loadHistoryFeed(true);

      // 8. Search Panel dropdowns
      populateSearchDropdowns(snapshot);

      // 9. Chaos Panel (demo only)
      renderChaosPanel(snapshot);
    }

    // Render Graphs Section
    function renderGraphsSection(snapshot, memberMap) {
      const summaryLine = document.getElementById('graph-summary-line');
      const fallbackNote = document.getElementById('greedy-fallback-note');
      const optBtn = document.getElementById('toggle-optimal-btn');
      const greedyBtn = document.getElementById('toggle-greedy-btn');
      const svgContainer = document.getElementById('graph-svg-container');
      const listContainer = document.getElementById('graph-list-container');
      const pairwiseWrap = document.getElementById('pairwise-svg-wrap');
      const planWrap = document.getElementById('plan-svg-wrap');
      const planDetailsSummary = document.getElementById('plan-details-summary');
      const planDetailsContent = document.getElementById('plan-details-content');

      const isOptimalAvailable = snapshot.graph.optimal !== null;
      if (!isOptimalAvailable) {
        activePlanType = 'GREEDY';
        if (fallbackNote) {
          fallbackNote.textContent = 'Greedy — more than 15 people have a balance, so the exact solver is skipped';
          fallbackNote.classList.remove('hidden');
        }
      } else if (fallbackNote) {
        fallbackNote.classList.add('hidden');
      }

      if (optBtn && greedyBtn) {
        if (!isOptimalAvailable) {
          optBtn.disabled = true;
          optBtn.className = 'btn btn-sm btn-secondary';
          greedyBtn.className = 'btn btn-sm btn-primary';
        } else {
          optBtn.disabled = false;
          optBtn.className = activePlanType === 'OPTIMAL' ? 'btn btn-sm btn-primary' : 'btn btn-sm btn-secondary';
          greedyBtn.className = activePlanType === 'GREEDY' ? 'btn btn-sm btn-primary' : 'btn btn-sm btn-secondary';

          optBtn.onclick = () => {
            activePlanType = 'OPTIMAL';
            renderGraphsSection(snapshot, memberMap);
          };
          greedyBtn.onclick = () => {
            activePlanType = 'GREEDY';
            renderGraphsSection(snapshot, memberMap);
          };
        }
      }

      const pairwiseCount = snapshot.graph.pairwise.count;
      const optimalCount = isOptimalAvailable ? snapshot.graph.optimal.count : null;
      const greedyCount = snapshot.graph.greedy ? snapshot.graph.greedy.count : 0;

      if (summaryLine) {
        if (isOptimalAvailable) {
          summaryLine.textContent = pairwiseCount + ' IOUs → ' + optimalCount + ' transfers (greedy ' + greedyCount + ')';
        } else {
          summaryLine.textContent = pairwiseCount + ' IOUs → ' + greedyCount + ' transfers';
        }
      }

      const activePlanEdges = (activePlanType === 'OPTIMAL' && isOptimalAvailable)
        ? snapshot.graph.optimal.edges
        : (snapshot.graph.greedy ? snapshot.graph.greedy.edges : []);

      // If <= 15 members: SVG mode. If > 15 members: List mode.
      if (snapshot.members.length <= 15) {
        if (svgContainer) svgContainer.classList.remove('hidden');
        if (listContainer) listContainer.classList.add('hidden');

        if (window.SplitSyncGraph && pairwiseWrap && planWrap) {
          window.SplitSyncGraph.renderSvgGraph(
            pairwiseWrap,
            snapshot.members,
            snapshot.graph.pairwise.edges,
            false,
            'IOUs as incurred (' + pairwiseCount + ')',
            'Pairwise debts between members before settlement simplification'
          );
          window.SplitSyncGraph.renderSvgGraph(
            planWrap,
            snapshot.members,
            activePlanEdges,
            true,
            (activePlanType === 'OPTIMAL' ? 'Optimal Plan (' : 'Greedy Plan (') + activePlanEdges.length + ')',
            'Recommended transfers to settle all debts'
          );
        }
      } else {
        if (svgContainer) svgContainer.classList.add('hidden');
        if (listContainer) listContainer.classList.remove('hidden');

        const iousList = document.getElementById('large-ious-list');
        const planList = document.getElementById('large-plan-list');
        if (iousList) {
          while (iousList.firstChild) iousList.removeChild(iousList.firstChild);
          const top20 = (snapshot.graph.pairwise.edges || []).slice(0, 20);
          top20.forEach(edge => {
            const li = document.createElement('li');
            li.className = 'plan-step-item';
            const fromName = memberMap.get(edge.from)?.name || '#' + edge.from;
            const toName = memberMap.get(edge.to)?.name || '#' + edge.to;
            li.textContent = fromName + ' owes ' + toName + ' ' + formatMoney(edge.amount_paise);
            iousList.appendChild(li);
          });
        }
        if (planList) {
          while (planList.firstChild) planList.removeChild(planList.firstChild);
          activePlanEdges.forEach(edge => {
            const li = document.createElement('li');
            li.className = 'plan-step-item';
            const fromName = memberMap.get(edge.from)?.name || '#' + edge.from;
            const toName = memberMap.get(edge.to)?.name || '#' + edge.to;
            li.textContent = fromName + ' pays ' + toName + ' ' + formatMoney(edge.amount_paise);
            planList.appendChild(li);
          });
        }
      }

      // Details Drawer: Optimal Groups or Greedy Steps
      if (planDetailsSummary && planDetailsContent) {
        while (planDetailsContent.firstChild) {
          planDetailsContent.removeChild(planDetailsContent.firstChild);
        }

        if (activePlanType === 'OPTIMAL' && isOptimalAvailable) {
          planDetailsSummary.textContent = 'View optimal groups (' + (snapshot.graph.optimal.groups || []).length + ')';
          (snapshot.graph.optimal.groups || []).forEach((grp, idx) => {
            const item = document.createElement('div');
            item.className = 'plan-group-item';
            const names = grp.map(id => memberMap.get(id)?.name || '#' + id).join(', ');
            item.textContent = 'Group ' + (idx + 1) + ': [' + names + ']';
            planDetailsContent.appendChild(item);
          });
        } else {
          planDetailsSummary.textContent = 'View greedy steps (' + (snapshot.graph.greedy?.steps || []).length + ')';
          (snapshot.graph.greedy?.steps || []).forEach(step => {
            const item = document.createElement('div');
            item.className = 'plan-step-item';
            const debtor = memberMap.get(step.debtor)?.name || '#' + step.debtor;
            const creditor = memberMap.get(step.creditor)?.name || '#' + step.creditor;
            const debtStr = formatMoney(step.debt_paise);
            const credStr = formatMoney(step.credit_paise);
            const amtStr = formatMoney(step.amount_paise);
            // SPEC §10 exact format:
            // "Pop debtor {name} (owes ₹X) and creditor {name} (owed ₹Y) → {name} pays {name} ₹Z"
            item.textContent = 'Pop debtor ' + debtor + ' (owes ' + debtStr + ') and creditor ' +
              creditor + ' (owed ' + credStr + ') → ' + debtor + ' pays ' + creditor + ' ' + amtStr;
            planDetailsContent.appendChild(item);
          });
        }
      }
    }

    // Render Balances Section
    function renderBalancesSection(snapshot) {
      const balancesContainer = document.getElementById('balances-list');
      if (!balancesContainer) return;
      while (balancesContainer.firstChild) {
        balancesContainer.removeChild(balancesContainer.firstChild);
      }

      snapshot.members.forEach(m => {
        const card = document.createElement('div');
        card.className = 'balance-card';

        const nameRow = document.createElement('div');
        nameRow.className = 'balance-member-name';

        const dot = document.createElement('span');
        dot.className = 'balance-dot';
        dot.style.setProperty('--member-color', m.color || '#2563eb');
        // DECISION: Use CSSOM setProperty for dynamic member color variable, complying with CSP.
        dot.style.backgroundColor = m.color || '#2563eb';
        nameRow.appendChild(dot);

        const nameSpan = document.createElement('span');
        nameSpan.textContent = m.name;
        nameRow.appendChild(nameSpan);
        card.appendChild(nameRow);

        const amountRow = document.createElement('div');
        amountRow.className = 'balance-amount';

        const net = m.net_balance_paise || 0;
        if (net > 0) {
          amountRow.classList.add('balance-positive');
          amountRow.textContent = '+' + formatMoney(net);

          const signText = document.createElement('span');
          signText.className = 'balance-sign-text';
          signText.textContent = '(owed)';
          amountRow.appendChild(signText);
        } else if (net < 0) {
          amountRow.classList.add('balance-negative');
          amountRow.textContent = '−' + formatMoney(Math.abs(net));

          const signText = document.createElement('span');
          signText.className = 'balance-sign-text';
          signText.textContent = '(owes)';
          amountRow.appendChild(signText);
        } else {
          amountRow.classList.add('balance-zero');
          amountRow.textContent = '₹0.00';

          const signText = document.createElement('span');
          signText.className = 'balance-sign-text';
          signText.textContent = '(settled)';
          amountRow.appendChild(signText);
        }
        card.appendChild(amountRow);

        balancesContainer.appendChild(card);
      });
    }

    // Render Expenses List
    function renderExpensesList(snapshot, memberMap) {
      const expensesList = document.getElementById('expenses-list');
      if (!expensesList) return;
      while (expensesList.firstChild) {
        expensesList.removeChild(expensesList.firstChild);
      }

      const openSearchBtn = document.getElementById('open-search-btn');
      if (openSearchBtn) {
        openSearchBtn.onclick = () => {
          const panel = document.getElementById('search-panel');
          if (panel) panel.classList.remove('hidden');
        };
      }

      snapshot.expenses.forEach(exp => {
        const li = document.createElement('li');
        li.className = 'item-row';

        const mainDiv = document.createElement('div');
        mainDiv.className = 'item-main';

        const titleRow = document.createElement('div');
        titleRow.className = 'item-title-row';

        const title = document.createElement('span');
        title.className = 'item-title';
        title.textContent = exp.title;
        titleRow.appendChild(title);

        if (exp.revised) {
          const badge = document.createElement('span');
          badge.className = 'badge badge-warning';
          badge.textContent = 'edited';
          titleRow.appendChild(badge);
        }
        mainDiv.appendChild(titleRow);

        const metaRow = document.createElement('div');
        metaRow.className = 'item-meta';

        const payerName = memberMap.get(exp.paid_by)?.name || ('Member #' + exp.paid_by);
        const payerSpan = document.createElement('span');
        payerSpan.textContent = 'Paid by ' + payerName;
        metaRow.appendChild(payerSpan);

        if (exp.spent_on) {
          const dateSpan = document.createElement('span');
          dateSpan.textContent = '· ' + formatDate(exp.spent_on);
          metaRow.appendChild(dateSpan);
        }

        mainDiv.appendChild(metaRow);

        // Secondary line showing each member's share per Master Guide demo step 9
        if (exp.shares && exp.shares.length > 0) {
          const sharesRow = document.createElement('div');
          sharesRow.className = 'item-meta item-shares';
          const shareParts = exp.shares.map(s => {
            const mName = memberMap.get(s.member_id)?.name || ('#' + s.member_id);
            return mName + ' ' + formatMoney(s.share_paise);
          });
          sharesRow.textContent = shareParts.join(' · ');
          mainDiv.appendChild(sharesRow);
        }

        li.appendChild(mainDiv);

        const amountDiv = document.createElement('div');
        amountDiv.className = 'item-amount';
        amountDiv.textContent = formatMoney(exp.total_paise);
        li.appendChild(amountDiv);

        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'item-actions';

        // Edit button
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'btn btn-sm btn-secondary';
        editBtn.textContent = 'Edit';
        editBtn.onclick = () => populateExpenseEditor(exp.id);
        actionsDiv.appendChild(editBtn);

        // Delete button
        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'btn btn-sm btn-outline-danger';
        deleteBtn.textContent = 'Delete';
        deleteBtn.onclick = async () => {
          deleteBtn.disabled = true;
          const key = generateIdempotencyKey();
          try {
            const res = await apiRequest('api/rooms/' + slug + '/expenses/' + exp.id, {
              method: 'DELETE',
              headers: {
                'Idempotency-Key': key,
                ...getActingMemberHeader(),
              },
            });
            if (res.ok) {
              lastDeletedVersionId = exp.id;
              // 10 s toast "Deleted '<title>' — Undo" (restore with a new key)
              showToast(
                "Deleted '" + exp.title + "'",
                '',
                'Undo',
                async () => {
                  const restoreKey = generateIdempotencyKey();
                  const restoreRes = await apiRequest('api/rooms/' + slug + '/expenses/' + exp.id + '/restore', {
                    method: 'POST',
                    headers: {
                      'Idempotency-Key': restoreKey,
                      ...getActingMemberHeader(),
                    },
                  });
                  if (restoreRes.ok) {
                    showToast("Restored '" + exp.title + "'");
                  }
                },
                10000
              );
            }
          } finally {
            deleteBtn.disabled = false;
          }
        };
        actionsDiv.appendChild(deleteBtn);

        // History button
        const histBtn = document.createElement('button');
        histBtn.type = 'button';
        histBtn.className = 'btn btn-sm btn-secondary';
        histBtn.textContent = 'History';
        histBtn.onclick = () => {
          currentLineageFilter = exp.lineage_id;
          const drawer = document.getElementById('history-drawer');
          if (drawer) drawer.scrollIntoView({ behavior: 'smooth' });
          loadHistoryFeed(true);
        };
        actionsDiv.appendChild(histBtn);

        li.appendChild(actionsDiv);
        expensesList.appendChild(li);
      });
    }

    // Populate Expense Form Dropdowns and Split List
    function populateExpenseFormDropdowns(snapshot) {
      const payerSelect = document.getElementById('expense-payer-select');
      const splitContainer = document.getElementById('split-members-list');
      const dateInput = document.getElementById('expense-date-input');

      if (dateInput && !dateInput.value) {
        dateInput.value = getTodayIsoDate();
      }

      if (payerSelect) {
        const curVal = payerSelect.value;
        while (payerSelect.firstChild) payerSelect.removeChild(payerSelect.firstChild);
        snapshot.members.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m.id.toString();
          opt.textContent = m.name;
          if (curVal && m.id.toString() === curVal) opt.selected = true;
          payerSelect.appendChild(opt);
        });
      }

      renderSplitMembersInput(snapshot);

      // Demo shortcut button
      const demoBtn = document.getElementById('demo-shortcut-btn');
      if (demoBtn) {
        if (snapshot.room.kind === 'DEMO') {
          demoBtn.classList.remove('hidden');
          demoBtn.onclick = async () => {
            demoBtn.disabled = true;
            try {
              const rohan = snapshot.members.find(m => m.name.toLowerCase() === 'rohan');
              const sneha = snapshot.members.find(m => m.name.toLowerCase() === 'sneha');
              const karthik = snapshot.members.find(m => m.name.toLowerCase() === 'karthik');

              if (!rohan || !sneha || !karthik) {
                showToast('Shortcut unavailable', 'Demo members Rohan, Sneha, or Karthik not found.');
                return;
              }

              const key = generateIdempotencyKey();
              const res = await apiRequest('api/rooms/' + slug + '/expenses', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  'Idempotency-Key': key,
                  ...getActingMemberHeader(),
                },
                body: JSON.stringify({
                  title: 'Demo split ₹100',
                  paid_by: rohan.id,
                  amount: '100',
                  spent_on: getTodayIsoDate(),
                  split: {
                    type: 'EQUAL',
                    members: [{ id: rohan.id }, { id: sneha.id }, { id: karthik.id }],
                  },
                }),
              });
              if (res.ok) {
                showToast('Added demo expense', '₹100 split 3 ways (Rohan, Sneha, Karthik)');
                resetExpenseForm();
              }
            } finally {
              demoBtn.disabled = false;
            }
          };
        } else {
          demoBtn.classList.add('hidden');
        }
      }
    }

    // Render Split Members inputs according to Split Type
    function renderSplitMembersInput(snapshot, existingSplits = null, splitTypeOverride = null) {
      const splitContainer = document.getElementById('split-members-list');
      if (!splitContainer) return;

      const splitTypeRadios = document.getElementsByName('split-type');
      let currentType = splitTypeOverride || 'EQUAL';
      for (let i = 0; i < splitTypeRadios.length; i++) {
        if (splitTypeRadios[i].checked) currentType = splitTypeRadios[i].value;
      }

      while (splitContainer.firstChild) {
        splitContainer.removeChild(splitContainer.firstChild);
      }

      snapshot.members.forEach(m => {
        const row = document.createElement('div');
        row.className = 'split-member-row';

        const leftDiv = document.createElement('div');
        leftDiv.className = 'split-member-left';

        const chk = document.createElement('input');
        chk.type = 'checkbox';
        chk.id = 'split-chk-' + m.id;
        chk.dataset.memberId = m.id.toString();

        // Default all checked
        let isChecked = true;
        let weightVal = '1';
        let amountVal = '';

        if (existingSplits) {
          const matchSplit = existingSplits.find(s => s.member_id === m.id || s.id === m.id);
          isChecked = !!matchSplit;
          if (matchSplit) {
            if (matchSplit.weight !== undefined) weightVal = matchSplit.weight.toString();
            if (matchSplit.amount !== undefined) amountVal = matchSplit.amount.toString();
            if (matchSplit.share_paise !== undefined) amountVal = (matchSplit.share_paise / 100).toFixed(2);
          }
        }
        chk.checked = isChecked;
        leftDiv.appendChild(chk);

        const lbl = document.createElement('label');
        lbl.htmlFor = 'split-chk-' + m.id;
        lbl.textContent = m.name;
        leftDiv.appendChild(lbl);
        row.appendChild(leftDiv);

        if (currentType === 'SHARES') {
          const weightInput = document.createElement('input');
          weightInput.type = 'number';
          weightInput.min = '1';
          weightInput.max = '1000';
          weightInput.className = 'split-input-field';
          weightInput.id = 'split-weight-' + m.id;
          weightInput.value = weightVal || '1';
          weightInput.placeholder = 'Weight';
          row.appendChild(weightInput);
        } else if (currentType === 'EXACT') {
          const amtInput = document.createElement('input');
          amtInput.type = 'text';
          amtInput.className = 'split-input-field';
          amtInput.id = 'split-exact-' + m.id;
          amtInput.value = amountVal || '';
          amtInput.placeholder = '₹ 0.00';
          row.appendChild(amtInput);
        }

        splitContainer.appendChild(row);
      });
    }

    // Split type radio listener
    const splitTypeRadios = document.getElementsByName('split-type');
    for (let i = 0; i < splitTypeRadios.length; i++) {
      splitTypeRadios[i].addEventListener('change', () => {
        if (currentSnapshot) renderSplitMembersInput(currentSnapshot);
      });
    }

    function resetExpenseForm() {
      const form = document.getElementById('expense-form');
      if (form) form.reset();
      const formTitle = document.getElementById('expense-form-title');
      if (formTitle) formTitle.textContent = 'Add Expense';
      const editingId = document.getElementById('editing-expense-id');
      if (editingId) editingId.value = '';
      const cancelBtn = document.getElementById('expense-cancel-btn');
      if (cancelBtn) cancelBtn.classList.add('hidden');
      const submitBtn = document.getElementById('expense-submit-btn');
      if (submitBtn) submitBtn.textContent = 'Add expense';
      const dateInput = document.getElementById('expense-date-input');
      if (dateInput) dateInput.value = getTodayIsoDate();
      if (currentSnapshot) renderSplitMembersInput(currentSnapshot);
    }

    const cancelExpenseBtn = document.getElementById('expense-cancel-btn');
    if (cancelExpenseBtn) {
      cancelExpenseBtn.addEventListener('click', resetExpenseForm);
    }

    // Populate Expense Editor on Edit click
    function populateExpenseEditor(expenseId) {
      if (!currentSnapshot) return;
      const exp = currentSnapshot.expenses.find(e => e.id === expenseId);
      if (!exp) return;

      const formTitle = document.getElementById('expense-form-title');
      if (formTitle) formTitle.textContent = 'Edit Expense';
      const editingId = document.getElementById('editing-expense-id');
      if (editingId) editingId.value = exp.id.toString();
      const titleInput = document.getElementById('expense-title-input');
      if (titleInput) titleInput.value = exp.title;
      const amountInput = document.getElementById('expense-amount-input');
      if (amountInput) amountInput.value = (exp.total_paise / 100).toFixed(2);
      const payerSelect = document.getElementById('expense-payer-select');
      if (payerSelect) payerSelect.value = exp.paid_by.toString();
      const dateInput = document.getElementById('expense-date-input');
      if (dateInput) dateInput.value = exp.spent_on || getTodayIsoDate();

      const splitType = exp.split_type || 'EQUAL';
      for (let i = 0; i < splitTypeRadios.length; i++) {
        splitTypeRadios[i].checked = (splitTypeRadios[i].value === splitType);
      }

      renderSplitMembersInput(currentSnapshot, exp.shares, splitType);

      const cancelBtn = document.getElementById('expense-cancel-btn');
      if (cancelBtn) cancelBtn.classList.remove('hidden');
      const submitBtn = document.getElementById('expense-submit-btn');
      if (submitBtn) submitBtn.textContent = 'Save changes';

      const form = document.getElementById('expense-form');
      if (form) form.scrollIntoView({ behavior: 'smooth' });
    }

    // Expense Form Submission (Add or Edit)
    const expenseForm = document.getElementById('expense-form');
    if (expenseForm) {
      expenseForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const editingId = document.getElementById('editing-expense-id').value;
        const title = document.getElementById('expense-title-input').value.trim();
        const rawAmount = document.getElementById('expense-amount-input').value;
        const cleanAmt = cleanRupeeInput(rawAmount);
        const payerId = parseInt(document.getElementById('expense-payer-select').value, 10);
        const spentOn = document.getElementById('expense-date-input').value || getTodayIsoDate();
        const submitBtn = document.getElementById('expense-submit-btn');

        let selectedType = 'EQUAL';
        for (let i = 0; i < splitTypeRadios.length; i++) {
          if (splitTypeRadios[i].checked) selectedType = splitTypeRadios[i].value;
        }

        const splitMembers = [];
        const chks = document.querySelectorAll('#split-members-list input[type="checkbox"]');
        chks.forEach(chk => {
          if (chk.checked) {
            const mId = parseInt(chk.dataset.memberId, 10);
            if (selectedType === 'EQUAL') {
              splitMembers.push({ id: mId });
            } else if (selectedType === 'SHARES') {
              const weightInput = document.getElementById('split-weight-' + mId);
              const weight = weightInput ? parseInt(weightInput.value, 10) || 1 : 1;
              splitMembers.push({ id: mId, weight });
            } else if (selectedType === 'EXACT') {
              const exactInput = document.getElementById('split-exact-' + mId);
              const exactAmt = cleanRupeeInput(exactInput ? exactInput.value : '0');
              splitMembers.push({ id: mId, amount: exactAmt });
            }
          }
        });

        if (splitMembers.length === 0) {
          showToast('Validation error', 'Please select at least one member to split the expense.');
          return;
        }

        const body = {
          title,
          paid_by: payerId,
          amount: cleanAmt,
          spent_on: spentOn,
          split: {
            type: selectedType,
            members: splitMembers,
          },
        };

        submitBtn.disabled = true;
        const key = generateIdempotencyKey();

        try {
          if (editingId) {
            // Edit: PUT /api/rooms/{slug}/expenses/{id}
            const res = await apiRequest('api/rooms/' + slug + '/expenses/' + editingId, {
              method: 'PUT',
              headers: {
                'Content-Type': 'application/json',
                'Idempotency-Key': key,
                ...getActingMemberHeader(),
              },
              body: JSON.stringify(body),
            });
            if (res.ok) {
              const data = await res.json().catch(() => ({}));
              if (data.unchanged) {
                showToast('No changes', 'Expense content was unchanged.');
              } else {
                showToast('Expense updated', title);
              }
              resetExpenseForm();
            }
          } else {
            // Add: POST /api/rooms/{slug}/expenses
            const res = await apiRequest('api/rooms/' + slug + '/expenses', {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'Idempotency-Key': key,
                ...getActingMemberHeader(),
              },
              body: JSON.stringify(body),
            });
            if (res.ok) {
              showToast('Expense added', title);
              resetExpenseForm();
            }
          }
        } finally {
          submitBtn.disabled = false;
        }
      });
    }

    // 6. Settlements Section
    function renderSettlementsSection(snapshot, memberMap) {
      const fromSelect = document.getElementById('settlement-from-select');
      const toSelect = document.getElementById('settlement-to-select');
      const amtInput = document.getElementById('settlement-amount-input');
      const settlementsList = document.getElementById('settlements-list');

      if (fromSelect && toSelect) {
        const curFrom = fromSelect.value;
        const curTo = toSelect.value;
        while (fromSelect.firstChild) fromSelect.removeChild(fromSelect.firstChild);
        while (toSelect.firstChild) toSelect.removeChild(toSelect.firstChild);

        snapshot.members.forEach(m => {
          const opt1 = document.createElement('option');
          opt1.value = m.id.toString();
          opt1.textContent = m.name;
          if (curFrom && m.id.toString() === curFrom) opt1.selected = true;
          fromSelect.appendChild(opt1);

          const opt2 = document.createElement('option');
          opt2.value = m.id.toString();
          opt2.textContent = m.name;
          if (curTo && m.id.toString() === curTo) opt2.selected = true;
          toSelect.appendChild(opt2);
        });

        // Pre-fill from plan if input is empty and plan transfers exist
        const planEdges = (snapshot.graph.optimal?.edges && snapshot.graph.optimal.edges.length > 0)
          ? snapshot.graph.optimal.edges
          : (snapshot.graph.greedy?.edges || []);

        if (amtInput && (!amtInput.value || amtInput.value === '0.00') && planEdges.length > 0) {
          const firstEdge = planEdges[0];
          fromSelect.value = firstEdge.from.toString();
          toSelect.value = firstEdge.to.toString();
          amtInput.value = (firstEdge.amount_paise / 100).toFixed(2);
        }
      }

      // Render settlements list
      if (settlementsList) {
        while (settlementsList.firstChild) {
          settlementsList.removeChild(settlementsList.firstChild);
        }

        snapshot.settlements.forEach(settlement => {
          const li = document.createElement('li');
          li.className = 'item-row';

          const mainDiv = document.createElement('div');
          mainDiv.className = 'item-main';

          const fromName = memberMap.get(settlement.from_member)?.name || '#' + settlement.from_member;
          const toName = memberMap.get(settlement.to_member)?.name || '#' + settlement.to_member;

          const titleRow = document.createElement('div');
          titleRow.className = 'item-title-row';

          const title = document.createElement('span');
          title.className = 'item-title';
          title.textContent = fromName + ' paid ' + toName;
          titleRow.appendChild(title);

          if (settlement.mode === 'NAIVE') {
            const badge = document.createElement('span');
            badge.className = 'badge badge-warning';
            badge.textContent = 'Naive';
            titleRow.appendChild(badge);
          }
          mainDiv.appendChild(titleRow);

          const metaRow = document.createElement('div');
          metaRow.className = 'item-meta';
          if (settlement.created_at) {
            const dateSpan = document.createElement('span');
            dateSpan.textContent = formatDate(settlement.created_at);
            metaRow.appendChild(dateSpan);
          }
          mainDiv.appendChild(metaRow);
          li.appendChild(mainDiv);

          const amountDiv = document.createElement('div');
          amountDiv.className = 'item-amount';
          amountDiv.textContent = formatMoney(settlement.amount_paise);
          li.appendChild(amountDiv);

          // Delete with two-step inline confirm: "Delete" -> "Confirm delete"
          const actionsDiv = document.createElement('div');
          actionsDiv.className = 'item-actions';

          const deleteBtn = document.createElement('button');
          deleteBtn.type = 'button';
          deleteBtn.className = 'btn btn-sm btn-outline-danger';
          deleteBtn.textContent = 'Delete';

          let confirmTimer = null;
          deleteBtn.onclick = async () => {
            if (deleteBtn.textContent === 'Delete') {
              deleteBtn.textContent = 'Confirm delete';
              deleteBtn.className = 'btn btn-sm btn-danger';
              confirmTimer = setTimeout(() => {
                deleteBtn.textContent = 'Delete';
                deleteBtn.className = 'btn btn-sm btn-outline-danger';
              }, 4000);
            } else {
              clearTimeout(confirmTimer);
              deleteBtn.disabled = true;
              const key = generateIdempotencyKey();
              try {
                const res = await apiRequest('api/rooms/' + slug + '/settlements/' + settlement.id, {
                  method: 'DELETE',
                  headers: {
                    'Idempotency-Key': key,
                    ...getActingMemberHeader(),
                  },
                });
                if (res.ok) {
                  showToast('Settlement deleted', fromName + ' → ' + toName);
                }
              } finally {
                deleteBtn.disabled = false;
              }
            }
          };

          actionsDiv.appendChild(deleteBtn);
          li.appendChild(actionsDiv);
          settlementsList.appendChild(li);
        });
      }
    }

    // Settlement Form Submit
    const settlementForm = document.getElementById('settlement-form');
    if (settlementForm) {
      settlementForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fromVal = parseInt(document.getElementById('settlement-from-select').value, 10);
        const toVal = parseInt(document.getElementById('settlement-to-select').value, 10);
        const rawAmt = document.getElementById('settlement-amount-input').value;
        const cleanAmt = cleanRupeeInput(rawAmt);
        const submitBtn = document.getElementById('record-settlement-btn');

        if (fromVal === toVal) {
          showToast('Invalid settlement', 'From and To member cannot be the same.');
          return;
        }

        submitBtn.disabled = true;
        const key = generateIdempotencyKey();

        try {
          const res = await apiRequest('api/rooms/' + slug + '/settlements', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Idempotency-Key': key,
              ...getActingMemberHeader(),
            },
            body: JSON.stringify({
              from_member: fromVal,
              to_member: toVal,
              amount: cleanAmt,
              expected_graph_version: currentSnapshot ? currentSnapshot.room.graph_version : 0,
              mode: 'PROTECTED',
            }),
          });
          if (res.ok) {
            showToast('Settlement recorded', '₹' + cleanAmt);
          }
        } finally {
          submitBtn.disabled = false;
        }
      });
    }

    // 7. Activity Feed / History Drawer
    const clearLineageBtn = document.getElementById('clear-lineage-filter-btn');
    if (clearLineageBtn) {
      clearLineageBtn.addEventListener('click', () => {
        currentLineageFilter = null;
        clearLineageBtn.classList.add('hidden');
        loadHistoryFeed(true);
      });
    }

    const historyLoadMoreBtn = document.getElementById('history-load-more-btn');
    if (historyLoadMoreBtn) {
      historyLoadMoreBtn.addEventListener('click', () => loadHistoryFeed(false));
    }

    async function loadHistoryFeed(reset) {
      const feedList = document.getElementById('history-feed-list');
      if (!feedList) return;

      const currentSeq = ++historyLoadSeq;
      if (reset && historyAbortController) {
        historyAbortController.abort();
      }
      historyAbortController = new AbortController();

      if (reset) {
        historyNextCursor = null;
      }

      if (clearLineageBtn) {
        if (currentLineageFilter) {
          clearLineageBtn.classList.remove('hidden');
        } else {
          clearLineageBtn.classList.add('hidden');
        }
      }

      const params = new URLSearchParams();
      params.set('limit', '50');
      if (!reset && historyNextCursor) params.set('cursor', historyNextCursor);
      if (currentLineageFilter) params.set('lineage', currentLineageFilter.toString());

      try {
        const res = await fetch(BASE + 'api/rooms/' + slug + '/activity?' + params.toString(), {
          signal: historyAbortController.signal,
        });
        if (res.ok) {
          const data = await res.json();
          // DECISION: Discard response if a newer load was initiated, preventing overlapping writes.
          if (currentSeq !== historyLoadSeq) {
            return;
          }

          historyNextCursor = data.next_cursor;
          if (historyLoadMoreBtn) {
            if (historyNextCursor) {
              historyLoadMoreBtn.classList.remove('hidden');
            } else {
              historyLoadMoreBtn.classList.add('hidden');
            }
          }

          if (reset) {
            while (feedList.firstChild) feedList.removeChild(feedList.firstChild);
          }

          const memberMap = new Map();
          if (currentSnapshot) currentSnapshot.members.forEach(m => memberMap.set(m.id, m));

          data.items.forEach(item => {
            const li = document.createElement('li');
            li.className = 'item-row';

            const mainDiv = document.createElement('div');
            mainDiv.className = 'item-main';

            const textLine = document.createElement('div');
            textLine.className = 'item-title';

            let showUndo = false;
            let undoExpenseId = null;

            if (item.type === 'MEMBER_ADDED') {
              // DECISION: For MEMBER_ADDED show only "{name} joined" without actor.
              const contentSpan = document.createElement('span');
              contentSpan.textContent = item.name + ' joined';
              textLine.appendChild(contentSpan);
            } else {
              // Actor label: self-reported tooltip or "Someone"
              const actorSpan = document.createElement('span');
              if (item.acted_by) {
                const m = memberMap.get(item.acted_by);
                actorSpan.textContent = m ? m.name : ('Member #' + item.acted_by);
                actorSpan.title = 'self-reported';
              } else {
                actorSpan.textContent = 'Someone';
              }
              actorSpan.className = 'text-muted';
              textLine.appendChild(actorSpan);

              const contentSpan = document.createElement('span');
              contentSpan.textContent = ' ';

              if (item.type === 'EXPENSE_ADDED') {
                contentSpan.textContent += 'added ' + item.title + ': ' + formatMoney(item.total_paise);
              } else if (item.type === 'EXPENSE_EDITED') {
                contentSpan.textContent += 'edited ' + item.title + ': ' +
                  formatMoney(item.old_total_paise) + ' → ' + formatMoney(item.total_paise);
              } else if (item.type === 'EXPENSE_RESTORED') {
                contentSpan.textContent += 'restored ' + item.title + ': ' + formatMoney(item.total_paise);
              } else if (item.type === 'EXPENSE_DELETED') {
                contentSpan.textContent += 'deleted ' + item.title;
                if (lastDeletedVersionId && lastDeletedVersionId === item.expense_id) {
                  showUndo = true;
                  undoExpenseId = item.expense_id;
                }
              } else if (item.type === 'SETTLEMENT_RECORDED') {
                const from = memberMap.get(item.from)?.name || '#' + item.from;
                const to = memberMap.get(item.to)?.name || '#' + item.to;
                contentSpan.textContent += 'recorded settlement: ' + from + ' paid ' + to + ' ' + formatMoney(item.amount_paise);
              } else if (item.type === 'SETTLEMENT_DELETED') {
                const from = memberMap.get(item.from)?.name || '#' + item.from;
                const to = memberMap.get(item.to)?.name || '#' + item.to;
                contentSpan.textContent += 'deleted settlement: ' + from + ' → ' + to + ' ' + formatMoney(item.amount_paise);
              }
              textLine.appendChild(contentSpan);
            }

            const timeSpan = document.createElement('span');
            timeSpan.className = 'item-meta';
            timeSpan.textContent = ' · ' + (formatTime(item.at) || formatDate(item.at));
            textLine.appendChild(timeSpan);

            mainDiv.appendChild(textLine);
            li.appendChild(mainDiv);

            if (showUndo) {
              const undoBtn = document.createElement('button');
              undoBtn.type = 'button';
              undoBtn.className = 'btn btn-sm btn-primary';
              undoBtn.textContent = 'Undo';
              undoBtn.onclick = async () => {
                undoBtn.disabled = true;
                const key = generateIdempotencyKey();
                const rRes = await apiRequest('api/rooms/' + slug + '/expenses/' + undoExpenseId + '/restore', {
                  method: 'POST',
                  headers: {
                    'Idempotency-Key': key,
                    ...getActingMemberHeader(),
                  },
                });
                if (rRes.ok) {
                  showToast('Expense restored');
                  loadHistoryFeed(true);
                }
              };
              li.appendChild(undoBtn);
            }

            feedList.appendChild(li);
          });
        }
      } catch (err) {
        if (err.name !== 'AbortError') {
          // ignore network abort
        }
      }
    }

    // 8. Search Panel
    const searchPanel = document.getElementById('search-panel');
    const closeSearchBtn = document.getElementById('close-search-btn');
    if (closeSearchBtn && searchPanel) {
      closeSearchBtn.onclick = () => searchPanel.classList.add('hidden');
    }

    const searchInput = document.getElementById('search-query-input');
    const paidBySelect = document.getElementById('search-paid-by-select');
    const involvingSelect = document.getElementById('search-involving-select');
    const minInput = document.getElementById('search-min-amount-input');
    const maxInput = document.getElementById('search-max-amount-input');
    const fromInput = document.getElementById('search-date-from-input');
    const toInput = document.getElementById('search-date-to-input');
    const includeDeletedChk = document.getElementById('search-include-deleted');
    const searchLoadMoreBtn = document.getElementById('search-load-more-btn');

    function populateSearchDropdowns(snapshot) {
      if (paidBySelect) {
        const cur = paidBySelect.value;
        while (paidBySelect.options.length > 1) paidBySelect.remove(1);
        snapshot.members.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m.id.toString();
          opt.textContent = m.name;
          if (cur && m.id.toString() === cur) opt.selected = true;
          paidBySelect.appendChild(opt);
        });
      }
      if (involvingSelect) {
        const cur = involvingSelect.value;
        while (involvingSelect.options.length > 1) involvingSelect.remove(1);
        snapshot.members.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m.id.toString();
          opt.textContent = m.name;
          if (cur && m.id.toString() === cur) opt.selected = true;
          involvingSelect.appendChild(opt);
        });
      }
    }

    async function executeSearch(reset = true) {
      if (reset) {
        searchNextCursor = null;
        const resultsList = document.getElementById('search-results-list');
        if (resultsList) {
          while (resultsList.firstChild) resultsList.removeChild(resultsList.firstChild);
        }
      }

      if (searchAbortController) {
        searchAbortController.abort();
      }
      searchAbortController = new AbortController();

      const params = new URLSearchParams();
      if (searchInput && searchInput.value.trim()) params.set('q', searchInput.value.trim());
      if (paidBySelect && paidBySelect.value) params.set('paid_by', paidBySelect.value);
      if (involvingSelect && involvingSelect.value) params.set('member', involvingSelect.value);
      if (minInput && minInput.value.trim()) params.set('min', cleanRupeeInput(minInput.value));
      if (maxInput && maxInput.value.trim()) params.set('max', cleanRupeeInput(maxInput.value));
      if (fromInput && fromInput.value) params.set('from', fromInput.value);
      if (toInput && toInput.value) params.set('to', toInput.value);
      if (includeDeletedChk && includeDeletedChk.checked) params.set('include_deleted', 'true');
      if (searchNextCursor) params.set('cursor', searchNextCursor);
      params.set('limit', '50');

      try {
        const res = await fetch(BASE + 'api/rooms/' + slug + '/expenses?' + params.toString(), {
          signal: searchAbortController.signal,
        });
        if (res.ok) {
          const data = await res.json();
          searchNextCursor = data.next_cursor;
          if (searchLoadMoreBtn) {
            if (searchNextCursor) searchLoadMoreBtn.classList.remove('hidden');
            else searchLoadMoreBtn.classList.add('hidden');
          }

          const resultsList = document.getElementById('search-results-list');
          if (!resultsList) return;

          const memberMap = new Map();
          if (currentSnapshot) currentSnapshot.members.forEach(m => memberMap.set(m.id, m));

          data.items.forEach(item => {
            const li = document.createElement('li');
            li.className = 'item-row';

            const mainDiv = document.createElement('div');
            mainDiv.className = 'item-main';

            const titleRow = document.createElement('div');
            titleRow.className = 'item-title-row';

            const title = document.createElement('span');
            title.className = 'item-title';
            title.textContent = item.title;
            titleRow.appendChild(title);

            if (item.revised) {
              const badge = document.createElement('span');
              badge.className = 'badge badge-warning';
              badge.textContent = 'edited';
              titleRow.appendChild(badge);
            }
            if (item.deleted) {
              const badge = document.createElement('span');
              badge.className = 'badge badge-danger';
              badge.textContent = 'deleted';
              titleRow.appendChild(badge);
            }
            mainDiv.appendChild(titleRow);

            const metaRow = document.createElement('div');
            metaRow.className = 'item-meta';
            const payerName = memberMap.get(item.paid_by)?.name || ('Member #' + item.paid_by);
            const payerSpan = document.createElement('span');
            payerSpan.textContent = 'Paid by ' + payerName;
            metaRow.appendChild(payerSpan);

            if (item.spent_on) {
              const dateSpan = document.createElement('span');
              dateSpan.textContent = '· ' + formatDate(item.spent_on);
              metaRow.appendChild(dateSpan);
            }
            mainDiv.appendChild(metaRow);
            li.appendChild(mainDiv);

            const amountDiv = document.createElement('div');
            amountDiv.className = 'item-amount';
            amountDiv.textContent = formatMoney(item.total_paise);
            li.appendChild(amountDiv);

            if (item.deleted) {
              const restoreBtn = document.createElement('button');
              restoreBtn.type = 'button';
              restoreBtn.className = 'btn btn-sm btn-primary';
              restoreBtn.textContent = 'Restore';
              restoreBtn.onclick = async () => {
                restoreBtn.disabled = true;
                const key = generateIdempotencyKey();
                const rRes = await apiRequest('api/rooms/' + slug + '/expenses/' + item.id + '/restore', {
                  method: 'POST',
                  headers: {
                    'Idempotency-Key': key,
                    ...getActingMemberHeader(),
                  },
                });
                if (rRes.ok) {
                  showToast('Expense restored', item.title);
                  executeSearch(true);
                }
              };
              li.appendChild(restoreBtn);
            }

            resultsList.appendChild(li);
          });
        }
      } catch (err) {
        if (err.name !== 'AbortError') {
          showToast('Search error', err.message);
        }
      }
    }

    if (searchInput) {
      searchInput.addEventListener('input', () => {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => executeSearch(true), 250);
      });
    }

    [paidBySelect, involvingSelect, minInput, maxInput, fromInput, toInput, includeDeletedChk].forEach(el => {
      if (el) el.addEventListener('change', () => executeSearch(true));
    });

    if (searchLoadMoreBtn) {
      searchLoadMoreBtn.addEventListener('click', () => executeSearch(false));
    }

    // 9. Chaos Panel (Demo Rooms Only)
    function renderChaosPanel(snapshot) {
      const chaosPanel = document.getElementById('chaos-panel');
      if (!chaosPanel) return;

      if (snapshot.room.kind !== 'DEMO') {
        chaosPanel.classList.add('hidden');
        return;
      }
      chaosPanel.classList.remove('hidden');

      const targetInfo = document.getElementById('chaos-target-info');
      const target = snapshot.graph.chaos_target;
      const memberMap = new Map();
      snapshot.members.forEach(m => memberMap.set(m.id, m));

      if (targetInfo) {
        if (target) {
          const fromName = memberMap.get(target.from)?.name || '#' + target.from;
          const toName = memberMap.get(target.to)?.name || '#' + target.to;
          targetInfo.textContent = 'Target: ' + fromName + ' pays ' + toName + ' ' + formatMoney(target.amount_paise) +
            ' (version ' + snapshot.room.graph_version + ')';
        } else {
          targetInfo.textContent = 'No chaos target available (all debts settled).';
        }
      }

      const runBtn = document.getElementById('chaos-run-btn');
      if (runBtn) {
        runBtn.onclick = async () => {
          if (!target) {
            showToast('Chaos halted', 'No settlement target available.');
            return;
          }

          let mode = 'PROTECTED';
          const modeRadios = document.getElementsByName('chaos-mode');
          for (let i = 0; i < modeRadios.length; i++) {
            if (modeRadios[i].checked) mode = modeRadios[i].value;
          }

          let scenario = 'DEVICES';
          const scenarioRadios = document.getElementsByName('chaos-scenario');
          for (let i = 0; i < scenarioRadios.length; i++) {
            if (scenarioRadios[i].checked) scenario = scenarioRadios[i].value;
          }

          runBtn.disabled = true;

          // Keys: 10 distinct or 1 shared key repeated 10 times
          let keys = [];
          if (scenario === 'DEVICES') {
            for (let i = 0; i < 10; i++) keys.push(generateIdempotencyKey());
          } else {
            const sharedKey = generateIdempotencyKey();
            keys = Array(10).fill(sharedKey);
          }

          const amountStr = (target.amount_paise / 100).toFixed(2);
          const reqVersion = snapshot.room.graph_version;

          const logBody = document.getElementById('chaos-log-body');
          if (logBody) {
            while (logBody.firstChild) logBody.removeChild(logBody.firstChild);
          }

          // Fire all 10 fetches in the SAME tick via Promise.allSettled
          const promises = keys.map((key, idx) => {
            const t0 = performance.now();
            return fetch(BASE + 'api/rooms/' + slug + '/settlements', {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'Idempotency-Key': key,
                ...getActingMemberHeader(),
              },
              body: JSON.stringify({
                from_member: target.from,
                to_member: target.to,
                amount: amountStr,
                expected_graph_version: reqVersion,
                mode: mode,
              }),
            }).then(async res => {
              const latency = Math.round(performance.now() - t0);
              let data = null;
              try { data = await res.json(); } catch (_) {}
              return {
                index: idx + 1,
                status: res.status,
                code: data?.code || (res.ok ? 'OK' : 'ERROR'),
                latency,
                receiptId: data?.settlement_id || data?.id || (data?.receipt?.id || null),
              };
            }).catch(err => {
              const latency = Math.round(performance.now() - t0);
              return {
                index: idx + 1,
                status: 0,
                code: err.name || 'NET_ERROR',
                latency,
                receiptId: null,
              };
            });
          });

          const results = await Promise.allSettled(promises);
          runBtn.disabled = false;

          // Compute summary and log table
          const counts = {};
          if (logBody) {
            results.forEach(r => {
              const res = r.value;
              counts[res.status] = (counts[res.status] || 0) + 1;

              const tr = document.createElement('tr');
              [res.index, res.status, res.code, res.latency, res.receiptId || '—'].forEach(val => {
                const td = document.createElement('td');
                td.textContent = val.toString();
                tr.appendChild(td);
              });
              logBody.appendChild(tr);
            });
          }

          const summaryDiv = document.getElementById('chaos-summary');
          if (summaryDiv) {
            // DECISION: Sort summary with success code (201, then 200) first, then other status codes.
            const sortedKeys = Object.keys(counts).sort((a, b) => {
              const ca = parseInt(a, 10);
              const cb = parseInt(b, 10);
              if (ca === 201) return -1;
              if (cb === 201) return 1;
              if (ca === 200) return -1;
              if (cb === 200) return 1;
              return ca - cb;
            });
            const parts = sortedKeys.map(st => counts[st] + ' × ' + st);
            summaryDiv.textContent = 'Summary: ' + parts.join(', ');
          }

          // Re-fetch state to show results
          fetchStateHttp();
        };
      }
    }

    // Initial HTTP state fetch & WebSocket connect
    fetchStateHttp().then(connectWs);
  }

  // DOMContentLoaded routing
  document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('landing-page')) {
      initLandingPage();
    } else if (document.getElementById('room-page')) {
      initRoomPage();
    }
  });
})();
