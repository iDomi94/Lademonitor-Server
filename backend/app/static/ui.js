// Gemeinsame kleine UI-Bausteine fuer die per JS gerenderten Tabellen
// (Ladevorgaenge, Fahrzeuge, Anbieter, Ladeorte, Nutzer). Bewusst ein eigenes
// statisches Skript statt derselben Konstanten in drei Templates - die
// Aktionsspalte soll ueberall gleich aussehen.
//
// Warum Icons statt beschrifteter Knoepfe: "Bearbeiten"/"Löschen" waren
// zusammen gut 200 px breit und damit die breiteste Spalte der
// zwoelfspaltigen Ladevorgangs-Tabelle - die Tabelle lief dadurch auch auf
// dem Desktop aus dem Container heraus und musste seitlich geschoben werden.
// Inline-SVG statt Emoji, weil Emoji je nach Plattform in Groesse und Farbe
// wild auseinanderlaufen; `currentColor` laesst die Icons dagegen der
// Textfarbe des Knopfes folgen (inkl. Hover-Faerbung).

(function () {
  const svg = (paths) =>
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    paths + '</svg>';

  // Stift
  window.ICON_EDIT = svg('<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4z"/><path d="M14.5 5.5l4 4"/>');
  // Papierkorb
  window.ICON_DELETE = svg(
    '<path d="M4 7h16"/><path d="M9.5 7V4.5h5V7"/>' +
    '<path d="M6.5 7l1 12.5h9L17.5 7"/><path d="M10 11v5"/><path d="M14 11v5"/>');

  /** Ein einzelner Icon-Knopf. `variant` steuert nur die Hover-Farbe. */
  window.iconButton = function (icon, label, onclick, variant) {
    return `<button type="button" class="icon-btn${variant ? ' ' + variant : ''}" ` +
      `title="${label}" aria-label="${label}" onclick="${onclick}">${icon}</button>`;
  };

  // Farben der Kostenkategorien (Dashboard "Gesamtkosten", Seite
  // "Fahrzeug"). Laden in derselben Farbe wie AC im Dashboard, Wertverlust
  // und Leasing teilen sich eine - es gibt je Fahrzeug nur eins von beiden.
  window.COST_COLORS = {
    charging: '#5b8def', depreciation: '#a78bfa', leasing: '#a78bfa',
    insurance: '#4fd1a5', tax: '#f2b84b', maintenance: '#f08a5d', repair: '#e5534b',
    tires: '#8b98a9', wallbox: '#2ea87f', parking: '#d48ac8', toll: '#c9a227',
    cleaning: '#6cc4e0', other: '#6b7280',
  };

  /** Je Kategorie eine Zeile mit Balken (relativ zur groessten), Betrag und
   *  Anteil an der Summe. `labels` bildet die Kategorie auf ihren Namen ab. */
  window.renderCostBars = function (items, labels) {
    const total = items.reduce((sum, c) => sum + c.amount, 0);
    const max = Math.max(...items.map(c => c.amount), 0);
    const money = n => Number(n).toLocaleString(window.LADEMONITOR_LANG,
      { style: 'currency', currency: 'EUR' });
    return items.map(c => `
      <div class="cost-row">
        <div class="cost-name">${labels[c.category] || c.category}</div>
        <div class="cost-bar"><div style="width:${max ? c.amount / max * 100 : 0}%;
          background:${window.COST_COLORS[c.category] || window.COST_COLORS.other}"></div></div>
        <div class="cost-amount">${money(c.amount)}
          <small>${total ? Math.round(c.amount / total * 100) : 0}%</small></div>
      </div>`).join('');
  };

  /** Vertikales (Saeulen-)Diagramm als Inline-SVG, z.B. fuer den
   *  Monatstrend beim Verbrauch (Dashboard) und kWh je Monat (Rueckblick).
   *  Werte mit null werden als Luecke dargestellt - eine Saeule der Hoehe 0
   *  saehe aus wie ein gemessener Monat ohne Verbrauch. */
  window.renderVerticalBarChart = function (containerId, labels, values, color, decimals = 1) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const known = values.filter(v => v != null);
    const max = Math.max(...known, 0.0001);
    const barWidth = 40;
    const gap = 30;
    const chartHeight = 200;
    const topPad = 24;
    const bottomPad = 30;
    const width = labels.length * (barWidth + gap);
    const height = topPad + chartHeight + bottomPad;

    let bars = '';
    labels.forEach((label, i) => {
      const value = values[i];
      const x = gap / 2 + i * (barWidth + gap);
      if (value != null) {
        const h = Math.max((value / max) * chartHeight, 2);
        const y = topPad + (chartHeight - h);
        bars += `
          <rect x="${x}" y="${y}" width="${barWidth}" height="${h}" rx="4" fill="${color}"></rect>
          <text class="bar-value" x="${x + barWidth / 2}" y="${y - 8}" fill="#e6e9f0" font-size="11" text-anchor="middle">${value.toFixed(decimals)}</text>
        `;
      }
      bars += `<text class="bar-label" x="${x + barWidth / 2}" y="${topPad + chartHeight + 20}" fill="#8b95ab" font-size="12" text-anchor="middle">${label}</text>`;
    });

    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}">${bars}</svg>`;
  };

  /** Bearbeiten + Loeschen als Paar - die Aktionsspalte praktisch aller
   *  Tabellen dieser App. */
  window.editDeleteButtons = function (editCall, deleteCall, editLabel, deleteLabel) {
    return '<div class="row-actions">' +
      window.iconButton(window.ICON_EDIT, editLabel, editCall, 'edit') +
      window.iconButton(window.ICON_DELETE, deleteLabel, deleteCall, 'danger') +
      '</div>';
  };
})();
