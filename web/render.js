// Shared dashboard renderer for RelayForge — used by both the live dashboard
// (index.html, fed by SSE) and the replay viewer (replay.html, fed by JSONL).
// Plain global (no ES modules) so it works over file:// without CORS issues.

function makeDashboard({ cardsEl, activeEl, logEl }) {
  let lastActive; // undefined until the first snapshot

  const nz = (v, suffix = "") => (v === null || v === undefined) ? "—" : (v + suffix);

  function logSwitch(from, to, ts) {
    const t = ts ? new Date(ts * 1000).toLocaleTimeString() : new Date().toLocaleTimeString();
    const d = document.createElement("div");
    d.innerHTML = `<span class="t">${t}</span> ACTIVE ${from ?? "∅"} → <b>${to ?? "∅"}</b>`;
    logEl.prepend(d);
  }

  function render(snap) {
    if (activeEl) activeEl.textContent = snap.active ?? "—";
    if (lastActive !== undefined && snap.active !== lastActive) logSwitch(lastActive, snap.active, snap.ts);
    lastActive = snap.active;

    if (!snap.links || !snap.links.length) {
      cardsEl.innerHTML = '<div class="empty">no publishers — send streamid=publish:&lt;name&gt;</div>';
      return;
    }
    cardsEl.innerHTML = "";
    for (const l of snap.links) {
      const active = l.name === snap.active;
      const c = document.createElement("div");
      c.className = `card ${l.state}${active ? " active" : ""}`;
      c.innerHTML = `
        <div class="row1">
          <span class="name">${l.name}</span>
          <span class="badge ${l.state}">${l.state}</span>
          ${active ? '<span class="active-pill">● ACTIVE</span>' : ""}
        </div>
        <div class="metrics">
          <span><b>${nz(l.bitrate_kbps)}</b>kbps</span>
          <span><b>${nz(l.freeze)}</b>freeze</span>
          <span><b>${nz(l.rtt_ms)}</b>rtt ms</span>
          <span><b>${nz(l.loss_pct)}</b>loss %</span>
          <span><b>${nz(l.readers)}</b>readers</span>
          <span><b>${nz(l.uptime_s)}</b>up s</span>
        </div>`;
      cardsEl.appendChild(c);
    }
  }

  function reset() {
    lastActive = undefined;
    if (logEl) logEl.innerHTML = "";
    cardsEl.innerHTML = "";
  }

  return { render, reset };
}
