// Панель модерации: Live (активные кластеры + лента) и Audit.
// Без сборки, как и index.html — обычный vanilla JS.
//
// Роль и логин больше не выбираются вручную в UI — они приходят с сервера
// после входа через Twitch (panel/auth.py), через GET /auth/me. cookie-
// сессия ходит с каждым запросом автоматически (same-origin), никаких
// заголовков вроде X-Panel-Role добавлять не нужно. Любой ответ 401
// означает "сессия истекла или не было входа" — показываем экран логина.

const state = {
  profile: localStorage.getItem("mod.profile") || "main",
  role: "VIEWER",
  login: "",
  ws: null,
  wsRetryMs: 1000,
};

const el = (id) => document.getElementById(id);

// Человекочитаемые названия сигналов детекторов (см. config/moderation.yml
// для полного списка и весов) — движок и API работают с английскими
// именами (стабильные идентификаторы для конфига/аудита/feedback), здесь
// только отображение в UI переведено, сами данные не меняются.
const SIGNAL_LABELS = {
  user_message_burst: "всплеск сообщений от юзера",
  channel_message_burst: "всплеск сообщений в канале",
  exact_duplicate: "точный дубликат",
  near_duplicate: "почти дубликат",
  skeleton_match: "похожий текст (скелет)",
  link_present: "есть ссылка",
  shared_link_multi_user: "одна ссылка у нескольких",
  url_shortener: "сокращённая ссылка",
  known_scam_domain: "известный скам-домен",
  invisible_chars: "невидимые символы",
  homoglyph_mix: "смешанные похожие символы",
  script_mix_in_word: "смешение алфавитов в слове",
  unexpected_language: "неожиданный язык",
  new_account: "новый аккаунт",
  first_message: "первое сообщение",
  no_history: "нет истории",
  generated_username_pattern: "похоже на сген. ник",
  emote_spam: "спам эмодзи",
  zalgo_text_spam: "залго-текст",
  synchronized_arrival: "синхронный приход",
  cluster_membership: "участие в кластере",
  mass_first_messages: "массовые первые сообщения",
};

function signalLabel(name) {
  return SIGNAL_LABELS[name] || name;
}

// verdict.id, отмеченные false positive в этой сессии панели — лента
// приходит целиком заново на каждое сообщение WebSocket (сам вердикт в
// БД никуда не девается, feedback только снижает вес сигнала на будущее),
// без этого списка убранная строка вернулась бы обратно на следующем же
// обновлении.
const dismissedVerdictIds = new Set();

function toast(message, kind = "info") {
  const stack = el("toast-stack");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  stack.appendChild(node);
  setTimeout(() => node.remove(), 5000);
}

// --- авторизация --------------------------------------------------------

function showLoginScreen() {
  el("login-overlay").style.display = "flex";
  el("app-root").style.display = "none";
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
}

function showApp() {
  el("login-overlay").style.display = "none";
  el("app-root").style.display = "flex";
}

async function checkAuth() {
  const resp = await fetch("/auth/me");
  const data = await resp.json();
  if (!data.authenticated) {
    if (data.login_configured === false) {
      el("login-message").textContent =
        "Вход через Twitch не настроен на сервере. Заполните PANEL_TWITCH_CLIENT_ID/SECRET/CHANNEL в .env и перезапустите панель.";
      el("btn-login").disabled = true;
    }
    showLoginScreen();
    return false;
  }
  state.role = data.role;
  state.login = data.login;
  el("user-login").textContent = data.login;
  el("user-avatar").textContent = data.login.slice(0, 2);
  el("user-role-badge").textContent = data.role;
  showApp();
  return true;
}

el("btn-login").addEventListener("click", () => {
  window.location.href = "/auth/login";
});
el("btn-logout").addEventListener("click", async () => {
  await fetch("/auth/logout");
  await checkAuth();
});

// fetch-обёртка: на 401 показывает экран логина вместо тихого падения.
async function apiFetch(url, options = {}) {
  const resp = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (resp.status === 401) {
    showLoginScreen();
    throw new Error("Требуется вход");
  }
  return resp;
}

// --- профили ----------------------------------------------------------

async function loadProfiles() {
  const select = el("profile-select");
  try {
    const resp = await fetch("/api/moderation/profiles");
    const profiles = await resp.json();
    select.innerHTML = "";
    for (const p of profiles) {
      const opt = document.createElement("option");
      opt.value = p.profile;
      // Канал — то, что реально узнаваемо для человека (dobriy_yura,
      // paverpapa); внутренний идентификатор профиля (совпадает с ним же
      // почти всегда) виден в скобках только если отличается, чтобы не
      // дублировать одно и то же дважды в каждой строке списка.
      opt.textContent = p.channel && p.channel !== p.profile ? `${p.channel} (${p.profile})` : p.channel || p.profile;
      select.appendChild(opt);
    }
    if (profiles.some((p) => p.profile === state.profile)) {
      select.value = state.profile;
    } else if (profiles.length) {
      state.profile = profiles[0].profile;
      select.value = state.profile;
    }
  } catch {
    select.innerHTML = '<option value="main">main</option>';
  }
}

// --- навигация ----------------------------------------------------------

const SCREEN_TITLES = {
  live: "Live — активные кластеры",
  users: "Users — зрители канала",
  audit: "Audit — журнал действий модераторов",
  patterns: "Bot Pattern Library",
  attack: "Attack Mode",
  stats: "Stats — статистика и FP-rate",
  settings: "Settings — конфигурация и токен бота",
};

function switchScreen(name) {
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.screen === name));
  document.querySelectorAll(".screen").forEach((s) => s.classList.toggle("active", s.id === `screen-${name}`));
  el("screen-title").textContent = SCREEN_TITLES[name] || "";
  if (name === "users") loadUsers();
  if (name === "audit") loadAudit();
  if (name === "patterns") loadPatterns();
  if (name === "attack") {
    loadAttackMode();
    loadGiveawayMode();
  }
  if (name === "stats") loadStats();
  if (name === "settings") loadSettings();
}

function canAdmin() {
  return ["ADMIN", "OWNER"].includes(state.role);
}

// --- рендер: кластеры ----------------------------------------------------

function riskClass(score) {
  if (score >= 80) return "critical";
  if (score >= 60) return "high";
  if (score >= 30) return "medium";
  return "low";
}

function renderClusters(clusters) {
  const list = el("cluster-list");
  if (!clusters || clusters.length === 0) {
    list.innerHTML = '<div class="empty">Активных кластеров нет — атак не обнаружено</div>';
    return;
  }
  list.innerHTML = "";
  for (const c of clusters) {
    const card = document.createElement("div");
    const cls = riskClass(c.risk_score);
    card.className = `cluster-card risk-${cls}`;
    card.innerHTML = `
      <div class="cluster-head">
        <div class="cluster-title">
          <span class="risk-badge ${cls}">${c.risk_score}</span>
          <span class="cluster-size">${c.size} участников</span>
        </div>
        <div class="cluster-meta">окно прихода ${Math.round(c.arrival_window_sec)}с · схожесть ${(c.similarity_score * 100).toFixed(0)}% · уверенность ${(c.confidence * 100).toFixed(0)}%</div>
      </div>
      <div class="signal-chips"></div>
      <div class="cluster-actions"></div>
    `;
    const chips = card.querySelector(".signal-chips");
    const userIds = c.user_ids || [];
    const logins = c.logins || [];
    for (let i = 0; i < Math.min(logins.length, 12); i++) {
      chips.appendChild(userChip(userIds[i], logins[i]));
    }
    if (logins.length > 12) {
      const more = document.createElement("span");
      more.className = "chip";
      more.textContent = `+${logins.length - 12}`;
      chips.appendChild(more);
    }
    const actions = card.querySelector(".cluster-actions");
    actions.appendChild(actionButton("BAN ALL", "btn-danger", () => confirmClusterAction(c, "BAN")));
    actions.appendChild(actionButton("TIMEOUT ALL", "btn-warning", () => confirmClusterAction(c, "TIMEOUT")));
    actions.appendChild(actionButton("IGNORE CLUSTER", "btn-ghost", () => decideCluster(c.id, "ignore")));
    list.appendChild(card);
  }
}

function userChip(userId, login) {
  const chip = document.createElement("span");
  chip.className = "chip chip-clickable";
  chip.textContent = login;
  chip.title = "Пометить как доверенного (не бот)";
  chip.addEventListener("click", () => markUserSafe(userId, login));
  return chip;
}

function actionButton(label, cls, onClick) {
  const btn = document.createElement("button");
  btn.className = `btn ${cls}`;
  btn.textContent = label;
  btn.disabled = !canAct();
  btn.title = canAct() ? "" : "Требуется роль MODERATOR и выше";
  btn.addEventListener("click", onClick);
  return btn;
}

function canAct() {
  return ["MODERATOR", "ADMIN", "OWNER"].includes(state.role);
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

// --- рендер: лента вердиктов ---------------------------------------------

function renderVerdicts(allVerdicts) {
  const feed = el("verdict-feed");
  const verdicts = (allVerdicts || []).filter((v) => !dismissedVerdictIds.has(v.id));
  if (verdicts.length === 0) {
    feed.innerHTML = '<div class="empty">Пока тихо — подозрительных сообщений не было</div>';
    return;
  }
  feed.innerHTML = "";
  for (const v of verdicts) {
    const row = document.createElement("div");
    row.className = "verdict-row";
    const messageHtml = v.message_text
      ? `<span class="verdict-message">«${escapeHtml(v.message_text)}»</span>`
      : `<span class="verdict-message verdict-message-missing">(текст сообщения недоступен)</span>`;
    row.innerHTML = `
      <span class="verdict-login">${escapeHtml(v.login)}</span>
      <span class="verdict-action ${v.recommended_action}">${v.recommended_action}</span>
      <span class="verdict-time">${formatTime(v.created_at)}</span>
      ${messageHtml}
      <span class="verdict-reason">${escapeHtml(v.reason)}</span>
      <span class="verdict-signals-label">Сработавшие сигналы — левый клик: это ошибка, правый клик: это точно бот</span>
      <span class="signal-chips" style="margin-top:0;"></span>
    `;
    const chips = row.querySelector(".signal-chips");
    const evidenceList = v.signal_evidence || [];
    (v.signal_names || []).forEach((name, i) => {
      const chip = document.createElement("span");
      chip.className = "chip signal-fp-chip";
      // Показываем перевод, но submitSignalFeedback получает исходное
      // английское name — движок и API работают только с ним (см.
      // config/moderation.yml), перевод чисто визуальный.
      chip.textContent = signalLabel(name);
      // evidence — конкретный факт, из-за которого сигнал сработал
      // ("17 пользователей появились за 2.3 сек"), не только его условное
      // имя. Показываем во всплывающей подсказке, чтобы не захламлять
      // саму строку — по наведению видно, что именно бот засёк.
      const evidence = evidenceList[i];
      chip.title = evidence
        ? `${evidence}\n\nКлик — ложное срабатывание · Правый клик — подтвердить, что это бот`
        : "Клик — ложное срабатывание · Правый клик — подтвердить, что это бот";
      chip.addEventListener("click", () => submitSignalFeedback(v, name, row, "FALSE_POSITIVE"));
      // Без обеих кнопок feedback был однобоким: каждый клик "ошибка" всё
      // сильнее занижал вес сигнала (fp_penalty = доля FALSE_POSITIVE
      // среди решений), а подтвердить настоящего бота было нечем — за
      // стрим 124 клика "ошибка" обнулили 8 сигналов на 100%, включая
      // exact_duplicate/synchronized_arrival, которые реально ловят ботов.
      chip.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        submitSignalFeedback(v, name, row, "CONFIRMED_BOT");
      });
      chips.appendChild(chip);
    });
    feed.appendChild(row);
  }
}

async function submitSignalFeedback(verdict, signalName, rowEl, decision = "FALSE_POSITIVE") {
  if (!canAct()) {
    toast("Требуется роль MODERATOR и выше", "error");
    return;
  }
  try {
    const resp = await apiFetch("/api/moderation/feedback", {
      method: "POST",
      body: JSON.stringify({
        profile: state.profile,
        signal_name: signalName,
        decision,
        verdict_id: verdict.id,
        cluster_id: verdict.cluster_id,
        user_id: verdict.user_id,
        pattern_id: verdict.pattern_id,
      }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    const verb = decision === "CONFIRMED_BOT" ? "подтверждено как бот" : "ложное срабатывание";
    toast(`Отмечено: "${signalLabel(signalName)}" — ${verb}`, "success");
    // Строка обработана — прячем сразу (мгновенный отклик) и запоминаем
    // id, чтобы следующий renderVerdicts() из WebSocket (лента приходит
    // целиком заново на каждое сообщение, вердикт в БД никуда не девается)
    // тоже её отфильтровал, а не вернул обратно.
    dismissedVerdictIds.add(verdict.id);
    if (rowEl) {
      rowEl.remove();
      const feed = el("verdict-feed");
      if (!feed.querySelector(".verdict-row")) {
        feed.innerHTML = '<div class="empty">Пока тихо — подозрительных сообщений не было</div>';
      }
    }
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
}

function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// --- действия: подтверждение и вызов API ---------------------------------

let pendingConfirm = null;

function confirmClusterAction(cluster, action) {
  const verb = action === "BAN" ? "забанить" : "выдать таймаут";
  el("modal-title").textContent = `${action} ALL — подтвердите`;
  el("modal-body").textContent =
    `Вы собираетесь ${verb} ~${cluster.size} пользователей из кластера #${cluster.id} ` +
    `(риск ${cluster.risk_score}/100). Точный список подтвердит сервер на момент ` +
    `исполнения — если кластер успел измениться, действие применится к его текущему ` +
    `составу, а не к тому, что видно на экране. Действие попадёт в аудит от имени "${state.login}".`;
  pendingConfirm = async () => {
    try {
      const resp = await apiFetch("/api/moderation/actions", {
        method: "POST",
        body: JSON.stringify({
          profile: state.profile,
          action,
          // target_user_ids всё ещё отправляется для точечных действий без
          // cluster_id, но сервер игнорирует это поле, когда cluster_id
          // указан, и сам подставляет актуальный состав из БД (BUG-001
          // аудита) — здесь оставлено для совместимости и как fallback.
          target_user_ids: cluster.user_ids,
          reason: `Кластер #${cluster.id}, риск ${cluster.risk_score}`,
          cluster_id: cluster.id,
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || resp.statusText);
      }
      const result = await resp.json();
      toast(
        `Задание поставлено в очередь (${action} ALL, кластер #${cluster.id}, целей: ${result.target_count ?? "?"})`,
        "success"
      );
    } catch (e) {
      toast(`Ошибка: ${e.message}`, "error");
    }
  };
  el("modal-overlay").classList.add("open");
}

async function decideCluster(clusterId, decision) {
  try {
    const resp = await apiFetch(`/api/moderation/clusters/${clusterId}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ profile: state.profile }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    toast(`Кластер #${clusterId}: ${decision}`, "success");
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
}

async function markUserSafe(userId, login) {
  if (!canAct()) {
    toast("Требуется роль MODERATOR и выше", "error");
    return;
  }
  try {
    const resp = await apiFetch(`/api/moderation/users/${encodeURIComponent(userId)}/mark_safe`, {
      method: "POST",
      body: JSON.stringify({ profile: state.profile, reason: "отмечен в панели как не бот" }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    toast(`${login} отмечен как доверенный`, "success");
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
}

el("modal-cancel").addEventListener("click", () => {
  pendingConfirm = null;
  el("modal-overlay").classList.remove("open");
});
el("modal-confirm").addEventListener("click", async () => {
  const fn = pendingConfirm;
  pendingConfirm = null;
  el("modal-overlay").classList.remove("open");
  if (fn) await fn();
});

// --- users -----------------------------------------------------------

async function loadUsers() {
  const body = el("users-body");
  const empty = el("users-empty");
  const search = el("users-search").value.trim();
  try {
    const params = new URLSearchParams({ profile: state.profile });
    if (search) params.set("search", search);
    const resp = await apiFetch(`/api/moderation/users?${params.toString()}`);
    const rows = await resp.json();
    if (!rows.length) {
      body.innerHTML = "";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";
    body.innerHTML = rows
      .map((u) => {
        const trustCell = u.marked_safe
          ? `<span class="trust-tag ${escapeHtml(u.trust_level)}">${escapeHtml(u.trust_level)}</span> <span class="safe-tag">✓ safe</span>`
          : `<span class="trust-tag ${escapeHtml(u.trust_level)}">${escapeHtml(u.trust_level)}</span>`;
        return `
        <tr>
          <td>${escapeHtml(u.login)}</td>
          <td>${u.message_count}</td>
          <td>${trustCell}</td>
          <td>${formatTime(u.first_seen)}</td>
          <td>${formatTime(u.last_seen)}</td>
          <td>${u.prior_timeouts}</td>
        </tr>`;
      })
      .join("");
  } catch {
    body.innerHTML = "";
    empty.style.display = "block";
  }
}

el("btn-users-search").addEventListener("click", () => loadUsers());
el("users-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadUsers();
});

// --- audit ----------------------------------------------------------

async function loadAudit() {
  const body = el("audit-body");
  const empty = el("audit-empty");
  try {
    const resp = await apiFetch(`/api/moderation/audit?profile=${encodeURIComponent(state.profile)}`);
    const rows = await resp.json();
    if (!rows.length) {
      body.innerHTML = "";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";
    body.innerHTML = rows
      .map(
        (r) => `
        <tr>
          <td>${formatTime(r.created_at)}</td>
          <td>${escapeHtml(r.actor)}</td>
          <td><span class="role-tag">${escapeHtml(r.actor_role)}</span></td>
          <td>${escapeHtml(r.action)}</td>
          <td>${escapeHtml(r.scope)}${r.cluster_id ? ` #${r.cluster_id}` : ""}</td>
          <td>${escapeHtml(r.reason || "")}</td>
          <td>${r.succeeded}/${r.succeeded + r.failed} успешно</td>
        </tr>`
      )
      .join("");
  } catch {
    body.innerHTML = "";
    empty.style.display = "block";
  }
}

// --- patterns (Bot Pattern Library, этап 9b) ------------------------------

async function loadPatterns() {
  el("patterns-admin-hint").style.display = canAdmin() ? "none" : "block";
  el("new-pattern-card").style.display = canAdmin() ? "block" : "none";

  const list = el("pattern-list");
  try {
    const resp = await apiFetch(`/api/moderation/patterns?profile=${encodeURIComponent(state.profile)}`);
    const patterns = await resp.json();
    if (!patterns.length) {
      list.innerHTML = '<div class="empty">Паттернов нет</div>';
      return;
    }
    list.innerHTML = "";
    for (const p of patterns) {
      const row = document.createElement("div");
      row.className = `pattern-row${p.enabled ? "" : " disabled"}`;
      const metaParts = [];
      if (p.required_signal_names.length) metaParts.push(`сигналы: ${p.required_signal_names.map(signalLabel).join(", ")}`);
      if (p.min_families) metaParts.push(`семейств ≥ ${p.min_families}`);
      if (p.min_risk_score) metaParts.push(`risk ≥ ${p.min_risk_score}`);
      if (p.min_confidence) metaParts.push(`confidence ≥ ${p.min_confidence}`);
      if (p.min_cluster_size) metaParts.push(`размер кластера ≥ ${p.min_cluster_size}`);
      row.innerHTML = `
        <span class="pattern-name">${escapeHtml(p.name)}</span>
        <span class="pattern-meta">${escapeHtml(p.description || "")}${p.description ? " — " : ""}${escapeHtml(metaParts.join(" · "))}</span>
      `;
      const actions = document.createElement("div");
      actions.className = "pattern-actions";
      if (canAdmin()) {
        const toggleBtn = document.createElement("button");
        toggleBtn.className = "btn btn-ghost btn-small";
        toggleBtn.textContent = p.enabled ? "Отключить" : "Включить";
        toggleBtn.addEventListener("click", () => togglePattern(p.id, !p.enabled));
        actions.appendChild(toggleBtn);

        const deleteBtn = document.createElement("button");
        deleteBtn.className = "btn btn-danger btn-small";
        deleteBtn.textContent = "Удалить";
        deleteBtn.addEventListener("click", () => deletePattern(p.id, p.name));
        actions.appendChild(deleteBtn);
      }
      row.appendChild(actions);
      list.appendChild(row);
    }
  } catch (e) {
    list.innerHTML = '<div class="empty">Не удалось загрузить паттерны</div>';
  }
}

async function togglePattern(patternId, enabled) {
  try {
    const resp = await apiFetch(`/api/moderation/patterns/${patternId}/enabled`, {
      method: "POST",
      body: JSON.stringify({ profile: state.profile, enabled }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    await loadPatterns();
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
}

async function deletePattern(patternId, name) {
  try {
    const resp = await apiFetch(`/api/moderation/patterns/${patternId}/delete`, {
      method: "POST",
      body: JSON.stringify({ profile: state.profile }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    toast(`Паттерн "${name}" удалён`, "success");
    await loadPatterns();
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
}

el("btn-create-pattern").addEventListener("click", async () => {
  const name = el("np-name").value.trim();
  if (!name) {
    toast("Укажите имя паттерна", "error");
    return;
  }
  const signals = el("np-signals").value.split(",").map((s) => s.trim()).filter(Boolean);
  const payload = {
    profile: state.profile,
    name,
    description: el("np-description").value.trim(),
    required_signal_names: signals,
    min_families: parseInt(el("np-min-families").value, 10) || 0,
    min_risk_score: parseInt(el("np-min-risk").value, 10) || 0,
    min_confidence: parseFloat(el("np-min-confidence").value) || 0,
    min_cluster_size: parseInt(el("np-min-cluster").value, 10) || 0,
    weight: parseFloat(el("np-weight").value) || 1.0,
  };
  try {
    const resp = await apiFetch("/api/moderation/patterns", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    toast(`Паттерн "${name}" создан`, "success");
    el("np-name").value = "";
    el("np-description").value = "";
    el("np-signals").value = "";
    el("np-min-families").value = "0";
    el("np-min-risk").value = "0";
    el("np-min-confidence").value = "0";
    el("np-min-cluster").value = "0";
    el("np-weight").value = "1";
    await loadPatterns();
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
});

// --- attack mode (этап 9c) ------------------------------------------------

function formatDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}м ${rem}с`;
}

async function loadAttackMode() {
  try {
    const resp = await apiFetch(`/api/moderation/attack_mode?profile=${encodeURIComponent(state.profile)}`);
    const data = await resp.json();
    renderAttackStatus(data);
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
}

function renderAttackStatus(data) {
  const label = el("attack-state-label");
  const meta = el("attack-state-meta");
  const banner = el("attack-banner");
  const remaining = el("attack-remaining");
  const activateBtn = el("btn-attack-activate");
  const deactivateBtn = el("btn-attack-deactivate");
  const durationRow = el("attack-duration-row");

  if (data.active) {
    label.textContent = "ВКЛЮЧЁН";
    label.className = "big-state on";
    meta.textContent = `Активирован ${escapeHtml(data.activated_by || "?")}, осталось ${formatDuration(data.seconds_remaining || 0)}`;
    banner.style.display = "flex";
    remaining.textContent = formatDuration(data.seconds_remaining || 0);
    durationRow.style.display = "none";
    activateBtn.style.display = "none";
    deactivateBtn.style.display = canAdmin() ? "inline-block" : "none";
  } else {
    label.textContent = "ВЫКЛЮЧЕН";
    label.className = "big-state off";
    meta.textContent = "Обычные пороги детекции";
    banner.style.display = "none";
    durationRow.style.display = "flex";
    activateBtn.style.display = canAdmin() ? "inline-block" : "none";
    deactivateBtn.style.display = "none";
  }
  if (!canAdmin()) {
    activateBtn.style.display = "none";
    deactivateBtn.style.display = "none";
  }
}

el("btn-attack-activate").addEventListener("click", () => {
  const duration = parseInt(el("attack-duration").value, 10) || 1800;
  el("modal-title").textContent = "Включить Attack Mode?";
  el("modal-body").textContent =
    `Пороги детекции будут снижены для ВСЕГО канала на ${formatDuration(duration)}. ` +
    `BAN всё равно требует 2+ независимых семейства сигналов — этот инвариант Attack Mode обойти не может.`;
  pendingConfirm = async () => {
    try {
      const resp = await apiFetch("/api/moderation/attack_mode/activate", {
        method: "POST",
        body: JSON.stringify({ profile: state.profile, duration_seconds: duration }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || resp.statusText);
      }
      toast("Attack Mode включён", "success");
      await loadAttackMode();
    } catch (e) {
      toast(`Ошибка: ${e.message}`, "error");
    }
  };
  el("modal-overlay").classList.add("open");
});

el("btn-attack-deactivate").addEventListener("click", async () => {
  try {
    const resp = await apiFetch("/api/moderation/attack_mode/deactivate", {
      method: "POST",
      body: JSON.stringify({ profile: state.profile }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    toast("Attack Mode выключен", "success");
    await loadAttackMode();
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
});

// --- giveaway mode (FALSE-BAN-001 аудита) ---------------------------------

async function loadGiveawayMode() {
  try {
    const resp = await apiFetch(`/api/moderation/giveaway_mode?profile=${encodeURIComponent(state.profile)}`);
    const data = await resp.json();
    renderGiveawayStatus(data);
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
}

function renderGiveawayStatus(data) {
  const label = el("giveaway-state-label");
  const meta = el("giveaway-state-meta");
  const activateBtn = el("btn-giveaway-activate");
  const deactivateBtn = el("btn-giveaway-deactivate");
  const durationRow = el("giveaway-duration-row");

  if (data.active) {
    label.textContent = "ВКЛЮЧЁН";
    label.className = "big-state on";
    meta.textContent = `Активирован ${escapeHtml(data.activated_by || "?")}, осталось ${formatDuration(data.seconds_remaining || 0)}`;
    durationRow.style.display = "none";
    activateBtn.style.display = "none";
    deactivateBtn.style.display = canAdmin() ? "inline-block" : "none";
  } else {
    label.textContent = "ВЫКЛЮЧЕН";
    label.className = "big-state off";
    meta.textContent = "Обычные пороги детекции";
    durationRow.style.display = "flex";
    activateBtn.style.display = canAdmin() ? "inline-block" : "none";
    deactivateBtn.style.display = "none";
  }
  if (!canAdmin()) {
    activateBtn.style.display = "none";
    deactivateBtn.style.display = "none";
  }
}

el("btn-giveaway-activate").addEventListener("click", () => {
  const duration = parseInt(el("giveaway-duration").value, 10) || 900;
  el("modal-title").textContent = "Включить Giveaway Mode?";
  el("modal-body").textContent =
    `Чувствительность детекции будет снижена для ВСЕГО канала на ${formatDuration(duration)} — ` +
    `используйте перед стартом розыгрыша, чтобы массовые "!giveaway" не выглядели как атака.`;
  pendingConfirm = async () => {
    try {
      const resp = await apiFetch("/api/moderation/giveaway_mode/activate", {
        method: "POST",
        body: JSON.stringify({ profile: state.profile, duration_seconds: duration }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || resp.statusText);
      }
      toast("Giveaway Mode включён", "success");
      await loadGiveawayMode();
    } catch (e) {
      toast(`Ошибка: ${e.message}`, "error");
    }
  };
  el("modal-overlay").classList.add("open");
});

el("btn-giveaway-deactivate").addEventListener("click", async () => {
  try {
    const resp = await apiFetch("/api/moderation/giveaway_mode/deactivate", {
      method: "POST",
      body: JSON.stringify({ profile: state.profile }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    toast("Giveaway Mode выключен", "success");
    await loadGiveawayMode();
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
});

// Баннер в topbar виден на любом экране, не только на screen-attack — лёгкий
// фоновый опрос раз в 15 сек достаточен (панель узнаёт об изменении и через
// переход на сам экран Attack Mode, здесь только фоновая индикация).
async function pollAttackBanner() {
  try {
    const resp = await fetch(`/api/moderation/attack_mode?profile=${encodeURIComponent(state.profile)}`);
    if (resp.status === 401) return;
    const data = await resp.json();
    const banner = el("attack-banner");
    const remaining = el("attack-remaining");
    if (data.active) {
      banner.style.display = "flex";
      remaining.textContent = formatDuration(data.seconds_remaining || 0);
    } else {
      banner.style.display = "none";
    }
  } catch {
    // тихая фоновая проверка — не мешаем пользователю тостами
  }
}

// --- stats (этап 9d) -------------------------------------------------

async function loadStats() {
  const tiles = el("stats-tiles");
  const fpBody = el("fp-stats-body");
  const fpEmpty = el("fp-stats-empty");
  try {
    const resp = await apiFetch(`/api/moderation/stats/daily?profile=${encodeURIComponent(state.profile)}&days=30`);
    const days = await resp.json();
    const totals = days.reduce(
      (acc, d) => {
        acc.total_messages += d.total_messages || 0;
        acc.suspicious += d.suspicious || 0;
        acc.would_timeout += d.would_timeout || 0;
        acc.would_ban += d.would_ban || 0;
        acc.actual_timeouts += d.actual_timeouts || 0;
        acc.actual_bans += d.actual_bans || 0;
        acc.clusters += d.clusters || 0;
        acc.false_positives += d.false_positives || 0;
        return acc;
      },
      { total_messages: 0, suspicious: 0, would_timeout: 0, would_ban: 0, actual_timeouts: 0, actual_bans: 0, clusters: 0, false_positives: 0 }
    );
    const tileData = [
      ["Сообщений (30д)", totals.total_messages],
      ["Подозрительных", totals.suspicious],
      ["would_timeout", totals.would_timeout],
      ["would_ban", totals.would_ban],
      ["Реальных таймаутов", totals.actual_timeouts],
      ["Реальных банов", totals.actual_bans],
      ["Кластеров", totals.clusters],
      ["False positives", totals.false_positives],
    ];
    tiles.innerHTML = tileData
      .map(([label, value]) => `<div class="stat-tile"><div class="stat-value">${value}</div><div class="stat-label">${label}</div></div>`)
      .join("");
  } catch (e) {
    tiles.innerHTML = '<div class="empty">Не удалось загрузить статистику</div>';
  }

  try {
    const resp = await apiFetch(`/api/moderation/feedback?profile=${encodeURIComponent(state.profile)}&limit=500`);
    const feedback = await resp.json();
    const bySignal = new Map();
    for (const f of feedback) {
      const entry = bySignal.get(f.signal_name) || { total: 0, fp: 0 };
      entry.total += 1;
      if (f.decision === "FALSE_POSITIVE") entry.fp += 1;
      bySignal.set(f.signal_name, entry);
    }
    const rows = [...bySignal.entries()]
      .map(([name, e]) => ({ name, total: e.total, fp: e.fp, rate: e.total ? e.fp / e.total : 0 }))
      .sort((a, b) => b.rate - a.rate);
    if (!rows.length) {
      fpBody.innerHTML = "";
      fpEmpty.style.display = "block";
    } else {
      fpEmpty.style.display = "none";
      fpBody.innerHTML = rows
        .map(
          (r) => `
          <tr>
            <td>${escapeHtml(r.name)}</td>
            <td>${r.total}</td>
            <td>${r.fp}</td>
            <td>
              <div style="display:flex;align-items:center;gap:8px;">
                <div class="fp-bar-track"><div class="fp-bar-fill" style="--fill:${r.rate};"></div></div>
                <span>${(r.rate * 100).toFixed(0)}%</span>
              </div>
            </td>
          </tr>`
        )
        .join("");
    }
  } catch (e) {
    fpBody.innerHTML = "";
    fpEmpty.style.display = "block";
  }
}

// --- settings: токен бота + config/moderation.yml ---------------------

async function loadSettings() {
  await Promise.all([loadBotTokenStatus(), loadConfig()]);
}

async function loadBotTokenStatus() {
  const badge = el("token-status-badge");
  const meta = el("token-status-meta");
  const btn = el("btn-get-bot-token");
  try {
    const resp = await fetch("/auth/bot/status");
    const data = await resp.json();
    if (data.configured) {
      badge.textContent = `настроен (${data.bot_login})`;
      badge.className = "token-status-badge ok";
      meta.textContent = "Executor может выполнять реальные действия этим токеном.";
    } else {
      badge.textContent = "не настроен";
      badge.className = "token-status-badge missing";
      meta.textContent = "Executor не сможет выполнять реальные действия, пока токен не получен.";
    }
  } catch {
    badge.textContent = "неизвестно";
    badge.className = "token-status-badge missing";
  }
  btn.disabled = !canAdmin();
  btn.title = canAdmin() ? "" : "Требуется роль ADMIN и выше";
}

el("btn-get-bot-token").addEventListener("click", () => {
  el("modal-title").textContent = "Получить токен бота?";
  // innerHTML — единственное место в файле (везде остальном textContent,
  // см. остальные modal-body): текст статичный литерал, не пользовательский
  // ввод, нужен только чтобы дать ссылку на twitch.tv/logout и жирный текст.
  el("modal-body").innerHTML =
    "На следующем экране войдите на Twitch <b>ПОД АККАУНТОМ БОТА</b> (не под своим личным) — " +
    "именно этот аккаунт получит права на реальные баны/таймауты. Аккаунт бота также " +
    "должен быть модератором канала.<br><br>" +
    "<b>Если вы только что входили в панель под другим Twitch-аккаунтом:</b> Twitch " +
    "запомнил его в этом браузере и может подставить его автоматически, минуя выбор " +
    'аккаунта. Сначала выйдите из Twitch — <a href="https://www.twitch.tv/logout" ' +
    'target="_blank" rel="noopener">twitch.tv/logout</a> (откроется в новой вкладке), ' +
    "затем возвращайтесь сюда и жмите «Продолжить».";
  pendingConfirm = async () => {
    window.location.href = "/auth/bot/login";
  };
  el("modal-overlay").classList.add("open");
});

let configLoaded = false;

async function loadConfig() {
  el("cfg-parse-error").classList.remove("show");
  el("cfg-restart-notice").classList.remove("show");
  try {
    const resp = await apiFetch("/api/moderation/config");
    if (resp.status === 404) {
      el("settings-not-loaded").style.display = "block";
      configLoaded = false;
      return;
    }
    el("settings-not-loaded").style.display = "none";
    const data = await resp.json();

    el("cfg-yaml-editor").value = data.yaml_text;
    if (data.parse_error) {
      el("cfg-parse-error").textContent = `Текущий файл не парсится: ${data.parse_error}`;
      el("cfg-parse-error").classList.add("show");
    }
    if (data.mode) el("cfg-mode").value = data.mode;
    if (data.risk_thresholds) {
      el("cfg-risk-observe").value = data.risk_thresholds.observe;
      el("cfg-risk-timeout").value = data.risk_thresholds.timeout;
      el("cfg-risk-ban").value = data.risk_thresholds.ban;
    }
    configLoaded = true;
  } catch (e) {
    toast(`Ошибка загрузки конфига: ${e.message}`, "error");
  }
  const canEdit = canAdmin();
  el("cfg-yaml-editor").disabled = !canEdit;
  el("btn-save-config").disabled = !canEdit;
  el("btn-save-config").title = canEdit ? "" : "Требуется роль ADMIN и выше";
}

// Быстрые поля (mode/пороги) — это удобный редактор поверх того же текста
// в yaml-editor, не отдельный источник правды: при изменении полей
// подставляем значения прямо в YAML-текст простой строковой заменой команд
// верхнего уровня, а не пересобираем весь YAML — так ручные правки
// остального файла в textarea не теряются.
function applyQuickFieldsToYaml() {
  let text = el("cfg-yaml-editor").value;
  const mode = el("cfg-mode").value;
  const observe = el("cfg-risk-observe").value;
  const timeout = el("cfg-risk-timeout").value;
  const ban = el("cfg-risk-ban").value;

  if (/^mode:.*$/m.test(text)) {
    text = text.replace(/^mode:.*$/m, `mode: ${mode}`);
  } else {
    text = `mode: ${mode}\n${text}`;
  }

  const thresholdsBlock = `risk_thresholds:\n  observe: ${observe}\n  timeout: ${timeout}\n  ban: ${ban}`;
  if (/^risk_thresholds:\n(?:[ \t].*\n?)*/m.test(text)) {
    text = text.replace(/^risk_thresholds:\n(?:[ \t].*\n?)*/m, `${thresholdsBlock}\n`);
  } else {
    text = `${text}\n${thresholdsBlock}\n`;
  }

  el("cfg-yaml-editor").value = text;
}

["cfg-mode", "cfg-risk-observe", "cfg-risk-timeout", "cfg-risk-ban"].forEach((id) => {
  el(id).addEventListener("change", () => {
    if (configLoaded) applyQuickFieldsToYaml();
  });
});

el("btn-reload-config").addEventListener("click", () => loadConfig());

el("btn-save-config").addEventListener("click", async () => {
  el("cfg-parse-error").classList.remove("show");
  el("cfg-restart-notice").classList.remove("show");
  try {
    const resp = await apiFetch("/api/moderation/config", {
      method: "POST",
      body: JSON.stringify({ yaml_text: el("cfg-yaml-editor").value }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      el("cfg-parse-error").textContent = body.detail || resp.statusText;
      el("cfg-parse-error").classList.add("show");
      return;
    }
    el("cfg-restart-notice").classList.add("show");
    toast("Конфиг сохранён", "success");
  } catch (e) {
    toast(`Ошибка: ${e.message}`, "error");
  }
});

// --- WebSocket live-обновления ---------------------------------------

function connectWs() {
  if (state.ws) {
    state.ws.close();
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/moderation/ws`);
  state.ws = ws;

  ws.addEventListener("open", () => {
    ws.send(state.profile);
    setConnStatus(true);
    state.wsRetryMs = 1000;
  });
  ws.addEventListener("message", (ev) => {
    try {
      const data = JSON.parse(ev.data);
      renderClusters(data.clusters);
      renderVerdicts(data.verdicts);
    } catch {
      // игнорируем нераспарсенные сообщения — не роняем соединение
    }
  });
  ws.addEventListener("close", () => {
    setConnStatus(false);
    if (el("app-root").style.display !== "none") {
      setTimeout(connectWs, state.wsRetryMs);
      state.wsRetryMs = Math.min(state.wsRetryMs * 1.5, 15000);
    }
  });
  ws.addEventListener("error", () => ws.close());
}

function setConnStatus(on) {
  el("conn-dot").className = `conn-dot ${on ? "on" : "off"}`;
  el("conn-label").textContent = on ? "live" : "переподключение…";
}

// --- инициализация ----------------------------------------------------

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => switchScreen(item.dataset.screen));
  // role="button" на div не даёт активацию по Enter/Space бесплатно, как
  // у нативной <button> — добавляем вручную для клавиатурной доступности.
  item.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      switchScreen(item.dataset.screen);
    }
  });
});

el("profile-select").addEventListener("change", (e) => {
  state.profile = e.target.value;
  localStorage.setItem("mod.profile", state.profile);
  connectWs();
  loadAudit();
});

// --- статус и управление чат-ботом (main.py, twitch-bots) --------------
// Отдельно от запуска профилей на экране "Боты": там процесс поднимается
// по профилю (свой .env.<profile>, свой INSTANCE), здесь — ровно один
// мульти-канальный бот модерации. Раньше это были ещё и разные процессы
// панели на разных портах, и оговорка "не проксируя через 8765" имела
// смысл; теперь оба экрана в одном приложении (см. panel/server.py).

async function refreshChatbotStatus() {
  const pulse = el("chatbotPulse");
  const text = el("chatbotStatusText");
  if (!pulse || !text) return;
  try {
    const resp = await apiFetch("/api/registry/bot/status");
    const s = await resp.json();
    pulse.classList.toggle("on", s.running);
    text.textContent = s.running ? `работает (pid ${s.pid})` : "остановлен";
  } catch {
    text.textContent = "нет данных";
  }
}

el("btn-chatbot-start").addEventListener("click", async () => {
  el("btn-chatbot-start").disabled = true;
  try {
    const resp = await apiFetch("/api/registry/bot/start", { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(err.detail || "Не удалось запустить бота");
    }
  } finally {
    el("btn-chatbot-start").disabled = false;
    await refreshChatbotStatus();
  }
});

el("btn-chatbot-stop").addEventListener("click", async () => {
  el("btn-chatbot-stop").disabled = true;
  try {
    await apiFetch("/api/registry/bot/stop", { method: "POST" });
  } finally {
    el("btn-chatbot-stop").disabled = false;
    await refreshChatbotStatus();
  }
});

(async function init() {
  const ok = await checkAuth();
  if (!ok) return;
  await loadProfiles();
  connectWs();
  await pollAttackBanner();
  setInterval(pollAttackBanner, 15000);
  await refreshChatbotStatus();
  setInterval(refreshChatbotStatus, 5000);
})();
