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

// Some decoded parameters are structures rather than numbers -- monitor status carries a
// nested map of every emissions self-test. Those are shown as compact JSON rather than as
// "[object Object]", which is what String() would produce.
function fmtValue(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return fmt(value);
}

// Timestamps arrive as ISO-8601. A report is read by somebody deciding whether to buy a
// car, not by a machine, so it gets their locale's rendering rather than the wire format.
function fmtTime(iso) {
  if (!iso) return '—';
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
}

// --- tabs --------------------------------------------------------------------

const TABS = [...document.querySelectorAll('.tab')];

function activate(tab) {
  for (const other of TABS) {
    const active = other === tab;
    other.classList.toggle('active', active);
    other.setAttribute('aria-selected', String(active));
    // Only the selected tab stays in the tab order; arrow keys move between them. That is
    // the ARIA tabs pattern, and it means a keyboard user does not have to step through
    // three tabs to reach the panel.
    other.setAttribute('tabindex', active ? '0' : '-1');
  }
  for (const view of document.querySelectorAll('.view')) {
    view.classList.toggle('active', view.id === `view-${tab.dataset.view}`);
  }
  // History is fetched on arrival. Landing on a Refresh button above blank space reads as
  // "no scans" when the truth is "not asked yet".
  if (tab.dataset.view === 'history') loadHistory();
}

for (const [index, tab] of TABS.entries()) {
  tab.addEventListener('click', () => activate(tab));
  tab.addEventListener('keydown', (event) => {
    const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = TABS[(index + step + TABS.length) % TABS.length];
    next.focus();
    activate(next);
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
    reflectBusy(health);
  } catch {
    $('interface').textContent = 'offline';
    // Health is unreachable, so we cannot know whether the bus is claimed. Leave the button
    // usable: refusing to let someone try, on a guess, is worse than letting them see the
    // real error. Never strand the button disabled on a failed poll.
    if (!scanning) $('start').disabled = false;
  }
}

// The unit refuses a second conversation rather than queueing it, so a busy interface is a
// normal state rather than an error. Saying so before the tap is better than letting the
// user press a button that looks ready and collect an HTTP 409.
function reflectBusy(health) {
  if (scanning) return; // our own scan; the click handler owns the button
  const status = $('bus-status');
  const other = health.busy ? health.activity : null;
  $('start').disabled = Boolean(other);
  status.textContent = other
    ? `The interface is busy with ${other.kind === 'live' ? 'live values' : 'an inspection'}.` +
      ' One conversation at a time — wait for it to finish.'
    : '';
}

// --- first run, and the bus check -------------------------------------------

const SEEN_KEY = 'carpi.notice.v1';

function showFirstRunNotice() {
  let seen = false;
  // Private browsing rejects localStorage writes. Showing the notice again is a far better
  // failure than throwing during boot and leaving the page half-initialised.
  try {
    seen = localStorage.getItem(SEEN_KEY) === '1';
  } catch {
    seen = false;
  }
  if (seen) return;

  $('first-run').hidden = false;
  $('first-run-ok').addEventListener('click', () => {
    $('first-run').hidden = true;
    try {
      localStorage.setItem(SEEN_KEY, '1');
    } catch {
      /* nothing to do; the notice simply appears again next time */
    }
  });
}

$('preflight').addEventListener('click', async () => {
  const target = $('preflight-result');
  const button = $('preflight');
  button.disabled = true;
  target.textContent = 'Listening for a few seconds. Nothing is being sent…';
  target.className = 'hint';

  try {
    const response = await fetch('api/preflight');
    if (response.status === 409) {
      target.textContent = 'The interface is busy. Wait for the running job to finish.';
      return;
    }
    const health = await response.json();
    // Advice is rendered as its own lines rather than one sentence: these are alternative
    // causes to work through in order, not a paragraph.
    clear(target);
    target.className = health.verdict === 'healthy' ? 'hint' : 'hint caution';
    target.appendChild(el('span', { text: verdictLine(health) }));
    if (health.advice.length) {
      target.appendChild(
        el('ul', { class: 'log' }, health.advice.map((line) => el('li', { text: line }))),
      );
    }
  } catch (err) {
    target.textContent = `Could not reach the unit: ${err.message}`;
  } finally {
    button.disabled = false;
  }
});

function verdictLine(health) {
  if (health.verdict === 'simulated') {
    return `There is no bus to check — ${health.summary}.`;
  }
  if (health.verdict === 'healthy') {
    return `The bus is alive and error-free — ${health.summary}. Safe to scan.`;
  }
  if (health.verdict === 'errors') {
    return `The bus is reporting errors — ${health.summary}. Do not scan yet.`;
  }
  if (health.verdict === 'silent') {
    return `Nothing heard — ${health.summary}. A scan now would report that the car ` +
      'answered nothing, which is not the same as a clean car.';
  }
  return health.summary;
}

// --- what will be checked ----------------------------------------------------

async function loadRules() {
  const target = $('rules');
  try {
    const { rules } = await (await fetch('api/defs/rules')).json();
    clear(target);
    for (const rule of rules) {
      const row = el('div', { class: 'rule' }, [
        el('span', { class: 'severity', text: rule.severity }),
        el('span', { text: rule.title }),
      ]);
      if (rule.confidence && rule.confidence !== 'official') {
        row.appendChild(el('span', { class: 'hint', text: ` (${rule.confidence})` }));
      }
      target.appendChild(row);
    }
  } catch {
    target.textContent = 'Could not load the list of checks.';
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
    $('progress').hidden = true;
    // Re-read health rather than assuming the button is usable again: another client may
    // have claimed the interface while this scan was running.
    loadHealth();
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
        // Awaited before resolving, so the spinner does not disappear while the report is
        // still on its way and leave the screen briefly blank.
        loadReport(id, message.summary).then(() => {
          socket.close();
          finish();
        });
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

// The fallback when the socket will not open -- a locked phone, a dropped hotspot. It reads
// the events endpoint rather than only the summary, so progress still appears: a spinner
// with no lines under it is indistinguishable from a hang.
async function pollScan(id) {
  let index = 0;
  let failures = 0;

  for (;;) {
    try {
      const response = await fetch(`api/scans/${id}/events?since=${index}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const { index: next, events, state } = await response.json();
      index = next;
      for (const message of events) addEvent(message);
      failures = 0;

      if (state === 'done' || state === 'failed') {
        const summary = await (await fetch(`api/scans/${id}`)).json();
        await loadReport(id, summary);
        return;
      }
    } catch (err) {
      // A scan runs for minutes on a hotspot that comes and goes, so one failed request is
      // not a reason to abandon a scan that is still running on the unit. Several in a row
      // is, because then there is nothing to report progress from.
      if (++failures >= 10) {
        showError(`Lost contact with the unit while scanning: ${err.message}`);
        return;
      }
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
    const skipped = report.not_assessed.filter((entry) => entry.missing.length > 0).length;
    target.appendChild(
      el('div', { class: 'card' }, [
        el('h2', { text: 'No findings' }),
        el('p', { text: 'Every check that could be run came back clean.' }),
        // The heading alone would read as a verdict on the car. It is only a verdict on the
        // checks that ran, and when some did not, that distinction is the whole report.
        skipped
          ? el('p', {
              class: 'hint',
              text:
                `${skipped} check${skipped === 1 ? '' : 's'} could not be run at all. ` +
                'Read the "Not assessed" section below before treating this as a clean car.',
            })
          : null,
      ]),
    );
  }

  const unassessed = report.not_assessed.filter((entry) => entry.missing.length > 0);
  if (unassessed.length) target.appendChild(unassessedBlock(unassessed));

  const broken = report.rule_errors || [];
  if (broken.length) target.appendChild(ruleErrorsBlock(broken));

  target.appendChild(codesBlock(report));
  const monitors = monitorsBlock(report);
  if (monitors) target.appendChild(monitors);
  target.appendChild(detailsBlock(report));
}

// Everything the report carries and the summary above does not show. Collapsed, so the
// simple path stays simple, but present -- until now the only way to see a module's raw
// bytes or its Mode 06 numbers was to download the JSON and open it somewhere else, on a
// phone, on a hotspot with no internet. The data was already here.
function detailsBlock(report) {
  const sections = [];

  const odometers = Object.entries(report.odometer_by_module || {});
  if (odometers.length) {
    sections.push(
      detail(
        `Odometer by module (${odometers.length})`,
        keyValues(odometers.map(([name, km]) => [name, `${fmt(km)} km`])),
      ),
    );
  }

  for (const ecu of report.ecus || []) {
    const name = ecu.ecu_name || ecu.address.label;
    const rows = [];
    const readings = Object.entries(ecu.readings || {});

    if (readings.length) {
      // Raw bytes are what makes a scan re-checkable after a definition is corrected, and
      // what somebody else needs to verify a finding rather than take it on trust.
      rows.push(el('h4', { text: 'Live parameters' }));
      rows.push(
        keyValues(
          readings.map(([key, r]) => [
            r.label || key,
            `${fmtValue(r.value)}${r.unit ? ' ' + r.unit : ''}` +
              `${r.raw ? '   [' + r.raw + ']' : ''}` +
              `${r.plausible === false ? '   implausible, so omitted from the facts' : ''}`,
          ]),
        ),
      );
    }

    if (ecu.monitor_tests?.length) {
      rows.push(el('h4', { text: 'Self-test results (Mode 06)' }));
      rows.push(
        el('p', {
          class: 'hint',
          text:
            'Raw counts, not engineering units. The unit-and-scaling table is not shipped, ' +
            'so these are not given units they might not have. Pass and margin are exact ' +
            'regardless, because the module supplies its own limits.',
        }),
      );
      rows.push(
        keyValues(
          ecu.monitor_tests.map((t) => [
            `monitor 0x${t.monitor_id.toString(16).toUpperCase()} test ${t.test_id}`,
            `${t.value} (min ${t.minimum}, max ${t.maximum}) — ${t.passed ? 'pass' : 'FAIL'}`,
          ]),
        ),
      );
    }

    const freeze = Object.entries(ecu.freeze_frame || {});
    if (freeze.length) {
      rows.push(el('h4', { text: 'Freeze frame' }));
      rows.push(el('p', { class: 'hint', text: 'Conditions recorded when a fault was stored.' }));
      rows.push(
        keyValues(freeze.map(([key, r]) => [key, `${fmtValue(r.value)}   [${r.raw}]`])),
      );
    }

    const identity = [
      ['Address', ecu.address.label],
      ['VIN (OBD-II)', ecu.vin],
      ['VIN (UDS)', ecu.uds_vin],
      ['Calibration IDs', ecu.calibration_ids?.join(', ')],
      ['Calibration verification', ecu.calibration_verification_numbers?.join(', ')],
      ['Supported parameters', ecu.supported_pids?.length],
      ['Did not support', ecu.unsupported?.join(', ')],
    ].filter(([, value]) => value !== null && value !== undefined && value !== '');
    if (identity.length) {
      rows.push(el('h4', { text: 'Module identity' }));
      rows.push(keyValues(identity));
    }

    if (ecu.errors?.length) {
      rows.push(el('h4', { text: 'Requests that failed' }));
      rows.push(el('ul', { class: 'log' }, ecu.errors.map((e) => el('li', { text: e }))));
    }

    if (rows.length) sections.push(detail(name, el('div', {}, rows)));
  }

  for (const module of report.module_readings || []) {
    const rows = Object.entries(module.values || {}).map(([key, value]) => [
      key,
      `${fmtValue(value)}${module.raw?.[key] ? '   [' + module.raw[key] + ']' : ''}`,
    ]);
    for (const key of module.unavailable || []) rows.push([key, 'the module did not answer']);
    // A locked identifier is a positive finding: something is there. Saying "no answer"
    // would throw that away.
    for (const key of module.protected || []) {
      rows.push([key, 'exists, but locked behind a login']);
    }
    for (const key of module.implausible || []) {
      rows.push([key, 'answered, but the value was out of range, so it was omitted']);
    }
    if (rows.length) {
      sections.push(detail(`${module.ecu} (manufacturer data)`, keyValues(rows)));
    }
  }

  if (report.scan?.notes?.length) {
    sections.push(
      detail(
        'Notes from the scan',
        el('ul', { class: 'log' }, report.scan.notes.map((n) => el('li', { text: n }))),
      ),
    );
  }

  const meta = [
    ['Started', fmtTime(report.scan?.started_at)],
    ['Finished', fmtTime(report.scan?.finished_at)],
    ['Transport', report.scan?.transport],
    ['Vehicle profile', report.scan?.profile_label || report.scan?.profile_id || 'none used'],
    ['Checks passed', (report.passed || []).join(', ') || 'none'],
    ['Schema', report.schema],
  ].filter(([, value]) => value !== undefined && value !== null);
  sections.push(detail('Scan details', keyValues(meta)));

  return el('div', { class: 'card' }, [
    el('h2', { text: 'Everything else' }),
    el('p', {
      class: 'hint',
      text:
        'The full data behind the report above, including raw bytes. Nothing here is ' +
        'interpreted for you.',
    }),
    ...sections,
  ]);
}

function detail(summary, body) {
  const node = el('details', {}, [el('summary', { text: summary }), body]);
  return node;
}

function keyValues(pairs) {
  return el('div', { class: 'scroll-x' }, [
    el(
      'table',
      {},
      [
        el(
          'tbody',
          {},
          pairs.map(([key, value]) =>
            el('tr', {}, [el('th', { text: String(key) }), el('td', { text: String(value) })]),
          ),
        ),
      ],
    ),
  ]);
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
    ['Scanned', fmtTime(report.scan.started_at)],
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

// A rule that threw is counted as neither passed, failed, nor not-assessed -- it simply
// vanishes. That is the one remaining way silence could read as a clean bill of health, so
// it is shown as a defect in car-pi rather than a fact about the car.
function ruleErrorsBlock(errors) {
  return el('div', { class: 'card error' }, [
    el('h2', { text: 'Checks that could not be evaluated' }),
    el('p', {
      text:
        'These checks failed to run because of a fault in car-pi itself, not in the ' +
        'vehicle. They are not counted anywhere above. Please report them.',
    }),
    el(
      'ul',
      { class: 'log' },
      errors.map((entry) => el('li', { text: `${entry.rule_id} — ${entry.error}` })),
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

  if (!any) {
    children.push(el('p', { text: 'None reported by any module.' }));
    return el('div', { class: 'card' }, children);
  }

  // A bare "P0420" sends the reader to a search engine, and there is no internet on the
  // unit's own hotspot. This is what SAE J2012 fixes about each code: which part of the
  // car, and whether a generic description can exist for it at all.
  const meanings = {};
  for (const ecu of report.ecus) Object.assign(meanings, ecu.dtcs.meanings || {});
  const codes = Object.keys(meanings).sort();
  if (codes.length) {
    children.push(el('h3', { text: 'What these codes are about' }));
    for (const code of codes) {
      children.push(
        el('div', { class: 'rule' }, [
          el('span', { class: 'codes', text: code }),
          el('span', { class: 'hint', text: meanings[code].summary }),
        ]),
      );
    }
  }

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
    // "no findings" on its own would describe a car nothing could be asked about exactly
    // as it describes a clean one. The skipped count travels with the verdict everywhere,
    // so a history entry cannot imply a pass the scan never established.
    const skipped = scan.not_assessed_count || 0;
    const worst = scan.worst_severity
      ? `worst: ${scan.worst_severity}`
      : skipped
        ? 'nothing found'
        : 'no findings';

    const summary = el('p', { class: 'hint' }, [
      el('span', { text: `${scan.state} · ${scan.vin || 'no VIN'} · ${worst}` }),
    ]);
    if (skipped) {
      summary.appendChild(
        el('span', { class: 'skipped-note', text: ` · ${skipped} not assessed` }),
      );
    }

    const card = el('div', { class: 'card' }, [
      el('h3', { text: fmtTime(scan.created_at) }),
      summary,
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

showFirstRunNotice();
loadHealth();
loadHistory();
loadRules();

// The interface can be claimed by another phone on the same hotspot, or drop out entirely.
// A header chip that was accurate once at load is not the same as an accurate one.
setInterval(loadHealth, 5000);

if ('serviceWorker' in navigator) {
  // Registration failing is not worth surfacing: it only costs offline reloads, and
  // the unit is the server, so if the page loaded at all the server was reachable.
  navigator.serviceWorker.register('sw.js').catch(() => {});
}
