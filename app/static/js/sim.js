/* Phone-call simulator console.
   Left: the handset (transcript + DTMF keypad). Right: the dispatch desk
   where you play every responder the system contacts. Both sides are real:
   the desk drives the same confirm/decline service calls a production
   provider webhook would. */

(function () {
  const root = document.getElementById("sim-app");
  if (!root) return;

  const LOCALES = JSON.parse(root.dataset.locales || "{}");
  let snapshot = null;
  let deskTimer = null;

  const screen = document.getElementById("handset-screen");
  const regionSel = document.getElementById("sim-region");
  const phoneInp = document.getElementById("sim-phone");
  const callBtn = document.getElementById("sim-call-btn");
  const hangupBtn = document.getElementById("sim-hangup");
  const silenceBtn = document.getElementById("sim-silence");
  const speakToggle = document.getElementById("sim-speak");
  const deskBox = document.getElementById("desk-items");
  const opsBox = document.getElementById("desk-operator");
  const caseBox = document.getElementById("desk-case");

  function line(cls, text) {
    const div = el("div", { class: cls }, text);
    screen.appendChild(div);
    screen.scrollTop = screen.scrollHeight;
    return div;
  }

  function renderPrompts(prompts) {
    (prompts || []).forEach((p) => {
      let cls = "ivr";
      if (p.key.startsWith("st_")) cls = "sys";
      if (p.key === "st_exhausted" || p.key === "st_operator_alerted") cls = "badline";
      if (p.key.startsWith("st_amb_confirmed") || p.key.startsWith("st_backup_confirmed") || p.key === "st_both_confirmed") cls = "goodline";
      if (p.key === "ivr_unclear_ack" || p.key === "ivr_invalid" || p.key === "ivr_silence") cls = "alertline";
      line(cls, "▸ " + p.text);
      if (speakToggle.checked) {
        Speech.setLocale(LOCALES[snapshot?.language] || "en-IN");
        Speech.speak(p.text);
      }
    });
  }

  callBtn.addEventListener("click", async () => {
    screen.innerHTML = "";
    FlowClient.unsubscribe();
    line("sys", "— outbound call placed to JeevanRekha helpline —");
    line("sys", "— ring… ring… connected —");
    snapshot = await FlowClient.start({
      region_id: regionSel.value ? Number(regionSel.value) : null,
      caller_phone: phoneInp.value.trim() || randomPhone(),
      channel: "phone_sim",
    });
    line("sys", `call ref ${snapshot.ref}`);
    renderPrompts(snapshot.prompts);
    FlowClient.subscribe(onServerEvent);
    setPhoneEnabled(true);
    refreshDesk();
  });

  function randomPhone() {
    return "9" + Math.floor(100000000 + Math.random() * 899999999);
  }

  function onServerEvent(ev) {
    if (ev.type === "message") {
      let cls = "sys";
      if (ev.key.startsWith("st_amb_confirmed") || ev.key.startsWith("st_backup_confirmed") || ev.key === "st_both_confirmed") cls = "goodline";
      else if (ev.key.startsWith("st_exhausted") || ev.key.startsWith("st_operator")) cls = "badline";
      else if (ev.key.startsWith("st_timeout") || ev.key.startsWith("st_amb_timeout")) cls = "alertline";
      line(cls, `[${ev.at || ""}] ${ev.text}`);
      if (speakToggle.checked) Speech.speak(ev.text);
    } else if (ev.type === "dispatch") {
      renderCase(ev.dispatch);
      refreshDesk();
    } else if (ev.type === "ended") {
      line("sys", "— call ended —");
    }
  }

  async function press(digit) {
    if (!snapshot) return;
    line("you", `you pressed ${digit}`);
    snapshot = await FlowClient.digit(digit);
    renderPrompts(snapshot.step_prompts && snapshot.step_prompts.length
      ? snapshot.step_prompts
      : snapshot.prompts);
    if (snapshot.state === "ended") {
      line("sys", "— call ended —");
      setPhoneEnabled(false);
    }
    refreshDesk();
  }

  document.querySelectorAll(".key").forEach((k) => {
    k.addEventListener("click", () => press(k.dataset.key));
  });

  silenceBtn.addEventListener("click", async () => {
    if (!snapshot) return;
    line("you", "— silence (no input) —");
    snapshot = await FlowClient.silence();
    renderPrompts(snapshot.step_prompts && snapshot.step_prompts.length
      ? snapshot.step_prompts
      : snapshot.prompts);
    refreshDesk();
  });

  hangupBtn.addEventListener("click", async () => {
    if (!snapshot) return;
    line("you", "— you hung up —");
    snapshot = await FlowClient.hangup();
    FlowClient.unsubscribe();
    if (snapshot.result) {
      line("sys", `final triage: ${snapshot.result.tier.toUpperCase()} (logged)`);
    }
    setPhoneEnabled(false);
    refreshDesk();
  });

  function setPhoneEnabled(on) {
    document.querySelectorAll(".key").forEach((k) => (k.disabled = !on));
    silenceBtn.disabled = !on;
    hangupBtn.disabled = !on;
  }
  setPhoneEnabled(false);

  /* ---------------- dispatch desk ---------------- */

  async function refreshDesk() {
    try {
      const res = await fetch("/api/sim/desk");
      const desk = await res.json();
      renderDesk(desk);
    } catch { /* ignore */ }
  }

  function renderDesk(desk) {
    deskBox.innerHTML = "";
    if (!desk.alerts.length) {
      deskBox.appendChild(el("div", { class: "desk-empty" },
        "No outbound alerts waiting. Start an emergency call and the system will ring the ambulance node and the local backup chain here."));
    }
    desk.alerts.forEach((a) => {
      const item = el("div", { class: "desk-item" });
      item.appendChild(el("div", { class: "flex-between" },
        el("span", { class: "who" }, `${a.contact_name || "Unknown"}`),
        el("span", { class: `chip ${a.track === "ambulance" ? "emergency" : "urgent"}` },
          a.track === "ambulance" ? "Track A · ambulance" : "Track B · backup")));
      item.appendChild(el("div", { class: "meta" },
        `${a.contact_phone || ""} · call ${a.call_ref} · attempt ${a.attempt} · window closes ${a.due_at} · ${a.region}`));
      const actions = el("div", { class: "desk-actions" });
      actions.appendChild(el("button", {
        class: "btn", onclick: () => deskAction(a.event_id, "confirm"),
      }, "✓ Confirm — help is moving"));
      actions.appendChild(el("button", {
        class: "btn ghost", onclick: () => deskAction(a.event_id, "decline"),
      }, "Cannot help"));
      item.appendChild(actions);
      deskBox.appendChild(item);
    });

    opsBox.innerHTML = "";
    desk.operator_alerts.forEach((o) => {
      const item = el("div", { class: "desk-item operator" });
      item.appendChild(el("div", { class: "who" },
        `OPERATOR FOLLOW-UP REQUIRED · call ${o.call_ref}`));
      item.appendChild(el("div", { class: "meta" }, `${o.at} — ${o.action}: ${o.detail || ""}`));
      const actions = el("div", { class: "desk-actions" });
      actions.appendChild(el("button", {
        class: "btn danger", onclick: () => deskAction(o.event_id, "ack"),
      }, "Acknowledge — handling manually"));
      item.appendChild(actions);
      opsBox.appendChild(item);
    });
  }

  async function deskAction(eventId, action) {
    await fetch(`/api/sim/desk/${eventId}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ by: "dispatch-desk" }),
    });
    line("sys", `— dispatch desk: ${action} on alert #${eventId} —`);
    refreshDesk();
  }

  function renderCase(d) {
    if (!caseBox) return;
    caseBox.innerHTML = "";
    if (!d) { caseBox.appendChild(el("div", { class: "desk-empty" }, "No active dispatch case.")); return; }
    const row = (label, t) => el("div", { class: "flex-between small", style: "padding:0.25rem 0" },
      el("span", {}, label),
      el("span", { class: `chip ${t.state === "confirmed" ? "confirmed" : t.state === "waiting" ? "waiting" : t.state === "exhausted" ? "exhausted" : t.state === "stood_down" ? "confirmed" : "neutral"}` },
        t.state === "confirmed" ? `confirmed ${t.confirmed_at || ""}` :
        t.state === "waiting" ? `awaiting confirmation (${t.attempts} attempt${t.attempts > 1 ? "s" : ""})` :
        t.state === "stood_down" ? "stood down (other track confirmed)" :
        t.state === "exhausted" ? "exhausted → operator" : "—"));
    caseBox.appendChild(el("div", { class: "panel-title" }, `Dispatch case #${d.case_id} · `,
      el("span", { class: `chip ${d.status}` }, d.status)));
    caseBox.appendChild(row("Track A · Ambulance", d.ambulance));
    caseBox.appendChild(row("Track B · Backup", d.backup));
  }

  deskTimer = setInterval(refreshDesk, 4000);
  refreshDesk();
  renderCase(null);
})();
