/*
 * car-pi phone UI.
 *
 * Plain JS on purpose -- see index.html. All rendering goes through helpers that set
 * textContent rather than innerHTML, so a fault-code string or an ECU name from a
 * vehicle can never be interpreted as markup. The data comes off a car rather than
 * from a person, but that is exactly the kind of assumption that stops being true
 * later, and the safe version is no harder to write.
 */
'use strict';

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'];

// --- tiny DOM helpers --------------------------------------------------------

const $ = (id) => document.getElementById(id);

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.attrs) {
    for (const [key, value] of Object.entries(opts.attrs)) node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function fmt(value, digits = 1) {
  if (value === null || value === undefined) return '—';
  if (typeof value !== 'number') return String(value);
  if (Number.isInteger(value)) return value.toLocaleString();
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function wsUrl(path) {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}${path}`;
}

// --- tabs --------------------------------------------------------------------

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    for (const other of document.querySelectorAll('.tab')) {
      const active = other === tab;
      other.classList.toggle('active', active);
      other.setAttribute('aria-selected', String(active));
    }
    for (const view of document.querySelectorAll('.view')) {
      view.classList.toggle('active', view.id === `view-${tab.dataset.view}`);
    }
  });
}

// --- header ------------------------------------------------------------------

async function loadHealth() {
  try {
    const response = await fetch('api/health');
    const health = await response.json();
    const chip = $('interface');
    chip.textContent = health.simulated
      ? `simulated: ${health.interface.replace(/^simulated vehicle /, '')}`
      : health.interface;
    chip.classList.toggle('simulated', Boolean(health.simulated));
  } catch {
    $('interface').textContent = 'offline';
  }
}

// --- scanning ----------------------------------------------------------------

let scanning = false;

$('start').addEventListener('click', async () => {
  if (scanning) return;
  const raw = $('odometer').value.trim();
  const body = {};
  if (raw !== '') body.claimed_odometer_km = Number(raw);

  clear($('events'));
  clear($('report'));
  $('progress').hidden = false;
  scanning = true;
  $('start').disabled = true;

  try {
    const response = await fetch('api/scans', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (response.status === 409) {
      const detail = await response.json();
      showError(detail?.detail?.message || 'The interface is already in use.');
      return;
    }
    if (!response.ok) {
      showError(`The unit refused the request (HTTP ${response.status}).`);
      return;
    }

    const { id } = await response.json();
    await followScan(id);
  } catch (err) {
    showError(`Could not reach the unit: ${err.message}`);
  } finally {
    scanning = false;
    $('start').disabled = false;
    $('progress').hidden = true;
  }
});

function addEvent(message) {
  const list = $('events');
  list.appendChild(el('li', { text: message }));
  list.scrollTop = list.scrollHeight;
}

function followScan(id) {
  return new Promise((resolve) => {
    const socket = new WebSocket(wsUrl(`/ws/scans/${id}`));
    let settled = false;

    const finish = () => {
      if (!settled) {
        settled = true;
        resolve();
      }
    };

    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === 'progress') addEvent(message.message);
      else if (message.type === 'error') showError(message.message);
      else if (message.type === 'finished') {
        loadReport(id, message.summary);
        socket.close();
      }
    };

    // If the socket cannot be established -- a locked phone, a dropped hotspot --
    // fall back to polling rather than leaving the user staring at a spinner.
    socket.onerror = () => {
      pollScan(id).then(finish);
    };
    socket.onclose = finish;
  });
}

async function pollScan(id) {
  for (;;) {
    const response = await fetch(`api/scans/${id}`);
    const summary = await response.json();
    if (summary.state === 'done' || summary.state === 'failed') {
      loadReport(id, summary);
      return;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
}

async function loadReport(id, summary) {
  if (summary.state === 'failed') {
    showError(summary.error || 'The scan failed.');
    return;
  }
  const response = await fetch(`api/scans/${id}/report`);
  if (!response.ok) {
    showError('The scan finished but the report could not be read.');
    return;
  }
  renderReport(await response.json(), id);
}

function showError(message) {
  const target = $('report');
  clear(target);
  target.appendChild(
    el('div', { class: 'card error' }, [el('p', { text: message })]),
  );
}

// --- report ------------------------------------------------------------------

function renderReport(report, id) {
  const target = $('report');
  clear(target);

  target.appendChild(verdictBlock(report));
  target.appendChild(headerBlock(report, id));

  for (const finding of report.findings) target.appendChild(findingCard(finding));

  if (report.findings.length === 0) {
    target.appendChild(
      el('div', { class: 'card' }, [
        el('h2', { text: 'No findings' }),
        el('p', { text: 'Every check that could be run came back clean.' }),
      ]),
    );
  }

  const unassessed = report.not_assessed.filter((entry) => entry.missing.length > 0);
  if (unassessed.length) target.appendChild(unassessedBlock(unassessed));

  target.appendChild(codesBlock(report));
  const monitors = monitorsBlock(report);
  if (monitors) target.appendChild(monitors);
}

function verdictBlock(report) {
  const counts = {};
  for (const finding of report.findings) {
    counts[finding.severity] = (counts[finding.severity] || 0) + 1;
  }
  const unassessed = report.not_assessed.filter((e) => e.missing.length > 0).length;

  const tiles = SEVERITY_ORDER.filter((s) => counts[s]).map((severity) =>
    el('div', { class: 'count' }, [
      el('b', { text: String(counts[severity]) }),
      el('span', { text: severity }),
    ]),
  );

  tiles.push(
    el('div', { class: 'count' }, [
      el('b', { text: String(report.passed.length) }),
      el('span', { text: 'passed' }),
    ]),
  );

  // Shown whenever it is non-zero, and styled as a caution. A check that could not
  // run is not a check that passed, and the UI must not let those look alike.
  if (unassessed) {
    tiles.push(
      el('div', { class: 'count unassessed' }, [
        el('b', { text: String(unassessed) }),
        el('span', { text: 'not assessed' }),
      ]),
    );
  }

  return el('div', { class: 'verdict' }, tiles);
}

function headerBlock(report, id) {
  const rows = [
    ['VIN', report.scan.vin || 'not reported'],
    ['Modules', String(report.ecus.length)],
    ['Scanned', report.scan.started_at],
  ];
  if (report.scan.claimed_odometer_km !== null) {
    rows.push(['Advertised', `${fmt(report.scan.claimed_odometer_km)} km`]);
  }

  const table = el('table', {}, [
    el(
      'tbody',
      {},
      rows.map(([key, value]) =>
        el('tr', {}, [el('th', { text: key }), el('td', { text: value })]),
      ),
    ),
  ]);

  const download = el('button', { text: 'Download report (JSON)' });
  download.addEventListener('click', () => {
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = el('a', { attrs: { href: url, download: `carpi-${id}.json` } });
    anchor.click();
    URL.revokeObjectURL(url);
  });

  return el('div', { class: 'card' }, [table, download]);
}

function findingCard(finding) {
  const children = [
    el('span', { class: 'severity', text: finding.severity }),
    el('h3', { text: finding.title }),
    el('p', { text: finding.explain }),
  ];

  const evidence = Object.entries(finding.evidence || {});
  if (evidence.length) {
    children.push(
      el('div', {
        class: 'evidence',
        text: evidence.map(([key, value]) => `${key} = ${fmt(value)}`).join('\n'),
      }),
    );
  }

  if (finding.confidence && finding.confidence !== 'official') {
    children.push(
      el('p', {
        class: 'hint',
        text:
          `Confidence: ${finding.confidence} — this check has not been confirmed ` +
          'against a known-good reference vehicle.',
      }),
    );
  }

  return el('div', { class: 'finding', attrs: { 'data-severity': finding.severity } }, children);
}

function unassessedBlock(entries) {
  return el('div', { class: 'card' }, [
    el('h2', { text: 'Not assessed' }),
    el('p', {
      text:
        'These checks could not be run because the vehicle did not report the data ' +
        'they need. That is not the same as passing them.',
    }),
    el(
      'ul',
      { class: 'log' },
      entries.map((entry) =>
        el('li', { text: `${entry.title} — missing ${entry.missing.join(', ')}` }),
      ),
    ),
  ]);
}

function codesBlock(report) {
  const children = [el('h2', { text: 'Fault codes' })];
  let any = false;

  for (const ecu of report.ecus) {
    const groups = [
      ['permanent', ecu.dtcs.permanent, 'cannot be cleared by any tool'],
      ['stored', ecu.dtcs.stored, ''],
      ['pending', ecu.dtcs.pending, ''],
    ].filter(([, codes]) => codes.length);

    if (!groups.length) continue;
    any = true;
    children.push(el('h3', { text: ecu.ecu_name || ecu.address.label }));
    for (const [kind, codes, note] of groups) {
      const label = note ? `${kind} (${note})` : kind;
      children.push(
        el('p', { class: 'codes' }, [
          el('span', { class: kind === 'permanent' ? 'permanent' : '', text: `${label}: ` }),
          el('span', { text: codes.join(', ') }),
        ]),
      );
    }
  }

  if (!any) children.push(el('p', { text: 'None reported by any module.' }));
  return el('div', { class: 'card' }, children);
}

function monitorsBlock(report) {
  const facts = report.facts || {};
  if (facts['readiness.supported_count'] === undefined) return null;

  const chips = Object.entries(facts)
    .filter(([key]) => key.startsWith('readiness.') && key.endsWith('.complete'))
    .map(([key, complete]) => {
      const name = key.slice('readiness.'.length, -'.complete'.length).replace(/_/g, ' ');
      return el('span', {
        class: complete ? 'monitor' : 'monitor incomplete',
        text: complete ? name : `${name} — not complete`,
      });
    });

  return el('div', { class: 'card' }, [
    el('h2', { text: 'Emissions self-test readiness' }),
    el('p', {
      text:
        `${facts['readiness.complete_count']} of ` +
        `${facts['readiness.supported_count']} complete`,
    }),
    el('div', { class: 'monitors' }, chips),
  ]);
}

// --- live values -------------------------------------------------------------

let liveSocket = null;

$('live-toggle').addEventListener('click', () => {
  if (liveSocket) {
    liveSocket.close();
    return;
  }
  startLive();
});

function startLive() {
  const status = $('live-status');
  status.textContent = 'Connecting…';
  clear($('gauges'));

  const socket = new WebSocket(wsUrl('/ws/live'));
  liveSocket = socket;
  $('live-toggle').textContent = 'Stop live values';

  const gauges = new Map();

  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);

    if (message.type === 'ready') {
      status.textContent = `Streaming from ${message.module}, ${message.pids.length} values.`;
      for (const pid of message.pids) {
        const value = el('div', { class: 'value', text: '—' });
        const card = el('div', { class: 'gauge' }, [
          el('div', { class: 'name', text: pid.label }),
          value,
        ]);
        if (pid.unit) value.appendChild(el('span', { class: 'unit', text: ` ${pid.unit}` }));
        gauges.set(pid.name, value);
        $('gauges').appendChild(card);
      }
    } else if (message.type === 'sample') {
      for (const [name, value] of Object.entries(message.values)) {
        const node = gauges.get(name);
        if (!node) continue;
        // Replace only the number, leaving the unit span in place.
        node.firstChild.nodeValue = fmt(value);
      }
      if (message.failures.length) {
        status.textContent = `Streaming. Not answering: ${message.failures.join(', ')}.`;
      }
    } else if (message.type === 'busy') {
      status.textContent = 'The interface is busy — an inspection is probably running.';
    } else if (message.type === 'error') {
      status.textContent = message.message;
    }
  };

  socket.onclose = () => {
    liveSocket = null;
    $('live-toggle').textContent = 'Start live values';
    if (status.textContent === 'Connecting…') status.textContent = 'Disconnected.';
  };

  socket.onerror = () => {
    status.textContent = 'Could not open a live connection.';
  };
}

// --- history -----------------------------------------------------------------

$('refresh-history').addEventListener('click', loadHistory);

async function loadHistory() {
  const target = $('history');
  clear(target);
  const response = await fetch('api/scans');
  const { scans } = await response.json();

  if (!scans.length) {
    target.appendChild(el('div', { class: 'card' }, [el('p', { text: 'No scans yet.' })]));
    return;
  }

  for (const scan of scans) {
    const worst = scan.worst_severity ? `worst: ${scan.worst_severity}` : 'no findings';
    const card = el('div', { class: 'card' }, [
      el('h3', { text: scan.created_at }),
      el('p', {
        class: 'hint',
        text: `${scan.state} · ${scan.vin || 'no VIN'} · ${worst}`,
      }),
    ]);
    if (scan.state === 'done') {
      const view = el('button', { text: 'View' });
      view.addEventListener('click', async () => {
        await loadReport(scan.id, scan);
        document.querySelector('.tab[data-view="scan"]').click();
      });
      card.appendChild(view);
    }
    target.appendChild(card);
  }
}

// --- boot --------------------------------------------------------------------

loadHealth();

if ('serviceWorker' in navigator) {
  // Registration failing is not worth surfacing: it only costs offline reloads, and
  // the unit is the server, so if the page loaded at all the server was reachable.
  navigator.serviceWorker.register('sw.js').catch(() => {});
}
