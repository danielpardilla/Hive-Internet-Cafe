/**
 * ICMS-ASI Shared Client JavaScript
 * LAN multi-client hardened:
 *  - Re-identifies automatically on every reconnect (new SID)
 *  - Syncs session timer when tab becomes visible again
 *  - Keepalive ping every 15s to prevent idle WiFi drops
 *  - Multi-handler: icms.on() appends, never overwrites
 */

// ── Toast ──────────────────────────────────────────────────────────────────
function showToast(message, type = "info", duration = 3500) {
  let container = document.querySelector(".toast-container");
  if (!container) {
    container = document.createElement("div");
    container.className = "toast-container";
    document.body.appendChild(container);
  }
  const icons = { success: "✓", error: "✕", info: "i", warning: "!" };
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span style="font-size:1.1rem">${icons[type]||"i"}</span> <span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(110%)";
    toast.style.transition = ".3s";
    setTimeout(() => toast.remove(), 350);
  }, duration);
}

// ── Live Clock ─────────────────────────────────────────────────────────────
function startClock(elementId) {
  const el = document.getElementById(elementId);
  if (!el) return;
  function tick() {
    el.textContent = new Date().toLocaleString("en-PH", {
      weekday:"short", month:"short", day:"numeric",
      year:"numeric", hour:"2-digit", minute:"2-digit", second:"2-digit"
    });
  }
  tick();
  setInterval(tick, 1000);
}

// ── ICMSSocket ─────────────────────────────────────────────────────────────
class ICMSSocket {
  constructor(role, pcLabel) {
    this.role        = role;
    this.pcLabel     = pcLabel;
    this.socket      = null;
    this.handlers    = {};   // event -> Array of handler functions
    this._pingTimer  = null;
    this._visHandler = null;
  }

  connect() {
    this.socket = io({
      transports:           ["websocket", "polling"],
      reconnectionAttempts: Infinity,
      reconnectionDelay:    1000,
      reconnectionDelayMax: 5000,
    });

    // Re-identify on EVERY connect (covers first connect AND all reconnects)
    // Each reconnect gets a new SID — server must be told who this is again
    this.socket.on("connect", () => {
      this._updateBanner(true);
      this.socket.emit("identify", { role: this.role, pc_label: this.pcLabel });
      this._fire("connect");
    });

    this.socket.on("disconnect", (reason) => {
      this._updateBanner(false);
      this._fire("disconnect", reason);
    });

    this.socket.on("connect_error", () => this._updateBanner(false));

    this.socket.on("server_ack", (data) => {
      console.log("[ICMS] Connected:", data.message, "| SID:", data.sid);
    });

    // All custom events — forward to registered handlers
    [
      "client_connected", "client_disconnected",
      "pc_update",
      "session_started", "session_ended", "session_extended",
      "billing_tick",
      "print_job_added", "print_job_updated", "print_confirmed", "print_status",
      "rates_updated",
      "client_end_request",
      "admin_broadcast",
      "identified",
    ].forEach(evt => {
      this.socket.on(evt, (data) => this._fire(evt, data));
    });

    // Keepalive ping every 15s — prevents idle WiFi/router from dropping the connection
    this._pingTimer = setInterval(async () => {
      if (!this.socket.connected) return;
      try { await fetch("/api/ping"); } catch(e) {}
    }, 15000);

    // Sync session state when tab becomes visible (user switches back to this tab)
    // This fires the "connect" handlers so pages can re-fetch their data
    this._visHandler = () => {
      if (document.visibilityState === "visible") {
        this._fire("tab_visible");
      }
    };
    document.addEventListener("visibilitychange", this._visHandler);

    return this;
  }

  /** Register a handler. Multiple calls for the same event ALL run (no overwrite). */
  on(event, fn) {
    if (!this.handlers[event]) this.handlers[event] = [];
    this.handlers[event].push(fn);
    return this;
  }

  emit(event, data) {
    if (this.socket && this.socket.connected) {
      this.socket.emit(event, data);
    }
  }

  _fire(event, data) {
    const handlers = this.handlers[event];
    if (handlers) handlers.forEach(fn => {
      try { fn(data); } catch(e) { console.error("[ICMS] Handler error on " + event + ":", e); }
    });
  }

  _updateBanner(online) {
    const banner = document.getElementById("conn-banner");
    if (!banner) return;
    if (online) {
      banner.className = "conn-banner online";
      banner.innerHTML = '<span class="dot dot-green"></span> Connected to Hive Internet Café Server';
    } else {
      banner.className = "conn-banner offline";
      banner.innerHTML = '<span class="dot dot-red"></span> Disconnected — Reconnecting...';
    }
  }
}

// ── Utilities ──────────────────────────────────────────────────────────────
function peso(amount) {
  return "₱" + parseFloat(amount || 0).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function formatDuration(totalSeconds) {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

async function pingServer() {
  try {
    const res = await fetch("/api/ping");
    const data = await res.json();
    return data.status === "ok";
  } catch(e) { return false; }
}
