/* iSponsorBlockTV WebUI – plain JavaScript, no build step. */
(() => {
  "use strict";

  const LANGUAGES = { en: "English", de: "Deutsch" };
  const FALLBACK = "en";
  const $ = (id) => document.getElementById(id);

  let strings = {};
  let fallbackStrings = {};
  let lang = FALLBACK;
  let meta = { categories: [], auth: false, version: "" };
  let config = null;
  let whitelist = [];
  let logSeq = 0;
  let pollTimer = null;
  let lastStatus = null;

  // ---------------------------------------------------------------- storage (may be unavailable)
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (_) { /* ignore */ } },
  };

  // ---------------------------------------------------------------- i18n
  function t(key, params) {
    let s = strings[key] ?? fallbackStrings[key] ?? key;
    if (params) for (const [k, v] of Object.entries(params)) s = s.split(`{${k}}`).join(String(v));
    return s;
  }

  async function loadLanguage(code) {
    const get = async (c) => {
      const r = await fetch(`/static/i18n/${c}.json`, { cache: "no-cache" });
      return r.ok ? r.json() : {};
    };
    fallbackStrings = await get(FALLBACK);
    strings = code === FALLBACK ? fallbackStrings : await get(code);
    lang = code;
    document.documentElement.lang = code;
    store.set("isbtv-lang", code);
    applyTranslations();
  }

  function detectLanguage() {
    const fromUrl = new URLSearchParams(location.search).get("lang");
    if (fromUrl && LANGUAGES[fromUrl]) return fromUrl;
    const saved = store.get("isbtv-lang");
    if (saved && LANGUAGES[saved]) return saved;
    for (const l of navigator.languages || [navigator.language || FALLBACK]) {
      const short = String(l).slice(0, 2).toLowerCase();
      if (LANGUAGES[short]) return short;
    }
    return FALLBACK;
  }

  function applyTranslations() {
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
    document.title = "iSponsorBlockTV – " + t("app.subtitle");
    if (config) renderAll();
    if (lastStatus) renderStatus(lastStatus);
  }

  // ---------------------------------------------------------------- API
  async function api(method, url, body) {
    const opts = { method, credentials: "same-origin", headers: { "X-Requested-With": "webui" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (res.status === 401 && url !== "/api/login") {
      showLogin();
      throw new Error("error.unauthorized");
    }
    if (!res.ok) {
      const err = new Error((data && data.error) || "error.generic");
      err.params = data && data.params;
      throw err;
    }
    return data;
  }

  // ---------------------------------------------------------------- UI helpers
  function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (v !== false && v !== undefined && v !== null) e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of [].concat(children)) if (c) e.append(c);
    return e;
  }

  let toastTimer = null;
  function toast(message, kind = "ok") {
    const box = $("toast");
    box.textContent = message;
    box.className = `toast ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => box.classList.add("hidden"), 4000);
  }

  function errorText(err) {
    const key = err && err.message ? err.message : "error.generic";
    const params = Object.assign({}, err && err.params);
    if (params.field) params.field = t(`field.${params.field}`);
    const text = t(key, params);
    return text === key ? t("error.generic") : text;
  }

  async function busy(button, fn) {
    button.disabled = true;
    button.classList.add("busy");
    try { return await fn(); } finally { button.disabled = false; button.classList.remove("busy"); }
  }

  function formatDuration(sec) {
    if (sec == null) return "–";
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d) return `${d} ${t("unit.d")} ${h} ${t("unit.h")}`;
    if (h) return `${h} ${t("unit.h")} ${m} ${t("unit.m")}`;
    return `${m} ${t("unit.m")} ${sec % 60} ${t("unit.s")}`;
  }

  function formatTime(ts) {
    try { return new Date(ts * 1000).toLocaleString(lang); } catch (_) { return ""; }
  }

  // ---------------------------------------------------------------- views
  function showLogin() {
    $("appView").classList.add("hidden");
    $("loginView").classList.remove("hidden");
    $("logoutBtn").classList.add("hidden");
    stopPolling();
    setTimeout(() => $("password").focus(), 50);
  }

  async function showApp() {
    $("loginView").classList.add("hidden");
    $("appView").classList.remove("hidden");
    $("logoutBtn").classList.toggle("hidden", !meta.auth);
    await loadConfig();
    startPolling();
  }

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== name));
    store.set("isbtv-tab", name);
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  }

  const TABS = ["status", "devices", "skip", "ads", "channels", "advanced"];
  function initialTab() {
    const fromHash = location.hash.replace("#", "");
    if (TABS.includes(fromHash)) return fromHash;
    const saved = store.get("isbtv-tab");
    return TABS.includes(saved) ? saved : "status";
  }

  // ---------------------------------------------------------------- status & log
  function renderStatus(s) {
    lastStatus = s;
    const pill = $("statusPill");
    pill.dataset.state = s.state;
    $("statusText").textContent = t(`state.${s.state}`);
    $("factState").textContent = t(`state.${s.state}`);
    $("factUptime").textContent = formatDuration(s.uptime);
    $("factDevices").textContent = config ? String(config.devices.length) : "–";
    $("factExit").textContent = s.last_exit ? t("status.exit_code", { code: s.last_exit.code, time: formatTime(s.last_exit.t) }) : "–";
    const running = s.state === "running";
    $("startBtn").disabled = running;
    $("stopBtn").disabled = !running && s.state !== "restarting";
    const box = $("problems");
    box.replaceChildren(...(s.problems || []).map((p) => el("div", { text: t(p) })));
    box.classList.toggle("hidden", !(s.problems || []).length);
  }

  async function poll() {
    try {
      renderStatus(await api("GET", "/api/status"));
      const data = await api("GET", `/api/logs?since=${logSeq}`);
      if (data.lines.length) {
        const pre = $("log");
        const frag = document.createDocumentFragment();
        for (const l of data.lines) {
          const time = new Date(l.t * 1000).toLocaleTimeString(lang);
          frag.append(el("span", { class: `line ${l.s}`, text: `${time}  ${l.m}\n` }));
        }
        pre.append(frag);
        while (pre.childNodes.length > 800) pre.removeChild(pre.firstChild);
        if ($("autoscroll").checked) pre.scrollTop = pre.scrollHeight;
      }
      logSeq = data.last;
    } catch (_) { /* shown via status or login */ }
  }

  function startPolling() { stopPolling(); poll(); pollTimer = setInterval(poll, 3000); }
  function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

  // ---------------------------------------------------------------- config rendering
  async function loadConfig() {
    config = await api("GET", "/api/config");
    whitelist = config.channel_whitelist.map((c) => ({ ...c }));
    renderAll();
  }

  function renderAll() {
    renderDevices();
    renderCategories();
    renderChannels();
    $("minSkip").value = config.minimum_skip_length;
    $("skipCount").checked = !!config.skip_count_tracking;
    $("muteAds").checked = !!config.mute_ads;
    $("skipAds").checked = !!config.skip_ads;
    $("autoPlay").checked = !!config.auto_play;
    $("joinName").value = config.join_name || "";
    $("useProxy").checked = !!config.use_proxy;
    $("apikey").value = "";
    $("apikeyDelete").checked = false;
    $("apikeyDelete").parentElement.classList.toggle("hidden", !config.has_apikey);
    $("apikeyState").textContent = config.has_apikey ? t("adv.apikey_set") : t("adv.apikey_unset");
    $("needKey").classList.toggle("hidden", config.has_apikey);
    $("searchForm").classList.toggle("disabled", !config.has_apikey);
    $("searchBtn").disabled = !config.has_apikey;
    $("version").textContent = meta.version ? `v${meta.version}` : "";
    if (lastStatus) $("factDevices").textContent = String(config.devices.length);
  }

  function renderDevices() {
    const list = $("deviceList");
    list.replaceChildren();
    $("noDevices").classList.toggle("hidden", config.devices.length > 0);
    for (const d of config.devices) {
      const name = el("input", { value: d.name, maxlength: "64", "aria-label": t("devices.name") });
      const offset = el("input", { type: "number", value: d.offset, step: "50", min: "-60000", max: "60000", "aria-label": t("devices.offset") });
      const save = el("button", { text: t("common.save") });
      const del = el("button", { class: "danger-ghost", text: t("devices.remove") });
      save.addEventListener("click", () => busy(save, async () => {
        try {
          config = await api("PATCH", `/api/devices/${encodeURIComponent(d.screen_id)}`, { name: name.value, offset: Number(offset.value || 0) });
          renderAll(); toast(t("toast.saved"));
        } catch (e) { toast(errorText(e), "error"); }
      }));
      del.addEventListener("click", () => {
        if (!window.confirm(t("devices.confirm_remove", { name: d.name || d.screen_id.slice(0, 8) }))) return;
        busy(del, async () => {
          try { config = await api("DELETE", `/api/devices/${encodeURIComponent(d.screen_id)}`); renderAll(); toast(t("toast.removed")); }
          catch (e) { toast(errorText(e), "error"); }
        });
      });
      list.append(el("li", { class: "device" }, [
        el("div", { class: "device-main" }, [
          el("label", { class: "field compact" }, [el("span", { text: t("devices.name") }), name]),
          el("label", { class: "field compact narrow" }, [el("span", { text: t("devices.offset") }), offset]),
        ]),
        el("div", { class: "device-meta" }, [
          el("code", { title: d.screen_id, text: `${d.screen_id.slice(0, 10)}…` }),
          d.legacy_id ? el("span", { class: "badge warn", text: t("devices.legacy") }) : null,
        ]),
        el("div", { class: "btn-row" }, [save, del]),
      ]));
    }
  }

  function renderCategories() {
    const box = $("categoryList");
    box.replaceChildren();
    const active = new Set(config.skip_categories || []);
    for (const c of meta.categories) {
      box.append(el("label", { class: "check" }, [
        el("input", { type: "checkbox", value: c, checked: active.has(c) }),
        el("span", {}, [el("b", { text: t(`cat.${c}`) }), el("small", { text: t(`cat.${c}.hint`) })]),
      ]));
    }
  }

  function renderChannels() {
    const list = $("channelList");
    list.replaceChildren();
    $("noChannels").classList.toggle("hidden", whitelist.length > 0);
    whitelist.forEach((c, i) => {
      const del = el("button", { class: "danger-ghost", text: t("channels.remove") });
      del.addEventListener("click", () => { whitelist.splice(i, 1); renderChannels(); markDirty("channels"); });
      list.append(el("li", { class: "row between" }, [
        el("div", {}, [el("b", { text: c.name || c.id }), el("small", { class: "muted block", text: c.id })]),
        del,
      ]));
    });
  }

  function markDirty(panel) {
    const btn = document.querySelector(`[data-save="${panel}"]`);
    if (btn) btn.classList.add("dirty");
  }

  // ---------------------------------------------------------------- saving
  async function saveSettings(panel, button) {
    let body = {};
    if (panel === "skip") {
      body.skip_categories = [...document.querySelectorAll("#categoryList input:checked")].map((i) => i.value);
      body.minimum_skip_length = Number($("minSkip").value || 0);
      body.skip_count_tracking = $("skipCount").checked;
    } else if (panel === "ads") {
      body = { mute_ads: $("muteAds").checked, skip_ads: $("skipAds").checked, auto_play: $("autoPlay").checked };
    } else if (panel === "channels") {
      body.channel_whitelist = whitelist;
    } else if (panel === "advanced") {
      body.join_name = $("joinName").value;
      body.use_proxy = $("useProxy").checked;
      if ($("apikeyDelete").checked) body.apikey = "";
      else if ($("apikey").value.trim()) body.apikey = $("apikey").value.trim();
      else body.apikey = null;
    }
    await busy(button, async () => {
      try {
        config = await api("PUT", "/api/config", body);
        whitelist = config.channel_whitelist.map((c) => ({ ...c }));
        renderAll();
        button.classList.remove("dirty");
        toast(t("toast.saved_restarted"));
      } catch (e) { toast(errorText(e), "error"); }
    });
  }

  // ---------------------------------------------------------------- events
  function bindEvents() {
    document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
    document.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", () => saveSettings(b.dataset.save, b)));
    document.querySelectorAll(".panel").forEach((p) => p.addEventListener("change", (e) => {
      if (e.target.closest(".device") || e.target.closest("#pairForm") || e.target.closest("#searchForm") || e.target.closest("#manualForm")) return;
      markDirty(p.dataset.panel);
    }));

    const sel = $("langSelect");
    for (const [code, name] of Object.entries(LANGUAGES)) sel.append(el("option", { value: code, text: name }));
    sel.addEventListener("change", () => loadLanguage(sel.value));

    $("loginForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const errBox = $("loginError");
      errBox.classList.add("hidden");
      try {
        await api("POST", "/api/login", { password: $("password").value });
        $("password").value = "";
        await showApp();
      } catch (err) {
        errBox.textContent = errorText(err);
        errBox.classList.remove("hidden");
      }
    });
    $("logoutBtn").addEventListener("click", async () => { try { await api("POST", "/api/logout"); } catch (_) { /* ignore */ } showLogin(); });

    const processAction = (btn, action) => btn.addEventListener("click", () => busy(btn, async () => {
      try { renderStatus(await api("POST", `/api/process/${action}`)); } catch (e) { toast(errorText(e), "error"); }
    }));
    processAction($("startBtn"), "start");
    processAction($("restartBtn"), "restart");
    processAction($("stopBtn"), "stop");

    $("pairForm").addEventListener("submit", (e) => {
      e.preventDefault();
      busy($("pairBtn"), async () => {
        try {
          config = await api("POST", "/api/devices/pair", { code: $("pairCode").value, name: $("pairName").value });
          $("pairCode").value = ""; $("pairName").value = "";
          renderAll(); toast(t("toast.paired"));
        } catch (err) { toast(errorText(err), "error"); }
      });
    });

    $("searchForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = $("searchQuery").value.trim();
      if (!q) return;
      busy($("searchBtn"), async () => {
        const out = $("searchResults");
        out.replaceChildren();
        try {
          const res = await api("GET", `/api/channels/search?q=${encodeURIComponent(q)}`);
          if (!res.channels.length) out.append(el("li", { class: "muted", text: t("channels.no_results") }));
          for (const c of res.channels) {
            const add = el("button", { text: t("channels.add") });
            add.addEventListener("click", () => {
              if (!whitelist.some((w) => w.id === c.id)) whitelist.push({ id: c.id, name: c.name });
              renderChannels(); markDirty("channels"); add.disabled = true;
            });
            out.append(el("li", { class: "row between" }, [
              el("div", {}, [el("b", { text: c.name }), el("small", { class: "muted block", text: t("channels.subscribers", { count: c.subscribers }) })]),
              add,
            ]));
          }
        } catch (err) { toast(errorText(err), "error"); }
      });
    });

    $("manualForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const id = $("manualId").value.trim();
      if (!id) return;
      if (!whitelist.some((w) => w.id === id)) whitelist.push({ id, name: $("manualName").value.trim() });
      $("manualId").value = ""; $("manualName").value = "";
      renderChannels(); markDirty("channels");
    });
  }

  // ---------------------------------------------------------------- start
  async function init() {
    bindEvents();
    const code = detectLanguage();
    $("langSelect").value = code;
    await loadLanguage(code);
    switchTab(initialTab());
    window.addEventListener("hashchange", () => switchTab(initialTab()));
    try { meta = await api("GET", "/api/meta"); } catch (_) { /* keep defaults */ }
    const session = await api("GET", "/api/session");
    if (session.auth && !session.authenticated) showLogin();
    else await showApp();
  }

  init().catch((e) => toast(errorText(e), "error"));
})();
