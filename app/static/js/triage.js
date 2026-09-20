/* Web companion triage flow.
   Mirrors the phone path exactly: same state machine, same digits,
   same safety defaults. Buttons map 1:1 to DTMF keys. */

(function () {
  const root = document.getElementById("triage-app");
  if (!root) return;

  const LANGS = JSON.parse(root.dataset.languages || "{}");
  const LANG_ORDER = JSON.parse(root.dataset.languageOrder || "[]");
  const LOCALES = JSON.parse(root.dataset.locales || "{}");

  let snapshot = null;

  const setupPane = document.getElementById("pane-setup");
  const flowPane = document.getElementById("pane-flow");
  const regionSel = document.getElementById("region-select");
  const phoneInp = document.getElementById("caller-phone");
  const startBtn = document.getElementById("start-btn");
  const speakToggle = document.getElementById("speak-toggle");

  speakToggle?.addEventListener("change", () => {
    Speech.enabled = speakToggle.checked;
    if (!Speech.enabled) Speech.stop();
    else renderPrompts(snapshot);
  });

  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    startBtn.textContent = "Connecting…";
    snapshot = await FlowClient.start({
      region_id: regionSel.value ? Number(regionSel.value) : null,
      caller_phone: phoneInp.value.trim() || null,
      channel: "web",
    });
    setupPane.hidden = true;
    flowPane.hidden = false;
    FlowClient.subscribe(onServerEvent);
    render();
  });

  function onServerEvent(ev) {
    if (ev.type === "message") {
      appendStatus(ev);
      if (ev.kind === "status" || ev.kind === "system") Speech.speak(ev.text);
    } else if (ev.type === "dispatch") {
      renderDispatch(ev.dispatch);
    } else if (ev.type === "ended") {
      FlowClient.unsubscribe();
    }
  }

  function render() {
    const body = document.getElementById("flow-body");
    body.innerHTML = "";
    if (!snapshot) return;

    if (snapshot.language) window.__jrLang = snapshot.language;
    Speech.setLocale(LOCALES[snapshot.language] || "en-IN");

    if (snapshot.state === "language") {
      body.appendChild(languagePane());
    } else if (snapshot.state === "track") {
      body.appendChild(trackPane());
    } else if (snapshot.state === "questions") {
      body.appendChild(questionPane());
    } else if (snapshot.state === "result") {
      body.appendChild(resultPane());
    } else if (snapshot.state === "ended") {
      body.appendChild(endedPane());
    }
    renderPrompts(snapshot);
  }

  function renderPrompts(snap) {
    if (!snap || !Speech.enabled) return;
    const texts = (snap.step_prompts || snap.prompts || []).map((p) => p.text);
    Speech.speakAll(texts);
  }

  function languagePane() {
    const wrap = el("div");
    wrap.appendChild(
      el("p", { class: "lede" }, "Choose your language / अपनी भाषा चुनें")
    );
    const grid = el("div", { class: "lang-grid" });
    LANG_ORDER.forEach(({ digit, code, name }) => {
      grid.appendChild(
        el("button", {
          class: "lang-btn",
          onclick: () => press(digit),
        }, name, el("small", {}, `press ${digit}`))
      );
    });
    wrap.appendChild(grid);
    return wrap;
  }

  function trackPane() {
    const wrap = el("div", { class: "question-card" });
    const prompt = (snapshot.prompts || []).find((p) => p.key === "ivr_track_prompt");
    wrap.appendChild(el("p", { class: "question-text" }, prompt ? prompt.text : "Is this about a pregnant woman, or a baby?"));
    const btns = el("div", { class: "answer-buttons" });
    btns.style.gridTemplateColumns = "1fr 1fr";
    btns.appendChild(el("button", { class: "answer-btn", onclick: () => press("1") },
      "Pregnant woman", el("small", {}, "गर्भवती महिला · press 1")));
    btns.appendChild(el("button", { class: "answer-btn", onclick: () => press("2") },
      "Baby", el("small", {}, "बच्चा · press 2")));
    wrap.appendChild(btns);
    return wrap;
  }

  function questionPane() {
    const q = snapshot.question;
    const wrap = el("div");
    wrap.appendChild(progressDots(q.order, q.total));

    const card = el("div", { class: "question-card" });
    const prompt = (snapshot.step_prompts || snapshot.prompts || []).find(
      (p) => p.key === `q_${q.id}`
    );
    const hint = (snapshot.step_prompts || snapshot.prompts || []).find(
      (p) => p.key === `h_${q.id}`
    );
    const ack = (snapshot.step_prompts || []).find((p) => p.key === "ivr_unclear_ack");
    const invalid = (snapshot.step_prompts || []).find((p) => p.key === "ivr_invalid");

    if (ack) card.appendChild(el("div", { class: "notice warn mb1" }, ack.text));
    if (invalid) card.appendChild(el("div", { class: "notice warn mb1" }, invalid.text));

    card.appendChild(el("p", { class: "faint small mb0" }, `Question ${q.order} of ${q.total}`));
    card.appendChild(el("p", { class: "question-text" }, prompt ? prompt.text : q.id));
    if (hint) card.appendChild(el("p", { class: "question-hint" }, hint.text));

    const btns = el("div", { class: "answer-buttons" });
    btns.appendChild(el("button", { class: "answer-btn yes", onclick: () => press("1") },
      t("btn_yes"), el("small", {}, "press 1")));
    btns.appendChild(el("button", { class: "answer-btn", onclick: () => press("2") },
      t("btn_no"), el("small", {}, "press 2")));
    btns.appendChild(el("button", { class: "answer-btn unsure", onclick: () => press("3") },
      t("btn_unsure"), el("small", {}, "press 3")));
    card.appendChild(btns);
    wrap.appendChild(card);
    return wrap;
  }

  function progressDots(order, total) {
    const dots = el("div", { class: "progress-dots", "aria-hidden": "true" });
    for (let i = 1; i <= total; i++) {
      let cls = "dot";
      if (i < order) cls += " done";
      else if (i === order) cls += " current";
      dots.appendChild(el("span", { class: cls }));
    }
    return dots;
  }

  function resultPane() {
    const wrap = el("div");
    const r = snapshot.result;
    if (!r) return wrap;

    const banner = el("div", { class: `tier-banner ${r.tier}` });
    banner.appendChild(el("div", { class: "tier-title" }, t(`tier_${r.tier}`)));
    const resultPrompt = (snapshot.step_prompts || snapshot.prompts || []).find(
      (p) => p.key === `ivr_result_${r.tier}`
    );
    if (resultPrompt) banner.appendChild(el("p", { class: "mb0" }, resultPrompt.text));
    wrap.appendChild(banner);

    wrap.appendChild(
      el("p", { class: "small muted" }, `Reference: `, el("strong", { class: "mono" }, snapshot.ref))
    );

    if (r.tier === "emergency") {
      const live = el("div", { class: "card" });
      live.appendChild(el("div", { class: "panel-title" },
        el("span", { class: "pulse-dot" }), "Live dispatch status"));
      live.appendChild(el("div", { id: "dispatch-summary", class: "small muted" }, "Starting parallel routing…"));
      const feed = el("ul", { class: "status-feed", id: "status-feed" });
      (snapshot.status_messages || []).forEach((m) => {
        const item = statusItem(m);
        item.dataset.mid = String(m.id);
        feed.appendChild(item);
      });
      live.appendChild(feed);
      wrap.appendChild(live);

      const guidance = el("div", { class: "card mt1" });
      guidance.appendChild(el("div", { class: "panel-title" }, "While you wait — do this now"));
      const gl = el("ul", { class: "is-list" });
      (snapshot.prompts || [])
        .filter((p) => p.key.startsWith("g_") || p.key.startsWith("u_") || p.key.startsWith("r_"))
        .forEach((p) => gl.appendChild(el("li", {}, p.text)));
      guidance.appendChild(gl);
      wrap.appendChild(guidance);

      if (snapshot.dispatch) renderDispatch(snapshot.dispatch);
    } else {
      const guidance = el("div", { class: "card" });
      guidance.appendChild(el("div", { class: "panel-title" }, "Care advice"));
      const gl = el("ul", { class: "is-list" });
      (snapshot.prompts || [])
        .filter((p) => p.key.startsWith("g_") || p.key.startsWith("u_") || p.key.startsWith("r_"))
        .forEach((p) => gl.appendChild(el("li", {}, p.text)));
      guidance.appendChild(gl);
      wrap.appendChild(guidance);

      const feed = (snapshot.status_messages || []);
      if (feed.length) {
        const logCard = el("div", { class: "card mt1" });
        logCard.appendChild(el("div", { class: "panel-title" }, "This call"));
        const ul = el("ul", { class: "status-feed" });
        feed.forEach((m) => ul.appendChild(statusItem(m)));
        logCard.appendChild(ul);
        wrap.appendChild(logCard);
      }

      wrap.appendChild(el("div", { class: "flex mt2" },
        el("button", { class: "btn secondary", onclick: () => press("1") }, "Finish call"),
        el("a", { class: "btn ghost", href: "/triage" }, "Start a new triage")));
    }
    return wrap;
  }

  function endedPane() {
    const wrap = el("div", { class: "card" });
    wrap.appendChild(el("h2", { class: "mt0" }, "Call ended"));
    wrap.appendChild(el("p", {}, "Thank you for using JeevanRekha. Your reference is ",
      el("strong", { class: "mono" }, snapshot.ref),
      ". Every call is logged and reviewable by health workers in your area."));
    if (snapshot.result && snapshot.result.tier === "emergency") {
      wrap.appendChild(el("div", { class: "notice bad" },
        "If this was an emergency, help routing continues even after this screen closes. ",
        "Keep the phone with you. If you can arrange any transport, leave for the nearest hospital now."));
      const live = el("div", { id: "dispatch-summary", class: "small muted mt1" }, "");
      wrap.appendChild(live);
      const feed = el("ul", { class: "status-feed", id: "status-feed" });
      wrap.appendChild(feed);
      if (snapshot.dispatch) renderDispatch(snapshot.dispatch);
    }
    wrap.appendChild(el("a", { class: "btn mt1", href: "/triage" }, "Start a new triage"));
    return wrap;
  }

  function statusItem(m) {
    let cls = "";
    if (m.key.startsWith("st_amb_confirmed") || m.key.startsWith("st_backup_confirmed") || m.key === "st_both_confirmed") cls = "good";
    else if (m.key.startsWith("st_exhausted") || m.key.startsWith("st_operator")) cls = "bad";
    else if (m.key.startsWith("st_timeout") || m.key.startsWith("st_amb_timeout")) cls = "alert";
    return el("li", { class: cls }, el("span", { class: "at" }, m.at || ""), m.text);
  }

  function appendStatus(m) {
    const feed = document.getElementById("status-feed");
    if (!feed) return;
    if ([...feed.children].some((li) => li.dataset.mid === String(m.id))) return;
    const item = statusItem(m);
    item.dataset.mid = String(m.id);
    feed.appendChild(item);
    item.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function renderDispatch(d) {
    const box = document.getElementById("dispatch-summary");
    if (!box || !d) return;
    box.innerHTML = "";
    const row = (label, t) => {
      const chipCls = t.state === "confirmed" ? "confirmed"
        : t.state === "waiting" ? "waiting"
        : t.state === "exhausted" ? "exhausted"
        : t.state === "stood_down" ? "confirmed" : "neutral";
      const stateText = t.state === "confirmed" ? `CONFIRMED ${t.confirmed_at || ""}`
        : t.state === "waiting" ? `requested — awaiting confirmation${t.current_contact ? ` (${t.current_contact})` : ""}`
        : t.state === "stood_down" ? "stood down — the other route confirmed"
        : t.state === "exhausted" ? "no confirmation — escalated to operator"
        : "—";
      return el("div", { class: "flex-between", style: "padding:0.3rem 0;border-bottom:1px dashed var(--line)" },
        el("span", {}, label),
        el("span", { class: `chip ${chipCls}` }, stateText));
    };
    box.appendChild(row("Track A · Ambulance", d.ambulance));
    box.appendChild(row("Track B · Local backup", d.backup));
    if (d.operator_alerts > 0) {
      box.appendChild(el("div", { class: "notice bad mt1 mb0" },
        `Operator alerted (${d.operator_alerts}). A human is following up manually.`));
    }
  }

  function t(key) {
    const node = document.getElementById(`i18n-${key}`);
    return node ? node.textContent : key;
  }

  async function press(digit) {
    const btns = document.querySelectorAll(".answer-btn, .lang-btn, .btn");
    btns.forEach((b) => (b.disabled = true));
    snapshot = await FlowClient.digit(digit);
    if (snapshot.state === "ended") FlowClient.unsubscribe();
    render();
  }

  // Keyboard support: 1/2/3 like a phone.
  document.addEventListener("keydown", (e) => {
    if (flowPane.hidden) return;
    if (["1", "2", "3"].includes(e.key)) press(e.key);
  });
})();
