"use strict";
const $ = (id) => document.getElementById(id);
const ui = (text) => window.VeyqI18N.text(text);
async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch {}
  }
  // Native HTML documents may have no secure browser origin. Copy still runs
  // only from a user's click; no clipboard content is read by this fallback.
  const previous=document.activeElement, field=document.createElement('textarea');
  field.className='clipboard-copy'; field.value=text; field.readOnly=true;
  document.body.append(field); field.select();
  try {
    if (!document.execCommand('copy')) throw Error(ui('Select the text and press Ctrl+C'));
  } finally { field.remove(); previous?.focus(); }
}
let projects = [],
  assignmentSession = "",
  activeSession = "",
  sessionLoad = 0,
  sessionsRefresh = 0,
  liveDrafts = new Map(),
  questionId = "",
  infoModel = "",
  modelBusy = false;
let api,
  sessionId = "",
  settings = {},
  sessions = [],
  busy = false,
  polling = false;
let lastSeq = 0,
  streamNode = null,
  streamText = "",
  approvalId = "",
  updateReady = false;
let voiceActive = false, voicePhase = 'idle', voiceSession = '', voiceTimer = null, voicePolling = false, voiceGeneration = 0;
let explorerPath = ".",
  selectedFile = "",
  previewLine = 1,
  previewTotal = 0;
const modalFocus = new Map();
const modes = {
  always: "Chiedi sempre",
  auto: "Approva per me",
  full: "Accesso completo",
};
function toast(text) {
  $("toast").textContent = ui(text);
  $("toast").style.display = "block";
  setTimeout(() => ($("toast").style.display = "none"), 6500);
}
async function call(name, ...args) {
  if (!api)
    throw Error("Apri l’app con Veynuq.bat per usare il motore locale.");
  return await api[name](...args);
}
function action(id, fn) {
  $(id).addEventListener("click", () =>
    Promise.resolve()
      .then(fn)
      .catch((e) => toast(e.message || e)),
  );
}
function open(id) {
  if (!$(id).classList.contains("open"))
    modalFocus.set(id, document.activeElement);
  $(id).classList.add("open");
  const focus =
    id === "approvalModal"
      ? $("deny")
      : $(id).querySelector("input, select, button");
  focus?.focus({ preventScroll: true });
}
function close(id) {
  $(id).classList.remove("open");
  modalFocus.get(id)?.focus();
  modalFocus.delete(id);
}
function setBusy(value, state) {
  const changed = busy !== value;
  busy = value;
  updateSendButton();

  $("state").textContent = state || (value ? "In esecuzione" : "Pronto");
  $("status").classList.toggle("busy", value);
  for (const id of [
    "newChat",
    "chooseFolder",
    "exploreFiles",
    "settingsButton",
    "newProject",
    "projectAssignment",
  ])
    $(id).disabled = value;
  $("prompt").disabled =
    value && !!activeSession && activeSession !== sessionId;
  if (changed) renderSessions();
  updateChatActions();
}
function updateSendButton() {
  if (busy && activeSession && activeSession !== sessionId) {
    $("send").textContent = ui("Return to active chat");
    $("send").classList.remove("danger", "follow-up");
    return;
  }
  const follow = busy && $("prompt").value.trim();
  $("send").textContent = ui(
    busy ? (follow ? "Send follow-up ↑" : "Ferma") : "Invia ↑",
  );
  $("send").classList.toggle("danger", busy && !follow);
  $("send").classList.toggle("follow-up", !!follow);
}
function codeBlock(parent, text, language) {
  const wrap = document.createElement("div");
  wrap.className = "codebox";
  const btn = document.createElement("button");
  btn.className = "copy";
  btn.textContent = "Copia " + (language || "");
  btn.addEventListener("click", () =>
    copyText(text)
      .then(() => toast("Copiato"))
      .catch(() => toast("Seleziona il testo e usa Ctrl+C")),
  );
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.textContent = text;
  pre.append(code);
  wrap.append(btn, pre);
  parent.append(wrap);
}
// Markdown is sanitized locally before it enters the DOM. No model HTML,
// embedded images, scripts, forms or bridge calls are allowed.
function renderText(parent, text) {
  const box = parent.closest(".message");
  if (box) box.dataset.raw = String(text);
  const dirty = marked.parse(String(text), { breaks: true, gfm: true });
  const clean = DOMPurify.sanitize(dirty, {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS: [
      "p",
      "br",
      "hr",
      "h1",
      "h2",
      "h3",
      "h4",
      "h5",
      "h6",
      "ul",
      "ol",
      "li",
      "strong",
      "em",
      "del",
      "blockquote",
      "pre",
      "code",
      "table",
      "thead",
      "tbody",
      "tr",
      "th",
      "td",
      "a",
    ],
    ALLOWED_ATTR: ["href", "title", "class"],
    ALLOW_DATA_ATTR: false,
  });
  parent.replaceChildren(clean);
  for (const a of parent.querySelectorAll("a")) {
    const href = a.getAttribute("href") || "";
    a.removeAttribute("href");
    a.setAttribute("role", "link");
    a.tabIndex = 0;
    const follow = () =>
      call("open_external", href).catch((e) => toast(e.message));
    a.addEventListener("click", follow);
    a.addEventListener("keydown", (e) => {
      if (e.key === "Enter") follow();
    });
  }
  for (const code of parent.querySelectorAll("pre code")) {
    const raw = code.textContent;
    try {
      hljs.highlightElement(code);
    } catch {}
    const button = document.createElement("button");
    button.className = "copy";
    button.textContent = ui("Copy");
    button.addEventListener("click", () =>
      copyText(raw)
        .then(() => toast("Copied"))
        .catch((e) => toast(e.message)),
    );
    code.parentElement.prepend(button);
  }
}
function message(role, text, historyIndex = null) {
  $("welcome").hidden = true;
  const box = document.createElement("article");
  box.className = "message " + role;
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "user" ? "Tu" : "Veynuq";
  const body = document.createElement("div");
  body.className = "message-body";
  box.dataset.raw = text;
  if (historyIndex !== null) box.dataset.historyIndex = historyIndex;
  renderText(body, text);
  box.append(label, body);
  const actions = document.createElement("div");
  actions.className = "message-actions";
  const copy = document.createElement("button");
  copy.textContent = ui("Copy Markdown");
  copy.addEventListener("click", () =>
    copyText(box.dataset.raw)
      .then(() => {
        copy.textContent = ui("Copied!");
        setTimeout(() => (copy.textContent = ui("Copy Markdown")), 2000);
      })
      .catch((e) => toast(e.message)),
  );
  actions.append(copy);
  if (role === "assistant") {
    const regenerate = document.createElement("button");
    regenerate.textContent = ui("Regenerate");
    regenerate.addEventListener("click", async () => {
      if (busy || box.dataset.historyIndex === undefined) return;
      if (
        !confirm(
          ui(
            "Regenerate this response? Later chat messages will be removed. Executed actions will remain applied.",
          ),
        )
      )
        return;
      try {
        await call(
          "regenerate",
          sessionId,
          Number(box.dataset.historyIndex),
          true,
        );
        await loadSession(sessionId);
        setBusy(true);
      } catch (e) {
        toast(e.message);
      }
    });
    actions.append(regenerate);
  }
  box.append(actions);
  $("messages").append(box);
  scrollBottom();
  return body;
}
function scrollBottom() {
  const el = $("messages");
  el.scrollTop = el.scrollHeight;
}
function renderPlan(steps) {
  $("plan").replaceChildren();
  if (!steps?.length) {
    $("plan").hidden = true;
    return;
  }
  $("plan").hidden = false;
  const title = document.createElement("div");
  title.className = "eyebrow";
  title.textContent = "Piano";
  $("plan").append(title);
  steps.forEach((s) => {
    const item = document.createElement("div");
    item.className = "plan-item " + s.status;
    item.textContent =
      (s.status === "completed"
        ? "✓ "
        : s.status === "in_progress"
          ? "◉ "
          : "○ ") + s.step;
    $("plan").append(item);
  });
}
function activity(title, data, kind = "info") {
  $("activityEmpty")?.remove();
  const item = document.createElement("details");
  item.className = "log-" + kind;
  const summary = document.createElement("summary");
  summary.textContent = title;
  const pre = document.createElement("pre");
  pre.textContent =
    typeof data === "string" ? data : JSON.stringify(data, null, 2);
  item.append(summary, pre);
  $("activity").append(item);
  while ($("activity").children.length > 150) $("activity").firstChild.remove();
  $("activity").scrollTop = $("activity").scrollHeight;
}
async function refreshSessions() {
  const request = ++sessionsRefresh;
  const [nextSessions, nextProjects] = await Promise.all([
    call("get_sessions", $("search").value),
    call("get_projects"),
  ]);
  if (request !== sessionsRefresh) return;
  sessions = nextSessions;
  projects = nextProjects;
  renderSessions();
}
function renderSessions() {
  const host = $("sessions");
  host.replaceChildren();
  for (const group of [{ id: "", name: ui("No project") }, ...projects]) {
    const items = sessions.filter((s) => (s.project_id || "") === group.id);
    if (!items.length) continue;
    const details = document.createElement("details");
    details.className = "project-group";
    details.open =
      !!$("search").value ||
      !(settings.collapsed_projects || []).includes(group.id);
    details.addEventListener("toggle", () => {
      if ($('search').value) return;
      const collapsed = new Set(settings.collapsed_projects || []);
      if (details.open === !collapsed.has(group.id)) return;
      if (details.open) collapsed.delete(group.id); else collapsed.add(group.id);
      settings.collapsed_projects = [...collapsed];
      call('set_project_group_open', group.id, details.open).catch(e => toast(e.message));
    });
    const summary = document.createElement("summary");
    summary.textContent = group.name + " · " + items.length;
    details.append(summary);
    for (const s of items) {
      const row = document.createElement("div");
      row.className = "session-row";
      const btn = document.createElement("button");
      btn.className = "session" + (s.id === sessionId ? " active" : "");
      const title = document.createElement("span");
      title.textContent = s.untitled ? ui("New activity") : s.title;
      btn.append(title);
      btn.title = title.textContent;
      btn.addEventListener("click", () =>
        loadSession(s.id).catch((e) => toast(e.message)),
      );
      const more = document.createElement("button");
      more.className = "session-more";
      more.textContent = "⋯";
      more.title = ui("More chat actions");
      more.disabled = busy;
      const menu = document.createElement("div");
      menu.className = "session-menu";
      menu.hidden = true;
      for (const [label, fn] of [
        [
          "Add to project",
          async () => {
            assignmentSession = s.id;
            $("chatProjectChoice").replaceChildren();
            for (const p of [{ id: "", name: ui("No project") }, ...projects]) {
              const option = document.createElement("option");
              option.value = p.id;
              option.textContent = p.name;
              $("chatProjectChoice").append(option);
            }
            $("chatProjectChoice").value = s.project_id || "";
            open("chatProjectModal");
          },
        ],
        [
          "Rename",
          async () => {
            const name = prompt(ui("Chat name:"), s.title);
            if (name) {
              await call("rename_session", s.id, name);
              await refreshSessions();
            }
          },
        ],
        [
          "Delete chat",
          async () => {
            if (confirm(ui("Delete this local chat?"))) {
              await deleteChatById(s.id);
            }
          },
        ],
      ]) {
        const action = document.createElement("button");
        action.textContent = ui(label);
        action.addEventListener("click", () => {
          menu.hidden = true;
          fn().catch((e) => toast(e.message));
        });
        menu.append(action);
      }
      more.addEventListener("click", () => {
        const show = menu.hidden;
        document
          .querySelectorAll(".session-menu")
          .forEach((m) => (m.hidden = true));
        menu.hidden = !show;
        if (show) {
          const rect = more.getBoundingClientRect();
          menu.style.left =
            Math.max(8, Math.min(rect.left, window.innerWidth - 192)) + "px";
          menu.style.top =
            Math.max(
              8,
              Math.min(
                rect.bottom + 6,
                window.innerHeight - menu.offsetHeight - 12,
              ),
            ) + "px";
        }
      });
      row.append(btn, more, menu);
      details.append(row);
    }
    host.append(details);
  }
}
async function loadSession(id) {
  if (voiceActive && id !== voiceSession) await cancelDictation();
  const request = ++sessionLoad;
  const s = await call("get_session", id);
  if (!s || request !== sessionLoad) return;
  sessionId = id;
  $("messages")
    .querySelectorAll(".message")
    .forEach((n) => n.remove());
  $("welcome").hidden = s.history.some((m) => m.role === "user");
  for (const [index, m] of s.history.entries()) {
    if (["user", "assistant"].includes(m.role) && m.content)
      message(m.role, m.content, index);
  }
  showWorkspace(s.workspace || settings.workspace);
  $("projectAssignment").replaceChildren();
  for (const project of [{ id: "", name: ui("No project") }, ...projects]) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.name;
    $("projectAssignment").append(option);
  }
  $("projectAssignment").value = s.project_id || "";
  renderPlan(s.plan);
  streamNode = null;
  streamText = "";
  if (liveDrafts.has(id)) {
    streamText = liveDrafts.get(id);
    streamNode = message("assistant", streamText);
  }
  $("prompt").disabled = busy && !!activeSession && activeSession !== id;
  updateSendButton();
  updateChatActions();
  renderSessions();
}
function updateChatActions() {
  for (const id of ["renameChat", "deleteChat"])
    $(id).disabled = busy || !sessionId;
  $("exportChat").disabled = !sessionId;
}
function emptyChat() {
  ++sessionLoad;
  sessionId = "";
  streamNode = null;
  streamText = "";
  $("messages")
    .querySelectorAll(".message")
    .forEach((n) => n.remove());
  $("welcome").hidden = false;
  $("prompt").value = "";
  $("projectAssignment").replaceChildren();
  for (const p of [{ id: "", name: ui("No project") }, ...projects]) {
    const option = document.createElement("option");
    option.value = p.id;
    option.textContent = p.name;
    $("projectAssignment").append(option);
  }
  showWorkspace(settings.workspace);
  renderPlan([]);
  updateChatActions();
  updateSendButton();
  renderSessions();
}
async function deleteChatById(id) {
  const selected = id === sessionId;
  ++sessionsRefresh;
  await call("delete_session", id);
  liveDrafts.delete(id);
  if (activeSession === id) activeSession = "";
  if (selected) emptyChat();
  await refreshSessions();
  if (selected && sessions.length) await loadSession(sessions[0].id);
}
async function newChat() {
  sessionId = await call("create_session");
  await refreshSessions();
  await loadSession(sessionId);
}
function header() {
  $("mode").textContent = modes[settings.permission];
  $("online").textContent = settings.network ? "Rete attiva" : "Solo locale";
  $("modelBadge").textContent = settings.model;
}
function showWorkspace(path) {
  $("workspace").textContent =
    String(path).split(/[\\/]/).filter(Boolean).at(-1) || path;
  $("workspace").title = path;
}
async function send() {
  if (voiceActive) { toast('Finish or cancel dictation before sending.'); return; }
  if (busy && activeSession && activeSession !== sessionId) {
    await loadSession(activeSession);
    return;
  }
  const text = $("prompt").value.trim();
  if (busy && !text) {
    await call("stop_run");
    setBusy(true, "Interruzione…");
    return;
  }
  if (!text) return;
  if (busy) {
    await call("follow_up", sessionId, text);
    message("user", text);
    $("prompt").value = "";
    updateSendButton();
    toast("Follow-up received. Updating the current activity.");
    return;
  }
  if (!sessionId) await newChat();
  await call("start_run", sessionId, text);
  activeSession = sessionId;
  $("prompt").value = "";
  message("user", text);
  streamText = "";
  streamNode = null;
  setBusy(true);
  renderSessions();
}
function showApproval(data) {
  if (approvalId === data.id) return;
  approvalId = data.id;
  $("approvalReason").textContent = data.reason;
  $("approvalTool").textContent = data.tool;
  $("approvalArgs").textContent = JSON.stringify(data.arguments, null, 2);
  $("approvalDiff").hidden = !data.diff;
  $("approvalDiff").textContent = data.diff || "";
  $("approvalWorkspace").textContent = data.workspace;
  open("approvalModal");
}
async function decide(allow) {
  const id = approvalId;
  if (!id) return;
  await call("resolve_approval", id, allow);
  approvalId = "";
  close("approvalModal");
}
async function poll() {
  if (polling || !api) return;
  polling = true;
  try {
    const result = await call("get_events", lastSeq);
    activeSession = result.session_id || activeSession;
    for (const e of result.events) {
      lastSeq = e.seq;
      const d = e.data;
      if (e.type === "text")
        liveDrafts.set(
          e.session_id,
          (liveDrafts.get(e.session_id) || "") + d.text,
        );
      if (e.type === "message" || e.type === "done")
        liveDrafts.delete(e.session_id);
      if (e.type === "done") await refreshSessions();
      if (e.session_id && e.session_id !== sessionId) continue;
      switch (e.type) {
        case "text":
          if (!streamNode) {
            streamNode = message("assistant", "");
            streamText = "";
          }
          streamText += d.text;
          renderText(streamNode, streamText);
          scrollBottom();
          break;
        case "message":
          if (d.message.content) {
            if (streamNode) {
              renderText(streamNode, d.message.content);
            } else
              message("assistant", d.message.content, d.history_index ?? null);
          }
          if (streamNode && d.history_index !== undefined)
            streamNode.closest(".message").dataset.historyIndex =
              d.history_index;
          streamNode = null;
          streamText = "";
          break;
        case "tool_start":
          activity(
            "↗ " + d.tool,
            d.arguments,
            d.tool === "save_memory" ? "memory" : "running",
          );
          $("state").textContent = d.tool;
          break;
        case "tool_result":
          activity(
            (d.result?.ok === false ? "⚠ " : "✓ ") + d.tool,
            d.result,
            d.result?.ok === false
              ? "error"
              : d.tool === "save_memory"
                ? "memory"
                : "success",
          );
          break;
        case "plan":
          renderPlan(d.steps);
          break;
        case "turn":
          $("state").textContent =
            ui("Step") + ` ${d.step} / ${d.max_steps || "∞"}`;
          break;
        case "approval":
          showApproval(d);
          break;
        case "error":
          toast(d.text);
          activity("Errore", d.text);
          break;
        case "notice":
          toast(d.text);
          break;
        case "model":
          $("modelStatus").textContent =
            ui(d.status || "Download progress") +
            (d.total
              ? " " + Math.round((100 * (d.completed || 0)) / d.total) + "%"
              : "");
          $("catalogProgress").textContent = $("modelStatus").textContent;
          break;
        case "question":
          showQuestion(d);
          break;
        case "done":
          streamNode = null;
          streamText = "";
          close("approvalModal");
          approvalId = "";
          close("questionModal");
          questionId = "";
          break;
      }
    }
    setBusy(
      result.busy,
      result.busy
        ? result.state === "question"
          ? ui("Waiting for your answer")
          : result.state === "approval"
            ? "Autorizzazione richiesta"
            : result.state === "stopping"
              ? "Interruzione…"
              : $("state").textContent
        : {
            completed: "Completato",
            failed: "Errore",
            cancelled: "Interrotto",
            limit: "Limite raggiunto",
          }[result.state] || "Pronto",
    );
    if (result.approval) showApproval(result.approval);
    if (result.question) showQuestion(result.question);
    modelBusy = !!result.model_busy;
    if (!result.busy && !voiceActive && updateReady) {
      updateReady = false;
      await call("apply_update");
    }
  } catch (e) {
    toast(e.message);
  } finally {
    polling = false;
  }
}
async function showTaskCenter() {
  if (!sessionId) throw Error(ui('Choose a chat first.'));
  const overview = await call('get_task_overview', sessionId);
  const task = overview.task;
  $('taskProgress').textContent = task.state ? [ui('Task state') + ': ' + ui(task.state), task.goal, task.progress, task.next_steps].filter(Boolean).join('\n\n') : ui('No saved task progress yet.');
  if (task.verification?.length) $('taskProgress').textContent += '\n\n' + ui('Pending observations: {count}').replace('{count}', task.verification.length);
  $('resumeTask').disabled = busy || !['interrupted','failed','cancelled','limit','unverified','blocked'].includes(task.state);
  $('prepareWindowsSandbox').disabled = busy;
  $('isolationStatus').textContent = ui(overview.windows_sandbox_available ? 'Windows Sandbox launcher is available; Windows feature and virtualization requirements still apply.' : 'Windows Sandbox is unavailable on this installation. Preparation does not enable Windows features.') + ' ' + ui(overview.background_enabled ? 'Background schedules are enabled for this user.' : 'Background schedules are disabled.');
  $('generatedArtifacts').replaceChildren();
  $('codingProposals').replaceChildren();
  for (const proposal of overview.proposals || []) {
    const row = document.createElement('div');
    const label = document.createElement('p');
    label.textContent = proposal.goal + ' · ' + ui(proposal.state);
    const review = document.createElement('button');
    review.textContent = ui('Review coding proposal');
    review.disabled = busy || proposal.state === 'running';
    review.addEventListener('click', async () => {
      try {
        const result = await call('review_proposal', sessionId, proposal.id);
        $('changesPreview').textContent = result.diff;
        $('changesPreview').hidden = false;
      } catch (error) { toast(error.message); }
    });
    row.append(label, review);
    $('codingProposals').append(row);
  }
  for (const artifact of overview.artifacts || []) {
    const button = document.createElement('button');
    button.textContent = ui('Preview generated image') + ' · ' + artifact.name;
    button.addEventListener('click',async () => {
      try {
        const preview=await call('preview_generated_image',sessionId,artifact.id);
        $('artifactPath').textContent=preview.path;
        $('artifactImage').src=preview.data_url;
        open('artifactModal');
      } catch(error) { toast(error.message); }
    });
    $('generatedArtifacts').append(button);
  }
  $('reviewChanges').disabled = busy;
  $('createAutomation').disabled = busy;
  $('changesPreview').hidden = true;
  $('procedureList').replaceChildren();
  for (const procedure of overview.procedures) {
    const button = document.createElement('button');
    button.textContent = ui(procedure.purpose);
    button.addEventListener('click', () => {
      $('prompt').value = ui('Use procedure {name} to ').replace('{name}', procedure.name);
      close('taskModal'); $('prompt').focus(); updateSendButton();
    });
    $('procedureList').append(button);
  }
  $('automationList').replaceChildren();
  for (const job of overview.automations) {
    const row = document.createElement('div'); row.className = 'schedule-row';
    const text = document.createElement('span'); text.textContent = job.prompt + ' · ' + job.interval_hours + 'h · ' + ui(job.last_state);
    const button = document.createElement('button'); button.textContent = ui(job.enabled ? 'Pause schedule' : 'Enable schedule');
    button.addEventListener('click', () => call('toggle_automation', job.id).then(showTaskCenter).catch(e => toast(e.message)));
    const remove = document.createElement('button'); remove.textContent = ui('Remove schedule');
    remove.addEventListener('click', () => {
      if (window.confirm(ui('Remove this schedule?'))) call('remove_automation', job.id).then(showTaskCenter).catch(e => toast(e.message));
    });
    row.append(text, button, remove); $('automationList').append(row);
  }
  open('taskModal'); $('taskModal').querySelector('.dialog').scrollTop = 0;
}
function voiceControls(state) {
  voicePhase = state;
  voiceActive = ['downloading','starting','recording','transcribing'].includes(state);
  $('voiceButton').textContent = ui(state === 'recording' ? 'Stop recording' : '🎙 Speak');
  $('voiceButton').disabled = voiceActive && state !== 'recording';
  $('voiceButton').classList.toggle('recording', state === 'recording');
  $('cancelVoice').hidden = !voiceActive;
  $('voiceStatus').hidden = !voiceActive;
}
async function pollVoice() {
  if (voicePolling || !voiceActive) return;
  voicePolling = true;
  const generation = voiceGeneration;
  try {
    const status = await call('voice_status');
    if (generation !== voiceGeneration) return;
    voiceControls(status.state);
    if (status.state === 'recording') {
      const elapsed = Math.max(0, Math.floor(Date.now()/1000 - status.started_at));
      $('voiceStatus').textContent = ui('Recording locally') + ' · ' + elapsed + 's / 120s';
    } else $('voiceStatus').textContent = ui(status.state === 'downloading' ? 'Downloading speech model…' : status.state === 'transcribing' ? 'Transcribing locally…' : 'Preparing microphone…');
    if (status.state === 'completed') {
      if (sessionId === voiceSession && status.text) {
        $('prompt').value += ($('prompt').value.trim() ? '\n' : '') + status.text;
        $('prompt').dispatchEvent(new Event('input'));
        $('prompt').focus();
        toast(status.audio_overflow ? 'Transcription ready. Audio gaps were detected; review the text.' : 'Transcription ready. Review it before sending.');
      } else toast('No speech detected. Try speaking clearly near the microphone.');
      await call('cancel_voice');
    }
    if (status.state === 'ready') toast('Speech model ready. Press Speak to start.');
    if (status.state === 'error') toast(status.error || 'Local dictation failed. Check the microphone and retry.');
    if (!voiceActive) { clearInterval(voiceTimer); voiceTimer = null; }
  } catch (error) { await cancelDictation(); toast(error.message); }
  finally { voicePolling = false; }
}
async function cancelDictation() {
  voiceGeneration++;
  clearInterval(voiceTimer); voiceTimer = null;
  voiceControls('idle');
  await call('cancel_voice');
}
function watchVoice(state) {
  voiceGeneration++;
  voiceControls(state); clearInterval(voiceTimer);
  voiceTimer = setInterval(pollVoice, 400);
}
action('voiceButton', async () => {
  if (voicePhase === 'recording') { await call('stop_voice'); voiceControls('transcribing'); return; }
  if (busy && activeSession && activeSession !== sessionId) { toast('Return to the active chat before dictating a follow-up.'); return; }
  const status = await call('voice_status');
  if (!status.ready) { open('voiceModal'); return; }
  voiceSession = sessionId;
  await call('start_voice'); watchVoice('starting');
});
action('cancelVoice', cancelDictation);
action('closeVoice', () => close('voiceModal'));
action('prepareVoice', async () => { await call('prepare_voice', true); close('voiceModal'); watchVoice('downloading'); });

action('taskOverview', showTaskCenter);
action('closeTasks', () => close('taskModal'));
action('resumeTask', async () => { await call('resume_task', sessionId); close('taskModal'); activeSession = sessionId; setBusy(true); });
action('reviewChanges', async () => {
  const result = await call('review_changes', sessionId);
  $('changesPreview').replaceChildren();
  for (const line of (result.diff || ui('No changes found.')).split('\n')) {
    const span = document.createElement('span'); span.textContent = line + '\n';
    if (line.startsWith('+')) span.className = 'diff-add';
    if (line.startsWith('-')) span.className = 'diff-remove';
    $('changesPreview').append(span);
  }
  $('changesPreview').hidden = false;
});
action('createAutomation', async () => { await call('save_automation', sessionId, $('automationPrompt').value, Number($('automationHours').value)); $('automationPrompt').value = ''; await showTaskCenter(); });
action('prepareWindowsSandbox',async () => {
  const result=await call('prepare_windows_sandbox',sessionId);
  $('changesPreview').hidden=false;
  $('changesPreview').textContent=ui('Isolated desktop configuration prepared')+'\n'+result.configuration+'\n'+ui('The project copy is read-only. Network, clipboard, microphone, camera and printer sharing are disabled. Opening the desktop requires the existing Windows Sandbox feature.');
});
action('closeArtifact',() => { $('artifactImage').removeAttribute('src'); close('artifactModal'); });

let imageSetupTimer=null, imageStatusPolling=false, imagePreviewShown=false;
const imageGuide = [
  'The chat model and image model are separate. An image engine must be prepared or configured before generation.',
  'Built-in local engine: choose SD 1.5 (CPU), click Prepare local image engine and wait for the verified download (approximately 4.3 GB). It needs Windows x64, approximately 5 GB free disk space, at least 12 GB RAM and preferably 16 GB or more. No account, server, Python package or subscription is required.',
  'Save settings after choosing the engine, then click Test image engine. The test generates one 256×256 image. In chat, ask: Generate a watercolor landscape at 512×512 and save it as landscape.png in the linked folder.',
  'SD 1.5 is a lightweight older model suited to illustrations and simple pictures. English prompts usually work best. Fine text and complex compositions are less reliable. CPU generation can take several minutes; use 512×512, up to 768 pixels per side. Stop remains available and prevents the output write.',
  'Local Stable Diffusion API: start an already installed WebUI with --api, select its checkpoint and enter its base URL, for example http://127.0.0.1:7860. Leave the Veynuq image model field empty; the WebUI chooses the model. Keep that engine running.',
  'Compatible image API: enter the provider HTTPS base URL including /v1 if required, the exact image model name and its token in the vault field. Do not append /images/generations yourself. Online access is required and the provider may charge for the test and subsequent images.',
  'The local image engine processes prompts and pictures offline. A remote chat provider still receives your conversation. Preparing the engine contacts GitHub and Hugging Face. Remote image generation sends the prompt to the configured provider. Tokens stay in the Windows vault; never paste them into chat.',
  'If generation fails: check that settings were saved, the engine is ready, the URL and image model match the provider, enough memory is available and the requested image size is supported. A chat model must use the generate_image tool to create an actual file.',
];
function updateImageFields() {
  const managed=['managed','disabled'].includes($('imageProvider').value);
  for(const id of ['imageEndpoint','imageModel','imageToken']) $(id).disabled=managed;
}
function showImageGuide() {
  const host=$('imageHelpContent');host.replaceChildren();
  for(const text of imageGuide) { const p=document.createElement('p');p.textContent=ui(text);host.append(p); }
  open('imageHelpModal');
}
async function pollImageSetup() {
  if(imageStatusPolling || !api?.image_engine_status) return;
  imageStatusPolling=true;
  try {
    const state=await call('image_engine_status');
    const active=['downloading','testing'].includes(state.state);
    $('prepareImageEngine').disabled=active; $('testImageEngine').disabled=active;
    $('cancelImageSetup').hidden=!active;
    const text=state.state==='downloading' ? ui('Downloading image engine')+(state.total?' · '+Math.floor(state.downloaded/state.total*100)+'%':'')
      : state.state==='testing' ? ui('Generating test image — CPU may take several minutes')
      : state.state==='error' ? ui(state.error)
      : state.state==='stopped' ? ui('Image operation stopped')
      : state.state==='tested' ? ui('Image generation test passed')
      : state.ready ? ui('Local image engine ready') : ui('Local image engine not prepared');
    $('imageSetupStatus').textContent=text;
    if(state.state==='tested' && state.data_url && !imagePreviewShown) {
      imagePreviewShown=true;
      $('artifactImage').src=state.data_url;$('artifactPath').textContent=ui('Image generation test passed');open('artifactModal');
    }
    if(!active && imageSetupTimer) { clearInterval(imageSetupTimer);imageSetupTimer=null; }
  } finally { imageStatusPolling=false; }
}
function watchImageSetup() {
  if(!imageSetupTimer) imageSetupTimer=setInterval(()=>pollImageSetup().catch(e=>toast(e.message)),750);
  pollImageSetup().catch(e=>toast(e.message));
}
action('imageInfo',showImageGuide);
action('closeImageHelp',()=>close('imageHelpModal'));
$('imageProvider').addEventListener('change',updateImageFields);
action('prepareImageEngine',async()=>{
  if(!confirm(ui('Download and install the optional local image engine and SD 1.5 model (approximately 4.3 GB)? No account is needed.'))) return;
  await call('prepare_image_engine',true);watchImageSetup();
});
action('testImageEngine',async()=>{
  const saved=await call('get_settings');
  if(saved.image_provider!==$('imageProvider').value || saved.image_url!==$('imageEndpoint').value.trim() || saved.image_model!==$('imageModel').value.trim() || $('imageToken').value) throw Error(ui('Save the image settings before testing.'));
  if(!confirm(ui('Generate one test image with the saved engine? A remote provider may charge for it.')+'\n'+(saved.image_provider==='managed'?ui('Built-in local engine — SD 1.5 (CPU)'):saved.image_url))) return;
  imagePreviewShown=false;await call('test_image_engine',true);watchImageSetup();
});
action('cancelImageSetup',async()=>{await call('cancel_image_setup');watchImageSetup();});

async function showSettings() {
  settings = await call("get_settings");
  $("language").value = settings.lang;
  loadInstalledModels().catch((e) => toast(e.message));
  loadCatalogChoices().catch((e) => toast(e.message));
  for (const [id, key] of [
    ["provider", "provider"],
    ["endpoint", "url"],
    ["model", "model"],
    ["permission", "permission"],
    ["maxSteps", "max_steps"],
    ["timeout", "command_timeout"],
    ["githubRepo", "github_repo"],
    ["executionEnvironment", "execution_environment"],
    ['desktopScope','desktop_scope'],['contextTokens','context_tokens'],['responseTokens','response_tokens'],
    ['imageProvider','image_provider'],['imageEndpoint','image_url'],['imageModel','image_model'],
  ])
    $(id).value = settings[key];
  $("network").checked = settings.network;
  $("vision").checked = !!settings.vision;
  $("autoUpdate").checked = !!settings.auto_update;
  $('backgroundSchedules').checked=!!settings.background_schedules;
  $('imageToken').value='';
  $('imageToken').placeholder=ui(settings.has_image_token ? 'Saved in vault; leave blank to keep it' : 'Optional image provider token');
  $("providerToken").value = "";
  $("githubToken").value = "";
  $("providerToken").placeholder = settings.has_provider_token
    ? ui("Saved in vault; leave blank to keep it")
    : ui("Token (optional for the local engine)");
  $("githubToken").placeholder = settings.has_github_token
    ? ui("Saved in vault; leave blank to keep it")
    : ui("Token with access to the repositories you need");
  $("fullConsent").checked = false;
  $("fullConfirm").style.display =
    settings.permission === "full" ? "flex" : "none";
  $("dataDir").textContent = settings.data_dir;
  updateImageFields();
  pollImageSetup().catch(e=>toast(e.message));
  open("settingsModal");
}
async function showTools() {
  const catalog = await call("get_tool_catalog");
  const host = $("toolCatalog");
  host.replaceChildren();
  for (const group of catalog.groups) {
    const item = document.createElement("section");
    item.className = "tool-category";
    const title = document.createElement("h3");
    title.textContent = ui(group.name) + " · " + group.count;
    const description = document.createElement("p");
    description.className = "sub";
    description.textContent = ui(group.description);
    item.append(title, description);
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = ui('Enable or disable individual tools');
    details.append(summary);
    for (const tool of group.tools || []) {
      const label = document.createElement('label');
      label.className = 'tool-toggle';
      const toggle = document.createElement('input');
      toggle.type = 'checkbox'; toggle.checked = tool.enabled;
      toggle.setAttribute('aria-label', tool.name);
      const text = document.createElement('span'); text.textContent = tool.name;
      label.append(toggle, text);
      toggle.addEventListener('change', async () => {
        toggle.disabled = true;
        try { await call('set_tool_enabled', tool.name, toggle.checked); }
        catch(e) { toggle.checked = !toggle.checked; toast(String(e.message || e)); }
        finally { toggle.disabled = false; }
      });
      details.append(label);
    }
    if ((group.tools || []).length) item.append(details);
    host.append(item);
  }
  open("toolsModal");
  $("toolsModal").querySelector(".dialog").scrollTop = 0;
}
action("toolsButton", showTools);
action("closeTools", () => close("toolsModal"));
action('enableAllTools', async () => { await call('enable_all_tools'); await showTools(); });
async function showPlugins() {
  const result = await call('get_plugins');
  const host = $('pluginList'); host.replaceChildren();
  if (!result.plugins.length) {
    const empty = document.createElement('p'); empty.textContent = ui('No external plugins installed. Built-in tools are already available.'); host.append(empty);
  }
  for (const plugin of result.plugins) {
    const row = document.createElement('section'); row.className = 'tool-category';
    const heading = document.createElement('h3'); heading.textContent = plugin.name + (plugin.version ? ' · '+plugin.version : '');
    const description = document.createElement('p'); description.textContent = plugin.description;
    const enabled = document.createElement('label'); enabled.className='tool-toggle';
    const check = document.createElement('input'); check.type='checkbox'; check.checked=plugin.enabled;
    const enabledText = document.createElement('span'); enabledText.textContent=ui('Enabled'); enabled.append(check,enabledText);
    check.addEventListener('change', async () => {
      check.disabled=true;
      try { await call('set_plugin_enabled',plugin.id,check.checked); }
      catch(e) { check.checked=!check.checked;toast(String(e.message || e)); }
      finally { check.disabled=false; }
    });
    const detail = document.createElement('details'); const summary = document.createElement('summary');summary.textContent=ui('Components and connection');
    const preview=document.createElement('pre'); preview.textContent=JSON.stringify({skills:Object.keys(plugin.skills),servers:plugin.servers,hooks:plugin.hooks,lsp:plugin.lsp,source:plugin.source,unsupported:plugin.unsupported},null,2); detail.append(summary,preview);
    const accounts=document.createElement('div');
    for(const [server,connected] of Object.entries(plugin.accounts || {})) {
      const button=document.createElement('button');button.textContent=server+' · '+ui(connected?'Disconnect account':'Connect account');
      button.disabled=!plugin.enabled;
      button.addEventListener('click',async()=>{
        try {
          if(connected) {
            if(confirm(ui('Remove local sign-in tokens? This does not revoke access at the service.'))) {await call('disconnect_plugin_account',plugin.id,server);await showPlugins();}
          } else if(confirm(ui('Check this server and open its sign-in page only if authentication is required? Complete authorization personally in your browser.'))) {
            await call('connect_plugin_account',plugin.id,server,true);pollPluginAccount();
          }
        } catch(e){toast(String(e.message||e));}
      });accounts.append(button);
    }
    const credentials=document.createElement('details');const credentialSummary=document.createElement('summary');credentialSummary.textContent=ui('Optional service token');
    const token=document.createElement('input'); token.type='password';token.autocomplete='off';token.placeholder=ui(plugin.has_token?'Saved in vault; leave blank to keep it':'Only if the service requires authentication'); token.setAttribute('aria-label',ui('Optional service token'));
    const save=document.createElement('button'); save.textContent=ui('Save token'); save.addEventListener('click',async()=>{try{await call('save_plugin_token',plugin.id,token.value);token.value='';await showPlugins();}catch(e){toast(String(e.message||e));}});
    const clear=document.createElement('button'); clear.textContent=ui('Remove token');clear.addEventListener('click',async()=>{if(confirm(ui('Remove the saved service token?'))){try{await call('save_plugin_token',plugin.id,'',true);await showPlugins();}catch(e){toast(String(e.message||e));}}});
    credentials.append(credentialSummary,token,save,clear);
    const remove=document.createElement('button');remove.textContent=ui('Uninstall plugin');remove.addEventListener('click',async()=>{if(confirm(ui('Uninstall this plugin and remove its saved token?'))){try{await call('remove_plugin',plugin.id);await showPlugins();}catch(e){toast(String(e.message||e));}}});
    row.append(heading,description,enabled,detail,accounts,credentials,remove);host.append(row);
  }
  open('pluginsModal');
}
const pluginConfirmation = 'Install this plugin? Its MCP servers can execute local code or contact external services when used. Review its configuration and trust its source.';
action('pluginsButton',showPlugins);
action('closePlugins',()=>close('pluginsModal'));
action('importPlugin',async()=>{if(confirm(ui(pluginConfirmation))){await call('install_plugin_folder',true);await showPlugins();}});
action('installMcp',async()=>{if(confirm(ui(pluginConfirmation))){await call('install_mcp_config',$('mcpPluginName').value.trim(),$('mcpPluginConfig').value,true);$('mcpPluginConfig').value='';await showPlugins();}});
let pluginAccountTimer;
async function pollPluginAccount() {
  clearTimeout(pluginAccountTimer);
  try {
    const status=await call('plugin_account_status');
    $('cancelPluginAccount').hidden=status.state!=='connecting';
    $('pluginAccountStatus').textContent=status.state==='connecting'?ui('Waiting for account authorization'):status.state==='connected'?ui('Server connection verified'):status.state==='stopped'?ui('Account connection stopped'):status.error || '';
    if(status.state==='connecting')pluginAccountTimer=setTimeout(pollPluginAccount,750);
    else if(status.state==='connected')await showPlugins();
  } catch(e){toast(String(e.message||e));}
}
action('cancelPluginAccount',async()=>{await call('cancel_plugin_account');await pollPluginAccount();});
action('browseMarketplace',async()=>{
  const host=$('marketplaceList');host.replaceChildren();$('marketplaceStatus').textContent=ui('Loading marketplace');
  const result=await call('get_plugin_marketplace',$('marketplaceRepo').value.trim());
  $('marketplaceStatus').textContent=result.repository+' · '+result.revision;
  for(const plugin of result.plugins) {
    const row=document.createElement('section');row.className='tool-category';
    const title=document.createElement('h3');title.textContent=plugin.name;
    const description=document.createElement('p');description.textContent=plugin.description;
    const button=document.createElement('button');button.textContent=ui(plugin.supported?'Review and install':'Unsupported source');button.disabled=!plugin.supported;
    button.addEventListener('click',async()=>{
      button.disabled=true;
      try {
        const reviewed=await call('review_marketplace_plugin',result.repository,result.revision,plugin.name);
        if(confirm(ui(pluginConfirmation)+'\n\n'+reviewed.repository+'\n'+reviewed.commit+'\n'+reviewed.path)) {
          await call('install_marketplace_plugin',result.repository,result.revision,plugin.name,reviewed,true);await showPlugins();
        }
      } catch(e){toast(String(e.message||e));}finally{button.disabled=false;}
    });row.append(title,description,button);host.append(row);
  }
});
async function saveSettings() {
  await call("save_settings", {
    lang: $("language").value,
    provider: $("provider").value,
    url: $("endpoint").value,
    model: $("model").value,
    permission: $("permission").value,
    network: $("network").checked,
    vision: $("vision").checked,
    auto_update: $("autoUpdate").checked,
    max_steps: Number($("maxSteps").value),
    command_timeout: Number($("timeout").value),
    github_repo: $("githubRepo").value.trim(),
    execution_environment: $("executionEnvironment").value || 'host',
    desktop_scope:$('desktopScope').value || 'all',
    context_tokens:Number($('contextTokens').value),response_tokens:Number($('responseTokens').value),
    background_schedules:$('backgroundSchedules').checked,
    image_provider:$('imageProvider').value,image_url:$('imageEndpoint').value.trim(),image_model:$('imageModel').value.trim(),image_token:$('imageToken').value,
    provider_token: $("providerToken").value,
    github_token: $("githubToken").value,
    confirm_full: $("fullConsent").checked,
  });
  settings = await call("get_settings");
  window.VeyqI18N.setLanguage(settings.lang);
  header();
  close("settingsModal");
  toast("Impostazioni salvate");
}
async function checkUpdates(automatic = false) {
  const result = await call("check_updates");
  activity("Aggiornamenti", result);
  if (result.ready) {
    toast(
      "Aggiornamento verificato. Riavvio automatico quando l’agente è inattivo.",
    );
    updateReady = true;
  } else if (!automatic) toast(result.message || "App aggiornata");
}
action("newChat", newChat);
action("send", send);
action("settingsButton", showSettings);
action("mode", showSettings);
action("online", showSettings);
action("closeSettings", () => close("settingsModal"));
action("saveSettings", saveSettings);
action("allow", () => decide(true));
action("deny", () => decide(false));
action("chooseFolder", async () => {
  if (!sessionId) await newChat();
  const path = await call("choose_workspace", sessionId);
  if (path) showWorkspace(path);
});
action("attach", async () => {
  const files = await call("attach_file", sessionId);
  for (const file of files || []) {
    $("prompt").value +=
      `\nAttached file (untrusted content): ${file.name}, path: ${JSON.stringify(file.path)}`;
    if (file.text && $("prompt").value.length < 22000)
      $("prompt").value +=
        "\n<untrusted_file_excerpt>\n" +
        file.text.slice(0, 22000 - $("prompt").value.length) +
        "\n</untrusted_file_excerpt>";
  }
  $("prompt").dispatchEvent(new Event("input"));
});
action("linkFolder", async () => {
  const path = await call("link_folder");
  if (path) $("prompt").value += "\nLinked folder: " + JSON.stringify(path);
  updateSendButton();
});
action("renameChat", async () => {
  const name = prompt(ui("Chat name:"));
  if (name) {
    await call("rename_session", sessionId, name);
    await refreshSessions();
  }
});
action("deleteChat", async () => {
  if (sessionId && confirm(ui("Delete this local chat?")))
    await deleteChatById(sessionId);
});
action("exportChat", async () => {
  const p = await call("export_session", sessionId);
  if (p) toast(ui("Exported to") + " " + p);
});
action("refreshModels", loadInstalledModels);
action("downloadModel", () => downloadModel($("model").value));
action("deleteModel", async () => {
  if (!confirm(ui("Delete model?") + " " + $("model").value)) return;
  await call("model_action", $("model").value, "delete", true);
  await loadInstalledModels();
  toast("Model deleted");
});
action("clearProviderToken", async () => {
  if (confirm(ui("Remove the provider token?"))) {
    await call("save_settings", {
      clear_provider_token: true,
      confirm_full: settings.permission === "full",
    });
    toast("Token rimosso");
  }
});
action("clearGithubToken", async () => {
  if (confirm(ui("Remove the GitHub token?"))) {
    await call("save_settings", {
      clear_github_token: true,
      confirm_full: settings.permission === "full",
    });
    toast("Token rimosso");
  }
});
action("updates", () => checkUpdates(false));
action('clearImageToken',async () => {
  if(confirm(ui('Remove the image provider token?'))) {
    await call('save_settings',{clear_image_token:true,confirm_full:settings.permission==='full'});
    $('imageToken').value=''; toast('Token rimosso');
  }
});
action("memory", async () => {
  $("memoryText").value = (await call("get_memory")) || "";
  open("memoryModal");
});
action("closeMemory", () => close("memoryModal"));
action("clearMemory", async () => {
  if (confirm(ui("Clear all saved preferences?"))) {
    await call("clear_memory");
    $("memoryText").value = "";
    toast("Memoria cancellata.");
  }
});
action("backups", async () => {
  const backups = await call("get_backups");
  $("backupList").replaceChildren();
  for (const b of backups) {
    const row = document.createElement("div");
    row.className = "backup-row";
    const label = document.createElement("span");
    label.textContent =
      b.path + " · " + new Date(b.time * 1000).toLocaleString();
    const btn = document.createElement("button");
    btn.textContent = "Ripristina";
    btn.addEventListener("click", async () => {
      if (
        confirm(
          ui("Restore") +
            " " +
            b.path +
            "? " +
            ui("The current version will be saved in a new backup."),
        )
      ) {
        try {
          await call("restore_backup", b.id, true);
          toast("File ripristinato");
        } catch (e) {
          toast(e.message);
        }
      }
    });
    row.append(btn, label);
    $("backupList").append(row);
  }
  if (!backups.length)
    $("backupList").textContent =
      "I backup appariranno dopo le prime modifiche ai file.";
  open("backupsModal");
});
action("closeBackups", () => close("backupsModal"));

async function browseFiles(path = ".") {
  if (busy) throw Error("Attendi che l’attività sia terminata.");
  $("fileList").textContent = ui("Reading the folder…");
  const result = await call("browse_project", sessionId, path);
  if (!result.ok) throw Error(result.error);
  explorerPath = path;
  $("explorerPath").textContent = path;
  $("explorerUp").disabled = path === ".";
  $("fileList").replaceChildren();
  const entries = [...result.result].sort(
    (a, b) =>
      Number(b.kind === "dir") - Number(a.kind === "dir") ||
      a.name.localeCompare(b.name),
  );
  for (const entry of entries) {
    const button = document.createElement("button");
    button.className = "file-entry";
    button.textContent =
      (entry.kind === "dir" ? "▸ " : entry.kind === "link" ? "↗ " : "· ") +
      entry.name;
    button.title = entry.name;
    const relative = path === "." ? entry.name : path + "/" + entry.name;
    button.addEventListener("click", () =>
      (entry.kind === "dir"
        ? browseFiles(relative)
        : previewFile(relative)
      ).catch((e) => toast(e.message)),
    );
    $("fileList").append(button);
  }
  if (!entries.length) $("fileList").textContent = ui("Empty folder.");
}
async function previewFile(path, line = 1) {
  if (busy) throw Error("Attendi che l’attività sia terminata.");
  $("filePreview").textContent = ui("Reading the file…");
  const result = await call("preview_project_file", sessionId, path, line);
  if (!result.ok) {
    $("filePreview").textContent = result.error;
    throw Error(result.error);
  }
  selectedFile = path;
  previewLine = line;
  previewTotal = result.result.total_lines;
  $("filePreviewTitle").textContent =
    path + " · " + previewTotal + " " + ui("lines");
  $("filePreview").textContent = result.result.content || ui("Empty file.");
  $("previousLines").disabled = line === 1;
  $("nextLines").disabled = line + 299 >= previewTotal;
  $("askAboutFile").disabled = false;
}
action("exploreFiles", async () => {
  if (!sessionId) await newChat();
  selectedFile = "";
  $("askAboutFile").disabled = true;
  $("filePreview").textContent = ui("Select a file to read its contents.");
  $("filePreviewTitle").textContent = "Anteprima";
  $("previousLines").disabled = true;
  $("nextLines").disabled = true;
  open("explorerModal");
  await browseFiles();
});
action("closeExplorer", () => close("explorerModal"));
action("explorerRoot", () => browseFiles());
action("explorerUp", () =>
  browseFiles(explorerPath.split("/").slice(0, -1).join("/") || "."),
);
action("previousLines", () =>
  previewFile(selectedFile, Math.max(1, previewLine - 300)),
);
action("nextLines", () => previewFile(selectedFile, previewLine + 300));
action("askAboutFile", () => {
  $("prompt").value +=
    ($("prompt").value ? "\n" : "") +
    ui("Read the file") +
    " " +
    JSON.stringify(selectedFile) +
    " " +
    ui("in the project and") +
    " ";
  close("explorerModal");
  $("prompt").focus();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape")
    document
      .querySelectorAll(".session-menu")
      .forEach((m) => (m.hidden = true));
  const dialogs = [...document.querySelectorAll(".modal.open")];
  const current =
    dialogs.find((d) => d.id === "approvalModal") || dialogs.at(-1);
  if (e.key === "Escape" && current) {
    e.preventDefault();
    if (current.id === "questionModal") {
      /* Keep essential questions available; use Stop activity to cancel. */
    } else if (current.id === "approvalModal")
      decide(false).catch((error) => toast(error.message));
    else close(current.id);
  }
  if (e.key === "Tab" && current) {
    const controls = [
      ...current.querySelectorAll(
        "button:not(:disabled), input, select, textarea",
      ),
    ].filter((n) => n.offsetParent !== null);
    if (!controls.length) return;
    const first = controls[0],
      last = controls.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
  if (e.ctrlKey && e.key.toLowerCase() === "n" && !busy && !current) {
    e.preventDefault();
    newChat().catch((error) => toast(error.message));
  }
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".session-more, .session-menu"))
    document
      .querySelectorAll(".session-menu")
      .forEach((m) => (m.hidden = true));
});
let searchTimer;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(
    () => refreshSessions().catch((e) => toast(e.message)),
    150,
  );
});
$("prompt").addEventListener("input", () => {
  $("prompt").style.height = "auto";
  $("prompt").style.height = Math.min(160, $("prompt").scrollHeight) + "px";
  updateSendButton();
});
$("permission").addEventListener("change", () => {
  $("fullConfirm").style.display =
    $("permission").value === "full" ? "flex" : "none";
  $("fullConsent").checked = false;
});
$("prompt").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send().catch((e) => toast(e.message));
  }
});
for (const button of document.querySelectorAll(".suggestion"))
  button.addEventListener("click", () => {
    $("prompt").value = ui(button.dataset.prompt);
    $("prompt").focus();
  });
window.addEventListener("pywebviewready", async () => {
  api = window.pywebview.api;
  try {
    settings = await call("get_settings");
    window.VeyqI18N.setLanguage(settings.lang);
    if (settings.recovery_notice) toast(settings.recovery_notice);
    header();
    await refreshSessions();
    if (sessions.length) await loadSession(sessions[0].id);
    else emptyChat();
    setInterval(poll, 350);
    if (!settings.setup_completed) {
      await call("finish_setup");
      try {
        const r = await call("get_models");
        $("setupStatus").textContent = r.ok
          ? ui("Installed models") + ": " + r.models.join(", ")
          : ui(
              "The local engine is unavailable. Start it, install it with the setup button, or choose a remote API.",
            );
      } catch {}
      open("setupModal");
    }
    if (settings.auto_update)
      setTimeout(
        () =>
          checkUpdates(true).catch((e) => activity("Aggiornamenti", e.message)),
        2500,
      );
    setInterval(() => {
      if (settings.auto_update && !busy)
        checkUpdates(true).catch((e) => activity("Aggiornamenti", e.message));
    }, 3600000);
  } catch (e) {
    toast(e.message);
  }
});

function showQuestion(data) {
  if (questionId === data.id) return;
  questionId = data.id;
  $("questionText").textContent = data.question;
  $("questionAnswer").value = "";
  $("questionOptions").replaceChildren();
  for (const option of data.options || []) {
    const btn = document.createElement("button");
    btn.textContent = option;
    btn.addEventListener("click", () => {
      $("questionAnswer").value = option;
      $("questionAnswer").focus();
    });
    $("questionOptions").append(btn);
  }
  open("questionModal");
}
async function loadInstalledModels() {
  const r = await call("get_models");
  if (!r.ok) throw Error(r.error);
  $("installedModels").replaceChildren();
  $("models").replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = ui("Choose a model");
  $("installedModels").append(empty);
  for (const name of r.models) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    $("installedModels").append(opt);
    $("models").append(opt.cloneNode(true));
  }
  $("installedModels").value = $("model").value || settings.model;
}
async function showModelInfo(name) {
  if (!name) throw Error(ui("Choose a model first."));
  const m = await call("get_model_info", name);
  infoModel = name;
  $("modelInfoTitle").textContent = name;
  $("modelInfoContent").replaceChildren();
  const list = document.createElement("dl");
  list.className = "model-facts";
  const yes = (v) => ui(v == null ? "Unknown" : v ? "Yes" : "No");
  for (const [label, value] of [
    ["Weight", ui(m.weight)],
    ["Best suited for", ui(m.capability || "Unknown")],
    ["Metadata", ui(m.metadata_origin || "Metadata unavailable")],
    ["Parameters", m.parameters],
    ["Quantization", m.quantization],
    [
      "Maximum context",
      m.context_tokens == null
        ? null
        : m.context_tokens.toLocaleString(settings.lang || "en") +
          " " +
          ui("tokens"),
    ],
    ["License", m.license],
    [
      "Installed size",
      m.installed_size_gb == null ? null : m.installed_size_gb + " GB",
    ],
    [
      "Estimated download",
      m.download_gb == null ? null : "≈ " + m.download_gb + " GB",
    ],
    ["Minimum RAM", m.ram_min_gb == null ? null : "≈ " + m.ram_min_gb + " GiB"],
    [
      "Recommended RAM",
      m.ram_recommended_gb == null
        ? null
        : "≈ " + m.ram_recommended_gb + " GiB",
    ],
    [
      "Optional GPU memory",
      m.vram_recommended_gb == null
        ? null
        : "≈ " + m.vram_recommended_gb + " GiB",
    ],
    [
      "Tool calls",
      yes(
        m.installed_capabilities
          ? m.installed_capabilities.includes("tools")
          : m.tools,
      ),
    ],
    [
      "Vision",
      yes(
        m.installed_capabilities
          ? m.installed_capabilities.includes("vision")
          : m.vision,
      ),
    ],
  ]) {
    const key = document.createElement("dt"),
      val = document.createElement("dd");
    key.textContent = ui(label);
    val.textContent = value == null ? ui("Unknown") : String(value);
    list.append(key, val);
  }
  $("modelInfoContent").append(list);
  $('modelCheckStatus').textContent=ui(m.autonomy_check?.state==='passed'?'Controlled file action test passed':m.autonomy_check?.state==='failed'?'Controlled file action test failed':'Autonomous actions not tested');
  for (const [label, text] of [
    ["Suggested uses", m.uses],
    ["Limitations", m.limitations],
    ["Memory and speed", m.note],
  ]) {
    if (!text) continue;
    const p = document.createElement("p");
    p.className = "intro";
    const title = document.createElement("b");
    title.textContent = ui(label) + ": ";
    p.append(title, document.createTextNode(ui(text)));
    $("modelInfoContent").append(p);
  }
  if (m.hardware) {
    const p = document.createElement("p");
    p.className = "model-fit";
    let fit = "Requirements cannot be assessed for this model.";
    if (m.download_gb != null && m.hardware.disk_free_gb < m.download_gb * 1.2)
      fit = "Not enough free disk for download and installation.";
    else if (m.ram_min_gb != null && m.hardware.ram_gb != null)
      fit =
        m.hardware.ram_gb < m.ram_min_gb
          ? "Below estimated minimum RAM. Choose a smaller model."
          : m.hardware.ram_gb < m.ram_recommended_gb
            ? "Above minimum RAM, below recommended. Use a shorter context."
            : "Meets estimated RAM requirements. Speed depends on CPU, GPU and context.";
    p.textContent =
      ui("This computer") +
      ": " +
      ui(fit) +
      " · " +
      ui("Detected RAM") +
      ": " +
      (m.hardware.ram_gb ?? ui("Unknown")) +
      " GiB · " +
      ui("Free disk") +
      ": " +
      m.hardware.disk_free_gb +
      " GB";
    $("modelInfoContent").append(p);
  }
  if (m.tools === false) {
    const p = document.createElement("p");
    p.className = "model-warning";
    p.textContent = ui(
      "Chat-only model: autonomous actions require native tool calls.",
    );
    $("modelInfoContent").prepend(p);
  }
  $("modelSource").hidden = !m.source;
  open("modelInfoModal");
}
async function loadCatalogChoices() {
  const r = await call("get_model_catalog");
  $("catalogChoice").replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = ui("Choose a model");
  $("catalogChoice").append(empty);
  for (const m of r.models) {
    const option = document.createElement("option");
    option.value = m.name;
    option.textContent = m.name + " · ≈ " + m.download_gb + " GB";
    $("catalogChoice").append(option);
  }
  $("downloadPopular").disabled = settings.provider !== "local";
  $("downloadCustom").disabled = settings.provider !== "local";
}
async function showCatalog() {
  const r = await call("get_model_catalog");
  const installed = await call("get_models");
  $("catalogList").replaceChildren();
  $("hardwareInfo").textContent =
    ui("Detected RAM") +
    ": " +
    (r.hardware.ram_gb ?? ui("Unknown")) +
    " GiB · " +
    ui("Free disk") +
    ": " +
    r.hardware.disk_free_gb +
    " GB";
  for (const m of r.models) {
    const row = document.createElement("div");
    row.className = "model-card";
    const label = document.createElement("div"),
      name = document.createElement("b"),
      uses = document.createElement("p");
    name.textContent = m.name;
    uses.className = "sub";
    uses.textContent =
      ui(m.weight) + " · ≈" + m.download_gb + " GB · " + ui(m.uses);
    const nameRow = document.createElement("div");
    nameRow.className = "model-name";
    const info = document.createElement("button");
    info.textContent = "ⓘ";
    info.title = ui("Model details");
    info.setAttribute("aria-label", ui("Model details") + ": " + m.name);
    info.addEventListener("click", () =>
      showModelInfo(m.name).catch((e) => toast(e.message)),
    );
    nameRow.append(name, info);
    label.append(nameRow, uses);
    const download = document.createElement("button");
    const available = installed.ok && installed.models.includes(m.name);
    download.textContent = ui(available ? "Select model" : "Download");
    download.disabled = settings.provider !== "local";
    download.addEventListener("click", () => {
      if (available) {
        $("model").value = m.name;
        $("installedModels").value = m.name;
        close("catalogModal");
        if (!$("settingsModal").classList.contains("open"))
          showSettings()
            .then(() => {
              $("model").value = m.name;
              $("installedModels").value = m.name;
            })
            .catch((e) => toast(e.message));
        toast("Save settings to use this model.");
      } else downloadModel(m.name).catch((e) => toast(e.message));
    });
    row.append(label, download);
    $("catalogList").append(row);
  }
  open("catalogModal");
}
async function downloadModel(name) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(name || ""))
    throw Error(ui("Enter a valid model name."));
  if (modelBusy) throw Error(ui("Wait for the current download to finish."));
  if (
    !confirm(
      ui(
        "Download this model? The local engine will contact its online catalog.",
      ) +
        " " +
        name,
    )
  )
    return;
  $("cancelDownload").hidden = false;
  $("catalogProgress").textContent = ui("Downloading…");
  modelBusy = true;
  try {
    const r = await call("model_action", name, "pull", true);
    if (r.cancelled) {
      toast("Download cancelled");
      return;
    }
    $("model").value = name;
    await loadInstalledModels();
    toast("Model installed. Select it in Settings to use it.");
  } finally {
    modelBusy = false;
    $("cancelDownload").hidden = true;
  }
}
action("cancelDownload", () => call("cancel_model_action"));
action("openCatalog", showCatalog);
action("closeCatalog", () => close("catalogModal"));
action("modelInfo", () => showModelInfo($("model").value));
let modelCheckTimer=null, modelCheckPolling=false;
async function pollModelCheck() {
  if(modelCheckPolling) return;
  modelCheckPolling=true;
  try {
    const result=await call('model_check_status');const active=result.state==='running';
    $('testModelActions').disabled=active;$('cancelModelCheck').hidden=!active;
    $('modelCheckStatus').textContent=ui(active?'Testing actions in a temporary folder':result.state==='passed'?'Controlled file action test passed':result.state==='failed'?'Controlled file action test failed':result.error||'Model test stopped');
    if(!active && modelCheckTimer){clearInterval(modelCheckTimer);modelCheckTimer=null;}
  } finally { modelCheckPolling=false; }
}
action('testModelActions',async()=>{
  if(!confirm(ui('Test this model using only one temporary file? No personal files or PC settings are accessible. Requests go to the configured model provider; a remote provider may charge.'))) return;
  await call('test_model_actions',infoModel,true);
  if(!modelCheckTimer)modelCheckTimer=setInterval(()=>pollModelCheck().catch(e=>toast(e.message)),750);
  await pollModelCheck();
});
action('cancelModelCheck',async()=>{await call('cancel_model_check');await pollModelCheck();});
action("installedModelInfo", () => showModelInfo($("installedModels").value));
action("catalogChoiceInfo", () => showModelInfo($("catalogChoice").value));
action("customModelInfo", () => showModelInfo($("customModel").value.trim()));
action("downloadPopular", () => downloadModel($("catalogChoice").value));
action("downloadCustom", () => downloadModel($("customModel").value.trim()));
action("closeModelInfo", () => close("modelInfoModal"));
action("modelSource", () => call("open_model_source", infoModel));
action("providerAccount", () => call("open_provider_account"));
async function setupEngine() {
  if (
    !confirm(
      ui(
        "Install or start the optional local engine? The official signed installer will be used if needed.",
      ),
    )
  )
    return;
  $("setupStatus").textContent = ui("Setting up the local engine…");
  const r = await call("setup_local_engine", true);
  toast(r.message);
  await loadInstalledModels();
}
action("setupEngine", setupEngine);
action("setupInstall", setupEngine);
action("setupSettings", () => {
  close("setupModal");
  return showSettings();
});
action("setupCatalog", () => {
  close("setupModal");
  return showCatalog();
});
action("closeSetup", () => close("setupModal"));
for (const [id, name] of [
  ["setupSmall", "qwen2.5:7b"],
  ["setupBalanced", "llama3.1:8b"],
  ["setupLarge", "qwen3:14b"],
])
  action(id, () => downloadModel(name));
action("newProject", () => {
  $("projectName").value = "";
  open("projectModal");
});
action("closeProject", () => close("projectModal"));
action("closeChatProject", () => close("chatProjectModal"));
action("saveChatProject", async () => {
  await call("assign_project", assignmentSession, $("chatProjectChoice").value);
  await refreshSessions();
  if (assignmentSession === sessionId) await loadSession(sessionId);
  close("chatProjectModal");
});
action("createProject", async () => {
  if (!sessionId) await newChat();
  const p = await call("create_project", $("projectName").value);
  await call("assign_project", sessionId, p.id);
  await refreshSessions();
  await loadSession(sessionId);
  close("projectModal");
});
$("projectAssignment").addEventListener("change", async () => {
  const selected = $("projectAssignment").value;
  try {
    if (!sessionId) await newChat();
    await call("assign_project", sessionId, selected);
    await loadSession(sessionId);
    await refreshSessions();
  } catch (e) {
    toast(e.message);
  }
});
$("language").addEventListener("change", async () => {
  try {
    const lang = $("language").value;
    await call("set_language", lang);
    settings.lang = lang;
    window.VeyqI18N.setLanguage(lang);
    header();
    renderSessions();
    updateSendButton();
  } catch (e) {
    toast(e.message);
  }
});
$("installedModels").addEventListener("change", () => {
  if ($("installedModels").value) $("model").value = $("installedModels").value;
});
action("submitAnswer", async () => {
  await call("answer_question", questionId, $("questionAnswer").value);
  questionId = "";
  close("questionModal");
});
action("saveMemory", async () => {
  await call("save_memory", $("memoryText").value);
  toast("Memory saved");
  close("memoryModal");
});
action("toggleActivity", () =>
  document
    .querySelector(".shell")
    .classList.toggle(
      matchMedia("(max-width:950px)").matches
        ? "activity-mobile-open"
        : "activity-hidden",
    ),
);

action("stopQuestion", () => call("stop_run"));

action("closeActivity", () =>
  document.querySelector(".shell").classList.remove("activity-mobile-open"),
);
