const $ = (id) => document.getElementById(id);
const state = { reportId: null, analysis: null, history: [], busy: false, profiles: [], profileId: null };
const profile = () => state.profiles.find((p) => p.id === state.profileId);

const URGENCY = {
  routine: ["✅", "Everything looks within normal range.", "Keep up with routine check-ups."],
  see_doctor_soon: ["⚠️", "Some results are outside the normal range.", "Not an emergency, but worth discussing with your doctor soon."],
  urgent: ["🚨", "Some results may need prompt attention.", "Please contact your doctor or care team as soon as possible."],
};

// ---------- utils ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function md(s) {
  // tiny, safe markdown: escape first, then bold, italics, bullet lists and paragraphs
  const lines = esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|\s)_(.+?)_(?=\s|$)/g, "$1<i>$2</i>").split("\n");
  let html = "", inList = false;
  for (const line of lines) {
    const m = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)/);
    if (m) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${m[1]}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.trim()) html += `<p>${line}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 4500);
}
function show(view) {
  for (const v of ["uploadView", "loadingView", "resultView"]) $(v).classList.toggle("hidden", v !== view);
  window.scrollTo({ top: 0 });
}

// ---------- status pills ----------
async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    $("netPill").className = "pill " + (s.internet ? "online" : "offline");
    $("netText").textContent = s.internet ? "Internet on · nothing is sent" : "Offline · running 100% on-device";
    $("modelPill").className = "pill " + (s.ollama && s.model_ready ? "good" : "bad");
    $("modelText").textContent = !s.ollama ? "Local AI not running" : s.model_ready ? `${s.model} · local` : `${s.model} not downloaded`;
  } catch {
    $("netText").textContent = "Server not reachable";
  }
}
refreshStatus();
setInterval(refreshStatus, 3000);

// ---------- upload ----------
const drop = $("drop");
["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (ev) => ev.dataTransfer.files[0] && analyze(ev.dataTransfer.files[0]));
$("fileInput").addEventListener("change", (ev) => ev.target.files[0] && analyze(ev.target.files[0]));
$("sampleBtn").addEventListener("click", async (ev) => {
  ev.preventDefault();
  ev.stopPropagation();
  const r = await fetch("/api/sample");
  if (!r.ok) return toast("Sample not found. Run: python samples/make_sample.py");
  analyze(new File([await r.blob()], "sample_lab_report.pdf", { type: "application/pdf" }));
});

async function analyze(file) {
  if (!state.profileId) return toast("Create a profile first.");
  show("loadingView");
  $("loadingFile").textContent = file.name;
  const steps = [...$("steps").children];
  const t0 = performance.now();
  const tick = setInterval(() => {
    const sec = (performance.now() - t0) / 1000;
    $("timer").textContent = sec.toFixed(1);
    // advance the visual steps on a rough schedule; the last one waits for the model
    const i = Math.min(steps.length - 1, Math.floor(sec / 2.5));
    steps.forEach((li, k) => (li.className = k < i ? "done" : k === i ? "active" : ""));
  }, 100);

  const form = new FormData();
  form.append("file", file);
  form.append("profile_id", state.profileId);
  try {
    const r = await fetch("/api/analyze", { method: "POST", body: form });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Analysis failed");
    render(data);
    loadProfiles();
  } catch (e) {
    toast(e.message);
    show("uploadView");
  } finally {
    clearInterval(tick);
    $("fileInput").value = "";
  }
}

// ---------- results ----------
function parseRange(ref) {
  const s = (ref || "").replace(/,/g, "");
  let m = s.match(/(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)/);
  if (m) return [+m[1], +m[2]];
  m = s.match(/<\s*(-?\d+(?:\.\d+)?)/);
  if (m) return [0, +m[1]];
  m = s.match(/>\s*(-?\d+(?:\.\d+)?)/);
  if (m) return [+m[1], +m[1] * 2];
  return null;
}
function rangeBar(value, ref) {
  const range = parseRange(ref);
  const v = parseFloat(String(value).replace(/,/g, ""));
  if (!range || isNaN(v) || range[1] === range[0]) return "";
  const [lo, hi] = range;
  // normal band occupies 25%..75% of the bar
  const pos = Math.max(3, Math.min(97, 25 + ((v - lo) / (hi - lo)) * 50));
  return `<div class="bar" title="Normal range ${esc(ref)}"><div class="band"></div><div class="mark" style="left:${pos}%"></div></div>`;
}

function renderResults() {
  const only = $("onlyFlagged").checked;
  const rows = (state.analysis.results || []).filter((r) => !only || r.status !== "normal");
  $("results").innerHTML = rows.length
    ? rows.map((r) => `
      <div class="result ${esc(r.status)}">
        <div>
          <div class="r-name">${esc(r.test)}</div>
          <div class="r-value">${esc(r.value)} <small>${esc(r.unit)}</small></div>
        </div>
        <div class="r-right">
          <div class="r-top">
            <span class="badge ${esc(r.status)}">${esc(r.status)}</span>
            ${r.verified ? '<span class="verified" title="Checked by code against the range printed on the report, not guessed by the AI">✓ range-checked</span>' : ""}
          </div>
          ${rangeBar(r.value, r.reference_range)}
          <div class="r-ref">Normal: ${esc(r.reference_range || "not given")}</div>
        </div>
        <div class="r-meaning">${esc(r.meaning)}</div>
      </div>`).join("")
    : `<p class="r-ref">No results to show.</p>`;
}

function render(data) {
  const a = data.analysis;
  state.reportId = data.report_id;
  state.analysis = a;
  state.history = [];

  $("fileName").textContent = data.filename;
  $("reportType").textContent = [a.report_type || "Medical report", data.date].filter(Boolean).join(" · ");
  const [icon, title, sub] = URGENCY[a.urgency] || URGENCY.routine;
  $("urgency").className = "urgency " + (a.urgency || "routine");
  $("urgency").innerHTML = `<span style="font-size:22px">${icon}</span><div>${esc(title)}<small>${esc(sub)}</small></div>`;
  $("summary").textContent = a.summary;

  const results = a.results || [];
  const flagged = results.filter((r) => r.status !== "normal");
  $("statTotal").textContent = results.length;
  $("statFlag").textContent = flagged.length;
  $("statTime").textContent = data.stats.seconds ? data.stats.seconds + "s" : "—";
  $("findings").innerHTML = (a.key_findings || []).map((f) => `<li>${esc(f)}</li>`).join("");
  $("questions").innerHTML = (a.questions_for_doctor || []).map((q) => `<button class="q">${esc(q)}</button>`).join("")
    || `<p class="r-ref">Ask the agent to help you prepare questions.</p>`;
  $("questions").querySelectorAll(".q").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));

  const chips = [...flagged.slice(0, 2).map((r) => `Why is my ${r.test} ${r.status}?`),
    "Could my medicines explain any of this?", "How have my results changed over time?"];
  $("chips").innerHTML = chips.map((c) => `<button class="chip">${esc(c)}</button>`).join("");
  $("chips").querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));

  $("chatLog").innerHTML = `<div class="msg bot">I've read your report. Ask me anything. I can check your medicines, compare with your past reports and look things up in my offline medical reference.</div>`;
  const p = profile();
  $("profileNote").textContent = p
    ? `👤 ${p.name}${p.medications?.length ? " · 💊 " + p.medications.join(", ") : " · no medicines listed"}`
    : "";
  fetch(`/api/profiles/${state.profileId}/reports`).then((r) => r.json()).then((rows) => {
    const past = rows.filter((x) => x.report_id !== data.report_id && (!data.date || (x.date || "") <= data.date));
    $("historyNote").textContent = past.length
      ? `📁 ${past.length} earlier report${past.length > 1 ? "s" : ""} for trends (${past.map((x) => x.date).join(", ")})`
      : "📁 No earlier reports yet. Upload more to see trends.";
  }).catch(() => {});

  $("perf").textContent = data.stats.model
    ? `Analyzed by ${data.stats.model} on this machine in ${data.stats.seconds}s` +
      (data.stats.tokens_per_sec ? ` · ${data.stats.tokens_per_sec} tokens/sec` : "") + " · 0 bytes sent to any cloud service"
    : "Imported report · stored only on this device";
  renderResults();
  show("resultView");
}
$("onlyFlagged").addEventListener("change", renderResults);

// ---------- agent chat ----------
function addMsg(role, html) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = html;
  $("chatLog").appendChild(div);
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
  return div;
}

function stepHtml(e) {
  const obs = esc(e.observation).replace(/\[THIS RESULT IS OUT OF RANGE\]/g, '<span class="hit">⚠ affects an out-of-range result</span>');
  return `<div class="step-head"><span class="n">${e.n}</span><code>${esc(e.tool)}</code>(${esc(e.input)})</div>
    <div class="step-thought">${esc(e.thought)}</div><div class="step-obs">${obs}</div>`;
}

async function ask(text) {
  text = text.trim();
  if (!text || state.busy || !state.reportId) return;
  state.busy = true;
  $("chatInput").value = "";
  addMsg("user", esc(text));

  const trace = document.createElement("div");
  trace.className = "trace";
  $("chatLog").appendChild(trace);
  const thinking = document.createElement("div");
  thinking.className = "thinking";
  thinking.textContent = "Agent is planning…";
  trace.appendChild(thinking);
  let bubble = null, reply = "", steps = 0;

  try {
    const r = await fetch("/api/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ report_id: state.reportId, question: text, history: state.history }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "Agent failed");
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const e = JSON.parse(line);
        if (e.type === "step") {
          steps++;
          const div = document.createElement("div");
          div.className = "step";
          div.innerHTML = stepHtml(e);
          trace.insertBefore(div, thinking);
          thinking.textContent = "Agent is deciding the next step…";
        } else if (e.type === "answer_start") {
          thinking.remove();
          bubble = addMsg("bot typing", "");
        } else if (e.type === "token") {
          reply += e.text;
          bubble.innerHTML = md(reply);
        } else if (e.type === "done" && bubble) {
          bubble.innerHTML = md(reply) + `<span class="meta">${steps} tool step${steps === 1 ? "" : "s"} · ${e.seconds}s · on-device</span>`;
        }
        $("chatLog").scrollTop = $("chatLog").scrollHeight;
      }
    }
    state.history.push({ role: "user", content: text }, { role: "assistant", content: reply });
  } catch (e) {
    thinking.remove();
    (bubble || addMsg("bot", "")).textContent = "⚠️ " + e.message;
  } finally {
    if (bubble) bubble.classList.remove("typing");
    state.busy = false;
  }
}
$("chatForm").addEventListener("submit", (ev) => { ev.preventDefault(); ask($("chatInput").value); });

// ---------- report actions ----------
$("newBtn").addEventListener("click", () => { show("uploadView"); loadPast(); });
$("forgetBtn").addEventListener("click", async () => {
  if (!state.reportId || !confirm("Delete this report from this device?")) return;
  await fetch(`/api/report/${state.reportId}`, { method: "DELETE" });
  state.reportId = null;
  state.analysis = null;
  state.history = [];
  show("uploadView");
  loadProfiles();
  toast("Report deleted from this device.");
});

// ---------- profiles ----------
async function loadProfiles() {
  state.profiles = await (await fetch("/api/profiles")).json();
  let saved = null;
  try { saved = localStorage.getItem("carelens.profile"); } catch {}
  if (!state.profiles.some((p) => p.id === state.profileId)) {
    state.profileId = state.profiles.some((p) => p.id === saved) ? saved : state.profiles[0]?.id || null;
  }
  $("profileSelect").innerHTML = state.profiles.length
    ? state.profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")
    : `<option value="">No profiles</option>`;
  $("profileSelect").value = state.profileId || "";
  $("editProfileBtn").disabled = !state.profileId;
  loadPast();
}

async function loadPast() {
  const p = profile();
  $("pastCard").classList.toggle("hidden", !p);
  if (!p) return;
  $("pastTitle").textContent = `Reports for ${p.name}`;
  const rows = await (await fetch(`/api/profiles/${p.id}/reports`)).json();
  $("pastList").innerHTML = rows.length
    ? rows.map((r) => `<button class="past-item" data-id="${esc(r.report_id)}">
        <span class="date">${esc(r.date || "no date")}</span>
        <span class="file">${esc(r.filename)}</span>
        <span class="badge ${r.flagged ? "high" : "normal"}" title="Results outside the normal range printed on this report">${r.flagged ? `${r.flagged} of ${r.total} out of range` : "All in range"}</span>
      </button>`).join("")
    : `<p class="past-empty">No reports yet. Upload one above; it'll be saved to this profile on this device.</p>`;
  $("pastList").querySelectorAll(".past-item").forEach((b) => b.addEventListener("click", () => openReport(b.dataset.id)));
}

async function openReport(id) {
  const r = await fetch(`/api/reports/${id}`);
  if (!r.ok) return toast("Could not open that report.");
  render(await r.json());
}

$("profileSelect").addEventListener("change", (e) => {
  state.profileId = e.target.value;
  try { localStorage.setItem("carelens.profile", state.profileId); } catch {}
  show("uploadView");
  loadPast();
});

function openProfileDialog(p) {
  const f = $("profileForm");
  f.dataset.id = p?.id || "";
  $("profileDialogTitle").textContent = p ? "Edit profile" : "New profile";
  f.name.value = p?.name || "";
  f.age.value = p?.age ?? "";
  f.sex.value = p?.sex || "";
  f.medications.value = (p?.medications || []).join(", ");
  f.conditions.value = (p?.conditions || []).join(", ");
  $("deleteProfileBtn").classList.toggle("hidden", !p);
  $("profileDialog").showModal();
}
$("newProfileBtn").addEventListener("click", () => openProfileDialog(null));
$("editProfileBtn").addEventListener("click", () => openProfileDialog(profile()));
$("cancelProfileBtn").addEventListener("click", () => $("profileDialog").close());

$("profileForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const split = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const r = await fetch("/api/profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: f.dataset.id || null,
      name: f.name.value,
      age: f.age.value ? +f.age.value : null,
      sex: f.sex.value || null,
      medications: split(f.medications.value),
      conditions: split(f.conditions.value),
    }),
  });
  if (!r.ok) return toast("Could not save profile.");
  const saved = await r.json();
  state.profileId = saved.id;
  try { localStorage.setItem("carelens.profile", saved.id); } catch {}
  $("profileDialog").close();
  await loadProfiles();
  if (state.reportId) {
    $("profileNote").textContent = `👤 ${saved.name}${saved.medications.length ? " · 💊 " + saved.medications.join(", ") : ""}`;
  }
  toast("Profile saved on this device.");
});

$("deleteProfileBtn").addEventListener("click", async () => {
  const p = profile();
  if (!p || !confirm(`Delete ${p.name} and all their reports from this device?`)) return;
  await fetch(`/api/profiles/${p.id}`, { method: "DELETE" });
  state.profileId = null;
  $("profileDialog").close();
  show("uploadView");
  await loadProfiles();
  toast("Profile deleted.");
});

loadProfiles();
