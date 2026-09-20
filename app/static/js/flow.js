/* Shared flow client + speech.
   One state machine on the server; this is the thin channel layer.
   No frameworks, no build step -- plain ES2020 that survives a 3G
   connection and a five-year-old Android browser. */

const FlowClient = {
  ref: null,
  eventSource: null,
  lastMessageId: 0,

  async _post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return res.json();
  },

  async start(payload) {
    const snap = await this._post("/api/flow/start", payload);
    this.ref = snap.ref;
    this.lastMessageId = 0;
    return snap;
  },

  async digit(d) {
    return this._post(`/api/flow/${this.ref}/digit`, { digit: String(d) });
  },

  async silence() {
    return this._post(`/api/flow/${this.ref}/silence`, {});
  },

  async hangup() {
    return this._post(`/api/flow/${this.ref}/hangup`, {});
  },

  async refresh() {
    const res = await fetch(`/api/flow/${this.ref}?after=${this.lastMessageId}`);
    return res.json();
  },

  subscribe(onEvent) {
    this.unsubscribe();
    if (!this.ref) return;
    this.eventSource = new EventSource(`/api/flow/${this.ref}/events`);
    this.eventSource.onmessage = (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch { return; }
      if (data.type === "message" && data.id) {
        if (data.id > this.lastMessageId) this.lastMessageId = data.id;
      }
      onEvent(data);
    };
    this.eventSource.onerror = () => { /* server closes stream on end; ignore */ };
  },

  unsubscribe() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  },
};

/* --- Browser speech synthesis (additive voice layer for the web
       companion; the phone path uses provider TTS in production) --- */

const Speech = {
  enabled: false,
  locale: "en-IN",
  queue: [],
  speaking: false,

  setLocale(code) {
    this.locale = code || "en-IN";
  },

  speak(text) {
    if (!this.enabled || !("speechSynthesis" in window) || !text) return;
    const utter = new SpeechSynthesisUtterance(text);
    utter.lang = this.locale;
    utter.rate = 0.92;
    window.speechSynthesis.speak(utter);
  },

  speakAll(texts) {
    if (!this.enabled) return;
    window.speechSynthesis?.cancel();
    (texts || []).forEach((t, i) => {
      if (t) setTimeout(() => this.speak(t), i * 120);
    });
  },

  stop() {
    window.speechSynthesis?.cancel();
  },
};

/* --- tiny DOM helpers --- */
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function fmtTime(isoOrEmpty) {
  return isoOrEmpty || "";
}
