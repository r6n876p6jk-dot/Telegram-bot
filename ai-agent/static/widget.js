/*!
 * KI-Chat-Widget. Einbinden mit einer Zeile vor </body>:
 *
 * <script src="https://IHR-SERVER/widget.js"
 *   data-name="Café Milchschaum"
 *   data-color="#2c6a4c"
 *   data-greeting="Hallo! Wie kann ich helfen?"
 *   data-privacy="https://ihre-seite.de/datenschutz"
 *   defer></script>
 */
(function () {
  if (window.__kiChatLoaded) return;
  window.__kiChatLoaded = true;

  var script = document.currentScript || document.querySelector('script[src*="widget.js"]');
  var ds = (script && script.dataset) || {};
  var API = new URL(script ? script.src : location.href).origin + "/api/chat";
  var NAME = ds.name || "KI-Assistent";
  var COLOR = ds.color || "#2c6a4c";
  var GREETING = ds.greeting || "Hallo! Ich bin der KI-Assistent. Wie kann ich Ihnen helfen?";
  var PRIVACY = ds.privacy || "";
  var STORE = "kiChat:" + location.host;

  var state = { messages: [], consent: false, open: false };
  try {
    var saved = JSON.parse(sessionStorage.getItem(STORE) || "null");
    if (saved && Array.isArray(saved.messages)) state = Object.assign(state, saved, { open: false });
  } catch (e) {}
  function save() {
    try { sessionStorage.setItem(STORE, JSON.stringify({ messages: state.messages.slice(-30), consent: state.consent })); } catch (e) {}
  }

  var host = document.createElement("div");
  host.id = "ki-chat-widget";
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML =
    '<style>' +
    ':host{all:initial}' +
    '*{box-sizing:border-box;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}' +
    '.fab{position:fixed;right:max(16px,env(safe-area-inset-right,0px));bottom:calc(16px + env(safe-area-inset-bottom,0px));z-index:2147483000;' +
    'display:flex;align-items:center;gap:8px;border:0;border-radius:999px;padding:12px 18px 12px 14px;background:var(--c);color:#fff;font-size:15px;font-weight:600;cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.2)}' +
    '.panel{position:fixed;right:max(16px,env(safe-area-inset-right,0px));bottom:calc(16px + env(safe-area-inset-bottom,0px));z-index:2147483001;width:380px;max-width:calc(100vw - 32px);height:600px;max-height:calc(100vh - 32px);' +
    'display:flex;flex-direction:column;background:#fff;color:#1d231f;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.25);overflow:hidden}' +
    '@media (max-width:480px){.panel{right:0;bottom:0;width:100vw;max-width:100vw;height:100%;max-height:100%;border-radius:0;padding-top:env(safe-area-inset-top,0px)}}' +
    '[hidden]{display:none!important}' +
    '.head{display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--c);color:#fff}' +
    '.head b{display:block;font-size:15px}.head small{font-size:12px;opacity:.85}' +
    '.head .t{flex:1;min-width:0}.x{border:0;background:transparent;color:#fff;font-size:24px;line-height:1;cursor:pointer;padding:4px 8px}' +
    '.log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;background:#f6f7f5}' +
    '.m{max-width:85%;padding:9px 12px;border-radius:14px;font-size:14px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}' +
    '.bot{background:#fff;border:1px solid #e3e6e0;align-self:flex-start;border-bottom-left-radius:4px}' +
    '.me{background:var(--c);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}' +
    '.wait{color:#6b746d;font-style:italic}.err{background:#fff3f0;border:1px solid #f1c8bd;color:#8a2b17;align-self:center;font-size:13px}' +
    '.ok{align-self:stretch;border:1px solid var(--c);background:#fff;border-radius:10px;padding:8px 10px;font-size:13px}' +
    '.consent{padding:10px 14px;font-size:12px;color:#4d554f;border-top:1px solid #e3e6e0;display:flex;gap:8px;align-items:flex-start;background:#fff}' +
    '.consent a{color:var(--c)}.consent input{margin-top:2px}' +
    'form{display:flex;gap:8px;padding:10px;border-top:1px solid #e3e6e0;background:#fff;padding-bottom:calc(10px + env(safe-area-inset-bottom,0px))}' +
    'textarea{flex:1;min-width:0;resize:none;height:42px;max-height:110px;border:1px solid #d5dad2;border-radius:10px;padding:10px;font-size:14px;color:#1d231f;background:#fff}' +
    'button.send{border:0;border-radius:10px;background:var(--c);color:#fff;font-weight:600;padding:0 14px;cursor:pointer;font-size:14px}' +
    'button.send:disabled{opacity:.45;cursor:default}' +
    '.ai{font-size:11px;color:#6b746d;text-align:center;padding:4px 0 0}' +
    ':focus-visible{outline:2px solid #e2a45c;outline-offset:2px}' +
    '</style>' +
    '<button class="fab" type="button" aria-label="Chat öffnen">' +
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>' +
    '<span>Fragen? Chat</span></button>' +
    '<div class="panel" role="dialog" aria-label="Chat" hidden>' +
    '<div class="head"><div class="t"><b class="name"></b><small>KI-Assistent · antwortet sofort</small></div>' +
    '<button class="x" type="button" aria-label="Chat schließen">×</button></div>' +
    '<div class="log" aria-live="polite"></div>' +
    '<label class="consent"><input type="checkbox" class="cb"><span>Ich bin einverstanden, dass meine Nachrichten von einem KI-Assistenten (Anthropic Claude) verarbeitet werden, um meine Anfrage zu beantworten.<span class="pv"></span> Bitte keine sensiblen Daten eingeben.</span></label>' +
    '<form><textarea placeholder="Ihre Frage…" aria-label="Ihre Nachricht" maxlength="2000"></textarea><button class="send" type="submit">Senden</button></form>' +
    '</div>';
  host.style.setProperty("--c", COLOR);
  root.host.style.setProperty("--c", COLOR);

  var $ = function (s) { return root.querySelector(s); };
  var fab = $(".fab"), panel = $(".panel"), log = $(".log"), ta = $("textarea"), send = $(".send"), cb = $(".cb"), consentBox = $(".consent");
  $(".name").textContent = NAME;
  if (PRIVACY) {
    var a = document.createElement("a");
    a.href = PRIVACY; a.target = "_blank"; a.rel = "noopener"; a.textContent = "Datenschutzerklärung";
    $(".pv").append(" Mehr in der ", a, ".");
  }

  function bubble(text, cls) {
    var d = document.createElement("div");
    d.className = "m " + cls; d.textContent = text;
    log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
  }
  function render() {
    log.textContent = "";
    bubble(GREETING, "bot");
    state.messages.forEach(function (m) { bubble(m.content, m.role === "user" ? "me" : "bot"); });
    consentBox.hidden = state.consent;
    cb.checked = state.consent;
    syncSend();
  }
  function syncSend() { send.disabled = busy || !cb.checked || !ta.value.trim(); }

  var busy = false;
  function open() { panel.hidden = false; fab.hidden = true; state.open = true; render(); setTimeout(function () { ta.focus(); }, 30); }
  function close() { panel.hidden = true; fab.hidden = false; state.open = false; }
  fab.addEventListener("click", open);
  $(".x").addEventListener("click", close);
  cb.addEventListener("change", function () { state.consent = cb.checked; save(); syncSend(); });
  ta.addEventListener("input", syncSend);
  ta.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!send.disabled) submit(); }
  });
  $("form").addEventListener("submit", function (e) { e.preventDefault(); if (!send.disabled) submit(); });

  function submit() {
    var text = ta.value.trim();
    if (!text || busy || !state.consent) return;
    ta.value = "";
    consentBox.hidden = true;
    state.messages.push({ role: "user", content: text });
    save();
    bubble(text, "me");
    var wait = bubble("Schreibt…", "bot wait");
    busy = true; syncSend();
    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: state.messages.slice(-20) })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error((data && data.detail) || "Der Assistent ist gerade nicht erreichbar.");
        return data;
      });
    }).then(function (data) {
      wait.className = "m bot"; wait.textContent = data.reply;
      state.messages.push({ role: "assistant", content: data.reply });
      (data.leads || []).forEach(function (l) {
        var d = document.createElement("div");
        d.className = "ok";
        d.textContent = "✓ Anfrage übermittelt" + (l.type ? ": " + l.type : "") + (l.when ? ", " + l.when : "");
        log.appendChild(d);
      });
      save();
    }).catch(function (err) {
      wait.remove();
      state.messages.pop(); save();
      bubble(String(err.message || err), "err");
      ta.value = text;
    }).then(function () {
      busy = false; syncSend(); log.scrollTop = log.scrollHeight; ta.focus();
    });
  }

  function mount() { document.body.appendChild(host); }
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);
})();
