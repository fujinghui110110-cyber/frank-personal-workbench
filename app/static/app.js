(() => {
  "use strict";

  const state = {
    actor: null,
    route: "today",
    online: navigator.onLine,
    overview: null,
    matters: [],
    reviews: [],
    nodes: [],
    selectedFile: null,
    sourceType: "text",
    chatSource: "all",
    wechatTab: "pending",
    policyTab: "pending",
    matterTab: "all",
    matterStatus: "open",
    reviewTab: "business",
    people: [],
    assigneeReviews: [],
    wechatNewOnly: false,
    queueCount: 0,
searchQuery: "",
searchIndex: [],
searchResults: null,
searchTimer: null,
searchFocus: false,
searchFilters: { source: "", status: "", dateFrom: "", dateTo: "", amount: "" },
    showEmptyPeople: false,
    homeRefreshTimer: null,
    todayDisclosures: { weekly: false, rules: false },
    sourceSyncRunning: false,
    sourceReceiptDismissed: false,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [
    ...root.querySelectorAll(selector),
  ];
  const page = () => $("#page-content");

  const api = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (!headers.has("Accept")) headers.set("Accept", "application/json");
    const response = await fetch(path, {
      credentials: "include",
      ...options,
      headers,
    });
    if (response.status === 401) {
      showLogin();
      throw new Error("会话已过期，请重新登录");
    }
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      try {
        const body = await response.json();
        message = body.detail || message;
      } catch (_) {}
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    if (response.status === 204) return null;
    const type = response.headers.get("content-type") || "";
    return type.includes("json") ? response.json() : response.text();
  };

  async function apiOptional(path, fallback) {
    try {
      return await api(path);
    } catch (_) {
      return fallback;
    }
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(
      /[&<>'"]/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          "'": "&#39;",
          '"': "&quot;",
        })[char],
    );
  }

  function humanText(value, fallback = "", maxLength = 220) {
    let text = String(value ?? "")
      .replace(/\s+/g, " ")
      .trim();

  text = text
    .replace(/Work\x42uddy(?:\s*\u8d22\u52a1\u53c2\u8c0b\u957f)?/gi, "贾维斯")
    .replace(/(?:\u4e2d\u6d77)?\u795e\u5dde\u9ad8\u5c14\u592b\u7403\u4f1a|\u9ad8\u5c14\u592b\u7403\u4f1a\u8d22\u52a1\u8d1f\u8d23\u4eba|\u8d22\u52a1\u53c2\u8c0b\u957f/g, "Frank 的个人工作台")
    .replace(
        /\b(?:matter|material|mat|job|reminder|review|action|evidence)_[a-z0-9-]+\b/gi,
        "",
      )
      .replace(/\b(?:provider|status)\s*=\s*[a-z0-9_-]+\b/gi, "")
      .replace(
        /\b(?:processed|queued|claimed|succeeded|retryable_failed|needs_review)\b/gi,
        "",
      )
      .replace(/(?:\(\s*\)|（\s*）|\[\s*\])/g, "")
      .replace(/\s+([，。；：、,.!?])/g, "$1")
      .replace(/\s{2,}/g, " ")
      .trim();
    const content = text.match(
      /(?:^|[,\s{])\s*["']content["']\s*:\s*"((?:\\.|[^"\\])*)"/i,
    );
    if (content)
      text = content[1].replace(/\\"/g, '"').replace(/\\n/g, " ").trim();
    const exportData = text.search(
      /\s+\{\s*["'](?:weflow|chatlab|session|meta)["']\s*:/i,
    );
    if (exportData > 0) text = text.slice(0, exportData).trim();
    if (
      !text ||
      /^\s*[\[{]/.test(text) ||
      /["'](?:weflow|chatlab|messages|avatars|session)["']\s*:/.test(text)
    )
      return fallback;
    return text.length > maxLength
      ? `${text.slice(0, maxLength - 3)}...`
      : text;
  }

function emailSubject(value, fallback = "无主题邮件", maxLength = 180) {
  const text = humanText(value, fallback, maxLength).replace(
    /^(?:(?:re|fw|fwd|回复|转发)\s*[:：]\s*)+/i,
    "",
  );
  return text.trim() || fallback;
}

function emailSummary(value, fallback, maxLength) {
  return humanText(value, fallback, maxLength)
    .replace(/^(?:[^，。；]{0,120}发起[，,]\s*)?[^，。；]{1,80}转发[，,]\s*/, "")
    .trim();
}

function friendlyError(error, fallback = "暂时无法完成，请稍后再试") {
    const text = String(error?.message || "").trim();
    if (
      !text ||
      /failed to fetch|load failed|networkerror|traceback|exception|sql|json/i.test(
        text,
      ) ||
      /^\s*[\[{]/.test(text)
    )
      return fallback;
    return humanText(text, fallback, 120);
  }

  function businessPersonLabel(value) {
    const text = String(value ?? "")
      .replace(/\((?:wxid_|gh_)?[a-z0-9_.-]{5,}\)/gi, "")
      .trim();
    if (
      !text ||
      /^(?:wxid_|gh_|wecom:)/i.test(text) ||
      /^[a-z][a-z0-9_.-]{5,}$/i.test(text)
    )
      return "";
    return text;
  }

  function matterTitle(value, fallback = "待整理事项", maxLength = 100) {
    return humanText(value, fallback, maxLength)
      .replace(/^群聊_/, "群聊：")
      .replace(/^私聊_/, "私聊：")
      .replace(/[-_](?:音频|录音|视频)$/i, "");
  }

  function mergeableMatters(matters = []) {
    return matters.filter(
      (matter) => matter && !matter.is_completed && matter.status !== "completed",
    );
  }

  const DEFAULT_PEOPLE = [
    { id: "person_self", name: "我自己", role: "Frank" },
    { id: "person_sun_qing", name: "孙庆", role: "片区财务领导" },
    { id: "person_li_jing", name: "李静", role: "仓管员" },
    { id: "person_ou_bo", name: "欧波", role: "仓管员" },
    { id: "person_feng_lixiang", name: "冯李香", role: "采购" },
    { id: "person_chen_zhenting", name: "陈贞婷", role: "出纳、兼职文员" },
    { id: "person_pan_chaohui", name: "潘朝荟", role: "应收、收入、资产管理、收入审计" },
    { id: "person_zhu_qingxia", name: "朱青霞", role: "总账主管" },
  ];

  function normalizePeople(items) {
    const source = Array.isArray(items) && items.length ? items : DEFAULT_PEOPLE;
    return source
      .map((item) => ({
        id: String(item.id || item.person_id || item.name || item.display_name || "").trim(),
        name: humanText(item.name || item.display_name, "未命名人员", 40),
        role: humanText(item.role || item.title || item.position, "", 80),
        open_action_count: Number(item.open_action_count || 0),
        is_self: Boolean(item.is_self || item.id === "person_self" || item.name === "我自己"),
      }))
      .filter((item) => item.id && item.name);
  }

  function personName(personId, people = state.people) {
    const person = people.find((item) => String(item.id) === String(personId));
    return person?.name || "未命名人员";
  }

  function assigneePersonId(item) {
    return item?.person_id || item?.person?.id || item?.id || "";
  }

  function confirmedAssignees(action) {
    return (action?.assignees || []).filter((item) =>
      ["confirmed", "manual"].includes(item.status || "confirmed"),
    );
  }

  function pendingAssignees(action) {
    const suggestions = action?.assignee_suggestions || [];
    const pending = (action?.assignees || []).filter(
      (item) => (item.status || "") === "pending",
    );
    return suggestions.length ? suggestions : pending;
  }

  function assigneeNames(items, people = state.people) {
    return items
      .map(
        (item) =>
          item.name ||
          item.display_name ||
          item.person_name ||
          item.person?.name ||
          personName(assigneePersonId(item), people),
      )
      .filter(Boolean);
  }

  function actionAssigneeText(action, people = state.people) {
    const confirmed = assigneeNames(confirmedAssignees(action), people);
    if (confirmed.length) return `负责人：${confirmed.join("、")}`;
    const suggested = assigneeNames(pendingAssignees(action), people);
    if (suggested.length) return `建议：${suggested.join("、")}，待确认`;
    return action?.owner
      ? `负责人：${humanText(action.owner, "待明确", 80)}`
      : "负责人待明确";
  }

  function actionMatterLabel(action) {
    return matterTitle(
      action?.matter_title || action?.matter?.title || action?.matter_name,
      "相关事项",
      80,
    );
  }

  function actionTitle(action) {
    return humanText(action?.title || action?.next_step, "待推进事项", 140);
  }

  function actionDetail(action) {
    return humanText(
      action?.detail || action?.summary || action?.reason,
      "打开原始材料查看依据。",
      260,
    );
  }

  function renderAssigneeEditor(action, people, selectedIds = []) {
    const selected = new Set(selectedIds.map(String));
    const options = people
      .map(
        (person) =>
          `<label class="person-check"><input type="checkbox" value="${escapeHtml(person.id)}" ${selected.has(String(person.id)) ? "checked" : ""}><span>${escapeHtml(person.name)}</span><small>${escapeHtml(person.role || "")}</small></label>`,
      )
      .join("");
    return `<div class="assignee-editor" data-assignee-editor="${escapeHtml(action.id)}" hidden><div class="person-check-grid">${options}</div><textarea rows="2" data-assignee-note placeholder="补充负责人判断依据，可不填" aria-label="负责人判断依据"></textarea><footer><button class="button button-primary" type="button" data-assignee-save="${escapeHtml(action.id)}">保存负责人</button><button class="button button-quiet" type="button" data-assignee-none="${escapeHtml(action.id)}">都不负责</button></footer></div>`;
  }

  function materialTitle(item, maxLength = 100) {
    const fallback = `${sourceLabel(item?.source_type)}材料`;
    const title = humanText(item?.filename, fallback, maxLength + 20).replace(
      /\.(?:json|md|markdown|txt|wav|mp3|m4a|mp4|mov|pdf|docx?|xlsx?|pptx?|png|jpe?g|heic)$/i,
      "",
    );
    return matterTitle(title, fallback, maxLength);
  }

  function sourceLabel(source) {
    return (
      {
        text: "文字",
        image: "图片",
        file: "文件",
        audio: "会议录音",
      video: "会议视频",
      wechat_channel: "个人微信",
      wecom_channel: "企业微信",
      assistant_channel: "贾维斯",
        wechat_markdown: "微信记录",
    wecom_approval: "企微审批",
    wechat_auto: "微信自动发现",
    wecom_auto: "企业微信自动发现",
    email_auto: "邮箱自动收件",
      }[source] || "材料"
    );
  }

  function kindLabel(kind) {
    return (
      {
        conclusion: "明确结论",
        task: "下一步",
        risk: "风险",
        decision: "待拍板",
        waiting: "等反馈",
        overdue: "已逾期",
        review: "待确认",
        processing: "处理中",
        follow_up: "主动复查",
      }[kind] || "工作"
    );
  }

  function fmtDate(value, options = {}) {
    if (!value) return "时间待定";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间待定";
    const defaults = {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    };
    return new Intl.DateTimeFormat("zh-CN", { ...defaults, ...options }).format(
      date,
    );
  }

  function greeting() {
    const hour = new Date().getHours();
    if (hour < 11) return "早上好";
    if (hour < 14) return "中午好";
    if (hour < 19) return "下午好";
    return "晚上好";
  }

  function badge(text, tone = "") {
    return `<span class="badge ${tone}">${escapeHtml(text)}</span>`;
  }

  function stateLabel(status) {
    const value = {
      queued: ["已收下", "amber"],
      claimed: ["准备开始", "blue"],
      processing: ["处理中", "blue"],
      transcribed: ["理解会议中", "violet"],
      retryable_failed: ["自动重试", "amber"],
      needs_review: ["需检查", "red"],
      processed: ["已整理", "green"],
      succeeded: ["已整理", "green"],
      pending: ["待你确认", "amber"],
      accepted: ["已确认", "green"],
      edited: ["已修正", "green"],
      rejected: ["已排除", "muted"],
      superseded: ["已更新", "muted"],
      online: ["在线", "green"],
      offline: ["未启动", "muted"],
      active: ["推进中", "blue"],
    }[status] || ["待处理", "muted"];
    return badge(value[0], value[1]);
  }

  function toast(message, tone = "") {
    const node = document.createElement("div");
    node.className = `toast ${tone}`;
    node.textContent = message;
    $("#toast-region").append(node);
    window.setTimeout(() => node.remove(), 4200);
  }

  function setConnection(online = state.online) {
    state.online = online;
    $("#connection-dot")?.classList.toggle("online", online);
    $("#connection-dot")?.classList.toggle("offline", !online);
    if ($("#connection-label"))
      $("#connection-label").textContent = online
        ? "工作台已连接"
        : "连接中断，当前设备暂存";
    if ($("#offline-pill")) $("#offline-pill").hidden = online;
  }

  function showLogin() {
    $("#login-view").hidden = false;
    $("#workbench").hidden = true;
    $("#passcode")?.focus();
  }

  function showWorkbench() {
    $("#login-view").hidden = true;
    $("#workbench").hidden = false;
    setConnection(navigator.onLine);
    renderRoute();
    refreshAnalysisButton();
    refreshShortcutCounts();
  }

  function setShortcutCount(selector, count) {
    const node = $(selector);
    if (!node) return;
    const value = Number(count || 0);
    node.hidden = value <= 0;
    node.textContent = value > 99 ? "99+" : String(value);
    node.closest("a")?.classList.toggle("has-items", value > 0);
  }

  async function refreshShortcutCounts() {
    if (!state.actor) return;
    const [wechat, email, policies, reviews, assignees, nodes, brief] = await Promise.all([
      apiOptional("/api/wechat/status", {}),
      apiOptional("/api/email/status", {}),
      apiOptional("/api/policies/status", {}),
      apiOptional("/api/reviews?review_status=pending", []),
      apiOptional("/api/assignee-reviews?status=pending", []),
      apiOptional("/api/nodes", []),
      apiOptional("/api/today/brief", {}),
    ]);
    updateWechatCount(wechat?.counts?.pending || 0);
    setShortcutCount("#email-count", email?.counts?.active || 0);
    setShortcutCount("#policy-count", policies?.counts?.pending || 0);
  const reviewTotal =
    (Array.isArray(reviews) ? reviews.length : 0) +
    (Array.isArray(assignees) ? assignees.length : 0);
  updateReviewCount(reviewTotal);
    const sourceAttentionTotal =
      Number(wechat?.counts?.pending || 0) +
      Number(email?.counts?.active || 0) +
      Number(policies?.counts?.pending || 0) +
      reviewTotal;
    const attentionTotal = Number(
      brief?.attention_counts?.total ?? sourceAttentionTotal,
    );
  if ($("#attention-total")) {
    $("#attention-total").textContent = attentionTotal
      ? `${attentionTotal} 项等待你处理`
      : "目前没有等待确认";
  }
  const sourceStates = [
    wechat?.personal_wechat || wechat?.sources?.personal_wechat,
    wechat?.wecom || wechat?.sources?.wecom,
    email,
  ];
  const sourceFailures = sourceStates.filter(
    (item) => item && ["failed", "error", "unavailable"].includes(item.status),
  ).length;
  if ($("#source-sync-detail")) {
    $("#source-sync-detail").textContent = sourceFailures
      ? `${sourceFailures} 个来源需要重新检查`
      : "个人微信、企业微信和邮箱可独立读取";
  }
    const online = (Array.isArray(nodes) ? nodes : []).some(
      (item) => item.status === "online",
    );
    $("#shortcut-node-dot")?.classList.toggle("online", online);
    $("#shortcut-node-dot")?.classList.toggle("offline", !online);
  }

  async function refreshAnalysisButton() {
    const button = $("#run-analysis-global");
    if (!button || !state.actor) return;
    try {
      const status = await api("/api/analysis/status");
      const pending = Number(status.pending || 0);
      const running = Number(status.running || 0);
      const needsReview = Number(status.needs_review || 0);
      if ($("#analysis-run-detail")) {
        $("#analysis-run-detail").textContent = running
          ? `正在整理 ${running} 份内容${needsReview ? `，另有 ${needsReview} 份需检查` : ""}`
          : pending
          ? `${pending} 份内容等待整理${needsReview ? `，另有 ${needsReview} 份需检查` : ""}`
          : needsReview
          ? `${needsReview} 份内容整理失败，需要检查`
          : "没有等待整理的新内容";
      }
      button.textContent = pending
        ? `让贾维斯整理（${pending}）${needsReview ? ` · 需检查 ${needsReview}` : ""}`
        : running
        ? `贾维斯正在整理（${running}）`
        : needsReview
        ? `查看需检查内容（${needsReview}）`
        : "让贾维斯整理新内容";
      button.title = running
        ? `贾维斯正在整理 ${status.running} 份内容`
        : "仅在点击后才会消耗分析用量";
      button.setAttribute("aria-busy", running ? "true" : "false");
    } catch (_) {
      button.title = "暂时无法读取待整理数量";
    }
  }

  function routeFromHash() {
    const raw = window.location.hash.replace(/^#\/?/, "") || "today";
    const parts = raw.split("/").filter(Boolean);
    if (parts[0] === "matter" && parts[1])
      return { name: "matter", id: decodeURIComponent(parts[1]) };
    const name = ["today", "intake", "wechat", "email", "policies", "matters", "reviews", "nodes", "search"].includes(
      parts[0],
    )
      ? parts[0]
      : "today";
    return { name };
  }

  function setRoute(route) {
    if (state.homeRefreshTimer) window.clearTimeout(state.homeRefreshTimer);
    state.homeRefreshTimer = null;
    state.route = route.name;
    const active = route.name === "matter" ? "matters" : route.name;
    $$("[data-route]").forEach((link) =>
      link.classList.toggle("active", link.dataset.route === active),
    );
    const titles = {
      today: "今天",
      intake: "随手投递",
      wechat: "聊天线索",
      email: "邮件工作",
      policies: "公司规定",
      matters: "事项推进",
      matter: "事项详情",
      reviews: "需要我拍板",
      nodes: "助理状态",
      search: "搜索",
    };
    $("#page-title").textContent = titles[route.name] || "工作台";
    $("#page-kicker").textContent = new Intl.DateTimeFormat("zh-CN", {
      month: "long",
      day: "numeric",
      weekday: "long",
    }).format(new Date());
  }

  function showLoading() {
    page().replaceChildren($("#loading-template").content.cloneNode(true));
  }

  async function renderRoute({ quiet = false, preserveScroll = false } = {}) {
    const route = routeFromHash();
    const scrollTop = preserveScroll ? window.scrollY : 0;
    setRoute(route);
    if (!quiet) showLoading();
    try {
if (route.name === "today") {
const [brief, overview, materials, wechatStatus, policyStatus, assigneeReviews, weeklyReview, learningRules] = await Promise.all([
api("/api/today/brief"),
api("/api/overview"),
api("/api/materials?limit=30"),
api("/api/wechat/status"),
api("/api/policies/status"),
apiOptional("/api/assignee-reviews?status=pending", []),
apiOptional("/api/weekly-review", null),
apiOptional("/api/learning-rules", []),
]);
renderToday(brief, overview, materials, wechatStatus, policyStatus, assigneeReviews, weeklyReview, learningRules);
    } else if (route.name === "intake") {
      renderIntakePage(await api("/api/analysis/issues"));
    } else if (route.name === "wechat") {
      const sourceQuery = state.chatSource === "all" ? "" : `&source=${encodeURIComponent(state.chatSource)}`;
      const conversationSourceQuery = state.chatSource === "all" ? "" : `?source=${encodeURIComponent(state.chatSource)}`;
      const [wechatStatus, pending, ignored, accepted, conversations, matters] = await Promise.all([
        api("/api/wechat/status"),
        api(`/api/wechat/candidates?candidate_status=pending&limit=300${sourceQuery}`),
        api(`/api/wechat/candidates?candidate_status=ignored&limit=300${sourceQuery}`),
        api(`/api/wechat/candidates?candidate_status=accepted&limit=300${sourceQuery}`),
        api(`/api/wechat/conversations${conversationSourceQuery}`),
        api("/api/matters?limit=100"),
      ]);
      renderWechat(wechatStatus, [...pending, ...ignored, ...accepted], conversations, matters);
    } else if (route.name === "email") {
      const [emailStatus, emailMessages, emailMatters] = await Promise.all([
        api("/api/email/status"),
        api("/api/email/messages?message_status=active&limit=200"),
        api("/api/email/matters"),
        ]);
        renderEmail(emailStatus, emailMessages, emailMatters);
      } else if (route.name === "policies") {
        const [policyStatus, candidates, activePolicies, repealedPolicies] = await Promise.all([
          api("/api/policies/status"),
          api("/api/policy-candidates?candidate_status=all&limit=300"),
          api("/api/policies?policy_status=active"),
          api("/api/policies?policy_status=retired"),
        ]);
        renderPolicies(policyStatus, candidates, activePolicies, repealedPolicies);
      } else if (route.name === "matters") {
        const [matters, people, openActions, doneActions] = await Promise.all([
          api("/api/matters?limit=100"),
          apiOptional("/api/people", []),
          apiOptional("/api/actions?status=open", []),
          apiOptional("/api/actions?status=done", []),
        ]);
        renderMatters(matters, people, { open: openActions, done: doneActions });
} else if (route.name === "matter") {
const [matter, people, timeline] = await Promise.all([
api(`/api/matters/${encodeURIComponent(route.id)}`),
apiOptional("/api/people", []),
apiOptional(`/api/matters/${encodeURIComponent(route.id)}/timeline`, []),
]);
renderMatter(matter, people, timeline);
      } else if (route.name === "reviews") {
        const [reviews, assigneeReviews, people] = await Promise.all([
          api("/api/reviews?review_status=pending"),
          apiOptional("/api/assignee-reviews?status=pending", []),
          apiOptional("/api/people", []),
        ]);
        renderReviews(reviews, assigneeReviews, people);
      } else if (route.name === "nodes") {
        renderNodes(await api("/api/nodes"));
} else if (route.name === "search") {
renderSearch();
}
    } catch (error) {
      renderError(error);
    } finally {
      if (preserveScroll) {
        window.scrollTo({ top: scrollTop, left: 0, behavior: "instant" });
      }
    }
  }

async function refreshRouteWithoutJump(focusSelectorOverride = null) {
  const surface = page();
  const scrollTop = window.scrollY;
    const active = document.activeElement;
    const focusAttribute = active && [...active.attributes].find((item) => item.name.startsWith("data-") && item.value && item.name !== "data-enabled");
    const focusSelector = focusSelectorOverride || (active?.id
      ? `#${CSS.escape(active.id)}`
      : focusAttribute
        ? `[${focusAttribute.name}="${CSS.escape(focusAttribute.value)}"]`
        : null);
    surface.style.minHeight = `${surface.offsetHeight}px`;
    try {
    await renderRoute({ quiet: true });
    const focusTarget = focusSelector ? surface.querySelector(focusSelector) : null;
    if (focusTarget instanceof HTMLElement) focusTarget.focus({ preventScroll: true });
    await new Promise((resolve) => requestAnimationFrame(resolve));
    window.scrollTo(0, scrollTop);
    } finally {
      requestAnimationFrame(() => {
        surface.style.minHeight = "";
      });
    }
  }

  function renderError(error) {
    page().innerHTML = `<section class="error-panel"><span>!</span><div><strong>这里暂时没读出来</strong><p>${escapeHtml(friendlyError(error, "服务暂时不可用"))}。材料不会丢失，可以稍后再试。</p><button class="button button-secondary" type="button" data-retry>重新读取</button></div></section>`;
    $("[data-retry]", page())?.addEventListener("click", renderRoute);
  }

  function materialStage(item) {
    const status = item.status || item.job?.status || "queued";
    const jobStatus = item.job?.status || status;
    const media = item.source_type === "audio" || item.source_type === "video";
    if (status === "processed" || jobStatus === "succeeded")
      return {
        key: "done",
        label: "贾维斯已整理",
        step: 4,
        active: false,
        detail: "重点、下一步和后续追踪已经写回事项。",
      };
    if (status === "transcribed")
      return {
        key: "thinking",
        label: "贾维斯正在理解",
        step: 3,
        active: true,
        detail: "录音已经转成文字，正在判断结论、任务和风险。",
      };
    if (
      ["processing", "claimed"].includes(status) ||
      ["processing", "claimed"].includes(jobStatus)
    )
      return {
        key: "working",
        label: media ? "正在本机转写" : "贾维斯正在研读",
        step: media ? 2 : 3,
        active: true,
        detail: media
          ? "M4 正在把录音转成文字，随后自动交给贾维斯。"
          : "正在理解上下文并归入正确事项。",
      };
    if (status === "retryable_failed" || jobStatus === "retryable_failed")
      return {
        key: "retry",
        label: "稍后自动重试",
        step: 1,
        active: true,
        detail: "本次处理暂时中断，原材料已经保留。",
      };
    if (status === "needs_review" || jobStatus === "needs_review")
      return {
        key: "attention",
        label: "需要检查",
        step: 1,
        active: false,
        detail: "贾维斯暂时没能完成，原材料仍安全保留。",
      };
    return {
      key: "queued",
      label: "已经收下",
      step: 1,
      active: true,
      detail: "已由工作台收下，等待本机后台领取。",
    };
  }

  function renderMaterialProgress(item) {
    const stage = materialStage(item);
    const media = item.source_type === "audio" || item.source_type === "video";
    const steps = [
      "安全收下",
      media ? "本机转写" : "读取材料",
      "贾维斯理解",
      "写回事项",
    ];
    const progress = steps
      .map(
        (label, index) =>
          `<li class="${index < stage.step ? "done" : index === stage.step ? "active" : ""}"><i></i><span>${label}</span></li>`,
      )
      .join("");
    const href = item.matter_id
      ? `#/matter/${encodeURIComponent(item.matter_id)}`
      : "#/matters";
    return `<article class="work-card ${stage.key}">
      <div class="work-card-top"><span class="source-icon">${escapeHtml(sourceLabel(item.source_type).slice(0, 1))}</span><div><small>${escapeHtml(sourceLabel(item.source_type))} · ${fmtDate(item.received_at)}</small><h3>${escapeHtml(materialTitle(item))}</h3></div><span class="live-state"><i></i>${escapeHtml(stage.label)}</span></div>
      <p>${escapeHtml(stage.detail)}</p>
      <ol class="work-progress">${progress}</ol>
      ${item.matter_id ? `<a href="${href}" class="text-link">查看贾维斯结果 →</a>` : ""}
    </article>`;
  }

function planHref(item) {
const matterId = item.matter_id || item.matter?.id;
if (matterId) return `#/matter/${encodeURIComponent(matterId)}`;
if (item.item_type === "review" || ["review", "decision"].includes(item.kind)) return "#/reviews";
return "#/matters";
}

function renderPlanItem(item, index, { controls = false } = {}) {
const title = humanText(item.title, "有一件事需要处理", 140).replace(/^确认推断[：:]\s*/, "");
const detail = humanText(
item.waiting_on
? `正在等待：${item.waiting_on}`
: item.blocked_reason || item.detail || item.summary,
"打开事项查看贾维斯整理的依据。",
260,
);
const reason = humanText(item.sort_reason, "", 120);
const meta = [
kindLabel(item.kind),
item.due_date ? `截止 ${item.due_date}` : "",
].filter(Boolean).join("，");
const actionButtons = controls && item.id && item.item_type !== "review"
? `<button class="button button-primary" type="button" data-today-complete="${escapeHtml(item.id)}">完成</button><button class="button button-secondary" type="button" data-today-snooze="${escapeHtml(item.id)}">稍后处理</button><button class="button button-quiet" type="button" data-today-pin="${escapeHtml(item.id)}" aria-pressed="${item.pinned ? "true" : "false"}">${item.pinned ? "取消固定" : "固定优先"}</button>`
: "";
return `<article class="plan-item ${escapeHtml(item.kind || "work")}"><div class="plan-item-index">${String(index + 1).padStart(2, "0")}</div><div class="plan-item-body"><small>${escapeHtml(meta)}</small><h3><a href="${planHref(item)}">${escapeHtml(title)}</a></h3><p>${escapeHtml(detail)}</p>${reason ? `<div class="plan-reason">${escapeHtml(reason)}</div>` : ""}<footer>${actionButtons}<a class="text-link" href="${planHref(item)}">查看事项</a></footer></div></article>`;
}

function renderPlanList(items, emptyTitle, emptyDetail) {
return items.length
? items.map((item, index) => renderPlanItem(item, index)).join("")
: `<div class="plan-empty"><span>✓</span><div><strong>${escapeHtml(emptyTitle)}</strong><p>${escapeHtml(emptyDetail)}</p></div></div>`;
}

function renderNowCard(item) {
if (!item) return `<article class="now-card is-empty"><div class="now-card-mark">✓</div><div class="now-card-body"><small>现在</small><h2>暂时没有必须立刻处理的事</h2><p>新材料交给贾维斯后，需要你介入的内容会出现在这里。</p><button class="button button-primary" type="button" data-open-intake>交给贾维斯</button></div></article>`;
const controls = item.id && item.item_type !== "review"
? `<footer class="now-actions"><button class="button button-primary" type="button" data-today-complete="${escapeHtml(item.id)}">完成</button><button class="button button-secondary" type="button" data-today-snooze="${escapeHtml(item.id)}">稍后处理</button><button class="button button-quiet" type="button" data-today-pin="${escapeHtml(item.id)}" aria-pressed="${item.pinned ? "true" : "false"}">${item.pinned ? "取消固定" : "固定优先"}</button><a class="text-link" href="${planHref(item)}">查看事项</a></footer>`
: `<footer class="now-actions"><a class="text-link" href="${planHref(item)}">查看详情</a></footer>`;
return `<article class="now-card ${escapeHtml(item.kind || "work")}"><div class="now-card-mark">1</div><div class="now-card-body"><small>现在只做这件</small><h2><a href="${planHref(item)}">${escapeHtml(humanText(item.title, "待处理行动", 180))}</a></h2><p>${escapeHtml(humanText(item.detail, "打开事项查看下一步。", 320))}</p><div class="now-reason"><strong>为什么现在做</strong><span>${escapeHtml(humanText(item.sort_reason, "按截止时间和最近变化排序", 160))}</span></div><dl class="now-facts">${item.due_date ? `<div><dt>截止时间</dt><dd>${escapeHtml(item.due_date)}</dd></div>` : ""}${item.waiting_on ? `<div><dt>等待对象</dt><dd>${escapeHtml(item.waiting_on)}</dd></div>` : ""}${item.blocked_reason ? `<div><dt>阻塞因素</dt><dd>${escapeHtml(item.blocked_reason)}</dd></div>` : ""}</dl>${controls}</div></article>`;
}

function renderWeeklyReview(review) {
if (!review) return "";
const counts = review.counts || {};
const people = (review.people || []).slice(0, 8);
    return `<details class="today-disclosure" data-today-disclosure="weekly" ${state.todayDisclosures.weekly ? "open" : ""}><summary><span>本周复盘</span><strong>${Number(counts.completed || 0)} 项完成，${Number(counts.overdue || 0)} 项超期</strong></summary><div class="weekly-review-grid"><section><h3>本周状态</h3><p>完成 ${Number(counts.completed || 0)} 项，等待 ${Number(counts.waiting || 0)} 项，阻塞 ${Number(counts.blocked || 0)} 项，公司规定变化 ${Number(counts.policy_changes || 0)} 条。</p></section><section><h3>负责人负荷</h3>${people.length ? `<ul>${people.map((item) => `<li><span>${escapeHtml(item.display_name)}</span><strong>${Number(item.open_count || 0)} 项开放${Number(item.overdue_count || 0) ? `，${Number(item.overdue_count)} 项超期` : ""}</strong></li>`).join("")}</ul>` : "<p>目前没有已确认负责人的开放行动。</p>"}</section></div></details>`;
}

  function renderIgnoredConversationRules(rules) {
    const rows = Array.isArray(rules) ? rules : [];
    return `<details class="today-disclosure" data-today-disclosure="rules" ${state.todayDisclosures.rules ? "open" : ""}><summary><span>我的工作规则</span><strong>${rows.filter((item) => item.enabled).length} 条正在使用</strong></summary><div class="learning-rule-list">${rows.length ? rows.map((item) => `<article><div><h3>${escapeHtml(humanText(item.description, "个人规则", 180))}</h3><p>${item.enabled ? "正在使用，可随时停用" : "已停用，需要时可以恢复"}</p></div><button class="button button-secondary" type="button" data-learning-rule="${escapeHtml(item.id)}" data-enabled="${item.enabled ? "true" : "false"}">${item.enabled ? "停用" : "恢复"}</button></article>`).join("") : `<div class="plan-empty"><span>✓</span><div><strong>还没有形成个人规则</strong><p>屏蔽会话和后续纠正会在这里变成可查看的规则。</p></div></div>`}</div></details>`;
}

async function changeTodayAction(button, actionId, change) {
const focusAttribute = [...button.attributes].find((item) => item.name.startsWith("data-") && item.value);
const focusSelector = focusAttribute ? `[${focusAttribute.name}="${CSS.escape(focusAttribute.value)}"]` : null;
button.disabled = true;
try {
if (change === "done") {
await api(`/api/actions/${encodeURIComponent(actionId)}/resolve`, {
method: "POST",
headers: { "Content-Type": "application/json" },
body: JSON.stringify({ status: "done" }),
});
toast("已标记完成", "success");
} else {
const payload = change === "snooze"
? { snoozed_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() }
: { pinned: button.getAttribute("aria-pressed") !== "true" };
await api(`/api/actions/${encodeURIComponent(actionId)}/planning-state`, {
method: "PATCH",
headers: { "Content-Type": "application/json" },
body: JSON.stringify(payload),
});
toast(change === "snooze" ? "已移到明天再处理" : "优先顺序已更新", "success");
}
await refreshRouteWithoutJump(focusSelector);
} catch (error) {
button.disabled = false;
toast(friendlyError(error), "error");
}
}

function attentionHref(item) {
  if (item?.target === "reviews") return "#/reviews";
  if (item?.matter_id) {
    return `#/matter/${encodeURIComponent(item.matter_id)}`;
  }
  return "#/today";
}

function attentionTypeLabel(type) {
  return {
    decision: "业务拍板",
    assignee_confirmation: "跟进人确认",
    overdue: "逾期行动",
    follow_up: "到期跟进",
    reminder: "到期提醒",
    risk: "阻塞风险",
    action: "普通行动",
  }[type] || "需要处理";
}

function renderAttention(attention, counts = {}) {
  const items = Array.isArray(attention) ? attention : [];
  const summary = [
    ["decision", "拍板"],
    ["assignee_confirmation", "跟进人确认"],
    ["overdue", "逾期"],
    ["follow_up", "到期跟进"],
    ["reminder", "提醒"],
    ["risk", "风险"],
    ["action", "行动"],
  ]
    .filter(([key]) => Number(counts[key] || 0) > 0)
    .map(([key, label]) => `${label} ${Number(counts[key])}`)
    .join(" · ");
  const rows = items.length
    ? items
        .map(
          (item, index) => `<article class="plan-item attention-item">
            <div class="plan-item-index">${String(index + 1).padStart(2, "0")}</div>
            <div class="plan-item-body">
              <small>${escapeHtml(attentionTypeLabel(item.item_type))}${item.due_at ? ` · ${escapeHtml(fmtDate(item.due_at))}` : ""}</small>
              <h3><a href="${attentionHref(item)}">${escapeHtml(humanText(item.title, "需要处理", 180))}</a></h3>
              <p>${escapeHtml(humanText(item.reason, "这件事仍需要处理。", 260))}</p>
            </div>
            <a class="button button-secondary" href="${attentionHref(item)}">${item.target === "reviews" ? "去确认" : "查看事项"}</a>
          </article>`,
        )
        .join("")
    : `<div class="plan-empty"><span>✓</span><div><strong>目前没有需要你处理的事项</strong><p>新的拍板、跟进、风险和行动会按优先级出现在这里。</p></div></div>`;
  return `<section class="plan-card today-attention"><header><div><p>统一入口</p><h2>需要我处理</h2></div><span>${items.length} 项</span></header>${summary ? `<p class="attention-summary">${escapeHtml(summary)}</p>` : ""}<div class="plan-list">${rows}</div></section>`;
}

function renderToday(brief, overview, materials, wechatStatus = null, policyStatus = null, assigneeReviews = [], weeklyReview = null, learningRules = []) {
state.overview = overview || {};
const nodeOnline = (state.overview.nodes || []).some((item) => item.status === "online");
const pendingReviews = Number(brief?.counts?.pending_reviews || 0) + (Array.isArray(assigneeReviews) ? assigneeReviews.length : 0);
updateReviewCount(pendingReviews);
updateWechatCount(Number(wechatStatus?.counts?.pending || 0));
  $("#assistant-presence-label").textContent = nodeOnline
    ? "在线，自动读取；整理需手工启动"
    : "本机未连接，材料会等待读取";

const next = Array.isArray(brief?.next) ? brief.next : [];
const waiting = Array.isArray(brief?.waiting) ? brief.waiting : [];
const risks = Array.isArray(brief?.risks) ? brief.risks : [];
const decisions = Array.isArray(brief?.decisions) ? brief.decisions : [];
const policyPending = Number(policyStatus?.counts?.pending || 0);

page().innerHTML = `<section class="today-page"><header class="today-heading"><div><p class="eyebrow">${greeting()}，Frank</p><h2>今天只看下一步。</h2><span>贾维斯已根据截止时间、风险、责任人和跟进日期自动排序。</span></div><div class="today-status"><span class="status-dot ${nodeOnline ? "online" : "offline"}"></span>${nodeOnline ? "本机在线" : "等待本机接手"}</div></header><section class="today-now"><div class="section-title"><div><p>现在</p><h2>只处理一件最重要的事</h2></div><span>更新于 ${escapeHtml(fmtDate(brief?.generated_at))}</span></div>${renderNowCard(brief?.now)}</section><section class="today-plan-grid"><section class="plan-card plan-next"><header><div><p>接下来</p><h2>接下来三项</h2></div><span>${next.length} 件</span></header><div class="plan-list">${renderPlanList(next, "暂时没有排好的下一步", "有新行动后会自动排到这里，最多显示三件。")}</div></section><section class="plan-card"><header><div><p>等待</p><h2>正在等别人</h2></div><span>${waiting.length} 项</span></header><div class="plan-list">${renderPlanList(waiting, "目前没有等待反馈", "等待事项到了跟进日期后会自动回到优先队列。")}</div></section><section class="plan-card"><header><div><p>风险</p><h2>延期或失控风险</h2></div><span>${risks.length} 项</span></header><div class="plan-list">${renderPlanList(risks, "目前没有明显风险", "逾期、阻塞和风险行动会留在这里。")}</div></section><section class="plan-card"><header><div><p>拍板</p><h2>需要你确认</h2></div><span>${decisions.length} 项</span></header><div class="plan-list">${renderPlanList(decisions, "目前没有需要拍板的内容", "付款、审批、对外发送和重大规定变化始终由你确认。")}</div>${pendingReviews ? `<a class="plan-review-link" href="#/reviews">还有 ${pendingReviews} 条负责人或业务判断</a>` : ""}</section></section><section class="today-alerts">${Number(wechatStatus?.counts?.pending || 0) ? `<a href="#/wechat"><span>聊天线索</span><strong>${Number(wechatStatus.counts.pending)} 条等待确认</strong></a>` : ""}${policyPending ? `<a href="#/policies"><span>公司规定</span><strong>${policyPending} 条变化等待确认</strong></a>` : ""}</section>${renderWeeklyReview(weeklyReview)}${renderLearningRules(learningRules)}</section>`;

  const attention = Array.isArray(brief?.attention) ? brief.attention : [];
  page().querySelector(".today-plan-grid")?.insertAdjacentHTML(
    "afterend",
    renderAttention(attention, brief?.attention_counts || {}),
  );
  $$('[data-today-disclosure]').forEach((details) => details.addEventListener('toggle', () => {
      state.todayDisclosures[details.dataset.todayDisclosure] = details.open;
    }));
    $$("[data-today-complete]").forEach((button) => button.addEventListener("click", () => changeTodayAction(button, button.dataset.todayComplete, "done")));
$$("[data-today-snooze]").forEach((button) => button.addEventListener("click", () => changeTodayAction(button, button.dataset.todaySnooze, "snooze")));
$$("[data-today-pin]").forEach((button) => button.addEventListener("click", () => changeTodayAction(button, button.dataset.todayPin, "pin")));
$$("[data-learning-rule]").forEach((button) => button.addEventListener("click", async () => {
const focusSelector = `[data-learning-rule="${CSS.escape(button.dataset.learningRule)}"]`;
button.disabled = true;
try {
await api(`/api/learning-rules/${encodeURIComponent(button.dataset.learningRule)}`, {
method: "PATCH",
headers: { "Content-Type": "application/json" },
body: JSON.stringify({ enabled: button.dataset.enabled !== "true" }),
});
await refreshRouteWithoutJump(focusSelector);
} catch (error) {
button.disabled = false;
toast(friendlyError(error), "error");
}
}));
if (state.homeRefreshTimer) window.clearTimeout(state.homeRefreshTimer);
state.homeRefreshTimer = window.setTimeout(() => {
if (routeFromHash().name === "today") refreshRouteWithoutJump();
}, (Array.isArray(materials) && materials.some((item) => ["queued", "claimed", "processing"].includes(item.status))) ? 10000 : 30000);
}


  function renderLearningRules(rules) {
    const learned = (Array.isArray(rules) ? rules : []).filter(
      (item) => item.rule_type !== "conversation_ignore",
    );
    const defaults = [
      ["读取和整理分开", "微信、企微和邮箱可以自动读取；贾维斯只在你手工点击后整理，避免无谓消耗。"],
      ["只留下需要推进的工作", "普通寒暄、加好友、占位图片和没有后续动作的内容不会要求你确认。"],
      ["同一件事持续合并", "后续聊天和邮件优先追加到仍未完成的事项，不重复建立推进事项。"],
      ["关键判断必须由你确认", "付款、审批、对外发送、重大规定变化和负责人建议不会自动成为最终结论。"],
      ["完成事项退出工作队列", "已经完成的事项不再参与合并，也不会继续占用今日优先级。"],
      ["自动处理可以追溯", "自动过滤、合并和排序保留理由与证据，出现误判时可以撤销或纠正。"],
    ];
    const defaultRows = defaults
      .map(
        ([title, description]) =>
          `<article class="work-rule"><div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(description)}</p></div><span>正在执行</span></article>`,
      )
      .join("");
    const learnedRows = learned
      .map(
        (item) =>
          `<article><div><h3>${escapeHtml(humanText(item.description, "个人规则", 180))}</h3><p>${item.enabled ? "根据你的纠正持续使用" : "已停用，需要时可以恢复"}</p></div><button class="button button-secondary" type="button" data-learning-rule="${escapeHtml(item.id)}" data-enabled="${item.enabled ? "true" : "false"}">${item.enabled ? "停用" : "恢复"}</button></article>`,
      )
      .join("");
    const activeCount = defaults.length + learned.filter((item) => item.enabled).length;
    return `<details class="today-disclosure" data-today-disclosure="rules" ${state.todayDisclosures.rules ? "open" : ""}><summary><span>我的工作方式</span><strong>${activeCount} 条正在执行</strong></summary><div class="learning-rule-list">${defaultRows}${learnedRows}<footer><span>会话监听和屏蔽属于来源设置，不再混进工作规则。</span><a class="text-link" href="#/wechat">管理监听范围</a></footer></div></details>`;
  }
function renderWechatCandidate(item, matters) {
  const sourceName = item.source === "wecom" ? "企业微信" : "个人微信";
  if (item.status === "processing") {
    return `<article class="wechat-candidate is-processing"><div class="wechat-card-head"><div><small>${sourceName} · ${escapeHtml(item.display_name)} ${fmtDate(item.window_end)}</small><h3>这段聊天已收下，等待你启动整理</h3></div><span class="wechat-state">等待整理</span></div><div class="wechat-card-skeleton"><i></i><i></i><i></i></div><p>点击页面顶部“让贾维斯整理新内容”后才会开始理解，不会自动消耗额度。</p></article>`;
  }

  const extracted = item.extracted || {};
  const facts = [
    ...(extracted.amounts || []).map((value) => `金额 ${value}`),
    ...(extracted.dates || []).map((value) => `日期 ${value}`),
    ...(extracted.people || [])
      .map(businessPersonLabel)
      .filter(Boolean)
      .map((value) => `相关人 ${value}`),
    ...(extracted.risks || []).map((value) => `风险 ${value}`),
  ]
    .filter(Boolean)
    .slice(0, 6);
  if (extracted.approval) facts.push(`审批 ${extracted.approval}`);
  const evidence = (item.evidence || [])
    .slice(0, 4)
    .map((value) => `<li>${escapeHtml(humanText(value, "", 360))}</li>`)
    .join("");
    const options = mergeableMatters(matters)
    .map(
      (matter) => `<option value="${escapeHtml(matter.id)}">${escapeHtml(matterTitle(matter.title))}</option>`,
    )
    .join("");
  const label = item.classification === "uncertain" ? "可能相关" : "工作相关";
  return `<article class="wechat-candidate" data-wechat-candidate="${escapeHtml(item.id)}">
    <div class="wechat-card-head"><div><small>${sourceName} · ${escapeHtml(item.display_name)} ${fmtDate(item.window_end)}</small><h3>${escapeHtml(humanText(item.summary, "发现一条可能需要跟进的工作", 220))}</h3></div><span class="wechat-state ${item.classification === "uncertain" ? "uncertain" : ""}">${label}</span></div>
    ${item.uncertainty_reason ? `<p class="wechat-uncertain">贾维斯拿不准：${escapeHtml(humanText(item.uncertainty_reason, "", 260))}</p>` : ""}
    ${facts.length ? `<div class="wechat-facts">${facts.map((fact) => `<span>${escapeHtml(humanText(fact, "", 100))}</span>`).join("")}</div>` : ""}
    ${evidence ? `<div class="wechat-evidence"><strong>判断依据</strong><ol>${evidence}</ol></div>` : ""}
    <div class="wechat-card-actions">
      <button class="button button-primary" type="button" data-wechat-action="accept">确认纳入</button>
      <button class="button button-secondary" type="button" data-wechat-action="ignore">不是工作</button>
      <label class="wechat-merge"><span>合并到事项</span><select data-wechat-matter><option value="">选择已有事项</option>${options}</select></label>
      <button class="button button-secondary" type="button" data-wechat-action="merge">确认合并</button>
      <button class="button button-quiet" type="button" data-wechat-conversation-action="block" data-session-id="${escapeHtml(item.session_id)}">以后忽略此会话</button>
    </div>
  </article>`;
}

function renderWechat(status, candidates, conversations, matters) {
  const counts = status?.counts || {};
  const sources = status?.sources || {};
  const personalStatus = sources.personal_wechat || {};
  const wecomStatus = sources.wecom || {};
  const selectedCounts = state.chatSource === "all"
    ? counts
    : sources[state.chatSource]?.counts || {};
  const pending = candidates.filter((item) => item.status === "pending");
  const processingCount = Number(selectedCounts.processing || 0);
  const ignored = candidates.filter((item) => item.status === "ignored");
  const accepted = candidates.filter((item) => item.status === "accepted");
  const ignoredCount = Number(selectedCounts.ignored_recent ?? ignored.length);
  const acceptedCount = Number(selectedCounts.accepted ?? accepted.length);
  updateWechatCount(Number(counts.pending || 0));
  const latest = state.chatSource === "all"
    ? status?.latest
    : sources[state.chatSource]?.latest;
  const lastSuccessAt = state.chatSource === "all"
    ? status?.last_success_at
    : sources[state.chatSource]?.last_success_at;
    const syncRunning = Object.values(sources).some((item) => item?.latest?.status === "running");
    const exportState = status?.export || {};
    const exportPaths = Array.isArray(exportState.output_paths) ? exportState.output_paths : [];
    const exportSucceeded = exportState.status === "completed";
    const exportTitle = exportSucceeded
      ? "聊天记录已保存到本机"
      : exportState.status === "failed"
        ? "聊天记录导出未完成"
        : "等待首次导出聊天记录";
    const exportDetail = exportSucceeded
      ? `${Number(exportState.conversation_count || 0)} 个监听会话，${Number(exportState.message_count || 0)} 条增量消息，${Number(exportState.file_count || 0)} 个文件`
      : exportState.status === "failed"
        ? friendlyError(new Error(exportState.error || "下次检查聊天时会自动重试"))
        : "完成第一次聊天检查后，这里会显示导出数量和保存位置。";
    const exportPathsHtml = exportPaths
      .map((path) => {
        const label = path.includes("企业微信") ? "企业微信" : "个人微信";
        return `<div><span>${label}</span><code>${escapeHtml(path)}</code><button class="button button-quiet" type="button" data-copy-path="${escapeHtml(path)}">复制位置</button></div>`;
      })
      .join("");
    const newCutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    const newConversationCount = conversations.filter(
      (item) => item.listen_status === "active" && Date.parse(item.created_at || "") >= newCutoff,
    ).length;
  const latestText = !navigator.onLine
    ? "当前离线，已经收下的消息不会丢"
    : latest?.status === "running"
      ? "正在读取最近新增的聊天消息"
      : latest?.status === "failed"
        ? `上次检查未完成：${friendlyError(new Error(latest.error || "稍后自动重试"))}`
      : lastSuccessAt
        ? `最近检查成功 ${fmtDate(lastSuccessAt)}`
          : "等待首次检查";
  const sourceStatusText = (item, unavailableText = "等待首次检查") => {
    if (!item?.available) return item?.message || unavailableText;
    if (item?.latest?.status === "running") return "正在读取新增消息";
    if (item?.latest?.status === "failed") return "上次检查未完成，可以重新检查";
    if (item?.last_success_at) return `最近成功 ${fmtDate(item.last_success_at)}`;
    return unavailableText;
  };
  const processingHtml = processingCount
    ? `<div class="wechat-processing-summary" role="status"><div><strong>还有 ${processingCount} 段聊天等待整理</strong><p>原始消息已经安全收下。点击顶部按钮后，贾维斯才会筛选真正需要确认的工作内容。</p></div><span>手工启动</span></div>`
    : "";
  const pendingHtml = pending.length || processingCount
    ? `${processingHtml}${pending.map((item) => renderWechatCandidate(item, matters)).join("")}`
    : `<div class="wechat-empty"><span>好</span><div><strong>目前没有待确认线索</strong><p>Mac 会自动收取新增消息；消息先等待整理，只有手工启动后才会筛选工作线索。</p></div></div>`;
  const ignoredHtml = ignored.length
    ? ignored
        .map(
          (item) => `<article class="wechat-history-row" data-wechat-candidate="${escapeHtml(item.id)}"><div><small>${item.source === "wecom" ? "企业微信" : "个人微信"} · ${escapeHtml(item.display_name)} ${fmtDate(item.window_end)}</small><h3>${escapeHtml(humanText(item.summary, "已自动忽略的非工作聊天", 180))}</h3></div><button class="button button-secondary" type="button" data-wechat-action="restore">恢复为待确认</button></article>`,
        )
        .join("")
    : `<div class="wechat-empty compact"><div><strong>最近没有自动忽略的内容</strong><p>贾维斯判为无关的内容会保留 7 天，误判时可以恢复。</p></div></div>`;
  const acceptedHtml = accepted.length
    ? accepted
        .map(
          (item) => `<article class="wechat-history-row" data-wechat-candidate="${escapeHtml(item.id)}"><div><small>${item.source === "wecom" ? "企业微信" : "个人微信"} · ${escapeHtml(item.display_name)} ${fmtDate(item.resolved_at)}</small><h3>${escapeHtml(humanText(item.summary, "已纳入事项", 180))}</h3></div><button class="button button-secondary" type="button" data-wechat-action="undo">撤销纳入</button></article>`,
        )
        .join("")
    : `<div class="wechat-empty compact"><div><strong>还没有已采纳线索</strong><p>确认后的微信线索会留下处理记录，必要时可以撤销。</p></div></div>`;
  const conversationHtml = conversations.length
    ? conversations
        .map(
        (item) => `<article class="wechat-conversation" data-wechat-conversation data-listen-status="${escapeHtml(item.listen_status)}" data-last-message-at="${escapeHtml(item.last_message_at || "")}" data-is-new="${item.listen_status === "active" && Date.parse(item.created_at || "") >= newCutoff ? "true" : "false"}" data-search="${escapeHtml(item.display_name.toLowerCase())}"><div><span>${escapeHtml(item.display_name.slice(0, 1))}</span><div><h3>${escapeHtml(item.display_name)}${item.listen_status === "active" && Date.parse(item.created_at || "") >= newCutoff ? '<i class="wechat-new-badge">新增监听</i>' : ""}</h3><p>${item.source === "wecom" ? "企业微信" : "个人微信"} · ${item.kind === "group" ? "群聊" : "私聊"} ${item.last_message_at ? `最近有消息 ${fmtDate(item.last_message_at)}` : "等待首次读取"}</p></div></div><div><strong class="${item.listen_status === "blocked" ? "blocked" : ""}">${item.listen_status === "blocked" ? "已屏蔽" : "监听中"}</strong><button class="button button-secondary" type="button" data-wechat-conversation-action="${item.listen_status === "blocked" ? "unblock" : "block"}" data-session-id="${escapeHtml(item.session_id)}">${item.listen_status === "blocked" ? "解除屏蔽" : "屏蔽"}</button><button class="button button-quiet" type="button" data-wechat-conversation-action="rescan" data-session-id="${escapeHtml(item.session_id)}">补扫 7 天</button></div></article>`,
        )
        .join("")
    : `<div class="wechat-empty"><span>聊</span><div><strong>这个来源还没有会话</strong><p>个人微信准备好后，最近 7 天活跃的私聊和群聊会出现在这里。</p></div></div>`;

  page().innerHTML = `<section class="wechat-page">
    <header class="wechat-command"><div><p>聊天工作线索</p><h2>贾维斯先筛选，你只确认真正需要推进的工作。</h2><span>${escapeHtml(latestText)}</span></div><button class="button button-primary" type="button" data-wechat-sync ${syncRunning ? "disabled" : ""}>${syncRunning ? "正在检查" : "立即检查聊天"}</button></header>
      <div class="wechat-summary"><article><span>发现线索</span><strong>${pending.length + acceptedCount}</strong><small>当前筛选范围内有工作价值</small></article><article><span>等待确认</span><strong>${pending.length}</strong><small>确认后才会进入事项</small></article><article><span>等待贾维斯</span><strong>${processingCount}</strong><small>后台理解中，不需要重复投递</small></article></div>
      <section class="chat-source-statuses" aria-label="聊天来源状态"><article><div><strong>个人微信</strong><span>${escapeHtml(sourceStatusText(personalStatus))}</span></div><b>${Number(personalStatus?.counts?.pending || 0)} 条待确认</b></article><article class="${wecomStatus.available ? "" : "is-unavailable"}"><div><strong>企业微信</strong><span>${escapeHtml(sourceStatusText(wecomStatus, "本机企业微信数据库暂不可安全读取"))}</span></div><b>${wecomStatus.available ? `${Number(wecomStatus?.counts?.pending || 0)} 条待确认` : "个人微信不受影响"}</b></article></section>
      <section class="wechat-export-card ${exportState.status === "failed" ? "is-failed" : ""}" aria-label="增量聊天记录导出状态"><header><div><span>本机增量导出</span><h2>${escapeHtml(exportTitle)}</h2><p>${escapeHtml(exportDetail)}</p></div><strong>${exportSucceeded && exportState.last_success_at ? `上次成功 ${fmtDate(exportState.last_success_at)}` : ""}</strong></header>${exportPathsHtml ? `<div class="wechat-export-paths">${exportPathsHtml}</div>` : ""}<footer>只导出监听中的会话，已屏蔽内容不会输出。</footer></section>
    <nav class="chat-source-filter" aria-label="聊天来源筛选"><button class="${state.chatSource === "all" ? "active" : ""}" type="button" data-chat-source="all">全部</button><button class="${state.chatSource === "personal_wechat" ? "active" : ""}" type="button" data-chat-source="personal_wechat">个人微信</button><button class="${state.chatSource === "wecom" ? "active" : ""}" type="button" data-chat-source="wecom">企业微信</button></nav>
      <nav class="wechat-tabs" aria-label="聊天线索分类"><button class="${state.wechatTab === "pending" ? "active" : ""}" type="button" data-wechat-tab="pending">待我确认 <span>${pending.length}</span></button><button class="${state.wechatTab === "ignored" ? "active" : ""}" type="button" data-wechat-tab="ignored">最近忽略 <span>${ignoredCount}</span></button><button class="${state.wechatTab === "accepted" ? "active" : ""}" type="button" data-wechat-tab="accepted">已采纳 <span>${acceptedCount}</span></button><button class="${state.wechatTab === "conversations" ? "active" : ""}" type="button" data-wechat-tab="conversations">监听范围 <span>${conversations.length}</span></button></nav>
    <section class="wechat-panel" data-wechat-panel="pending" ${state.wechatTab === "pending" ? "" : "hidden"}><div class="wechat-panel-head"><div><h2>待我确认</h2><p>只展示必要原文，不把整段聊天堆到工作台。</p></div></div><div class="wechat-candidate-list">${pendingHtml}</div></section>
    <section class="wechat-panel" data-wechat-panel="ignored" ${state.wechatTab === "ignored" ? "" : "hidden"}><div class="wechat-panel-head"><div><h2>最近忽略</h2><p>无关内容保留 7 天，发现误判可以恢复。</p></div></div><div class="wechat-history-list">${ignoredHtml}</div></section>
    <section class="wechat-panel" data-wechat-panel="accepted" ${state.wechatTab === "accepted" ? "" : "hidden"}><div class="wechat-panel-head"><div><h2>已采纳</h2><p>已进入事项的线索仍可撤销，原始证据不会被删除。</p></div></div><div class="wechat-history-list">${acceptedHtml}</div></section>
      <section class="wechat-panel" data-wechat-panel="conversations" ${state.wechatTab === "conversations" ? "" : "hidden"}><div class="wechat-panel-head"><div><h2>监听范围</h2><p>最近 7 天新出现的会话会标为新增监听，已屏蔽会话仍放在最后。</p></div><div class="wechat-conversation-tools"><button class="button button-secondary ${state.wechatNewOnly ? "active" : ""}" type="button" data-wechat-new-only aria-pressed="${state.wechatNewOnly}">只看新增 ${newConversationCount}</button><label class="wechat-search"><span>搜索</span><input type="search" data-wechat-search placeholder="联系人或群聊名称" /></label></div></div><div class="wechat-conversation-list">${conversationHtml}</div></section>
  </section>`;
  bindDynamic();
  filterWechatConversationRows();
}

function setWechatTab(name) {
  state.wechatTab = name;
  $$('[data-wechat-tab]').forEach((button) => button.classList.toggle("active", button.dataset.wechatTab === name));
  $$('[data-wechat-panel]').forEach((panel) => { panel.hidden = panel.dataset.wechatPanel !== name; });
  $('[data-wechat-panel]:not([hidden]) button, [data-wechat-panel]:not([hidden]) input')?.focus({ preventScroll: true });
}

async function setChatSource(source) {
  if (!["all", "personal_wechat", "wecom"].includes(source) || state.chatSource === source) return;
  state.chatSource = source;
  await refreshRouteWithoutJump();
}

function sortWechatConversationRows() {
  const list = $(".wechat-conversation-list");
  if (!list) return;
  const rows = $$('[data-wechat-conversation]', list).sort((left, right) => {
    const statusOrder = Number(left.dataset.listenStatus === "blocked")
      - Number(right.dataset.listenStatus === "blocked");
    if (statusOrder) return statusOrder;
    const timeOrder = String(right.dataset.lastMessageAt || "")
      .localeCompare(String(left.dataset.lastMessageAt || ""));
    if (timeOrder) return timeOrder;
    return String(left.dataset.search || "").localeCompare(String(right.dataset.search || ""), "zh-CN");
  });
    rows.forEach((row) => list.append(row));
}

function filterWechatConversationRows() {
  const query = ($("[data-wechat-search]")?.value || "").trim().toLowerCase();
  let visible = 0;
  $$('[data-wechat-conversation]').forEach((row) => {
    const matchesSearch = row.dataset.search.includes(query);
    const matchesNew = !state.wechatNewOnly || row.dataset.isNew === "true";
    row.hidden = !matchesSearch || !matchesNew;
    if (!row.hidden) visible += 1;
  });
  const count = $('[data-wechat-tab="conversations"] span');
  if (count) count.textContent = visible;
  const list = $(".wechat-conversation-list");
  let empty = $("[data-wechat-filter-empty]");
  if (list && !empty) {
    empty = document.createElement("div");
    empty.dataset.wechatFilterEmpty = "";
    empty.className = "wechat-empty compact";
    empty.innerHTML = "<div><strong>没有匹配的会话</strong><p>换一个关键词，或者关闭“只看新增”。</p></div>";
    list.after(empty);
  }
  if (empty) empty.hidden = visible !== 0;
}

async function runWechatSync(button) {
  button.disabled = true;
  button.textContent = "正在安排检查";
  try {
    await api("/api/wechat/sync/run", { method: "POST" });
    toast("已安排检查个人微信和企业微信，结果会自动回到这里", "success");
    const routeName = routeFromHash().name;
    window.setTimeout(() => {
      if (routeFromHash().name === routeName) refreshRouteWithoutJump();
    }, 900);
  } catch (error) {
    button.disabled = false;
    button.textContent = "立即检查聊天";
    toast(friendlyError(error), "error");
  }
}

async function resolveWechatCandidate(button) {
  const card = button.closest("[data-wechat-candidate]");
  const action = button.dataset.wechatAction;
  const matterId = card?.querySelector("[data-wechat-matter]")?.value || null;
  if (action === "merge" && !matterId) {
    card.querySelector("[data-wechat-matter]")?.focus();
    toast("请先选择要合并的事项", "error");
    return;
  }
  button.disabled = true;
  try {
    await api(`/api/wechat/candidates/${encodeURIComponent(card.dataset.wechatCandidate)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, matter_id: matterId }),
    });
    const message = action === "ignore" ? "已忽略，可在最近忽略中恢复" : action === "restore" ? "已恢复为待确认" : action === "undo" ? "已撤销纳入，线索回到待确认" : "已纳入事项，可在已采纳中撤销";
    toast(message, "success");
    await refreshRouteWithoutJump();
  } catch (error) {
    button.disabled = false;
    toast(friendlyError(error), "error");
  }
}

async function changeWechatConversation(button) {
  const action = button.dataset.wechatConversationAction;
  const sessionId = button.dataset.sessionId;
  const row = button.closest("[data-wechat-conversation]");
  const status = row?.querySelector("strong");
  button.disabled = true;
  try {
    if (action === "block") {
      await api(`/api/wechat/conversations/${encodeURIComponent(sessionId)}/block`, { method: "POST" });
      toast("会话已屏蔽，可在监听范围中解除", "success");
    } else {
      await api(`/api/wechat/conversations/${encodeURIComponent(sessionId)}/unblock`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rescan_days: action === "rescan" ? 7 : null }),
      });
      toast(action === "rescan" ? "已安排补扫最近 7 天" : "已解除屏蔽，从现在开始监听", "success");
    }
    if (action === "block") {
      if (!row || !status) {
        toast("会话已屏蔽，相关线索已移到最近忽略", "success");
        await refreshRouteWithoutJump();
        return;
      }
      status.textContent = "已屏蔽";
      status.classList.add("blocked");
      row.dataset.listenStatus = "blocked";
      button.dataset.wechatConversationAction = "unblock";
      button.textContent = "解除屏蔽";
    } else if (action === "unblock") {
      status.textContent = "监听中";
      status.classList.remove("blocked");
      row.dataset.listenStatus = "active";
      button.dataset.wechatConversationAction = "block";
      button.textContent = "屏蔽";
    }
    sortWechatConversationRows();
    button.disabled = false;
  } catch (error) {
    button.disabled = false;
    toast(friendlyError(error), "error");
  }
}

function renderSearchResults() {
const target = $("#search-results");
const summary = $("#search-summary");
if (!target || !summary) return;
const query = state.searchQuery.trim();
if (!query) {
summary.textContent = "输入关键词后，贾维斯会在允许的资料中查找";
target.innerHTML = `<div class="search-empty"><span>⌕</span><strong>从一个关键词开始</strong><p>例如“游艇保险”“我在等谁”或一位同事的名字。</p></div>`;
return;
}
if (!state.searchResults) {
summary.textContent = "正在查找证据";
target.innerHTML = `<div class="search-loading" aria-live="polite"><i></i><i></i><i></i></div>`;
return;
}
const items = Array.isArray(state.searchResults.items) ? state.searchResults.items : [];
const answer = state.searchResults.answer || {};
summary.textContent = items.length ? `找到 ${items.length} 条相关记录` : "没有找到直接证据";
const answerHtml = `<section class="search-answer"><header><span>贾维斯根据现有证据整理</span><strong>${items.length ? "找到可核实的相关内容" : "当前资料不足"}</strong></header>${(answer.facts || []).length ? `<ul>${answer.facts.map((fact) => `<li>${escapeHtml(humanText(fact, "", 260))}</li>`).join("")}</ul>` : ""}${(answer.missing || []).length ? `<div class="search-missing"><strong>还缺什么</strong><p>${escapeHtml(answer.missing.join("；"))}</p></div>` : ""}</section>`;
const rows = items.length
? items.map((item) => `<a class="search-result" href="${escapeHtml(item.href || "#/search")}"><span class="search-result-type">${escapeHtml(item.type_label || "记录")}</span><div><h3>${escapeHtml(humanText(item.title, "未命名记录", 160))}</h3><p>${escapeHtml(humanText(item.summary || item.body, "暂无摘要", 260))}</p><small>${escapeHtml(item.sort_reason || "正文匹配")}${item.created_at ? `，${fmtDate(item.created_at)}` : ""}</small></div><i>→</i></a>`).join("")
: `<div class="search-empty"><span>⌕</span><strong>没有找到匹配内容</strong><p>可以减少筛选条件，或换一个更具体的业务关键词。</p></div>`;
target.innerHTML = answerHtml + `<div class="search-result-list">${rows}</div>`;
}

async function performSearch() {
const query = state.searchQuery.trim();
if (!query) {
state.searchResults = null;
renderSearchResults();
return;
}
state.searchResults = null;
renderSearchResults();
const params = new URLSearchParams({ q: query, limit: "80" });
const filters = state.searchFilters || {};
if (filters.source) params.set("source", filters.source);
if (filters.status) params.set("status", filters.status);
if (filters.dateFrom) params.set("date_from", filters.dateFrom);
if (filters.dateTo) params.set("date_to", filters.dateTo);
if (filters.amount) params.set("amount", filters.amount);
try {
state.searchResults = await api(`/api/search?${params.toString()}`);
renderSearchResults();
} catch (error) {
const target = $("#search-results");
if (target) target.innerHTML = `<div class="error-panel"><strong>搜索暂时不可用</strong><p>${escapeHtml(friendlyError(error))}</p></div>`;
}
}

function renderSearch() {
const filters = state.searchFilters || {};
page().innerHTML = `<section class="search-page"><header class="search-hero"><p class="eyebrow">跨来源查证</p><h2>在全部工作证据里找答案</h2><span>覆盖事项、行动、聊天依据、工作邮件、会议材料和公司规定。被过滤的内容不会进入结果。</span><label class="global-search-field"><span>搜索内容</span><input id="global-search-input" type="search" value="${escapeHtml(state.searchQuery)}" placeholder="输入事项、人员、金额或一句问题" autocomplete="off"></label><div class="search-filters"><label><span>来源</span><select data-search-filter="source"><option value="">全部来源</option><option value="matter">事项</option><option value="action">行动</option><option value="material">聊天和会议材料</option><option value="email">邮件</option><option value="policy">公司规定</option></select></label><label><span>状态</span><select data-search-filter="status"><option value="">全部状态</option><option value="open">未完成</option><option value="done">已完成</option><option value="active">当前有效</option></select></label><label><span>开始日期</span><input type="date" data-search-filter="dateFrom" value="${escapeHtml(filters.dateFrom || "")}"></label><label><span>结束日期</span><input type="date" data-search-filter="dateTo" value="${escapeHtml(filters.dateTo || "")}"></label><label><span>金额</span><input inputmode="decimal" data-search-filter="amount" value="${escapeHtml(filters.amount || "")}" placeholder="例如 50000"></label></div><small id="search-summary"></small></header><div id="search-results"></div></section>`;
$('[data-search-filter="source"]').value = filters.source || "";
$('[data-search-filter="status"]').value = filters.status || "";
renderSearchResults();
const input = $("#global-search-input");
input.addEventListener("input", (event) => {
state.searchQuery = event.target.value;
if (state.searchTimer) window.clearTimeout(state.searchTimer);
state.searchTimer = window.setTimeout(performSearch, 260);
});
$$("[data-search-filter]").forEach((field) => field.addEventListener("change", () => {
const key = field.dataset.searchFilter;
state.searchFilters[key] = field.value;
if (state.searchTimer) window.clearTimeout(state.searchTimer);
state.searchTimer = window.setTimeout(performSearch, 100);
}));
if (state.searchFocus) {
state.searchFocus = false;
requestAnimationFrame(() => input.focus({ preventScroll: true }));
}
if (state.searchQuery.trim()) performSearch();
}


  function renderIntakePage(issues = []) {
    const issueRows = issues.length
      ? issues
          .map(
            (item) => `<article class="analysis-issue-row">
              <div><small>${escapeHtml(item.source_label || sourceLabel(item.source_type))} · ${fmtDate(item.updated_at)}</small><h3>${escapeHtml(materialTitle(item))}</h3><p>${escapeHtml(item.reason || "上次整理没有完成，可以重新整理。")}</p></div>
              <footer><button class="button button-primary" type="button" data-analysis-retry="${escapeHtml(item.id)}">重新整理</button><a class="button button-secondary" href="/api/materials/${encodeURIComponent(item.material_id)}/content" target="_blank" rel="noopener">查看原材料</a></footer>
            </article>`,
          )
          .join("")
      : "";
    page().innerHTML = `<section class="intake-page">
      <div class="intake-page-copy"><p class="eyebrow">最省心的入口</p><h2>有东西就丢，何时整理由你决定。</h2><p>Mac 开机时从这里直接投递；Mac 关机时从安卓分享到“财务工作台投递箱 / 待处理”，开机后自动接手。</p><button class="button button-primary" type="button" data-open-intake>＋ 开始投递</button></div>
      <div class="intake-scenarios"><article><span>声</span><div><strong>会议录音</strong><p>本机先收件和转写，由你决定何时让贾维斯提炼结论与责任人。</p></div></article><article><span>微</span><div><strong>微信聊天</strong><p>先增量收取，手工启动后再按业务事项筛选真正要推进的内容。</p></div></article><article><span>审</span><div><strong>企微审批</strong><p>先看风险和资料缺口，最终同意或驳回仍由你决定。</p></div></article><article><span>文</span><div><strong>一句话或文件</strong><p>先记录，等你启动后再与已有事项归并。</p></div></article></div>
      ${issues.length ? `<section class="analysis-issues-card" id="analysis-issues"><header><div><p>需要检查</p><h2>${issues.length} 份内容上次没有整理完成</h2></div><span>原材料没有丢失</span></header><div>${issueRows}</div></section>` : ""}
      <section class="queue-card icloud-card"><div><p>安卓关机投递</p><h3>分享 → Syncthing-Fork → 财务工作台投递箱 / 待处理</h3></div><div><p class="queue-empty">材料先留在手机本地；Mac 开机后点对点同步，自动移动到“已接收”并等待手工整理，不需要云主机或域名。</p></div></section>
      <section class="queue-card"><div><p>当前浏览器暂存</p><h3 id="queue-summary">正在检查等待上传的材料</h3></div><div id="queue-list"></div></section>
    </section>`;
    bindDynamic();
    updateQueueView();
  }

  function renderMatterRows(items, completed) {
    if (!items.length) {
      return `<div class="calm-panel matter-empty"><span>✓</span><div><strong>${completed ? "暂无已完成事项" : "当前没有待推进事项"}</strong><p>${completed ? "完成的事项会收在这里，随时可以回来查证。" : "新材料形成行动或待确认内容后，会自动出现在这里。"}</p></div></div>`;
    }
    return `<div class="matter-list">${items
      .map(
        (item) =>
          `<article class="matter-row ${completed ? "completed" : ""}" data-search="${escapeHtml(`${item.title} ${item.summary}`.toLowerCase())}"><a href="#/matter/${encodeURIComponent(item.id)}"><span class="matter-monogram">${escapeHtml(matterTitle(item.title).slice(0, 1))}</span><div><p>${completed ? "完成归档" : "更新于"} ${fmtDate(item.updated_at)}</p><h3>${escapeHtml(matterTitle(item.title))}</h3><span>${escapeHtml(humanText(item.summary, "贾维斯尚未形成摘要", 180))}</span><footer>${badge(`${item.material_count || 0} 份材料`, "muted")}${completed ? badge("已完成", "green") : badge(`${item.open_action_count || 0} 个下一步`, item.open_action_count ? "blue" : "muted")}${!completed && item.pending_review_count ? badge(`${item.pending_review_count} 待拍板`, "amber") : ""}${!completed && item.open_reminder_count ? badge(`${item.open_reminder_count} 个提醒`, "muted") : ""}</footer></div><i>→</i></a></article>`,
      )
      .join("")}</div>`;
  }

function renderEmail(status, messages, matters) {
  const account = status?.accounts?.[0];
  const latest = status?.latest || {};
  const checking = ["pending", "running"].includes(latest.status);
  const failed = latest.status === "failed";
  const pending = Number(latest.pending_count || 0);
  const openMatters = matters.filter((item) => !item.is_completed);
  const completedMatters = matters.filter((item) => item.is_completed);
  const accountText = account
    ? `${account.address_hint} · ${account.last_success_at ? `最近成功 ${fmtDate(account.last_success_at)}` : "等待首次检查"}`
    : "尚未连接邮箱";
  const workCards = messages.length
    ? messages
        .map(
          (item) => `<article class="email-work-card" data-email-message="${escapeHtml(item.id)}">
            <header><div><small>${fmtDate(item.sent_at)}</small><h3>${escapeHtml(emailSubject(item.subject || item.matter_title))}</h3></div><span>需要推进</span></header>
            <p>${escapeHtml(emailSummary(item.summary, "已识别出需要继续推进的工作邮件", 320))}</p>
            ${item.evidence?.length ? `<div class="email-evidence"><strong>邮件中的明确要求</strong><ul>${item.evidence.map((value) => `<li>${escapeHtml(humanText(value, "", 360))}</li>`).join("")}</ul></div>` : ""}
            <footer>${item.matter_id ? `<a class="button button-secondary" href="#/matter/${encodeURIComponent(item.matter_id)}">查看事项推进</a>` : ""}<button class="button button-quiet" type="button" data-email-ignore>不是工作</button></footer>
          </article>`,
        )
        .join("")
    : `<div class="wechat-empty"><span>邮</span><div><strong>目前没有需要推进的工作邮件</strong><p>广告、验证码、订阅、个人邮件和没有后续动作的通知不会进入这里。</p></div></div>`;
  const matterRows = (items, completed) =>
    items.length
      ? items
          .map(
            (item) => `<a class="email-matter-row ${completed ? "completed" : ""}" href="#/matter/${encodeURIComponent(item.id)}"><div><small>${completed ? "已完成" : `${item.open_action_count || 0} 项待推进`}</small><h3>${escapeHtml(emailSubject(item.title, "待整理事项", 100))}</h3><p>${escapeHtml(emailSummary(item.summary, "等待整理", 220))}</p></div><i>→</i></a>`,
          )
          .join("")
      : `<div class="wechat-empty compact"><div><strong>${completed ? "还没有已完成的邮件事项" : "还没有邮件事项"}</strong><p>${completed ? "完成后的邮件工作会自动归档到这里。" : "只有确实需要跟进的工作邮件才会建立事项。"}</p></div></div>`;

  page().innerHTML = `<section class="email-page">
    <header class="email-command"><div><p>邮件工作</p><h2>贾维斯只留下真正需要你推进的邮件。</h2><span>${escapeHtml(accountText)}</span></div><button class="button button-primary" type="button" data-email-sync ${checking || !status?.configured ? "disabled" : ""}>${checking ? "正在检查" : "立即检查邮箱"}</button>${pending > 0 ? `<p class="email-pending-feedback" data-email-pending-feedback="true">已读取 ${pending} 封邮件，等待整理。点击顶部整理按钮后才会进入后续处理。</p>` : ""}</header>
    ${!status?.configured ? `<section class="email-setup"><span>只读</span><div><h2>先在本机安全连接邮箱</h2><p>授权码只保存到 macOS 钥匙串，网页和工作台数据库都不会保存。连接后只读取新增邮件，不会自动回信。</p><code>.venv/bin/python -m scripts.configure_email</code><button class="button button-secondary" type="button" data-copy-email-setup>复制配置命令</button></div></section>` : ""}
    ${failed ? `<section class="email-warning"><strong>上次邮箱检查未完成</strong><p>${escapeHtml(friendlyError(new Error(latest.error), "可点击重新检查，已处理的邮件不会重复生成事项"))}</p></section>` : ""}
        <section class="email-summary"><article><span>需要推进</span><strong>${messages.length}</strong><small>真正进入工作台的邮件</small></article><article><span>自动过滤</span><strong>不留存</strong><small>非工作邮件不会保存</small></article><article><span>邮件事项</span><strong>${openMatters.length}</strong><small>当前仍在推进</small></article></section>
    <section class="email-layout"><div><div class="wechat-panel-head"><div><h2>新增工作邮件</h2><p>只显示邮件要求、必要证据和下一步，不堆整封正文。</p></div></div><div class="email-work-list">${workCards}</div></div><aside><section><header><div><span>邮件事项推进</span><h2>未完成</h2></div><strong>${openMatters.length}</strong></header>${matterRows(openMatters, false)}</section><section><header><div><span>完成归档</span><h2>已完成</h2></div><strong>${completedMatters.length}</strong></header>${matterRows(completedMatters, true)}</section></aside></section>
  </section>`;

  $("[data-email-sync]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在读取邮箱…";
    try {
      const request = await api("/api/email/sync/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      toast("已开始检查新增邮件", "success");
      for (let attempt = 0; attempt < 30; attempt += 1) {
        const latestStatus = (await api("/api/email/status")).latest || {};
        if (
          ["completed", "failed"].includes(latestStatus.status) &&
          (!request?.id || latestStatus.id === request.id)
        ) {
          break;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
      if (routeFromHash().name === "email") await refreshRouteWithoutJump();
    } catch (error) {
      button.disabled = false;
      button.textContent = "立即检查邮箱";
      toast(friendlyError(error), "error");
    }
  });
  $("[data-copy-email-setup]")?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(".venv/bin/python -m scripts.configure_email");
    toast("配置命令已复制", "success");
  });
  $$("[data-email-ignore]").forEach((button) =>
    button.addEventListener("click", async () => {
      const card = button.closest("[data-email-message]");
      button.disabled = true;
      try {
        await api(`/api/email/messages/${encodeURIComponent(card.dataset.emailMessage)}/ignore`, { method: "POST" });
        toast("已移出邮件工作，相关待办一并关闭", "success");
        card.innerHTML = `<div><strong>已移出邮件工作</strong><p>如果判断有误，可以立即恢复。</p></div><button class="button button-secondary" type="button" data-email-restore-inline>撤销</button>`;
        $("[data-email-restore-inline]", card)?.addEventListener("click", async (event) => {
          event.currentTarget.disabled = true;
          try {
            await api(`/api/email/messages/${encodeURIComponent(card.dataset.emailMessage)}/restore`, {
              method: "POST",
            });
            toast("邮件和相关待办已恢复", "success");
            await refreshRouteWithoutJump();
          } catch (error) {
            event.currentTarget.disabled = false;
            toast(friendlyError(error), "error");
          }
        });
      } catch (error) {
        button.disabled = false;
        toast(friendlyError(error), "error");
      }
    }),
  );
}

  function renderPolicyCandidate(item, policies) {
    const options = policies
      .map(
        (policy) =>
          `<option value="${escapeHtml(policy.id)}" ${policy.id === item.matched_policy_id ? "selected" : ""}>${escapeHtml(humanText(policy.title, "现行规定", 100))}</option>`,
      )
      .join("");
    const requirements = (item.requirements || [])
      .slice(0, 6)
      .map((value) => `<li>${escapeHtml(humanText(value, "", 320))}</li>`)
      .join("");
    const evidence = (item.evidence || [])
      .slice(0, 4)
      .map((value) => `<li>${escapeHtml(humanText(value, "", 360))}</li>`)
      .join("");
    const attachments = (item.attachments || [])
      .map((value) => value.split("/").pop())
      .filter(Boolean)
      .slice(0, 8)
      .map((value) => `<span>${escapeHtml(value)}</span>`)
      .join("");
    const standaloneLabel = item.matched_policy_id
      ? item.change_type === "repeal"
        ? "确认废止已有规定"
        : "更新匹配到的规定"
      : item.change_type === "repeal"
        ? "记录废止通知"
        : "作为新规定收录";
    const linkExisting = policies.length
      ? `<details class="policy-linker">
          <summary>这属于工作台已有规定</summary>
          <p>只有确实是同一条规定的后续修订、解释或废止时才需要关联。</p>
          <div><label><span>选择已有规定</span><select data-policy-target><option value="">选择规定</option>${options}</select></label>
          <button class="button button-secondary" type="button" data-policy-action="merge">更新已有规定</button></div>
        </details>`
      : `<div class="policy-link-note"><strong>目前无需关联</strong><span>工作台还没有可关联的现行规定。直接收录即可，Obsidian 中已有旧资料或暂时没有旧资料都不影响本次记录。</span></div>`;
    return `<article class="policy-candidate" data-policy-candidate="${escapeHtml(item.id)}">
      <header><div><small>${escapeHtml(item.publisher || "发布单位待确认")} · ${escapeHtml(item.change_label || "规定更新")}</small><h3>${escapeHtml(humanText(item.title, "待确认规定", 160))}</h3></div><span>${escapeHtml(item.source_name || "工作消息")}</span></header>
      <p>${escapeHtml(humanText(item.summary, "贾维斯已识别到可能需要持续执行的要求", 520))}</p>
      ${item.scope ? `<div class="policy-scope"><strong>适用范围</strong><span>${escapeHtml(humanText(item.scope, "", 300))}</span></div>` : ""}
      ${item.change_summary ? `<div class="policy-diff"><strong>这次变化</strong><span>${escapeHtml(humanText(item.change_summary, "", 360))}</span></div>` : ""}
      ${requirements ? `<div class="policy-detail"><strong>执行要求</strong><ul>${requirements}</ul></div>` : ""}
      ${evidence ? `<details class="policy-evidence"><summary>查看必要原文</summary><ol>${evidence}</ol></details>` : ""}
      ${attachments ? `<div class="policy-attachments"><strong>正式附件</strong>${attachments}</div>` : ""}
      <footer>
        <div class="policy-primary-actions">
          <button class="button button-primary" type="button" data-policy-action="apply">${standaloneLabel}</button>
          <button class="button button-secondary" type="button" data-policy-action="ignore">不是规定</button>
          <button class="button button-quiet" type="button" data-policy-action="temporary">改为临时事项</button>
        </div>
        ${linkExisting}
      </footer>
    </article>`;
  }

  function renderPolicyRows(items, emptyText) {
    if (!items.length) {
      return `<div class="wechat-empty compact"><span>规</span><div><strong>${escapeHtml(emptyText)}</strong><p>聊天和邮件有新内容后，贾维斯会先筛选、比对，再决定是否需要你确认。</p></div></div>`;
    }
    return items
      .map(
        (item) => `<article class="policy-row" data-search="${escapeHtml(`${item.title} ${item.publisher} ${item.topic} ${item.summary}`.toLowerCase())}">
          <div><small>${escapeHtml(item.publisher || "发布单位待核实")} · ${escapeHtml(item.topic || "综合管理")}</small><h3>${escapeHtml(humanText(item.title, "现行规定", 140))}</h3><p>${escapeHtml(humanText(item.summary, "", 320))}</p></div>
          <aside><strong>${escapeHtml(item.status_label || "当前有效")}</strong><span>v${Number(item.version || 1)}</span>${item.last_verified_at ? `<small>${fmtDate(item.last_verified_at)}</small>` : ""}</aside>
        </article>`,
      )
      .join("");
  }

  function setPolicyTab(tab) {
    state.policyTab = tab;
    $$('[data-policy-tab]').forEach((button) => {
      const active = button.dataset.policyTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    $$('[data-policy-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.policyPanel !== tab;
    });
  }

  async function resolvePolicyCandidate(button) {
    const card = button.closest("[data-policy-candidate]");
    const action = button.dataset.policyAction;
    const policyId = card?.querySelector("[data-policy-target]")?.value || null;
    if (action === "merge" && !policyId) {
      card?.querySelector("[data-policy-target]")?.focus();
      toast("请先选择要合并的现行规定", "error");
      return;
    }
    button.disabled = true;
    try {
      await api(`/api/policy-candidates/${encodeURIComponent(card.dataset.policyCandidate)}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, policy_id: policyId }),
      });
      toast(
        action === "ignore"
          ? "已标记为不是规定"
          : action === "temporary"
            ? "已转为临时事项"
            : "规定已更新到 Obsidian",
        "success",
      );
      await refreshRouteWithoutJump();
    } catch (error) {
      button.disabled = false;
      toast(friendlyError(error), "error");
    }
  }

  function renderPolicies(status, candidates, activePolicies, repealedPolicies) {
    const pending = candidates.filter((item) => item.status === "pending");
    const recent = candidates.filter((item) => ["applied", "auto_applied", "undone"].includes(item.status)).slice(0, 40);
    const obsidian = status?.obsidian || {};
    const written = Number(obsidian.written_count || 0);
    const syncText = obsidian.status === "failed"
      ? "上次同步未完成"
      : obsidian.last_success_at
        ? `最近同步 ${fmtDate(obsidian.last_success_at)}`
        : "等待首次写入";
    const pendingHtml = pending.length
      ? pending.map((item) => renderPolicyCandidate(item, activePolicies)).join("")
      : renderPolicyRows([], "目前没有规定变化需要确认");
    const recentHtml = recent.length
      ? recent.map((item) => `<article class="policy-change-row"><div><small>${escapeHtml(item.source_name || "工作消息")} · ${escapeHtml(item.change_label || "规定更新")}</small><h3>${escapeHtml(humanText(item.title, "规定更新", 140))}</h3><p>${escapeHtml(humanText(item.change_summary || item.summary, "", 300))}</p></div><strong>${escapeHtml(item.status_label || "已处理")}</strong></article>`).join("")
      : renderPolicyRows([], "近期没有规定变化");

    page().innerHTML = `<section class="policy-page">
      <header class="policy-command"><div><p>公司规定</p><h2>零散要求，整理成一份持续有效的执行口径。</h2><span>只分析已监听聊天和已配置邮箱，重要变化确认后才会改写现行规定。</span></div><button class="button button-secondary" type="button" data-policy-sync>重试同步</button></header>
      <section class="policy-summary"><article><span>待我确认</span><strong>${pending.length}</strong><small>新增、修订、废止或重要解释</small></article><article><span>当前有效</span><strong>${activePolicies.length}</strong><small>已写入 Obsidian 的执行口径</small></article><article><span>今日新增</span><strong>${Number(status?.counts?.added || 0)}</strong><small>今天确认的新规定</small></article><article><span>今日修订</span><strong>${Number(status?.counts?.revised || 0)}</strong><small>今天确认的变更</small></article></section>
      <section class="policy-vault-card ${obsidian.status === "failed" ? "is-failed" : ""}"><div><span>Obsidian 保存位置</span><h3>${escapeHtml(syncText)}</h3><code>${escapeHtml(obsidian.path || "尚未配置")}</code></div><aside><strong>${written}</strong><small>最近更新文件</small><button class="button button-quiet" type="button" data-copy-path data-copy-path="${escapeHtml(obsidian.path || "")}">复制位置</button></aside></section>
      <nav class="wechat-tabs policy-tabs" aria-label="公司规定分类"><button class="active" aria-selected="true" data-policy-tab="pending">待我确认 <span>${pending.length}</span></button><button aria-selected="false" data-policy-tab="active">当前有效 <span>${activePolicies.length}</span></button><button aria-selected="false" data-policy-tab="recent">最近变更 <span>${recent.length}</span></button><button aria-selected="false" data-policy-tab="repealed">已废止 <span>${repealedPolicies.length}</span></button></nav>
      <section data-policy-panel="pending"><div class="policy-panel-head"><div><h2>待我确认</h2><p>只有会改变持续执行方式的要求才停在这里。</p></div></div><div class="policy-candidate-list">${pendingHtml}</div></section>
      <section data-policy-panel="active" hidden><div class="policy-panel-head"><div><h2>当前有效</h2><p>按发布单位、主题和适用范围查询正在执行的规定。</p></div><label class="matter-search compact"><span>⌕</span><input data-policy-search placeholder="搜索规定、单位或业务主题"></label></div><div class="policy-list">${renderPolicyRows(activePolicies, "还没有已确认的公司规定")}</div></section>
      <section data-policy-panel="recent" hidden><div class="policy-panel-head"><div><h2>最近变更</h2><p>自动补充证据和人工确认都留有记录。</p></div></div><div class="policy-change-list">${recentHtml}</div></section>
      <section data-policy-panel="repealed" hidden><div class="policy-panel-head"><div><h2>已废止</h2><p>不再执行的规定保留历史版本，避免误用旧口径。</p></div></div><div class="policy-list">${renderPolicyRows(repealedPolicies, "目前没有已废止规定")}</div></section>
    </section>`;

    $$('[data-policy-tab]').forEach((button) => button.addEventListener("click", () => setPolicyTab(button.dataset.policyTab)));
    $$('[data-policy-action]').forEach((button) => button.addEventListener("click", () => resolvePolicyCandidate(button)));
    $('[data-policy-search]')?.addEventListener("input", (event) => {
      const query = event.target.value.trim().toLowerCase();
      $$('.policy-row').forEach((row) => { row.hidden = Boolean(query && !row.dataset.search.includes(query)); });
    });
    $('[data-policy-sync]')?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await api("/api/policies/obsidian/sync", { method: "POST" });
        toast("公司规定已重新同步到 Obsidian", "success");
        await refreshRouteWithoutJump();
      } catch (error) {
        button.disabled = false;
        toast(friendlyError(error), "error");
      }
    });
    $$('[data-copy-path]').forEach((button) => button.addEventListener("click", async () => {
      if (!button.dataset.copyPath) return;
      await navigator.clipboard.writeText(button.dataset.copyPath);
      toast("保存位置已复制", "success");
    }));
    setPolicyTab(state.policyTab);
  }

  function renderPersonActionRows(actions, people) {
    if (!actions.length) {
      return `<div class="calm-panel matter-empty"><span>✓</span><div><strong>这个分类暂时没有事项</strong><p>确认负责人后，未完成行动会自动归到对应人员下面。</p></div></div>`;
    }
    return `<div class="person-action-list">${actions
      .map((action) => {
        const matterId = action.matter_id || action.matter?.id;
        const href = matterId ? `#/matter/${encodeURIComponent(matterId)}` : "#/matters";
        return `<article class="person-action-row" data-search="${escapeHtml(`${actionTitle(action)} ${actionDetail(action)} ${actionMatterLabel(action)}`.toLowerCase())}"><div><small>${escapeHtml(actionMatterLabel(action))}${action.due_date ? ` · 截止 ${escapeHtml(action.due_date)}` : ""}</small><h3>${escapeHtml(actionTitle(action))}</h3><p>${escapeHtml(actionDetail(action))}</p><footer>${badge(action.status === "done" ? "已完成" : "未完成", action.status === "done" ? "green" : "blue")}${pendingAssignees(action).length ? badge("负责人待确认", "amber") : ""}</footer></div><a class="button button-secondary" href="${href}">查看事项</a></article>`;
      })
      .join("")}</div>`;
  }

  function actionsForPerson(actions, personId) {
    if (personId === "__unassigned") {
      return actions.filter((action) => !confirmedAssignees(action).length);
    }
    return actions.filter((action) =>
      confirmedAssignees(action).some(
        (assignee) => String(assigneePersonId(assignee)) === String(personId),
      ),
    );
  }

  function renderPersonGroups(actions, people) {
    const self = people.find((person) => person.is_self) || people.find((person) => person.id === "person_self") || { id: "person_self", name: "我自己", role: "Frank", is_self: true };
    const others = people.filter((person) => person.id !== self.id).sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
    const groups = [
      { id: self.id, name: self.name, role: self.role, actions: actionsForPerson(actions, self.id), pinned: true },
      { id: "__unassigned", name: "负责人待明确", role: "有建议或尚未判断的行动", actions: actionsForPerson(actions, "__unassigned"), pinned: true },
      ...others.map((person) => ({ id: person.id, name: person.name, role: person.role, actions: actionsForPerson(actions, person.id), pinned: false })),
    ];
    const emptyCount = groups.filter((group) => !group.actions.length && !group.pinned).length;
    const visible = state.showEmptyPeople ? groups : groups.filter((group) => group.pinned || group.actions.length);
    return `<div class="person-group-list">${visible
      .map(
        (group) =>
          `<section class="person-group ${group.pinned ? "is-pinned" : ""}" data-person-group="${escapeHtml(group.id)}"><header><div><h3>${escapeHtml(group.name)}${group.pinned ? " · 置顶" : ""}</h3><p>${escapeHtml(group.role || "")}</p></div><strong>${group.actions.length} 项</strong></header>${renderPersonActionRows(group.actions, people)}</section>`,
      )
      .join("")}</div>${emptyCount ? `<button class="button button-quiet people-empty-toggle" type="button" data-toggle-empty-people>${state.showEmptyPeople ? "隐藏没有行动的人员" : `显示 ${emptyCount} 位暂无行动的人员`}</button>` : ""}`;
  }

  function renderMatters(items, people = [], actionGroups = {}) {
    state.matters = Array.isArray(items) ? items : [];
    state.people = normalizePeople(people);
    const openMatters = state.matters.filter((item) => !item.is_completed);
    const completedMatters = state.matters.filter((item) => item.is_completed);
    const openActions = Array.isArray(actionGroups.open) ? actionGroups.open : [];
    const doneActions = Array.isArray(actionGroups.done) ? actionGroups.done : [];
    page().innerHTML = `<section class="section-heading"><div><p class="eyebrow">按事情，而不是按来源</p><h2>事项推进</h2><span>默认只看仍需推进的事项，完成后自动归入已完成。</span></div><button class="button button-primary" type="button" data-open-intake>＋ 新材料</button></section><label class="matter-search"><span>⌕</span><input id="matter-filter" placeholder="搜索事项、金额、人员或关键词"></label><nav class="wechat-tabs matter-top-tabs" aria-label="事项查看方式" role="tablist"><button class="${state.matterTab === "all" ? "active" : ""}" type="button" role="tab" aria-selected="${state.matterTab === "all"}" data-matter-tab="all">全部事项</button><button class="${state.matterTab === "people" ? "active" : ""}" type="button" role="tab" aria-selected="${state.matterTab === "people"}" data-matter-tab="people">按负责人</button></nav><nav class="wechat-tabs matter-view-tabs" aria-label="事项完成状态" role="tablist"><button class="${state.matterStatus === "open" ? "active" : ""}" type="button" role="tab" aria-selected="${state.matterStatus === "open"}" data-matter-view="open">未完成 <span>${state.matterTab === "people" ? openActions.length : openMatters.length}</span></button><button class="${state.matterStatus === "completed" ? "active" : ""}" type="button" role="tab" aria-selected="${state.matterStatus === "completed"}" data-matter-view="completed">已完成 <span>${state.matterTab === "people" ? doneActions.length : completedMatters.length}</span></button></nav><section class="matter-mode" data-matter-mode="all" ${state.matterTab === "all" ? "" : "hidden"}><section id="open-matters" class="matter-group" role="tabpanel" data-matter-panel="open" ${state.matterStatus === "open" ? "" : "hidden"}><header><div><h3>正在推进</h3><p>仍有下一步、待拍板、提醒或后台处理的事项。</p></div><strong>${openMatters.length} 项</strong></header>${renderMatterRows(openMatters, false)}</section><section id="completed-matters" class="matter-group" role="tabpanel" data-matter-panel="completed" ${state.matterStatus === "completed" ? "" : "hidden"}><header><div><h3>已完成</h3><p>所有推进动作和提醒都已关闭，可随时回查证据。</p></div><strong>${completedMatters.length} 项</strong></header>${renderMatterRows(completedMatters, true)}</section></section><section class="matter-mode" data-matter-mode="people" ${state.matterTab === "people" ? "" : "hidden"}><section class="matter-group" role="tabpanel" data-matter-person-panel="open" ${state.matterStatus === "open" ? "" : "hidden"}><header><div><h3>按负责人推进</h3><p>只显示已确认负责人；建议人选仍停在负责人待明确。</p></div><strong>${openActions.length} 项</strong></header>${renderPersonGroups(openActions, state.people)}</section><section class="matter-group" role="tabpanel" data-matter-person-panel="completed" ${state.matterStatus === "completed" ? "" : "hidden"}><header><div><h3>已完成行动</h3><p>完成后的行动按最后确认的负责人归档。</p></div><strong>${doneActions.length} 项</strong></header>${renderPersonGroups(doneActions, state.people)}</section></section>`;

    $$("[data-matter-tab]", page()).forEach((button) => {
      button.addEventListener("click", () => {
        state.matterTab = button.dataset.matterTab;
        renderMatters(state.matters, state.people, actionGroups);
      });
    });
    $$("[data-matter-view]", page()).forEach((button) => {
      button.addEventListener("click", () => {
        state.matterStatus = button.dataset.matterView;
        const view = state.matterStatus;
        $$("[data-matter-view]", page()).forEach((item) => {
          const active = item.dataset.matterView === view;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
        });
        $$("[data-matter-panel]", page()).forEach((panel) => {
          panel.hidden = panel.dataset.matterPanel !== view;
        });
        $$("[data-matter-person-panel]", page()).forEach((panel) => {
          panel.hidden = panel.dataset.matterPersonPanel !== view;
        });
      });
    });
  $("#matter-filter")?.addEventListener("input", (event) => {
    const query = event.target.value.trim().toLowerCase();
    $$(".matter-row, .person-action-row", page()).forEach((row) => {
      row.hidden = Boolean(query && !row.dataset.search.includes(query));
    });
    $$("[data-matter-panel], [data-matter-person-panel]", page()).forEach((panel) => {
      const rows = $$(".matter-row, .person-action-row", panel);
      const visible = rows.filter((row) => !row.hidden).length;
      const count = $("header strong", panel);
      if (count) count.textContent = `${visible} 项`;
      let empty = $("[data-matter-filter-empty]", panel);
      if (!empty) {
        empty = document.createElement("div");
        empty.dataset.matterFilterEmpty = "";
        empty.className = "calm-empty";
        empty.innerHTML = "<span>⌕</span><div><strong>没有匹配的事项</strong><p>换一个关键词继续查找。</p></div>";
        panel.append(empty);
      }
      empty.hidden = visible !== 0 || !query;
    });
  });
    $("[data-toggle-empty-people]", page())?.addEventListener("click", () => {
      state.showEmptyPeople = !state.showEmptyPeople;
      renderMatters(state.matters, state.people, actionGroups);
    });
    bindDynamic();
  }

  async function showTranscript(materialId, filename) {
    const panel = $("#transcript-panel");
    if (!panel) return;
    panel.hidden = false;
    panel.innerHTML = `<div class="loading-view compact"><span></span><strong>正在打开转写</strong></div>`;
    try {
      const transcript = await api(
        `/api/materials/${encodeURIComponent(materialId)}/transcript`,
      );
      panel.innerHTML = `<div class="transcript-head"><div><p>完整转写</p><h3>${escapeHtml(materialTitle({ filename, source_type: "audio" }))}</h3></div><button class="icon-button" type="button" data-close-transcript>×</button></div><pre>${escapeHtml(transcript.text || "暂无转写内容")}</pre>`;
      $("[data-close-transcript]", panel)?.addEventListener("click", () => {
        panel.hidden = true;
      });
    } catch (error) {
      panel.innerHTML = `<div class="error-panel"><strong>转写暂时打不开</strong><p>${escapeHtml(friendlyError(error))}</p></div>`;
    }
  }

function renderMatterTimeline(events = []) {
const labels = {
"material.received": "收到原始材料",
"material.assigned": "归入事项",
"job.completed": "贾维斯完成整理",
"action.created": "建立行动",
"action.planning_updated": "调整行动安排",
"action.completed": "行动完成",
"review.resolved": "完成拍板",
"assignee.confirmed": "确认跟进人",
    "reminder.created": "建立提醒",
    "matter.target_date.updated": "闭环日期调整",
    "progress.note": "业务推进",
};
const rows = Array.isArray(events) ? events : [];
const html = rows.length
? rows.map((item) => {
const title = humanText(item.summary, labels[item.type] || "事项有新进展", 220);
const payload = item.payload || {};
const detail = humanText(
payload.detail || payload.reason || payload.note || payload.evidence || "",
"",
240,
);
return `<li class="timeline-entry"><span class="timeline-dot"></span><div><header><strong>${escapeHtml(labels[item.type] || "事项进展")}</strong><time datetime="${escapeHtml(item.created_at || "")}">${escapeHtml(fmtDate(item.created_at))}</time></header><h3>${escapeHtml(title)}</h3>${detail ? `<p>${escapeHtml(detail)}</p>` : ""}</div></li>`;
}).join("")
: `<li class="timeline-empty"><span>✓</span><div><strong>还没有可展示的时间线</strong><p>材料、行动和依据会按发生时间自动收在这里。</p></div></li>`;
return `<section class="content-card matter-timeline-card"><div class="section-title"><div><p>按时间回看</p><h2>事项时间线</h2></div><span>${rows.length} 条记录</span></div><ol class="matter-timeline">${html}</ol></section>`;
}


  function renderAction(item, people = state.people) {
    const done = item.status === "done";
    const controls = done
      ? `<button type="button" class="button button-secondary" data-action-id="${escapeHtml(item.id)}" data-action-status="open">重新打开</button>`
      : `<button type="button" class="button button-primary" data-action-id="${escapeHtml(item.id)}" data-action-status="done">标记完成</button><button type="button" class="button button-quiet" data-action-id="${escapeHtml(item.id)}" data-action-status="dismissed">无需继续</button>`;
    const selectedIds = [
      ...confirmedAssignees(item).map(assigneePersonId),
      ...pendingAssignees(item).map(assigneePersonId),
    ].filter(Boolean);
    return `<li class="action-row ${done ? "done" : ""}" data-action-row="${escapeHtml(item.id)}"><div class="action-row-main"><div class="action-kind">${kindLabel(item.kind)}</div><h3>${escapeHtml(actionTitle(item))}</h3><p>${escapeHtml(actionDetail(item))}</p><small>${escapeHtml(actionAssigneeText(item, people))}${item.due_date ? ` · 截止 ${escapeHtml(item.due_date)}` : ""}</small>${renderAssigneeEditor(item, people, selectedIds)}</div><div class="action-controls"><button type="button" class="button button-secondary" data-assignee-toggle="${escapeHtml(item.id)}">调整负责人</button>${controls}</div></li>`;
  }

  function renderReminder(item) {
    const dismiss = `<button type="button" class="button button-quiet" data-reminder-id="${escapeHtml(item.id)}" data-reminder-status="dismissed">不再提醒</button>`;
    let primary = `<button type="button" class="button button-primary" data-reminder-id="${escapeHtml(item.id)}" data-reminder-status="done">已处理</button>`;
    if (item.kind === "review") {
      primary = `<a class="button button-primary" href="#/reviews">去拍板</a>`;
    } else if (item.action_id) {
      primary = `<button type="button" class="button button-primary" data-action-id="${escapeHtml(item.action_id)}" data-action-status="done">完成对应行动</button>`;
    }
    return `<article class="matter-reminder"><div><span>${escapeHtml(item.kind === "overdue" ? "已到期" : item.kind === "review" ? "待拍板" : "需关注")}</span><h3>${escapeHtml(humanText(item.title, "未完成提醒", 160))}</h3><p>${escapeHtml(humanText(item.reason, "这件事仍需要处理。", 280))}</p></div><footer>${primary}${dismiss}</footer></article>`;
  }

function renderMatter(matter, people = [], timeline = []) {
    state.people = normalizePeople(people);
    const assistant = matter.assistant || {};
    const brief = assistant.brief || {};
    const actions = (matter.actions || []).filter(
      (item) => item.status !== "dismissed",
    );
    const reviews = (matter.reviews || []).filter(
      (item) =>
        item.status === "pending" && item.payload?.field_type !== "材料类型",
    );
    const evidence = (matter.evidence || []).filter(
      (item) => !["rejected", "superseded"].includes(item.status),
    );
  const reminders = (matter.reminders || []).filter(
    (item) => !["done", "dismissed"].includes(item.status),
  );
  const organizedMaterialIds = new Set(
    [...actions, ...evidence, ...(matter.reviews || [])]
      .map((item) => item.material_id)
      .filter(Boolean),
  );
    const facts = evidence.filter((item) => item.claim_type === "fact");
    const whatDone = (brief.what_i_did || [])
      .map(
        (item) => `<li>${escapeHtml(humanText(item, "已完成整理", 140))}</li>`,
      )
      .join("");
    const agentCard = assistant.display_name
      ? `<section class="matter-agent-card"><div class="matter-agent-head"><div class="assistant-avatar large"><span>贾</span><i></i></div><div><p>贾维斯已接手</p><h2>${escapeHtml(humanText(brief.headline, matter.summary, 220))}</h2></div>${badge(humanText(assistant.source, "已完成理解", 32), "green")}</div>${whatDone ? `<ul>${whatDone}</ul>` : ""}${brief.needs_you ? `<div class="matter-needs-you"><span>需要你拍板</span><p>${escapeHtml(humanText(brief.needs_you, "", 300))}</p></div>` : ""}<footer><span>下一次检查</span><strong>${brief.next_check_at ? `${fmtDate(brief.next_check_at)} · ${escapeHtml(humanText(brief.next_check_reason, "复查进展", 160))}` : "有新进展时主动提醒"}</strong></footer></section>`
      : `<section class="matter-agent-card waiting"><div class="matter-agent-head"><div class="assistant-avatar large"><span>贾</span><i></i></div><div><p>贾维斯</p><h2>这件事还没有经过贾维斯深度整理</h2></div></div><p>可以让贾维斯重新研读原始材料，生成业务摘要、下一步和追踪计划。</p></section>`;

    const actionHtml = actions.length
      ? actions.map((item) => renderAction(item, state.people)).join("")
      : `<li class="calm-empty"><span>✓</span><div><strong>暂时没有开放动作</strong><p>贾维斯识别到新的下一步后会出现在这里。</p></div></li>`;
    const reminderHtml = reminders.length
      ? reminders.map(renderReminder).join("")
      : `<div class="calm-inline"><span>✓</span>目前没有未完成提醒</div>`;
    const reviewHtml = reviews.length
      ? reviews
          .map(
            (item) =>
              `<article class="decision-box"><span>需要你</span><h3>${escapeHtml(humanText(item.payload?.value || item.title, "有一条判断需要确认", 160).replace(/^确认推断[：:]\s*/, ""))}</h3><p>${escapeHtml(humanText(item.payload?.quote, "请回到原始依据确认。", 260))}</p><a href="#/reviews">去拍板 →</a></article>`,
          )
          .join("")
      : `<div class="calm-inline"><span>✓</span>目前没有需要你判断的内容</div>`;
    const factsHtml = facts.length
      ? facts
          .slice(0, 12)
          .map(
            (item) =>
              `<article class="fact-card"><header><span>${escapeHtml(item.field_type || "依据")}</span><small>${escapeHtml(humanText(item.source_locator, "来自原始材料", 90))}</small></header><h3>${escapeHtml(humanText(item.value, "已核实信息", 260))}</h3>${item.quote && item.quote !== item.value ? `<details><summary>查看原话</summary><blockquote>${escapeHtml(humanText(item.quote, "", 500))}</blockquote></details>` : ""}</article>`,
          )
          .join("")
      : `<div class="calm-inline">尚无可直接核实的事实依据</div>`;
  const matterProgressHtml = `<section class="content-card matter-progress-card"><div class="section-title"><div><p>手工更新</p><h2>状态、闭环日期与业务推进</h2></div><span>每次保存都会进入时间线</span></div><div class="matter-progress-grid"><form data-matter-status-form data-matter-id="${escapeHtml(matter.id)}"><label for="matter-status">事项状态</label><div class="matter-inline-fields"><select id="matter-status" name="status"><option value="active" ${matter.status === "completed" ? "" : "selected"}>正在推进</option><option value="completed" ${matter.status === "completed" ? "selected" : ""}>已完成</option></select><button class="button button-secondary" type="submit">保存状态</button></div><small>标记完成后进入完成归档；重新打开后回到推进列表。</small></form><form data-matter-date-form data-matter-id="${escapeHtml(matter.id)}"><label for="matter-target-date">要求闭环日期</label><div class="matter-inline-fields"><input id="matter-target-date" name="target_date" type="date" value="${escapeHtml(matter.target_date || "")}"><button class="button button-secondary" type="submit">保存日期</button></div><small>不确定日期时可以留空后保存。</small></form><form data-matter-progress-form data-matter-id="${escapeHtml(matter.id)}"><label for="matter-progress-summary">本次推进结果</label><input id="matter-progress-summary" name="summary" maxlength="200" placeholder="例如：已完成第一轮数据核对" required><label for="matter-progress-detail">补充说明</label><textarea id="matter-progress-detail" name="detail" rows="3" maxlength="2000" placeholder="记录已做了什么、还缺什么、下一步等谁。"></textarea><button class="button button-primary" type="submit">记录推进</button></form></div></section>`;
  const materialsHtml = (matter.materials || [])
      .map(
        (item) =>
        `<article class="source-row"><span class="source-icon">${escapeHtml(sourceLabel(item.source_type).slice(0, 1))}</span><div><p>${escapeHtml(sourceLabel(item.source_type))} · ${fmtDate(item.received_at)}</p><h3>${escapeHtml(materialTitle(item))}</h3><footer>${stateLabel(item.status)}${item.metadata?.assistant || organizedMaterialIds.has(item.id) ? badge("贾维斯已读", "green") : badge("待贾维斯整理", "amber")}</footer></div><div class="source-actions">${["audio", "video"].includes(item.source_type) && item.metadata?.transcription ? `<button class="button button-secondary" type="button" data-view-transcript="${escapeHtml(item.id)}" data-transcript-name="${escapeHtml(item.filename || "会议录音")}">查看转写</button>` : ""}<button class="button button-quiet" type="button" data-reanalyze="${escapeHtml(item.id)}">让贾维斯重整</button></div></article>`,
      )
      .join("");

    page().innerHTML = `<a class="back-link" href="#/matters">← 返回事项</a><section class="matter-hero"><div><p>${stateLabel(matter.status)} 更新于 ${fmtDate(matter.updated_at)}</p><h2>${escapeHtml(matterTitle(matter.title))}</h2><span>${escapeHtml(humanText(matter.summary, "贾维斯正在整理摘要。", 420))}</span></div><aside><strong>${actions.filter((item) => item.status === "open").length}</strong><small>待推进</small><strong>${reviews.length}</strong><small>待你拍板</small></aside></section>${agentCard}${renderMatterTimeline(timeline)}<div class="matter-layout"><main><section class="content-card"><div class="section-title"><div><p>行动台账</p><h2>接下来怎么做</h2></div><span>${actions.length} 项</span></div><ul class="action-list">${actionHtml}</ul></section><section class="content-card"><div class="section-title"><div><p>原始依据</p><h2>已经核实的内容</h2></div></div><div class="fact-grid">${factsHtml}</div></section></main><aside><section class="content-card decision-section"><div class="section-title"><div><p>人工边界</p><h2>需要你拍板</h2></div></div>${reviewHtml}</section><section class="content-card"><div class="section-title"><div><p>证据链</p><h2>来源材料</h2></div></div><div class="source-list">${materialsHtml}</div></section></aside></div><section id="transcript-panel" class="transcript-panel" hidden></section>`;
  page().querySelector(".matter-hero")?.insertAdjacentHTML("afterend", matterProgressHtml);
  $("[data-matter-status-form]", page())?.addEventListener("submit", saveMatterStatus);
  $("[data-matter-date-form]", page())?.addEventListener("submit", saveMatterTargetDate);
  $("[data-matter-progress-form]", page())?.addEventListener("submit", saveMatterProgress);
  $(".action-list", page())
      ?.closest(".content-card")
      ?.insertAdjacentHTML(
        "afterend",
        `<section class="content-card reminder-section"><div class="section-title"><div><p>主动跟进</p><h2>未完成提醒</h2></div><span>${reminders.length} 条</span></div><div class="matter-reminders">${reminderHtml}</div></section>`,
      );
    bindDynamic();
    $$("[data-view-transcript]", page()).forEach((button) =>
      button.addEventListener("click", () =>
        showTranscript(
          button.dataset.viewTranscript,
          button.dataset.transcriptName,
        ),
      ),
    );
  }

  async function saveMatterStatus(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api(`/api/matters/${encodeURIComponent(form.dataset.matterId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: form.elements.status.value }),
      });
      toast(
        form.elements.status.value === "completed"
          ? "事项已归入完成"
          : "事项已重新打开",
        "success",
      );
      await refreshRouteWithoutJump("#matter-status");
    } catch (error) {
      button.disabled = false;
      toast(friendlyError(error), "error");
    }
  }

  async function saveMatterTargetDate(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api(`/api/matters/${encodeURIComponent(form.dataset.matterId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_date: form.elements.target_date.value || null }),
      });
      toast("要求闭环日期已保存", "success");
      await refreshRouteWithoutJump("#matter-target-date");
    } catch (error) {
      button.disabled = false;
      toast(friendlyError(error), "error");
    }
  }

  async function saveMatterProgress(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const summary = form.elements.summary.value.trim();
    if (!summary) {
      form.elements.summary.focus({ preventScroll: true });
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api(`/api/matters/${encodeURIComponent(form.dataset.matterId)}/progress`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ summary, detail: form.elements.detail.value.trim() }),
      });
      toast("业务推进已记入时间线", "success");
      await refreshRouteWithoutJump("#matter-progress-summary");
    } catch (error) {
      button.disabled = false;
      toast(friendlyError(error), "error");
    }
  }

  function renderAssigneeReview(item, people) {
    const action = {
      ...(item.action || item),
      id: item.action?.id || item.action_id || item.id,
      title: item.action?.title || item.action_title || item.title,
      detail: item.action?.detail || item.action_detail || item.detail,
      kind: item.action?.kind || item.action_kind || item.kind,
      status: item.action?.status || item.action_status || item.status,
      matter_title: item.action?.matter_title || item.matter_title,
      assignee_suggestions:
        item.action?.assignee_suggestions ||
        item.assignee_suggestions ||
        item.suggested_people ||
        [],
    };
    const suggestions = item.suggested_people || pendingAssignees(action);
    const selectedIds = suggestions.map(assigneePersonId).filter(Boolean);
    const suggested = assigneeNames(suggestions, people);
    const evidenceQuote = suggestions
      .flatMap((suggestion) => suggestion.evidence || [])
      .find(Boolean);
    const quote = humanText(
      item.quote || item.evidence || item.reason_quote || evidenceQuote,
      "请根据原始材料确认跟进人。",
      360,
    );
    const reason = humanText(
      item.reason || suggestions[0]?.reason,
      suggested.length ? `贾维斯认为可能由 ${suggested.join("、")} 跟进。` : "贾维斯需要你确认这条行动由谁跟进。",
      220,
    );
    return `<article class="review-card assignee-review-card" data-assignee-review="${escapeHtml(action.id)}"><header><span>跟进人待确认</span><small>${escapeHtml(actionMatterLabel(action))}</small></header><h3>${escapeHtml(actionTitle(action))}</h3><p>${escapeHtml(actionDetail(action))}</p><div class="assignee-suggestion"><strong>${suggested.length ? `建议：${escapeHtml(suggested.join("、"))}` : "负责人待明确"}</strong><span>${escapeHtml(reason)}</span></div><blockquote>${escapeHtml(quote)}</blockquote>${renderAssigneeEditor(action, people, selectedIds)}<footer><button class="button button-primary" type="button" data-assignee-confirm="${escapeHtml(action.id)}">确认这些人</button><button class="button button-secondary" type="button" data-assignee-toggle="${escapeHtml(action.id)}">调整人选</button><button class="button button-quiet" type="button" data-assignee-none="${escapeHtml(action.id)}">都不负责</button></footer></article>`;
  }

  function renderReviews(items, assigneeItems = [], people = []) {
    state.people = normalizePeople(people);
    state.reviews = Array.isArray(items)
      ? items.filter((item) => item.payload?.field_type !== "材料类型")
      : [];
    state.assigneeReviews = Array.isArray(assigneeItems) ? assigneeItems : [];
    updateReviewCount(state.reviews.length + state.assigneeReviews.length);
    const rows = state.reviews.length
      ? state.reviews
          .map(
            (item) =>
              `<article class="review-card" data-review-row="${escapeHtml(item.id)}"><header><span>贾维斯拿不准</span><small>${item.matter_title ? escapeHtml(matterTitle(item.matter_title)) : "相关事项"}</small></header><h3>${escapeHtml(humanText(item.payload?.value || item.title, "有一条判断需要确认", 180).replace(/^确认推断[：:]\s*/, ""))}</h3><blockquote>${escapeHtml(humanText(item.payload?.quote, "请根据原始材料判断。", 360))}</blockquote><textarea class="review-note" rows="2" placeholder="如需修正，在这里写下正确内容" aria-label="修正内容"></textarea><footer><button class="button button-primary" type="button" data-review-action="accepted" data-review-id="${escapeHtml(item.id)}">确认无误</button><button class="button button-secondary" type="button" data-review-action="edited" data-review-id="${escapeHtml(item.id)}">修正后确认</button><button class="button button-quiet" type="button" data-review-action="rejected" data-review-id="${escapeHtml(item.id)}">这条不对</button></footer></article>`,
          )
          .join("")
      : `<div class="calm-panel large"><span>✓</span><div><strong>现在没有需要你拍板的内容</strong><p>贾维斯能确定的继续推进，拿不准的才会停在这里。</p></div><button class="button button-primary" type="button" data-open-intake>继续投递</button></div>`;
    const assigneeRows = state.assigneeReviews.length
      ? state.assigneeReviews
          .map((item) => renderAssigneeReview(item, state.people))
          .join("")
      : `<div class="calm-panel large"><span>✓</span><div><strong>没有待确认的跟进人</strong><p>新行动识别到可能负责人后，会先停在这里等你确认。</p></div></div>`;
    page().innerHTML = `<section class="section-heading"><div><p class="eyebrow">只把判断留给你</p><h2>需要我拍板</h2><span>推断不会直接变成事实；确认或修正后才进入事项结论。</span></div></section><nav class="wechat-tabs review-tabs" aria-label="拍板分类" role="tablist"><button class="${state.reviewTab === "business" ? "active" : ""}" type="button" role="tab" aria-selected="${state.reviewTab === "business"}" data-review-tab="business">业务判断 <span>${state.reviews.length}</span></button><button class="${state.reviewTab === "assignees" ? "active" : ""}" type="button" role="tab" aria-selected="${state.reviewTab === "assignees"}" data-review-tab="assignees">跟进人 <span>${state.assigneeReviews.length}</span></button></nav><section class="review-panel" data-review-panel="business" ${state.reviewTab === "business" ? "" : "hidden"}><div class="review-grid">${rows}</div></section><section class="review-panel" data-review-panel="assignees" ${state.reviewTab === "assignees" ? "" : "hidden"}><div class="review-grid">${assigneeRows}</div></section>`;
    $$("[data-review-tab]", page()).forEach((button) => {
      button.addEventListener("click", () => {
        state.reviewTab = button.dataset.reviewTab;
        $$("[data-review-tab]", page()).forEach((item) => {
          const active = item.dataset.reviewTab === state.reviewTab;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
        });
        $$("[data-review-panel]", page()).forEach((panel) => {
          panel.hidden = panel.dataset.reviewPanel !== state.reviewTab;
        });
      });
    });
    bindDynamic();
  }

  function renderNodes(items) {
    state.nodes = Array.isArray(items) ? items : [];
    const online = state.nodes.some((item) => item.status === "online");
  const current =
    state.nodes.find((item) => item.lifecycle === "current") || state.nodes[0];
    page().innerHTML = `<section class="section-heading"><div><p class="eyebrow">看清每一步由谁完成</p><h2>助理状态</h2><span>安卓本地等待，Mac 开盖和每两小时自动收件，贾维斯由你手工启动。</span></div></section><div class="assistant-status-grid"><article><span class="status-illustration cloud">收</span><div><p>第一步</p><h3>安卓手机投递箱</h3><strong class="online"><i></i>随时可存</strong><span>Mac 关机时材料留在手机“待处理”，不需要云主机或域名。</span></div></article><article><span class="status-illustration mac">M4</span><div><p>第二步</p><h3>Mac 本机处理</h3><strong class="${online ? "online" : "waiting"}"><i></i>${online ? "现在在线" : "等待开机"}</strong><span>${online ? "工作日开盖和每两小时自动收取新增内容。" : "开机后自动接收安卓中等待的材料。"}</span></div></article><article><span class="status-illustration buddy">贾</span><div><p>第三步</p><h3>贾维斯</h3><strong class="${online ? "online" : "waiting"}"><i></i>${online ? "等待你启动" : "随本机恢复"}</strong><span>只有你点击“让贾维斯整理新内容”后，才开始理解、归并和提取行动。</span></div></article></div>${current ? `<section class="status-note"><span>最近一次接手</span><strong>${fmtDate(current.last_seen_at)}</strong><p>${escapeHtml(current.metadata?.agent || "贾维斯")}</p></section>` : ""}`;
  const lifecycleLabels = {
    current: "5 分钟内在线",
    stale: "超过 5 分钟未响应",
    historical: "历史离线，记录保留",
  };
  const nodeHistory = state.nodes
    .map((item) => {
      const lifecycle =
        item.lifecycle || (item.status === "online" ? "current" : "stale");
      return `<li><span>${escapeHtml(item.name || "本机执行节点")}</span><strong>${escapeHtml(lifecycleLabels[lifecycle] || "状态待确认")}</strong><small>${escapeHtml(fmtDate(item.last_seen_at))}</small></li>`;
    })
    .join("");
  if (nodeHistory) {
    page().querySelector(".assistant-status-grid")?.insertAdjacentHTML(
      "afterend",
      `<section class="status-note node-history"><span>节点历史</span><ul>${nodeHistory}</ul></section>`,
    );
  }
}

function bindDynamic() {
    $$("[data-open-intake]").forEach((button) =>
      button.addEventListener("click", openIntake),
    );
    $$("[data-analysis-retry]").forEach((button) =>
      button.addEventListener("click", async () => {
        const original = button.textContent;
        button.disabled = true;
        button.textContent = "正在重新排队…";
        try {
          await api(
            `/api/analysis/issues/${encodeURIComponent(button.dataset.analysisRetry)}/retry`,
            { method: "POST" },
          );
          toast("已重新交给贾维斯整理", "success");
          await renderRoute({ quiet: true, preserveScroll: true });
          await refreshAnalysisButton();
        } catch (error) {
          toast(friendlyError(error), "error");
          button.disabled = false;
          button.textContent = original;
        }
      }),
    );
    $$("[data-review-action]").forEach((button) =>
      button.addEventListener("click", () => resolveReview(button)),
    );
    $$("[data-reanalyze]").forEach((button) =>
      button.addEventListener("click", () => reanalyzeMaterial(button)),
    );
  $$("[data-action-id]").forEach((button) =>
    button.addEventListener("click", () => resolveAction(button)),
  );
  $$("[data-assignee-toggle]").forEach((button) =>
    button.addEventListener("click", () => toggleAssigneeEditor(button)),
  );
  $$("[data-assignee-save], [data-assignee-confirm], [data-assignee-none]").forEach(
    (button) =>
      button.addEventListener("click", () => resolveAssignees(button)),
  );
  $$("[data-reminder-id]").forEach((button) =>
    button.addEventListener("click", () => resolveReminder(button)),
  );
  $("[data-wechat-sync]")?.addEventListener("click", (event) =>
    runWechatSync(event.currentTarget),
  );
  $$("[data-wechat-tab]").forEach((button) =>
    button.addEventListener("click", () => setWechatTab(button.dataset.wechatTab)),
  );
  $$("[data-chat-source]").forEach((button) =>
    button.addEventListener("click", () => setChatSource(button.dataset.chatSource)),
  );
  $$("[data-wechat-action]").forEach((button) =>
    button.addEventListener("click", () => resolveWechatCandidate(button)),
  );
 $$("[data-wechat-conversation-action]").forEach((button) =>
 button.addEventListener("click", () => changeWechatConversation(button)),
 );
 $("[data-wechat-new-only]")?.addEventListener("click", (event) => {
 state.wechatNewOnly = !state.wechatNewOnly;
 event.currentTarget.classList.toggle("active", state.wechatNewOnly);
 event.currentTarget.setAttribute("aria-pressed", String(state.wechatNewOnly));
 filterWechatConversationRows();
 });
 $$("[data-copy-path]").forEach((button) =>
 button.addEventListener("click", async () => {
 try {
 await navigator.clipboard.writeText(button.dataset.copyPath || "");
 toast("保存位置已复制", "success");
 } catch (_) {
 toast("暂时无法复制，可以直接选中路径", "error");
 }
 }),
 );
  $("[data-wechat-search]")?.addEventListener("input", (event) => {
    filterWechatConversationRows();
  });
}

  function assigneeContainer(button) {
    return (
      button.closest("[data-action-row]") ||
      button.closest("[data-assignee-review]")
    );
  }

  function toggleAssigneeEditor(button) {
    const container = assigneeContainer(button);
    const editor = container?.querySelector("[data-assignee-editor]");
    if (!editor) return;
    editor.hidden = !editor.hidden;
    if (!editor.hidden) {
      editor.querySelector("input, textarea, button")?.focus({ preventScroll: true });
    }
  }

  function selectedAssigneeIds(container) {
    return $$("[data-assignee-editor] input:checked", container).map(
      (input) => input.value,
    );
  }

  async function resolveAssignees(button) {
    const actionId =
      button.dataset.assigneeSave ||
      button.dataset.assigneeConfirm ||
      button.dataset.assigneeNone;
    const container = assigneeContainer(button);
    const ids = button.dataset.assigneeNone ? [] : selectedAssigneeIds(container);
    const note = container?.querySelector("[data-assignee-note]")?.value || "";
    button.disabled = true;
    try {
      await api(`/api/actions/${encodeURIComponent(actionId)}/assignees`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ person_ids: ids, note }),
      });
      toast(ids.length ? "负责人已确认" : "已标记为暂无负责人", "success");
      await refreshRouteWithoutJump();
    } catch (error) {
      button.disabled = false;
      toast(friendlyError(error), "error");
    }
  }

  async function resolveReview(button) {
    const row = button.closest("[data-review-row]");
    const note = row.querySelector(".review-note")?.value || "";
    if (button.dataset.reviewAction === "edited" && !note.trim()) {
      row.querySelector(".review-note")?.focus();
      toast("先写下正确内容，再点修正后确认");
      return;
    }
    $$("[data-review-action]", row).forEach((item) => {
      item.disabled = true;
    });
    try {
      await api(
        `/api/reviews/${encodeURIComponent(button.dataset.reviewId)}/resolve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            resolution: button.dataset.reviewAction,
            note,
          }),
        },
      );
      toast("判断已保存，贾维斯会按你的结论继续", "success");
      await refreshRouteWithoutJump();
    } catch (error) {
      $$("[data-review-action]", row).forEach((item) => {
        item.disabled = false;
      });
      toast(friendlyError(error), "error");
    }
  }

  async function resolveReminder(button) {
    button.disabled = true;
    try {
      await api(
        `/api/reminders/${encodeURIComponent(button.dataset.reminderId)}/resolve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: button.dataset.reminderStatus }),
        },
      );
      toast(
        button.dataset.reminderStatus === "done" ? "提醒已处理" : "这条提醒已关闭",
        "success",
      );
      await refreshRouteWithoutJump();
    } catch (error) {
      button.disabled = false;
      toast(friendlyError(error), "error");
    }
  }

  async function resolveAction(button) {
    button.disabled = true;
    try {
      await api(
        `/api/actions/${encodeURIComponent(button.dataset.actionId)}/resolve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: button.dataset.actionStatus }),
        },
      );
      const message =
        button.dataset.actionStatus === "done"
          ? "已完成一项"
          : button.dataset.actionStatus === "dismissed"
            ? "已标记为无需继续"
            : "已重新打开";
      toast(message, "success");
      await refreshRouteWithoutJump();
    } catch (error) {
      button.disabled = false;
      toast(friendlyError(error), "error");
    }
  }

  async function reanalyzeMaterial(button) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "已交给贾维斯";
    try {
      await api(
        `/api/materials/${encodeURIComponent(button.dataset.reanalyze)}/assistant`,
        { method: "POST" },
      );
      toast("贾维斯已接手重新整理", "success");
      window.setTimeout(refreshRouteWithoutJump, 600);
    } catch (error) {
      button.disabled = false;
      button.textContent = original;
      toast(friendlyError(error), "error");
    }
  }

function updateReviewCount(count) {
  setShortcutCount("#review-count", count);
}

function updateWechatCount(count) {
  setShortcutCount("#wechat-count", count);
}

  function openIntake() {
    const dialog = $("#intake-dialog");
    if (!dialog.open) dialog.showModal();
    loadMatterOptions();
    $("#intake-note")?.focus();
  }

  function closeIntake() {
    $("#intake-dialog")?.close();
  }

  async function loadMatterOptions() {
    const select = $("#intake-matter");
    if (!select) return;
    if (!state.matters.length) {
      try {
        state.matters = await api("/api/matters?limit=100");
      } catch (_) {
        return;
      }
    }
    const current = select.value;
    select.innerHTML = `<option value="">交给贾维斯判断</option>${mergeableMatters(state.matters).map((matter) => `<option value="${escapeHtml(matter.id)}">${escapeHtml(matterTitle(matter.title))}</option>`).join("")}`;
    select.value = current;
  }

  function setSourceType(sourceType) {
    state.sourceType = sourceType;
    $("#intake-source-type").value = sourceType;
    $$(".source-tab").forEach((tab) => {
      const active = tab.dataset.source === sourceType;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function setSelectedFile(file) {
    state.selectedFile = file || null;
    const selected = $("#selected-file");
    selected.hidden = !file;
    selected.textContent = file
      ? `${file.name} · ${formatBytes(file.size)}`
      : "";
    if (!file) return;
    let source = file.type.startsWith("image/")
      ? "image"
      : file.type.startsWith("audio/")
        ? "audio"
        : file.type.startsWith("video/")
          ? "video"
          : "file";
    if (
      /\.(?:json|md|markdown)$/i.test(file.name) &&
      /微信|群聊|私聊|wechat/i.test(file.name)
    )
      source = "wechat_markdown";
    setSourceType(source);
  }

  function formatBytes(size) {
    if (!size) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(
      Math.floor(Math.log(size) / Math.log(1024)),
      units.length - 1,
    );
    return `${(size / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function makeIdempotencyKey() {
    return crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function sendIntake(item) {
    const body = new FormData();
    body.set("source_type", item.sourceType);
    body.set("text_note", item.textNote || "");
    if (item.matterId) body.set("matter_id", item.matterId);
    if (item.file)
      body.set(
        "upload",
        item.file,
        item.file.name || item.fileName || "upload",
      );
    return api("/api/intake", {
      method: "POST",
      headers: { "Idempotency-Key": item.idempotencyKey },
      body,
    });
  }

  async function submitIntake(event) {
    event.preventDefault();
    const note = $("#intake-note").value.trim();
    const message = $("#intake-message");
    if (!note && !state.selectedFile) {
      message.hidden = false;
      message.className = "form-message error-message";
      message.textContent = "粘贴一句话或选择一个文件就可以。";
      return;
    }
    const item = {
      sourceType: state.sourceType,
      textNote: note,
      matterId: $("#intake-matter").value || "",
      file: state.selectedFile,
      fileName: state.selectedFile?.name || "",
      fileType: state.selectedFile?.type || "",
      idempotencyKey: makeIdempotencyKey(),
      createdAt: new Date().toISOString(),
    };
    const submit = $('[data-testid="intake-submit"]');
    submit.disabled = true;
    submit.textContent = "正在安全收下…";
    try {
      if (!navigator.onLine) throw new TypeError("offline");
      await sendIntake(item);
      message.hidden = false;
      message.className = "form-message success-message";
      message.textContent =
        "已收下并进入等待整理。需要时点击顶部“让贾维斯整理新内容”。";
      toast("材料已收下，等待你启动整理", "success");
      window.setTimeout(() => {
        closeIntake();
        resetIntake();
        const alreadyToday = window.location.hash === "#/today";
        window.location.hash = "#/today";
        if (alreadyToday) refreshRouteWithoutJump();
      }, 900);
    } catch (error) {
      if (!error.status || error.name === "TypeError") {
        await queueIntake(item);
        message.hidden = false;
        message.className = "form-message success-message";
        message.textContent =
          "当前连接中断，材料已暂存在这台设备；重新连接工作台后自动上传。";
        toast("已暂存，联网后自动上传", "success");
      } else {
        message.hidden = false;
        message.className = "form-message error-message";
        message.textContent = friendlyError(error, "投递失败，请稍后再试");
      }
    } finally {
      submit.disabled = false;
      submit.textContent = "交给贾维斯";
    }
  }

  function resetIntake() {
    $("#intake-form")?.reset();
    state.selectedFile = null;
    setSourceType("text");
    $("#selected-file").hidden = true;
    $("#intake-message").hidden = true;
  }

  const DB_NAME = "finance-workbench";
  const DB_VERSION = 1;

  function openQueueDb() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () =>
        request.result.createObjectStore("intakeQueue", {
          keyPath: "id",
          autoIncrement: true,
        });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function queueIntake(item) {
    const db = await openQueueDb();
    await new Promise((resolve, reject) => {
      const request = db
        .transaction("intakeQueue", "readwrite")
        .objectStore("intakeQueue")
        .add(item);
      request.onsuccess = resolve;
      request.onerror = () => reject(request.error);
    });
    state.queueCount += 1;
  }

  async function readQueue() {
    try {
      const db = await openQueueDb();
      return await new Promise((resolve, reject) => {
        const request = db
          .transaction("intakeQueue", "readonly")
          .objectStore("intakeQueue")
          .getAll();
        request.onsuccess = () => resolve(request.result || []);
        request.onerror = () => reject(request.error);
      });
    } catch (_) {
      return [];
    }
  }

  async function deleteQueueItem(id) {
    const db = await openQueueDb();
    return new Promise((resolve, reject) => {
      const request = db
        .transaction("intakeQueue", "readwrite")
        .objectStore("intakeQueue")
        .delete(id);
      request.onsuccess = resolve;
      request.onerror = () => reject(request.error);
    });
  }

  async function flushQueue() {
    if (!navigator.onLine) return;
    for (const item of await readQueue()) {
      try {
        await sendIntake(item);
        await deleteQueueItem(item.id);
      } catch (error) {
        if (
          error.status >= 400 &&
          error.status < 500 &&
          ![401, 403].includes(error.status)
        )
          await deleteQueueItem(item.id);
        else break;
      }
    }
    state.queueCount = (await readQueue()).length;
    updateQueueView();
  }

  async function updateQueueView() {
    const items = await readQueue();
    state.queueCount = items.length;
    const summary = $("#queue-summary");
    const list = $("#queue-list");
    if (!summary || !list) return;
    summary.textContent = items.length
      ? `${items.length} 份材料等待联网`
      : "没有等待上传的材料";
    list.innerHTML = items.length
      ? items
          .map(
            (item) =>
              `<div class="queue-row"><strong>${escapeHtml(item.fileName || "文字材料")}</strong><span>${sourceLabel(item.sourceType)} · ${fmtDate(item.createdAt)}</span></div>`,
          )
          .join("")
      : `<p class="queue-empty">连接中断时暂存的材料会出现在这里。</p>`;
  }

  async function handleLogin(event) {
    event.preventDefault();
    const input = $("#passcode");
    const button = $("#login-submit");
    const error = $("#login-error");
    button.disabled = true;
    button.textContent = "正在进入…";
    error.hidden = true;
    try {
      state.actor = await api("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: input.value }),
      });
      input.value = "";
      showWorkbench();
    } catch (reason) {
      error.hidden = false;
      error.textContent = friendlyError(reason, "口令不正确或服务暂时不可用");
    } finally {
      button.disabled = false;
      button.textContent = "进入工作台";
    }
  }

  async function logout() {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch (_) {}
    state.actor = null;
    showLogin();
  }

  async function runPendingAnalysis() {
    const button = $("#run-analysis-global");
    if (!button) return;
    button.disabled = true;
    button.textContent = "正在交给贾维斯…";
    try {
      const result = await api("/api/analysis/run", { method: "POST" });
      const released = Number(result.released || 0);
      const needsReview = Number(result.needs_review || 0);
      toast(
        released
          ? `已交给贾维斯整理 ${released} 份新内容`
          : needsReview
          ? `有 ${needsReview} 份内容需要检查`
          : "现在没有等待整理的新内容",
        released ? "success" : needsReview ? "error" : "info",
      );
      if (!released && needsReview) window.location.hash = "#/intake";
      await refreshRouteWithoutJump();
      await refreshAnalysisButton();
    } catch (error) {
      toast(friendlyError(error), "error");
    } finally {
      button.disabled = false;
      await refreshAnalysisButton();
    }
  }

  async function runSourceSyncLegacy() {
    const button = $("#run-source-sync-global");
    if (!button) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "正在读取三个平台…";
    try {
      const results = await Promise.allSettled([
        api("/api/wechat/sync/run", { method: "POST" }),
        api("/api/email/sync/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
      ]);
      const succeeded = results.filter((item) => item.status === "fulfilled").length;
      if (succeeded === 2) {
        toast("已开始读取个人微信、企业微信和邮箱；读取完成后再点贾维斯整理", "success");
      } else if (succeeded === 1) {
        toast("已有部分来源开始读取，另一个来源暂时未连接", "info");
      } else {
        throw results[0].reason || new Error("三个平台暂时都没有开始读取");
      }
      await refreshShortcutCounts();
      window.setTimeout(refreshShortcutCounts, 1200);
    } catch (error) {
      toast(friendlyError(error), "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  const sourceSyncLabels = {
    personal_wechat: "个人微信",
    wecom: "企业微信",
    email: "邮箱",
  };

  const sourceSyncTerminalStates = new Set([
    "completed",
    "failed",
    "unsupported",
  ]);

  function sourceSyncResultText(source, result) {
    if (!result) return "等待本机开始读取";
    if (result.status === "pending") return "已排队，等待本机读取";
    if (result.status === "running") return "正在读取新增内容";
    if (result.status === "failed" || result.status === "unsupported") {
      return humanText(result.error, "本次读取未完成，请稍后重试", 180);
    }
    if (source === "email") {
      const pending = Number(result.pending_count || 0);
      const ignored = Number(result.ignored_count || 0);
      return pending || ignored
        ? `新增 ${pending} 封待整理邮件，过滤 ${ignored} 封非工作邮件`
        : "没有新增工作邮件";
    }
    const messages = Number(result.message_count || 0);
    const windows = Number(result.window_count || 0);
    return windows
      ? `读取 ${messages} 条消息，形成 ${windows} 组待整理线索`
      : messages
        ? `读取 ${messages} 条消息，没有新增待整理线索`
        : "没有新增消息";
  }

  function renderSourceSyncReceipt(rows, options = {}) {
    const receipt = $("#source-sync-receipt");
    const grid = $("#source-sync-result-grid");
    if (!receipt || !grid) return;
    if (options.resetDismissed) state.sourceReceiptDismissed = false;
    if (state.sourceReceiptDismissed) return;
    receipt.hidden = false;
    const terminal = rows.every((item) =>
      sourceSyncTerminalStates.has(item.result?.status),
    );
    $("#source-sync-receipt-kicker").textContent = terminal
      ? "本次读取结果"
      : "读取进度";
    $("#source-sync-receipt-title").textContent = terminal
      ? "三个平台已经检查完毕"
      : "正在检查个人微信、企业微信和邮箱";
    grid.innerHTML = rows
      .map((item) => {
        const status = item.result?.status || "pending";
        const stateText =
          status === "completed"
            ? "已完成"
            : status === "failed" || status === "unsupported"
              ? "未完成"
              : status === "running"
                ? "读取中"
                : "等待中";
        return `<article class="source-sync-result ${escapeHtml(status)}"><div><span class="status-dot"></span><strong>${escapeHtml(sourceSyncLabels[item.source])}</strong><em>${escapeHtml(stateText)}</em></div><p>${escapeHtml(sourceSyncResultText(item.source, item.result))}</p></article>`;
      })
      .join("");
    const newItems = rows.reduce(
      (total, item) =>
        total +
        (item.source === "email"
          ? Number(item.result?.pending_count || 0)
          : Number(item.result?.window_count || 0)),
      0,
    );
    const pending = Number(options.analysisPending || 0);
    const failed = rows.filter((item) =>
      ["failed", "unsupported"].includes(item.result?.status),
    ).length;
    const summary = $("#source-sync-summary");
    if (summary) {
      summary.textContent = !terminal
        ? "读取完成后会在这里显示新增数量，不会自动消耗贾维斯额度。"
        : failed
          ? `${3 - failed} 个来源完成，${failed} 个来源需要重试。`
          : newItems
            ? `本次新增 ${newItems} 份待整理内容；当前共有 ${pending} 份等待贾维斯整理。`
            : pending
              ? `本次没有新增内容；此前还有 ${pending} 份等待贾维斯整理。`
              : "本次没有新增工作内容，也没有等待整理的材料。";
    }
    const organize = $("#source-sync-organize");
    if (organize) {
      organize.hidden = !terminal || pending <= 0;
      organize.textContent = pending ? `整理当前 ${pending} 份内容` : "整理这些新内容";
    }
  }

  function sourceSyncRows(chatStatus, emailStatus, requestIds, failures) {
    return ["personal_wechat", "wecom", "email"].map((source) => {
      if (failures[source]) {
        return {
          source,
          result: { status: "failed", error: failures[source] },
        };
      }
      const latest =
        source === "email"
          ? emailStatus?.latest
          : chatStatus?.sources?.[source]?.latest;
      if (requestIds[source] && latest?.id !== requestIds[source]) {
        return { source, result: { status: "pending" } };
      }
      return { source, result: latest || { status: "pending" } };
    });
  }

  async function waitForSourceSync(requestIds, failures) {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const [chatStatus, emailStatus, analysisStatus] = await Promise.all([
        apiOptional("/api/wechat/status", {}),
        apiOptional("/api/email/status", {}),
        apiOptional("/api/analysis/status", {}),
      ]);
      const rows = sourceSyncRows(
        chatStatus,
        emailStatus,
        requestIds,
        failures,
      );
      const terminal = rows.every((item) =>
        sourceSyncTerminalStates.has(item.result?.status),
      );
      renderSourceSyncReceipt(rows, {
        analysisPending: analysisStatus?.pending,
      });
      if (terminal) return { rows, analysisStatus };
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
    throw new Error("读取仍在后台继续，可稍后再次查看结果");
  }

  async function runSourceSync() {
    const button = $("#run-source-sync-global");
    if (!button || state.sourceSyncRunning) return;
    const original = button.textContent;
    state.sourceSyncRunning = true;
    button.disabled = true;
    button.textContent = "正在读取三个平台…";
    renderSourceSyncReceipt(
      ["personal_wechat", "wecom", "email"].map((source) => ({
        source,
        result: { status: "pending" },
      })),
      { resetDismissed: true },
    );
    try {
      const results = await Promise.allSettled([
        api("/api/wechat/sync/run", { method: "POST" }),
        api("/api/email/sync/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }),
      ]);
      const requestIds = {};
      const failures = {};
      if (results[0].status === "fulfilled") {
        for (const request of results[0].value?.requests || []) {
          requestIds[request.source] = request.id;
        }
      } else {
        failures.personal_wechat = friendlyError(results[0].reason);
        failures.wecom = friendlyError(results[0].reason);
      }
      if (results[1].status === "fulfilled") {
        requestIds.email = results[1].value?.id;
      } else {
        failures.email = friendlyError(results[1].reason);
      }
      const completed = await waitForSourceSync(requestIds, failures);
      const failed = completed.rows.filter((item) =>
        ["failed", "unsupported"].includes(item.result?.status),
      ).length;
      toast(
        failed ? "读取结束，部分来源需要重试" : "三个平台读取完成，结果已经列出",
        failed ? "info" : "success",
      );
      await Promise.all([refreshShortcutCounts(), refreshAnalysisButton()]);
    } catch (error) {
      toast(friendlyError(error), "error");
      $("#source-sync-receipt-title").textContent = "读取仍在后台继续";
      $("#source-sync-summary").textContent = friendlyError(error);
    } finally {
      state.sourceSyncRunning = false;
      button.disabled = false;
      button.textContent = original;
    }
  }

  function setupIntake() {
    $("#intake-form").addEventListener("submit", submitIntake);
    $("[data-close-dialog]").addEventListener("click", closeIntake);
    $("#choose-file").addEventListener("click", () =>
      $("#intake-file").click(),
    );
    $("#intake-file").addEventListener("change", (event) =>
      setSelectedFile(event.target.files[0]),
    );
    const dropzone = $("#dropzone");
    ["dragenter", "dragover"].forEach((type) =>
      dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      }),
    );
    ["dragleave", "drop"].forEach((type) =>
      dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      }),
    );
    dropzone.addEventListener("drop", (event) =>
      setSelectedFile(event.dataTransfer.files[0]),
    );
    dropzone.addEventListener("keydown", (event) => {
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault();
        $("#intake-file").click();
      }
    });
    $$(".source-tab").forEach((tab) =>
      tab.addEventListener("click", () => setSourceType(tab.dataset.source)),
    );
  }

  async function init() {
    setupIntake();
    $("#login-form").addEventListener("submit", handleLogin);
    $("#logout-button").addEventListener("click", logout);
    $("#run-source-sync-global")?.addEventListener("click", runSourceSync);
    $("#run-analysis-global")?.addEventListener("click", runPendingAnalysis);
    $("#source-sync-organize")?.addEventListener("click", runPendingAnalysis);
  $("#source-sync-receipt-close")?.addEventListener("click", () => {
    state.sourceReceiptDismissed = true;
    $("#source-sync-receipt").hidden = true;
  });
    window.addEventListener("hashchange", () => {
      if (state.actor) renderRoute();
    });
    window.addEventListener("keydown", (event) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k") return;
      if (!state.actor) return;
      event.preventDefault();
      state.searchFocus = true;
      if (window.location.hash === "#/search") {
        $("#global-search-input")?.focus({ preventScroll: true });
      } else {
        window.location.hash = "#/search";
      }
    });
    window.addEventListener("online", () => {
      setConnection(true);
      toast("网络已恢复，正在上传暂存材料", "success");
      flushQueue();
    });
    window.addEventListener("offline", () => {
      setConnection(false);
      toast("连接中断，材料会暂存在当前设备");
    });
    window.addEventListener("paste", (event) => {
      const file = [...(event.clipboardData?.files || [])][0];
      if (file && $("#intake-dialog")?.open) setSelectedFile(file);
    });
    if ("serviceWorker" in navigator)
    navigator.serviceWorker.register("/sw.js?v=51").catch(() => {});
    try {
    state.actor = await api("/api/auth/session");
    $("#logout-button").hidden = state.actor.password_required === false;
    showWorkbench();
      flushQueue();
    } catch (_) {
      showLogin();
    }
  }

  init();
})();
