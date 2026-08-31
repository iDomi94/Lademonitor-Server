// Gemeinsamer Zeitraum-Filter fuer Dashboard und Ladevorgaenge. Der Zustand lebt in
// localStorage statt nur im JS-Speicher, weil jede Navigation zwischen den Seiten hier
// ein vollstaendiger Seiten-Reload ist (serverseitig gerendert, kein SPA-Routing) -
// ohne localStorage wuerde der Filter beim Seitenwechsel verloren gehen. Pendant zu
// SessionFilter.swift in der iOS-App, dort aber bewusst nur In-Memory, weil dort ein
// Tab-Wechsel keinen Reload ausloest.

const FILTER_STORAGE_KEY = 'lademonitor_filter';

// window.I18N_FILTER wird von base.html VOR dieser Datei gesetzt (siehe
// i18n/__init__.py::translations_for) - diese Datei ist ein normales
// statisches Asset, kein Jinja2-Template, bekommt Uebersetzungen also nicht
// per {{ t('...') }}. Deutscher Fallback direkt hier, falls die Datei doch
// mal ohne dieses Setup geladen wird (z.B. lokal isoliert getestet).
function ft(key, fallback) {
  return (window.I18N_FILTER && window.I18N_FILTER[key]) || fallback;
}

function loadFilter() {
  try {
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveFilter(startDate, endDate) {
  localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify({ start_date: startDate, end_date: endDate }));
}

function clearFilter() {
  localStorage.removeItem(FILTER_STORAGE_KEY);
}

// Haengt start_date/end_date an einen bestehenden Query-String (falls vorhanden) an -
// so laesst es sich einfach mit weiteren Parametern (z.B. vehicle_id) kombinieren.
function appendFilterParams(params) {
  const f = loadFilter();
  if (f?.start_date) params.set('start_date', f.start_date);
  if (f?.end_date) params.set('end_date', f.end_date);
  return params;
}

function filterQueryString() {
  const qs = appendFilterParams(new URLSearchParams()).toString();
  return qs ? '?' + qs : '';
}

function toISODate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const FILTER_PRESETS = [
  ['last7Days', ft('filter.presets.last7Days', 'Letzte 7 Tage')],
  ['last30Days', ft('filter.presets.last30Days', 'Letzte 30 Tage')],
  ['last90Days', ft('filter.presets.last90Days', 'Letzte 90 Tage')],
  ['lastMonth', ft('filter.presets.lastMonth', 'Letzter Monat')],
  ['monthToDate', ft('filter.presets.monthToDate', 'Monat bis jetzt')],
  ['yearToDate', ft('filter.presets.yearToDate', 'Jahr bis jetzt')],
  ['lastYear', ft('filter.presets.lastYear', 'Letztes Jahr')],
];

function presetRange(id) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  switch (id) {
    case 'last7Days': {
      const start = new Date(today); start.setDate(start.getDate() - 6);
      return [toISODate(start), toISODate(today)];
    }
    case 'last30Days': {
      const start = new Date(today); start.setDate(start.getDate() - 29);
      return [toISODate(start), toISODate(today)];
    }
    case 'last90Days': {
      const start = new Date(today); start.setDate(start.getDate() - 89);
      return [toISODate(start), toISODate(today)];
    }
    case 'lastMonth': {
      const thisMonthStart = new Date(today.getFullYear(), today.getMonth(), 1);
      const lastMonthStart = new Date(today.getFullYear(), today.getMonth() - 1, 1);
      const lastMonthEnd = new Date(thisMonthStart);
      lastMonthEnd.setDate(lastMonthEnd.getDate() - 1);
      return [toISODate(lastMonthStart), toISODate(lastMonthEnd)];
    }
    case 'monthToDate': {
      const start = new Date(today.getFullYear(), today.getMonth(), 1);
      return [toISODate(start), toISODate(today)];
    }
    case 'yearToDate': {
      const start = new Date(today.getFullYear(), 0, 1);
      return [toISODate(start), toISODate(today)];
    }
    case 'lastYear': {
      const start = new Date(today.getFullYear() - 1, 0, 1);
      const end = new Date(today.getFullYear() - 1, 11, 31);
      return [toISODate(start), toISODate(end)];
    }
    default:
      return null;
  }
}

function formatDateDE(iso) {
  const [y, m, d] = iso.split('-');
  return `${d}.${m}.${y}`;
}

// Auf-/zugeklappt-Zustand pro Container, damit ein Preset-Klick das Panel nicht
// wieder zuklappt (renderFilterBar baut bei jeder Aenderung das komplette innerHTML
// neu auf, dieser Zustand muesste sonst verloren gehen).
const _filterPanelExpanded = {};

/// Rendert die Filterleiste in #containerId und ruft onChange() nach jeder Aenderung auf
/// (Preset-Klick, "Anwenden", "Filter löschen") - der Aufrufer laedt daraufhin seine
/// Daten mit filterQueryString() neu, statt dass diese Datei etwas ueber Dashboard/
/// Ladevorgaenge-spezifische Ladefunktionen wissen muesste.
///
/// Versteckt hinter einem Umschalt-Button (Disclosure), der den aktiven Zeitraum als
/// Label zeigt - eine dauerhaft ausgeklappte Leiste wirkte auf beiden Seiten zu wuchtig.
function renderFilterBar(containerId, onChange) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const current = loadFilter();
  const expanded = _filterPanelExpanded[containerId] ?? false;
  const startId = `${containerId}-start`;
  const endId = `${containerId}-end`;

  const presetButtons = FILTER_PRESETS.map(([id, title]) =>
    `<button type="button" class="filter-preset-btn" data-preset="${id}">${title}</button>`
  ).join('');

  const summary = current
    ? `${formatDateDE(current.start_date)} – ${formatDateDE(current.end_date)}`
    : ft('filter.all_sessions', 'Alle Ladevorgänge');

  container.innerHTML = `
    <div class="filter-widget">
      <button type="button" class="filter-toggle-btn${current ? ' filter-toggle-active' : ''}">
        <span>🗓️ ${summary}</span>
        <span class="filter-toggle-chevron">${expanded ? '▲' : '▼'}</span>
      </button>
      <div class="card filter-panel" style="display:${expanded ? 'block' : 'none'}">
        <div class="filter-row">${presetButtons}</div>
        <div class="filter-row">
          <input type="date" id="${startId}" value="${current?.start_date || ''}">
          <input type="date" id="${endId}" value="${current?.end_date || ''}">
          <button type="button" class="filter-apply-btn">${ft('filter.apply', 'Anwenden')}</button>
          ${current ? `<button type="button" class="filter-clear-btn" style="background:var(--danger)">${ft('filter.clear', 'Filter löschen')}</button>` : ''}
        </div>
      </div>
    </div>`;

  container.querySelector('.filter-toggle-btn').addEventListener('click', () => {
    _filterPanelExpanded[containerId] = !expanded;
    renderFilterBar(containerId, onChange);
  });

  container.querySelectorAll('.filter-preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const [start, end] = presetRange(btn.dataset.preset);
      saveFilter(start, end);
      _filterPanelExpanded[containerId] = true;
      renderFilterBar(containerId, onChange);
      onChange();
    });
  });

  container.querySelector('.filter-apply-btn').addEventListener('click', () => {
    const start = document.getElementById(startId).value;
    const end = document.getElementById(endId).value;
    if (!start || !end) return;
    saveFilter(start <= end ? start : end, start <= end ? end : start);
    _filterPanelExpanded[containerId] = true;
    renderFilterBar(containerId, onChange);
    onChange();
  });

  const clearBtn = container.querySelector('.filter-clear-btn');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      clearFilter();
      _filterPanelExpanded[containerId] = false;
      renderFilterBar(containerId, onChange);
      onChange();
    });
  }
}
