/* 邮箱监控台前端
 *
 * 三条硬规则（对应设计文档 §4 与 §8）：
 *   1. 所有文本一律经 textContent / createElement 渲染：消息正文、错误文本和数据库
 *      内容都不会被当作标记解析，恶意标签在页面上只是字面文本。
 *   2. 只读：本文件只发 GET 请求，页面上没有任何写操作入口。
 *   3. 失败不得伪装成功：请求失败时保留"最后成功刷新时间"并明确标注当前数据是旧的。
 */

(function () {
  "use strict";

  var STORAGE_KEY = "mailbox-dashboard-token";
  var PAGE_SIZE = 25;
  var AUTH_HELP = "缺少或无效的访问令牌。请使用启动时打印的带 token 的 URL 重新打开页面。";

  // ---------------------------------------------------------------- token

  var token = null;

  function readTokenSource() {
    var hash = window.location.hash || "";
    if (hash.indexOf("token=") >= 0) {
      return { value: decodeURIComponent(hash.split("token=")[1].split("&")[0]), source: "hash" };
    }
    var search = window.location.search || "";
    if (search.indexOf("token=") >= 0) {
      return { value: decodeURIComponent(search.split("token=")[1].split("&")[0]), source: "query" };
    }
    try {
      var stored = window.sessionStorage.getItem(STORAGE_KEY);
      if (stored) return { value: stored, source: "session" };
    } catch (err) {
      /* sessionStorage 可能被策略禁用；此时退化为"每次都要带 token 打开" */
    }
    return { value: null, source: "none" };
  }

  function initToken() {
    var found = readTokenSource();
    token = found.value;
    if (token) {
      try { window.sessionStorage.setItem(STORAGE_KEY, token); } catch (err) { /* 忽略 */ }
    }
    // 无论来源如何，都把令牌从地址栏抹掉，避免出现在截图、历史记录和 Referer 里。
    if (found.source === "hash" || found.source === "query") {
      try {
        window.history.replaceState({}, document.title, window.location.pathname);
      } catch (err) {
        window.location.hash = "";
      }
    }
    return Boolean(token);
  }

  // ------------------------------------------------------------------ api

  function ApiError(code, message, status) {
    this.name = "ApiError";
    this.code = code;
    this.message = message;
    this.status = status;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function friendlyMessage(status, code) {
    if (status === 401) return AUTH_HELP;
    if (status === 405) return "监控台是只读的，不支持写操作。";
    if (status === 503) return "数据库正忙（可能正在被写入），请稍后重试。";
    if (status === 404) return "请求的资源不存在，或已被其他进程删除。";
    if (status === 400) return "请求参数或游标非法。";
    if (code) return "请求失败（" + code + "）。";
    return "请求失败（HTTP " + status + "）。";
  }

  function apiGet(path) {
    var headers = { Accept: "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    return fetch(path, { method: "GET", headers: headers, credentials: "omit", cache: "no-store" })
      .then(function (response) {
        return response.text().then(function (text) {
          var payload = null;
          try { payload = JSON.parse(text); } catch (err) { payload = null; }
          if (!response.ok) {
            var code = payload && payload.error ? String(payload.error) : "http_" + response.status;
            var msg = payload && payload.message
              ? String(payload.message)
              : friendlyMessage(response.status, code);
            throw new ApiError(code, msg, response.status);
          }
          if (!payload || payload.ok !== true) {
            throw new ApiError("malformed", "服务返回了无法解析的响应。", response.status);
          }
          return payload;
        });
      })
      .catch(function (err) {
        if (err instanceof ApiError) throw err;
        throw new ApiError("network", "无法连接到监控台服务（网络错误或服务已停止）。", 0);
      });
  }

  // ------------------------------------------------------------ dom utils

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function looksLikeMarkup(value) {
    return typeof value === "string" && /<\s*[a-zA-Z!/]/.test(value);
  }

  function dash(value) {
    if (value === null || value === undefined || value === "") return "—";
    return String(value);
  }

  function shortId(value) {
    if (!value) return "—";
    var text = String(value);
    return text.length > 14 ? text.slice(0, 9) + "…" + text.slice(-4) : text;
  }

  function formatTime(value) {
    if (!value) return "—";
    var parsed = new Date(value);
    if (isNaN(parsed.getTime())) return String(value);
    function pad(n) { return n < 10 ? "0" + n : String(n); }
    return parsed.getFullYear() + "-" + pad(parsed.getMonth() + 1) + "-" + pad(parsed.getDate()) +
      " " + pad(parsed.getHours()) + ":" + pad(parsed.getMinutes()) + ":" + pad(parsed.getSeconds());
  }

  var GLYPHS = {
    connected: "◎", offline: "✕", unknown: "?",
    ok: "✓", warn: "!", bad: "✕",
    delivered: "✓", dead_letter: "✕", failed: "✕", queued: "…", dispatched: "→",
    completed: "✓", blocked: "!", running: "▶", pending: "…", cancelled: "⊘",
    unread: "●", seen: "○"
  };

  function chip(label, tone, glyph) {
    var node = el("span", "chip chip-" + (tone || "unknown"));
    if (glyph) node.appendChild(el("span", "glyph", glyph));
    node.appendChild(el("span", null, label));
    return node;
  }

  /** 在线状态：只看托管进程；能不能被注入开工由 wake_basis 决定。 */
  function presenceChip(presence, explanation) {
    var tone = presence === "connected" ? "connected" : "offline";
    var label = presence === "connected" ? "在线 · 托管进程活着" : "离线 · 消息排队等它上线";
    var node = chip(label, tone, GLYPHS[presence] || GLYPHS.unknown);
    if (explanation) node.title = explanation;
    return node;
  }

  /** 注入能力：说清依据，别把"没有通道"说成"可以唤醒"。 */
  function wakeChip(canWake, basis, explanation) {
    var labels = {
      direct_channel: "可直接注入开工",
      declared_level2: "声明可注入（未验证）",
      no_channel: "无注入通道 · 等对方取信",
      offline: "离线 · 只存不发"
    };
    var tone = basis === "direct_channel" ? "ok" :
      basis === "declared_level2" ? "warn" : "bad";
    var glyph = basis === "direct_channel" ? GLYPHS.ok :
      basis === "declared_level2" ? GLYPHS.warn : GLYPHS.bad;
    var node = chip(labels[basis] || (canWake ? "可注入" : "不可注入"), tone, glyph);
    if (explanation) node.title = explanation;
    return node;
  }

  function stateLine(text, kind, withSpinner) {
    var node = el("p", "state" + (kind ? " is-" + kind : ""));
    if (withSpinner) node.appendChild(el("span", "spinner"));
    node.appendChild(el("span", null, (withSpinner ? " " : "") + text));
    return node;
  }

  /** 空状态：统一样式与措辞，保证每个区域都有明确的空态展示。 */
  function emptyLine(text) {
    return stateLine(text, "empty");
  }

  function kvList(pairs) {
    var dl = el("dl", "kv");
    pairs.forEach(function (pair) {
      if (!pair) return;
      dl.appendChild(el("dt", null, pair[0]));
      var dd = el("dd", pair[2] ? "mono" : null, dash(pair[1]));
      if (pair[3] && looksLikeMarkup(pair[1])) {
        dd.appendChild(el("span", "warn-text", "（内容含标记字符，已在下方以纯文本展示）"));
      }
      dl.appendChild(dd);
    });
    return dl;
  }

  function table(headers, rows, caption) {
    var wrap = el("div", "table-wrap");
    var tbl = el("table");
    if (caption) tbl.appendChild(el("caption", null, caption));
    var thead = el("thead");
    var headRow = el("tr");
    headers.forEach(function (header) {
      var th = el("th", header.mono ? "mono" : null, header.label);
      if (header.scope) th.setAttribute("scope", "col");
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    tbl.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (cells) {
      var tr = el("tr");
      cells.forEach(function (cell) {
        var td = el("td", cell && cell.mono ? "mono" : null);
        var value = cell && Object.prototype.hasOwnProperty.call(cell, "value") ? cell.value : cell;
        if (value instanceof Node) {
          td.appendChild(value);
        } else {
          td.textContent = dash(value);
        }
        if (cell && cell.title) td.title = cell.title;
        if (cell && cell.nowrap) td.className += " nowrap";
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
    return wrap;
  }

  function button(label, onClick, className) {
    var node = el("button", "btn" + (className ? " " + className : ""), label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  // ----------------------------------------------------------------- state

  var state = {
    area: "conversations",
    lastSuccessAt: null,
    lastError: null,
    data: {
      overview: null, accounts: null, account: null,
      conversations: null, conversation: null, messages: null,
      deliveries: null, diagnostics: null
    },
    filters: {
      accountsHost: "", accountsPresence: "",
      conversationsAccount: "", conversationsUnread: false,
      deliveriesState: "", deliveriesAccount: ""
    },
    cursors: { accounts: "", conversations: "", deliveries: "" },
    selection: { accountId: null, conversationId: null },
    seq: {
      overview: 0, accounts: 0, account: 0, conversations: 0,
      conversation: 0, messages: 0, deliveries: 0, diagnostics: 0
    },
    timer: null,
    autoSeconds: 0
  };

  function nextSeq(key) {
    state.seq[key] += 1;
    return state.seq[key];
  }

  function isCurrent(key, value) {
    return state.seq[key] === value;
  }

  // ---------------------------------------------------------------- alerts

  function showAlert(kind, title, detail) {
    var region = document.getElementById("alert-region");
    clear(region);
    region.hidden = false;
    region.className = "alert-region is-" + kind;
    region.appendChild(el("span", "alert-title", title));
    if (detail) region.appendChild(el("span", null, detail));
  }

  function clearAlert() {
    var region = document.getElementById("alert-region");
    clear(region);
    region.hidden = true;
  }

  function setLinkState(kind, label) {
    var node = document.getElementById("link-state");
    node.className = "chip chip-" + kind;
    clear(node);
    var glyph = kind === "ok" ? GLYPHS.ok : kind === "bad" ? GLYPHS.bad : GLYPHS.warn;
    node.appendChild(el("span", "glyph", glyph));
    node.appendChild(el("span", null, label));
  }

  function noteSuccess() {
    state.lastSuccessAt = new Date();
    state.lastError = null;
    document.getElementById("last-success").textContent =
      formatTime(state.lastSuccessAt.toISOString());
    setLinkState("ok", "正常");
    clearAlert();
  }

  function noteFailure(error) {
    state.lastError = error;
    setLinkState("bad", error && error.status === 401 ? "未授权" : "异常");
    var stale = state.lastSuccessAt
      ? "页面显示的是旧数据（最后成功刷新：" +
        formatTime(state.lastSuccessAt.toISOString()) + "）。"
      : "尚未成功加载过任何数据。";
    if (error && error.status === 401) {
      showAlert("error", "鉴权失败：", error.message + " " + stale);
    } else if (error && error.status === 503) {
      showAlert("warn", "数据库忙：", error.message + " " + stale);
    } else if (error && error.status === 404) {
      showAlert("warn", "资源消失：", error.message + " " + stale);
    } else {
      showAlert("error", "请求失败：", (error ? error.message : "未知错误") + " " + stale);
    }
  }

  // --------------------------------------------------------------- loaders

  function loadOverview() {
    var seq = nextSeq("overview");
    return apiGet("/api/overview").then(function (payload) {
      if (!isCurrent("overview", seq)) return;
      state.data.overview = payload.data;
      noteSuccess();
      renderOverview();
    });
  }

  function loadAccounts() {
    var seq = nextSeq("accounts");
    var params = ["page_size=" + PAGE_SIZE];
    if (state.filters.accountsHost) {
      params.push("host_type=" + encodeURIComponent(state.filters.accountsHost));
    }
    if (state.filters.accountsPresence) {
      params.push("presence=" + encodeURIComponent(state.filters.accountsPresence));
    }
    if (state.cursors.accounts) {
      params.push("cursor=" + encodeURIComponent(state.cursors.accounts));
    }
    return apiGet("/api/accounts?" + params.join("&")).then(function (payload) {
      if (!isCurrent("accounts", seq)) return;
      state.data.accounts = { items: payload.data, meta: payload.meta };
      noteSuccess();
      renderAccounts();
    });
  }

  function loadAccount(accountId) {
    var seq = nextSeq("account");
    state.selection.accountId = accountId;
    return apiGet("/api/accounts/" + encodeURIComponent(accountId)).then(function (payload) {
      if (!isCurrent("account", seq)) return;
      state.data.account = payload.data;
      noteSuccess();
      renderAccounts();
    });
  }

  function loadConversations() {
    var seq = nextSeq("conversations");
    var params = ["page_size=" + PAGE_SIZE];
    if (state.filters.conversationsAccount) {
      params.push("account_id=" + encodeURIComponent(state.filters.conversationsAccount));
    }
    if (state.filters.conversationsUnread) params.push("unread_only=true");
    if (state.cursors.conversations) {
      params.push("cursor=" + encodeURIComponent(state.cursors.conversations));
    }
    return apiGet("/api/conversations?" + params.join("&")).then(function (payload) {
      if (!isCurrent("conversations", seq)) return;
      state.data.conversations = { items: payload.data, meta: payload.meta };
      noteSuccess();
      renderConversations();
    });
  }

  function loadConversation(conversationId, cursor) {
    var seq = nextSeq("conversation");
    var msgSeq = nextSeq("messages");
    state.selection.conversationId = conversationId;
    var path = "/api/conversations/" + encodeURIComponent(conversationId) +
      "/messages?page_size=" + PAGE_SIZE;
    if (cursor) path += "&after_message_id=" + encodeURIComponent(cursor);
    return Promise.all([
      apiGet("/api/conversations/" + encodeURIComponent(conversationId)),
      apiGet(path)
    ]).then(function (results) {
      if (!isCurrent("conversation", seq) || !isCurrent("messages", msgSeq)) return;
      var page = results[1];
      if (cursor && state.data.messages) {
        state.data.messages = {
          items: state.data.messages.items.concat(page.data),
          meta: page.meta
        };
      } else {
        state.data.messages = { items: page.data, meta: page.meta };
      }
      state.data.conversation = results[0].data;
      noteSuccess();
      renderConversations();
    });
  }

  function loadDeliveries() {
    var seq = nextSeq("deliveries");
    var params = ["page_size=" + PAGE_SIZE];
    if (state.filters.deliveriesState) {
      params.push("state=" + encodeURIComponent(state.filters.deliveriesState));
    }
    if (state.filters.deliveriesAccount) {
      params.push("account_id=" + encodeURIComponent(state.filters.deliveriesAccount));
    }
    if (state.cursors.deliveries) {
      params.push("cursor=" + encodeURIComponent(state.cursors.deliveries));
    }
    return apiGet("/api/deliveries?" + params.join("&")).then(function (payload) {
      if (!isCurrent("deliveries", seq)) return;
      state.data.deliveries = { items: payload.data, meta: payload.meta };
      noteSuccess();
      renderDeliveries();
    });
  }

  function loadDiagnostics() {
    var seq = nextSeq("diagnostics");
    return apiGet("/api/diagnostics").then(function (payload) {
      if (!isCurrent("diagnostics", seq)) return;
      state.data.diagnostics = payload.data;
      noteSuccess();
      renderDiagnostics();
    });
  }

  // --------------------------------------------------------------- renders

  function card(title, body, note) {
    var node = el("section", "card");
    if (title) node.appendChild(el("h2", null, title));
    if (note) node.appendChild(el("p", "card-note", note));
    node.appendChild(body);
    return node;
  }

  function metricCard(label, value, sub, tone) {
    var node = el("div", "metric" + (tone ? " is-" + tone : ""));
    node.appendChild(el("span", "metric-label", label));
    node.appendChild(el("span", "metric-value", value));
    if (sub) node.appendChild(el("span", "metric-sub", sub));
    return node;
  }

  function renderOverview() {
    var panel = document.getElementById("panel-overview");
    clear(panel);
    var data = state.data.overview;
    if (!data) {
      panel.appendChild(stateLine("正在加载总览…", null, true));
      return;
    }

    var counts = data.counts;
    var grid = el("div", "grid-cards");
    grid.appendChild(metricCard("账号总数", counts.accounts,
      "在线 " + counts.accounts_online + " 个", null));
    grid.appendChild(metricCard("未读消息", counts.unread_messages,
      "对话 " + counts.conversations + " 个", counts.unread_messages > 0 ? "warn" : "ok"));
    grid.appendChild(metricCard("消息总数", counts.messages, null, null));
    grid.appendChild(metricCard("当前可注入开工的账号", counts.can_wake_now,
      "在线且有注入通道（或目标自己声明 Level 2）",
      counts.can_wake_now > 0 ? "ok" : "warn"));
    panel.appendChild(card("总览计数", grid, "计数来自真实聚合查询，不是当前页长度。"));

    var backlog = el("div", "grid-cards");
    backlog.appendChild(metricCard("queued", data.deliveries.queued,
      "已入队未派发", data.deliveries.queued > 0 ? "warn" : "ok"));
    backlog.appendChild(metricCard("dispatched", data.deliveries.dispatched, "等待宿主确认", null));
    backlog.appendChild(metricCard("delivered", data.deliveries.delivered,
      "宿主已接收 ≠ 任务完成", "ok"));
    backlog.appendChild(metricCard("failed", data.deliveries.failed,
      "仍会重试", data.deliveries.failed > 0 ? "warn" : "ok"));
    backlog.appendChild(metricCard("dead_letter", data.deliveries.dead_letter,
      "等待人工处理", data.deliveries.dead_letter > 0 ? "bad" : "ok"));
    panel.appendChild(card("投递积压与失败", backlog,
      "delivery 只是传输状态；模型是否处理完成请看 processing。"));

    var presenceBox = el("div", "grid-cards");
    ["connected", "offline"].forEach(function (key) {
      var tone = key === "connected" ? "ok" : "bad";
      presenceBox.appendChild(metricCard(key, data.presence[key], null, tone));
    });
    panel.appendChild(card("在线状态分布", presenceBox,
      "在线只看托管该会话的进程是否活着：进程活着 = 在线，进程没了 / 没有进程信息 = 离线。" +
      "租约、心跳、能力等级都不参与这个判定。"));

    var capability = data.capability;
    var capGrid = el("div", "grid-cards");
    [0, 1, 2].forEach(function (level) {
      var slug = level === 0 ? "tools-only" : level === 1 ? "notify" : "wake";
      var count = capability.declared_levels[String(level)] || 0;
      capGrid.appendChild(metricCard("Level " + level + " · " + slug, count, null,
        level === 2 ? "warn" : null));
    });
    capGrid.appendChild(metricCard("已声明 Level 2 的宿主",
      capability.hosts_declaring_level2.length,
      capability.hosts_declaring_level2.join("、") || "无",
      capability.hosts_declaring_level2.length ? "warn" : null));
    capGrid.appendChild(metricCard("真实验证过的宿主", capability.hosts_verified.length,
      capability.hosts_verified.join("、") || "无",
      capability.hosts_verified.length ? "ok" : "bad"));
    panel.appendChild(card("宿主能力与验证状态", capGrid, capability.verified_note));

    var timeline = el("ul", "timeline");
    if (!data.timeline.length) {
      timeline.appendChild(el("li", null, "暂无事件。"));
    } else {
      data.timeline.forEach(function (item) {
        var li = el("li");
        li.appendChild(el("span", "evt", item.event_type));
        var ids = [];
        if (item.account_id) ids.push("acc=" + shortId(item.account_id));
        if (item.conversation_id) ids.push("conv=" + shortId(item.conversation_id));
        if (item.message_id) ids.push("msg=" + shortId(item.message_id));
        if (item.delivery_id) ids.push("del=" + shortId(item.delivery_id));
        li.appendChild(el("span", "ids", ids.join(" ") || "—"));
        li.appendChild(el("span", "when", formatTime(item.created_at)));
        timeline.appendChild(li);
      });
    }
    panel.appendChild(card("最近活动时间线", timeline, data.timeline_note));
  }

  function pager(page, cursorKey, reload, label) {
    var box = el("div", "pager");
    box.appendChild(el("span", null,
      label + "：共 " + page.meta.total + " 条，本页 " + page.items.length + " 条" +
      (page.meta.has_more ? "，还有更多" : "，已到末尾")));
    if (page.meta.has_more && page.meta.next_cursor) {
      box.appendChild(button("加载下一页", function () {
        state.cursors[cursorKey] = page.meta.next_cursor;
        reload();
      }));
    }
    if (state.cursors[cursorKey]) {
      box.appendChild(button("回到第一页", function () {
        state.cursors[cursorKey] = "";
        reload();
      }));
    }
    return box;
  }

  function renderAccounts() {
    var panel = document.getElementById("panel-accounts");
    clear(panel);

    var filters = el("div", "toolbar");
    var hostInput = el("input");
    hostInput.type = "search";
    hostInput.placeholder = "按宿主类型过滤（dsh / codex …）";
    hostInput.value = state.filters.accountsHost;
    hostInput.setAttribute("aria-label", "按宿主类型过滤账号");
    var hostTimer = null;
    hostInput.addEventListener("input", function () {
      if (hostTimer) window.clearTimeout(hostTimer);
      hostTimer = window.setTimeout(function () {
        state.filters.accountsHost = hostInput.value.trim();
        state.cursors.accounts = "";
        loadAccounts().catch(noteFailure);
      }, 300);
    });
    filters.appendChild(hostInput);

    var presenceSelect = el("select");
    presenceSelect.setAttribute("aria-label", "按在线状态过滤账号");
    [["", "全部在线状态"], ["connected", "在线"], ["offline", "离线"]].forEach(function (pair) {
      var option = el("option", null, pair[1]);
      option.value = pair[0];
      if (state.filters.accountsPresence === pair[0]) option.selected = true;
      presenceSelect.appendChild(option);
    });
    presenceSelect.addEventListener("change", function () {
      state.filters.accountsPresence = presenceSelect.value;
      state.cursors.accounts = "";
      loadAccounts().catch(noteFailure);
    });
    filters.appendChild(presenceSelect);
    panel.appendChild(card("账号筛选", filters,
      "在线状态只有两态：connected（托管进程活着）/ offline（进程不在）。"));

    var page = state.data.accounts;
    if (!page) {
      panel.appendChild(stateLine("正在加载账号…", null, true));
      return;
    }
    if (!page.items.length) {
      panel.appendChild(emptyLine("没有匹配的账号。空的邮箱表示还没有会话接入。"));
    } else {
      var rows = page.items.map(function (account) {
        var chips = el("span", "toolbar");
        chips.appendChild(presenceChip(account.presence, account.presence_explanation));
        chips.appendChild(chip("L" + account.capability_level + " · " + account.capability_slug,
          account.capability_level >= 2 ? "warn" : "seen", GLYPHS.ok));
        chips.appendChild(wakeChip(account.can_wake, account.wake_basis,
          account.wake_basis_explanation));
        var open = button("详情", function () {
          loadAccount(account.account_id).catch(noteFailure);
        });
        var connection = account.current_connection;
        return [
          { value: account.display_name },
          { value: account.host_type, mono: true },
          { value: account.host_instance_id, mono: true },
          { value: account.native_session_id, mono: true, nowrap: true },
          { value: chips },
          { value: account.address, mono: true },
          {
            value: connection
              ? (connection.host_pid === null || connection.host_pid === undefined
                ? "无进程信息"
                : "PID " + connection.host_pid)
              : "—",
            mono: true,
            nowrap: true
          },
          { value: open }
        ];
      });
      panel.appendChild(table([
        { label: "显示名" }, { label: "宿主" }, { label: "宿主实例" },
        { label: "原生会话 ID" }, { label: "在线状态与注入能力" }, { label: "地址" },
        { label: "托管进程" }, { label: "操作" }
      ], rows, "共 " + page.meta.total + " 个账号，本页 " + page.items.length +
        " 个（每页上限 " + page.meta.page_size_limit + "）"));
    }

    panel.appendChild(pager(page, "accounts", function () {
      loadAccounts().catch(noteFailure);
    }, "账号"));

    if (state.data.account) panel.appendChild(accountDetailCard(state.data.account));
  }

  function accountDetailCard(account) {
    var body = el("div");
    body.appendChild(kvList([
      ["账号 ID", account.account_id, true],
      ["地址", account.address, true],
      ["宿主类型 / 实例", account.host_type + " / " + account.host_instance_id, true],
      ["原生会话 ID", account.native_session_id, true],
      ["工作目录提示", account.workspace_hint, true],
      ["presence", account.presence + " — " + account.presence_explanation],
      ["在线判定依据", "托管进程是否活着（租约/心跳不参与）"],
      ["声明能力等级", "Level " + account.capability_level + " · " + account.capability_slug],
      ["can_wake（能否直接投进去让它开工）", (account.can_wake ? "是" : "否") +
        " — " + (account.wake_basis_explanation || "")],
      ["verified（是否真实验证）", "否 — 本仓库没有经过真实宿主端到端验证的适配器"],
      ["账号是否被停用", account.blocked ? "是" : "否"],
      ["创建 / 更新", formatTime(account.created_at) + " / " + formatTime(account.updated_at)]
    ]));

    var stats = account.stats || {};
    var statGrid = el("div", "grid-cards");
    statGrid.appendChild(metricCard("发出消息", dash(stats.sent)));
    statGrid.appendChild(metricCard("收到消息", dash(stats.received)));
    statGrid.appendChild(metricCard("参与对话", dash(stats.conversations)));
    statGrid.appendChild(metricCard("未读", dash(stats.unread), null,
      stats.unread > 0 ? "warn" : "ok"));
    statGrid.appendChild(metricCard("投递总数", dash(stats.deliveries)));
    statGrid.appendChild(metricCard("死信", dash(stats.dead_letter), null,
      stats.dead_letter > 0 ? "bad" : "ok"));
    body.appendChild(statGrid);

    body.appendChild(el("h3", null, "连接与托管进程证据"));
    var rows = (account.connections || []).map(function (connection) {
      return [
        { value: connection.generation, mono: true },
        { value: connection.state },
        { value: connection.is_current ? "是" : "否" },
        { value: "L" + connection.capability_level + " · " + connection.capability_slug },
        { value: connection.adapter_name, mono: true },
        {
          value: connection.host_pid === null || connection.host_pid === undefined
            ? "无（= 离线）" : "PID " + connection.host_pid,
          mono: true,
          nowrap: true
        },
        { value: formatTime(connection.closed_at), mono: true, nowrap: true }
      ];
    });
    body.appendChild(table([
      { label: "代次" }, { label: "状态" }, { label: "是否当前" }, { label: "能力等级" },
      { label: "适配器" }, { label: "托管进程 PID" }, { label: "关闭时间" }
    ], rows, "连接历史（最多 50 条，按代次倒序）。在线只看托管进程是否活着。"));

    var node = card("账号详情 · " + account.display_name, body,
      "页面只读取这些数据，不会续租连接、注册账号或修改任何状态。");
    node.id = "account-detail";
    return node;
  }

  function renderConversations() {
    var panel = document.getElementById("panel-conversations");
    clear(panel);

    var filters = el("div", "toolbar");
    var accountInput = el("input");
    accountInput.type = "search";
    accountInput.placeholder = "按账号 ID 过滤对话";
    accountInput.value = state.filters.conversationsAccount;
    accountInput.setAttribute("aria-label", "按账号 ID 过滤对话");
    var timer = null;
    accountInput.addEventListener("input", function () {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        state.filters.conversationsAccount = accountInput.value.trim();
        state.cursors.conversations = "";
        loadConversations().catch(noteFailure);
      }, 300);
    });
    filters.appendChild(accountInput);

    var unreadLabel = el("label");
    var unreadInput = el("input");
    unreadInput.type = "checkbox";
    unreadInput.checked = state.filters.conversationsUnread;
    unreadInput.addEventListener("change", function () {
      state.filters.conversationsUnread = unreadInput.checked;
      state.cursors.conversations = "";
      loadConversations().catch(noteFailure);
    });
    unreadLabel.appendChild(unreadInput);
    unreadLabel.appendChild(el("span", null, "只看有未读的对话"));
    filters.appendChild(unreadLabel);
    var filterPanel = document.getElementById("panel-conversation-filters");
    clear(filterPanel);
    filterPanel.appendChild(filters);

    var page = state.data.conversations;
    if (!page) {
      panel.appendChild(stateLine("正在加载对话…", null, true));
      return;
    }

    var layout = el("div", "master-detail");
    var master = el("div");
    master.className = "conversation-list";
    master.appendChild(el("h2", "page-title", "对话"));
    if (!page.items.length) {
      master.appendChild(emptyLine("没有匹配的对话。"));
    }
    var list = el("ul", "list-plain");
    page.items.forEach(function (conversation) {
      var li = el("li");
      var selected = conversation.conversation_id === state.selection.conversationId;
      var item = el("button", "conv-item" + (selected ? " is-selected" : ""));
      item.type = "button";
      item.setAttribute("aria-current", selected ? "true" : "false");
      var row1 = el("div", "row1");
      row1.appendChild(el("span", "who", conversationLabel(conversation)));
      row1.appendChild(chip(
        conversation.unread_total > 0 ? "未读 " + conversation.unread_total : "无未读",
        conversation.unread_total > 0 ? "unread" : "seen",
        conversation.unread_total > 0 ? GLYPHS.unread : GLYPHS.seen
      ));
      item.appendChild(row1);
      item.appendChild(el("div", "conv-preview", conversation.last_content || "暂无消息"));
      item.appendChild(el("div", "row2", formatTime(conversation.last_message_at || conversation.created_at)));
      item.addEventListener("click", function () {
        loadConversation(conversation.conversation_id).catch(noteFailure);
      });
      li.appendChild(item);
      list.appendChild(li);
    });
    master.appendChild(list);
    master.appendChild(pager(page, "conversations", function () {
      loadConversations().catch(noteFailure);
    }, "对话"));
    layout.appendChild(master);

    var detail = el("div", "detail");
    if (!state.data.conversation) {
      detail.appendChild(emptyLine("从左侧选择一个对话查看参与者与消息。"));
    } else {
      detail.appendChild(conversationDetailCard(state.data.conversation, state.data.messages));
    }
    layout.appendChild(detail);
    panel.appendChild(layout);
    renderMessageMonitor();
  }

  function conversationLabel(conversation) {
    var names = conversation.participant_names ||
      (conversation.participants || []).map(function (participant) { return participant.display_name; });
    return names.length ? names.join(" · ") : "对话";
  }

  function conversationDetailCard(conversation, messagesPage) {
    var body = el("div", "message-list");
    if (!messagesPage) {
      body.appendChild(stateLine("正在加载消息…", null, true));
    } else if (!messagesPage.items.length) {
      body.appendChild(emptyLine("这个对话还没有消息。"));
    } else {
      messagesPage.items.forEach(function (message) {
        body.appendChild(messageCard(message, false));
      });
      if (messagesPage.meta && messagesPage.meta.has_more) {
        body.appendChild(button("加载更多消息", function () {
          loadConversation(conversation.conversation_id, messagesPage.meta.next_cursor).catch(noteFailure);
        }));
      }
    }
    return card(conversationLabel(conversation), body, null);
  }

  function renderMessageMonitor() {
    var panel = document.getElementById("panel-message-monitor");
    clear(panel);
    if (!state.data.conversation) {
      panel.appendChild(emptyLine("先在对话页选择一个对话。"));
      return;
    }
    panel.appendChild(conversationMonitorCard(state.data.conversation, state.data.messages));
  }

  function conversationMonitorCard(conversation, messagesPage) {
    var body = el("div");
    body.appendChild(kvList([
      ["对话 ID", conversation.conversation_id, true],
      ["类型", conversation.kind],
      ["消息数", conversation.message_count],
      ["未读合计", conversation.unread_total],
      ["创建时间", formatTime(conversation.created_at)],
      ["最后活动", formatTime(conversation.last_message_at || conversation.created_at)],
      ["是否阻塞", conversation.blocked
        ? "是 — " + dash(conversation.blocked_reason) : "否"],
      ["连续自动往返计数", conversation.auto_turn_count]
    ]));

    var breakdown = conversation.delivery_breakdown || {};
    var breakdownGrid = el("div", "grid-cards");
    ["queued", "dispatched", "delivered", "failed", "dead_letter"].forEach(function (name) {
      var count = breakdown[name] || 0;
      var tone = name === "dead_letter" && count > 0 ? "bad" :
        name === "failed" && count > 0 ? "warn" : "ok";
      breakdownGrid.appendChild(metricCard(name, count, null, tone));
    });
    body.appendChild(breakdownGrid);

    body.appendChild(el("h3", null, "参与者"));
    var participantRows = (conversation.participants || []).map(function (participant) {
      return [
        { value: participant.display_name },
        { value: participant.account_id, mono: true },
        { value: participant.role },
        { value: participant.unread_count },
        { value: formatTime(participant.joined_at), nowrap: true },
        {
          value: shortId(participant.last_read_message_id),
          mono: true,
          title: participant.last_read_message_id
        }
      ];
    });
    body.appendChild(table([
      { label: "显示名" }, { label: "账号 ID" }, { label: "角色" }, { label: "未读" },
      { label: "加入时间" }, { label: "最后已读位点" }
    ], participantRows, null));

    body.appendChild(el("h3", null, "消息（按入队顺序）"));
    if (!messagesPage) {
      body.appendChild(stateLine("正在加载消息…", null, true));
    } else if (!messagesPage.items.length) {
      body.appendChild(emptyLine("这个对话还没有消息。"));
    } else {
      var list = el("div");
      messagesPage.items.forEach(function (message) {
        list.appendChild(messageCard(message, true));
      });
      body.appendChild(list);
      if (messagesPage.meta && messagesPage.meta.has_more) {
        var more = el("div", "pager");
        more.appendChild(el("span", null,
          "已加载 " + messagesPage.items.length + " / " + messagesPage.meta.total + " 条"));
        more.appendChild(button("加载更多消息", function () {
          loadConversation(conversation.conversation_id, messagesPage.meta.next_cursor)
            .catch(noteFailure);
        }));
        body.appendChild(more);
      } else if (messagesPage.meta) {
        body.appendChild(el("p", "note-inline",
          "已到末尾（共 " + messagesPage.meta.total + " 条）。"));
      }
    }

    return card("对话详情", body,
      "三个状态维度彼此独立：delivery（传输）、visibility（可见性）、processing（处理）。" +
      "delivered 只表示宿主已接收，不代表任务完成。");
  }

  function messageCard(message, technical) {
    var node = el("article", "msg");
    node.id = (technical ? "monitor-msg-" : "msg-") + message.message_id;
    var head = el("div", "msg-head");
    head.appendChild(el("span", "from",
      dash(message.sender_display_name) + " → " + dash(message.recipient_display_name)));
    if (technical) head.appendChild(el("span", "mono", shortId(message.message_id)));
    if (message.reply_to) {
      var link = el("button", "reply-link", technical ? "回复 " + shortId(message.reply_to) : "查看引用");
      link.type = "button";
      link.addEventListener("click", function () {
        var target = document.getElementById((technical ? "monitor-msg-" : "msg-") + message.reply_to);
        if (target) {
          target.scrollIntoView({ block: "center", behavior: "smooth" });
          target.classList.add("is-target");
          window.setTimeout(function () { target.classList.remove("is-target"); }, 2400);
        } else {
          showAlert("info", "被回复的消息不在本页：",
            "它可能在更早的一页里，请先加载更多消息。");
        }
      });
      head.appendChild(link);
    }
    head.appendChild(el("span", "when", formatTime(message.created_at)));
    node.appendChild(head);

    var bodyText = message.content || "";
    if (technical && looksLikeMarkup(bodyText)) {
      node.appendChild(el("p", "note-inline warn-text",
        "正文包含 HTML/标记字符，已按纯文本安全展示（不会被解析或执行）。"));
    }
    node.appendChild(el("p", "msg-body", bodyText));

    if (!technical) return node;

    var states = el("div", "msg-states");
    states.appendChild(stateGroup("delivery", message.delivery, message.delivery_explanation));
    states.appendChild(stateGroup("visibility", message.visibility, message.visibility_explanation));
    states.appendChild(stateGroup("processing", message.processing, message.processing_explanation));
    if (message.processing_result) {
      states.appendChild(el("span", "state-group", "结果：" + message.processing_result));
    }
    if (message.auto_generated) states.appendChild(chip("自动唤醒产生", "warn", GLYPHS.warn));
    node.appendChild(states);
    return node;
  }

  function stateGroup(label, value, explanation) {
    var group = el("span", "state-group");
    group.appendChild(el("span", null, label + "："));
    var tone = value === "delivered" || value === "completed" ? "ok" :
      value === "failed" || value === "dead_letter" || value === "blocked" ? "bad" :
      value === "seen" || value === "cancelled" ? "seen" : "warn";
    var node = chip(dash(value), tone, GLYPHS[value] || GLYPHS.unknown);
    if (explanation) node.title = explanation;
    group.appendChild(node);
    if (explanation) group.appendChild(el("span", null, "（" + explanation + "）"));
    return group;
  }

  function renderDeliveries() {
    var panel = document.getElementById("panel-deliveries");
    clear(panel);

    var filters = el("div", "toolbar");
    var stateSelect = el("select");
    stateSelect.setAttribute("aria-label", "按投递状态过滤");
    [["", "全部状态"], ["queued", "queued"], ["dispatched", "dispatched"],
      ["delivered", "delivered"], ["failed", "failed"], ["dead_letter", "dead_letter"]]
      .forEach(function (pair) {
        var option = el("option", null, pair[1]);
        option.value = pair[0];
        if (state.filters.deliveriesState === pair[0]) option.selected = true;
        stateSelect.appendChild(option);
      });
    stateSelect.addEventListener("change", function () {
      state.filters.deliveriesState = stateSelect.value;
      state.cursors.deliveries = "";
      loadDeliveries().catch(noteFailure);
    });
    filters.appendChild(stateSelect);

    var accountInput = el("input");
    accountInput.type = "search";
    accountInput.placeholder = "按目标账号 ID 过滤";
    accountInput.value = state.filters.deliveriesAccount;
    accountInput.setAttribute("aria-label", "按目标账号 ID 过滤投递");
    var timer = null;
    accountInput.addEventListener("input", function () {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        state.filters.deliveriesAccount = accountInput.value.trim();
        state.cursors.deliveries = "";
        loadDeliveries().catch(noteFailure);
      }, 300);
    });
    filters.appendChild(accountInput);
    panel.appendChild(card("投递筛选", filters,
      "queued 表示目标离线时仍在队列里等待补投；failed 会按指数退避重试；" +
      "dead_letter 需要人工处理。"));

    var page = state.data.deliveries;
    if (!page) {
      panel.appendChild(stateLine("正在加载投递…", null, true));
      return;
    }
    if (!page.items.length) {
      panel.appendChild(emptyLine("没有匹配的投递记录。"));
    } else {
      var rows = page.items.map(function (delivery) {
        var tone = delivery.state === "delivered" ? "delivered" :
          delivery.state === "dead_letter" ? "dead_letter" :
          delivery.state === "failed" ? "failed" :
          delivery.state === "dispatched" ? "dispatched" : "queued";
        var stateChip = chip(delivery.state, tone, GLYPHS[delivery.state] || GLYPHS.unknown);
        stateChip.title = delivery.state_explanation;
        var reason = el("span");
        if (delivery.last_error) {
          var fold = el("details", "fold");
          fold.appendChild(el("summary", null, "失败原因"));
          fold.appendChild(el("p", "msg-body", delivery.last_error));
          reason.appendChild(fold);
        } else {
          reason.textContent = "—";
        }
        return [
          { value: delivery.delivery_id, mono: true, nowrap: true },
          { value: shortId(delivery.message_id), mono: true, title: delivery.message_id },
          { value: shortId(delivery.account_id), mono: true, title: delivery.account_id },
          { value: stateChip, title: delivery.state_explanation },
          { value: String(delivery.attempt) },
          { value: formatTime(delivery.next_attempt_at), nowrap: true },
          { value: formatTime(delivery.updated_at), nowrap: true },
          { value: reason }
        ];
      });
      panel.appendChild(table([
        { label: "投递 ID" }, { label: "消息" }, { label: "目标账号" }, { label: "状态" },
        { label: "尝试" }, { label: "下次重试" }, { label: "更新时间" }, { label: "最近失败" }
      ], rows, "共 " + page.meta.total + " 条投递，本页 " + page.items.length +
        " 条（每页上限 " + page.meta.page_size_limit + "）"));
    }
    panel.appendChild(pager(page, "deliveries", function () {
      loadDeliveries().catch(noteFailure);
    }, "投递"));
  }

  function renderDiagnostics() {
    var panel = document.getElementById("panel-diagnostics");
    clear(panel);
    var data = state.data.diagnostics;
    if (!data) {
      panel.appendChild(stateLine("正在加载诊断…", null, true));
      return;
    }

    var db = data.database;
    var dbBody = el("div");
    dbBody.appendChild(kvList([
      ["数据库（仅文件名）", db.path_display, true],
      ["文件大小", db.file_size_bytes + " 字节"],
      ["完整性检查", db.integrity_check],
      ["日志模式", db.journal_mode],
      ["schema 版本", db.schema_version],
      ["只读连接", "mode=ro + query_only=ON（任何写语句会被 SQLite 拒绝）"]
    ]));
    if (db.integrity_error) {
      dbBody.appendChild(el("p", "note-inline bad-text",
        "完整性检查读取失败：" + db.integrity_error));
    }
    panel.appendChild(card("数据库", dbBody,
      "出于安全考虑，这里只显示数据库文件名，不显示绝对路径。"));

    var tableNames = Object.keys(db.row_counts || {});
    var countRows = tableNames.map(function (name) {
      return [{ value: name, mono: true }, { value: String(db.row_counts[name]), mono: true }];
    });
    panel.appendChild(card("表与行数",
      table([{ label: "表" }, { label: "行数" }], countRows, "行数来自只读聚合查询"), null));

    var migrationRows = (data.migrations || []).map(function (migration) {
      return [
        { value: migration.version, mono: true },
        { value: migration.name },
        { value: formatTime(migration.applied_at), nowrap: true }
      ];
    });
    panel.appendChild(card("迁移",
      table([{ label: "版本" }, { label: "名称" }, { label: "应用时间" }], migrationRows,
        "共 " + migrationRows.length + " 个迁移。监控台不会执行迁移。"),
      "迁移只能通过 migrate 命令显式运行。"));

    var broker = data.broker;
    var brokerBody = el("div");
    brokerBody.appendChild(kvList([
      ["权威状态", broker.authority],
      ["Broker 仅当前进程实例", broker.in_process_instance ? "是" : "否"],
      ["跨进程事件通道", broker.cross_process_event_channel ? "已实现" : "未实现"]
    ]));
    brokerBody.appendChild(el("p", "note-inline warn-text", broker.cross_process_note));
    brokerBody.appendChild(el("p", "note-inline", broker.maintenance_loop));
    panel.appendChild(card("Broker", brokerBody, null));

    var capability = data.capability;
    var capBody = el("div");
    capBody.appendChild(el("p", "card-note", capability.note));
    var hostRows = capability.adapters.map(function (adapter) {
      var verified = chip(adapter.verified ? "verified" : "verified=false",
        adapter.verified ? "ok" : "bad", adapter.verified ? GLYPHS.ok : GLYPHS.bad);
      var wake = chip(adapter.can_wake ? "can_wake" : "不可唤醒",
        adapter.can_wake ? "warn" : "bad", adapter.can_wake ? GLYPHS.warn : GLYPHS.bad);
      var evidence = el("details", "fold");
      evidence.appendChild(el("summary", null, "证据与限制"));
      var list = el("ul", "list-plain");
      (adapter.evidence || []).forEach(function (line) {
        list.appendChild(el("li", "note-inline", line));
      });
      if ((adapter.not_verified || []).length) {
        list.appendChild(el("li", "note-inline bad-text",
          "未验证：" + adapter.not_verified.join("；")));
      }
      (adapter.forbidden || []).forEach(function (line) {
        list.appendChild(el("li", "note-inline warn-text", "禁止：" + line));
      });
      evidence.appendChild(list);
      return [
        { value: adapter.host_type, mono: true },
        { value: "Level " + adapter.level + " · " + adapter.level_slug },
        { value: wake },
        { value: verified },
        { value: evidence }
      ];
    });
    capBody.appendChild(table([
      { label: "宿主" }, { label: "声明等级" }, { label: "can_wake" }, { label: "verified" },
      { label: "证据" }
    ], hostRows, "声明等级来自适配器代码；verified 表示是否已在真实宿主上端到端验证。"));
    capBody.appendChild(el("p", "note-inline warn-text",
      "capability_level=2 只是适配器的声明；verified=false 时不得理解为真实可唤醒。"));
    panel.appendChild(card("宿主适配器能力与验证状态", capBody, null));

    var settings = data.settings;
    var settingsBody = el("div");
    settingsBody.appendChild(kvList([
      ["心跳间隔（秒）", settings.heartbeat_seconds + "（仅诊断，不参与在线判定）"],
      ["租约期限（秒）", settings.lease_seconds + "（仅诊断，不参与在线判定）"],
      ["断线宽限（秒）", settings.grace_seconds + "（仅诊断，不参与在线判定）"],
      ["单对话自动往返上限", settings.max_auto_turns],
      ["每小时自动唤醒上限", settings.max_auto_wakes_per_hour],
      ["重复内容阈值", settings.repeated_content_limit],
      ["单账号发送速率（/分钟）", settings.max_sends_per_minute],
      ["单条消息长度上限", settings.max_message_chars],
      ["投递最大尝试次数", settings.max_delivery_attempts],
      ["全局暂停", settings.global_pause ? "已开启" : "关闭"],
      ["legacy 工具", settings.legacy_tools_enabled ? "已开启" : "关闭"],
      ["记录消息正文到日志", settings.log_message_content ? "已开启（有泄露风险）" : "关闭"]
    ]));
    panel.appendChild(card("当前生效配置（非敏感项）", settingsBody, null));

    var notes = el("ul", "list-plain");
    (data.honesty_notes || []).forEach(function (line) {
      notes.appendChild(el("li", "note-inline", line));
    });
    panel.appendChild(card("如实声明", notes, null));
  }

  // --------------------------------------------------------------- refresh

  function refreshActive() {
    if (state.area === "conversations") {
      var tasks = [loadConversations()];
      if (state.selection.conversationId) tasks.push(loadConversation(state.selection.conversationId));
      return Promise.all(tasks);
    }
    var requests = [];
    Array.prototype.forEach.call(document.querySelectorAll("details[data-monitor]"), function (fold) {
      if (fold.open) requests.push(loadMonitorPart(fold.getAttribute("data-monitor")));
    });
    return Promise.all(requests);
  }

  function loadMonitorPart(part) {
    if (part === "overview") return loadOverview();
    if (part === "accounts") return loadAccounts();
    if (part === "deliveries") return loadDeliveries();
    if (part === "diagnostics") return loadDiagnostics();
    if (part === "conversation" && state.selection.conversationId) {
      return loadConversation(state.selection.conversationId);
    }
    renderMessageMonitor();
    return Promise.resolve();
  }

  function refreshAll() {
    setLinkState("warn", "刷新中");
    return refreshActive().then(function () { noteSuccess(); }).catch(function (err) {
      noteFailure(err);
    });
  }

  function setAutoRefresh(seconds) {
    if (state.timer) {
      window.clearInterval(state.timer);
      state.timer = null;
    }
    state.autoSeconds = seconds;
    if (seconds > 0) {
      state.timer = window.setInterval(function () {
        refreshAll();
      }, seconds * 1000);
    }
  }

  // ------------------------------------------------------------------- nav

  function selectArea(area) {
    state.area = area;
    var buttons = document.querySelectorAll(".nav-btn");
    Array.prototype.forEach.call(buttons, function (node) {
      var active = node.getAttribute("data-area") === area;
      node.classList.toggle("is-active", active);
      node.setAttribute("aria-selected", active ? "true" : "false");
    });
    var panels = document.querySelectorAll("main > section[data-area]");
    Array.prototype.forEach.call(panels, function (node) {
      node.hidden = node.getAttribute("data-area") !== area;
    });

    if (area === "conversations") {
      loadConversations().catch(noteFailure);
    } else {
      renderMessageMonitor();
    }
  }

  function wireKeyboard() {
    var buttons = document.querySelectorAll(".nav-btn");
    Array.prototype.forEach.call(buttons, function (node, index) {
      node.addEventListener("keydown", function (event) {
        var keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"];
        if (keys.indexOf(event.key) < 0) return;
        event.preventDefault();
        var next = index;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") {
          next = (index + 1) % buttons.length;
        }
        if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
          next = (index - 1 + buttons.length) % buttons.length;
        }
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = buttons.length - 1;
        buttons[next].focus();
        selectArea(buttons[next].getAttribute("data-area"));
      });
    });
  }

  // ------------------------------------------------------------------ boot

  function boot() {
    var authorized = initToken();

    Array.prototype.forEach.call(document.querySelectorAll(".nav-btn"), function (node) {
      node.addEventListener("click", function () {
        selectArea(node.getAttribute("data-area"));
      });
    });
    wireKeyboard();

    Array.prototype.forEach.call(document.querySelectorAll("details[data-monitor]"), function (fold) {
      fold.addEventListener("toggle", function () {
        if (fold.open) loadMonitorPart(fold.getAttribute("data-monitor")).catch(noteFailure);
      });
    });

    document.getElementById("refresh").addEventListener("click", function () {
      refreshAll();
    });

    var autoSelect = document.getElementById("auto-refresh");
    autoSelect.addEventListener("change", function () {
      setAutoRefresh(parseInt(autoSelect.value, 10) || 0);
    });

    if (!authorized) {
      setLinkState("bad", "未授权");
      showAlert("error", "缺少访问令牌：", AUTH_HELP);
      document.getElementById("panel-conversations").appendChild(
        stateLine("请使用启动监控台时打印的带 token 的 URL 打开本页面。", "error")
      );
      return;
    }

    setLinkState("warn", "加载中");
    loadConversations()
      .then(function () { setLinkState("ok", "正常"); })
      .catch(noteFailure);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
