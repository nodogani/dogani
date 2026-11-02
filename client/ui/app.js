/*
 * 오버레이 UI 로직
 * - 서버의 SSE(/api/events)를 구독해서 노트가 바뀌면 즉시 다시 그린다 (읽기 전용)
 * - 창 이동/크기/펼침 등 네이티브 동작은 QWebChannel 의 bridge 객체로 파이썬에 요청
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const WD = ["일", "월", "화", "수", "목", "금", "토"];
  const DEFAULT_PORT = "8765";

  const pad = (n) => String(n).padStart(2, "0");
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parseISO = (s) => {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  };
  const addDays = (s, n) => {
    const d = parseISO(s);
    d.setDate(d.getDate() + n);
    return iso(d);
  };
  const addMonths = (ym, n) => {
    const [y, m] = ym.split("-").map(Number);
    const d = new Date(y, m - 1 + n, 1);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
  };
  const dayLabel = (s) => {
    const d = parseISO(s);
    return `${String(d.getFullYear()).slice(2)}${pad(d.getMonth() + 1)}${pad(d.getDate())} ${WD[d.getDay()]}`;
  };

  const state = {
    server: "",
    base: "",
    view: null,        // null | "note" | "cal"
    today: iso(new Date()),
    noteDate: iso(new Date()),
    calYM: iso(new Date()).slice(0, 7),
    status: "offline", // online | offline | paused | blocked
    shownDate: null,   // 실제로 표시 중인 노트 날짜 (오늘 노트가 없으면 최근 노트)
    es: null,
    retryTimer: null,
  };
  let bridge = null;

  // ------------------------------------------------------------ server
  async function api(path) {
    const r = await fetch(state.base + path, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(body.error || "HTTP " + r.status);
      err.code = body.error || "http";
      throw err;
    }
    return body;
  }

  const STATUS_TEXT = {
    offline: "서버에 연결할 수 없습니다.",
    paused: "서버에서 공유를 일시 중지했습니다.",
    blocked: "서버에서 접속이 차단되었습니다.",
    client_only: "서버 접속 키가 맞지 않습니다.",
  };

  function setStatus(status) {
    if (state.status === status) return;
    state.status = status;
    const dot = $("status-dot");
    for (const s of ["online", "paused", "blocked"]) dot.classList.toggle(s, s === status);
    if (bridge) bridge.setStatus(status);
  }

  function errorText(e) {
    return STATUS_TEXT[e.code] || STATUS_TEXT[state.status] || STATUS_TEXT.offline;
  }

  function pulse() {
    const dot = $("status-dot");
    dot.classList.remove("pulse");
    void dot.offsetWidth;
    dot.classList.add("pulse");
  }

  function connect() {
    clearTimeout(state.retryTimer);
    if (state.es) state.es.close();
    state.es = null;
    setStatus("offline");
    if (!state.server) return;

    state.base = "http://" + state.server;
    const es = new EventSource(state.base + "/api/events");
    const applyPaused = (e) => {
      setStatus(JSON.parse(e.data).paused ? "paused" : "online");
      refresh();
    };
    es.addEventListener("hello", applyPaused);
    es.addEventListener("status", applyPaused);
    es.addEventListener("changed", (e) => {
      const dates = JSON.parse(e.data).dates || [];
      pulse();
      if (state.view === "note" && (dates.includes(state.noteDate) || dates.includes(state.shownDate))) loadNote(true);
      if (state.view === "cal" && dates.some((d) => d.startsWith(state.calYM))) loadMonth();
    });
    es.addEventListener("reload", () => refresh());
    es.onerror = () => {
      if (state.es !== es) return;
      // 브라우저가 자동 재연결을 포기한 경우(CLOSED: 403 등)에는 이유를 확인하고 직접 재시도
      if (es.readyState === EventSource.CLOSED) {
        api("/api/info")
          .then(() => setStatus("offline"))
          .catch((e) => setStatus(e.code === "blocked" ? "blocked" : "offline"))
          .finally(() => { refresh(); state.retryTimer = setTimeout(connect, 5000); });
      } else {
        setStatus("offline");
      }
    };
    state.es = es;
  }

  function refresh() {
    if (state.view === "note") loadNote(true);
    else if (state.view === "cal") loadMonth();
  }

  // ------------------------------------------------------------ note view
  async function loadNote(keepScroll) {
    const date = state.noteDate;
    const body = $("note-body");
    $("note-title").textContent = dayLabel(date);
    $("note-title").classList.toggle("is-today", date === state.today);
    let note;
    try {
      // 오늘 노트가 아직 없으면 가장 최근 노트를 대신 보여준다
      note = await api(`/api/note?date=${date}${date === state.today ? "&fallback=1" : ""}`);
    } catch (e) {
      if (date !== state.noteDate) return;
      body.innerHTML = `<div class="empty">${errorText(e)}<br>${state.server || "(서버 미지정)"}</div>`;
      $("note-progress").hidden = true;
      $("note-notice").hidden = true;
      $("note-footer").textContent = "";
      return;
    }
    if (date !== state.noteDate) return;

    state.shownDate = note.date;
    $("note-notice").hidden = !note.fallback_for;
    $("note-notice").textContent = note.fallback_for ? "오늘 노트가 아직 없어 가장 최근 노트를 표시합니다." : "";
    const scroll = keepScroll ? body.scrollTop : 0;
    if (note.exists) {
      $("note-title").textContent = note.title;
      body.innerHTML = ObsidianMarkdown.render(note.content);
      setProgress(note.done, note.total);
      const t = new Date(note.mtime);
      $("note-footer").textContent =
        `마지막 수정 ${t.getMonth() + 1}/${t.getDate()} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
    } else {
      body.innerHTML = `<div class="empty">이 날짜의 노트가 없습니다.</div>`;
      $("note-progress").hidden = true;
      $("note-footer").textContent = "";
    }
    body.scrollTop = scroll;
  }

  function setProgress(done, total) {
    $("note-progress").hidden = total === 0;
    $("note-count").textContent = `${done}/${total}`;
    const bar = $("note-bar");
    bar.style.width = total ? `${(done / total) * 100}%` : "0";
    bar.classList.toggle("done", total > 0 && done === total);
  }

  function showNote(date) {
    state.noteDate = date;
    setView("note");
    loadNote(false);
  }

  // ------------------------------------------------------------ calendar view
  async function loadMonth() {
    const ym = state.calYM;
    const [y, m] = ym.split("-").map(Number);
    $("cal-title").textContent = `${y}년 ${m}월`;
    let days = {};
    let failed = false;
    try {
      days = (await api(`/api/month?ym=${ym}`)).days;
    } catch (e) {
      failed = errorText(e);
    }
    if (ym !== state.calYM) return;

    const grid = $("cal-grid");
    let html = WD.map((w, k) => `<div class="wd${k === 0 ? " sun" : k === 6 ? " sat" : ""}">${w}</div>`).join("");
    const first = new Date(y, m - 1, 1);
    const cursor = new Date(y, m - 1, 1 - first.getDay());
    let notes = 0, sumDone = 0, sumTotal = 0;

    for (let k = 0; k < 42; k++) {
      const d = iso(cursor);
      const info = days[d];
      const cls = ["cal-day"];
      if (cursor.getMonth() !== m - 1) cls.push("other");
      if (cursor.getDay() === 0) cls.push("sun");
      if (cursor.getDay() === 6) cls.push("sat");
      if (d === state.today) cls.push("today");
      if (d === state.noteDate) cls.push("selected");
      let count = "";
      if (info) {
        notes++;
        sumDone += info.done;
        sumTotal += info.total;
        const c = info.total === 0 ? "none" : info.done === info.total ? "done" : "";
        count = `<span class="count ${c}">${info.done}/${info.total}</span>`;
      }
      html += `<div class="${cls.join(" ")}" data-date="${d}"><span class="num">${cursor.getDate()}</span>${count}</div>`;
      cursor.setDate(cursor.getDate() + 1);
    }
    grid.innerHTML = html;
    $("cal-footer").textContent = failed
      ? failed
      : `노트 ${notes}개 · 완료 ${sumDone}/${sumTotal}`;
  }

  // ------------------------------------------------------------ view / window
  function setView(view) {
    state.view = view;
    $("panel").hidden = !view;
    $("view-note").hidden = view !== "note";
    $("view-cal").hidden = view !== "cal";
    $("btn-note").classList.toggle("active", view === "note");
    $("btn-cal").classList.toggle("active", view === "cal");
    document.body.classList.toggle("expanded", !!view);
    if (bridge) bridge.setExpanded(!!view);
    if (!view) reportNavSize();
  }

  function reportNavSize() {
    requestAnimationFrame(() => {
      const r = $("nav").getBoundingClientRect();
      if (bridge) bridge.setNavSize(Math.ceil(r.width) + 12, Math.ceil(r.height) + 12);
    });
  }

  function checkDateRollover() {
    const now = iso(new Date());
    if (now === state.today) return;
    const prev = state.today;
    state.today = now;
    // 오늘 노트를 보고 있었다면 자정에 다음 날로 넘어간다
    if (state.noteDate === prev) {
      state.noteDate = now;
      if (state.view === "note") loadNote(false);
    }
    if (state.view === "cal") loadMonth();
  }

  // ------------------------------------------------------------ events
  $("btn-note").addEventListener("click", () => {
    if (state.view === "note") return setView(null);
    showNote(state.view ? state.noteDate : state.today);
  });
  $("btn-cal").addEventListener("click", () => {
    if (state.view === "cal") return setView(null);
    state.calYM = state.noteDate.slice(0, 7);
    setView("cal");
    loadMonth();
  });
  $("note-prev").addEventListener("click", () => showNote(addDays(state.noteDate, -1)));
  $("note-next").addEventListener("click", () => showNote(addDays(state.noteDate, 1)));
  $("note-today").addEventListener("click", () => showNote(state.today));
  $("cal-prev").addEventListener("click", () => { state.calYM = addMonths(state.calYM, -1); loadMonth(); });
  $("cal-next").addEventListener("click", () => { state.calYM = addMonths(state.calYM, 1); loadMonth(); });
  $("cal-today").addEventListener("click", () => { state.calYM = state.today.slice(0, 7); loadMonth(); });
  $("cal-grid").addEventListener("click", (e) => {
    const cell = e.target.closest(".cal-day");
    if (cell) showNote(cell.dataset.date);
  });

  $("nav").addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest("button")) return;
    e.preventDefault();
    if (bridge) bridge.startMove();
  });
  $("resize-grip").addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    if (bridge) bridge.startResize();
  });
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a) return;
    e.preventDefault();
    if (bridge) bridge.openUrl(a.href);
    else window.open(a.href);
  });
  document.addEventListener("contextmenu", (e) => e.preventDefault());
  setInterval(checkDateRollover, 30 * 1000);

  // ------------------------------------------------------------ init
  window.App = {
    /** 파이썬에서 호출: { server: "ip:port", theme: "light" | "dark" } */
    configure(cfg) {
      if (cfg.theme) {
        document.body.classList.toggle("theme-light", cfg.theme !== "dark");
        document.body.classList.toggle("theme-dark", cfg.theme === "dark");
      }
      if (cfg.server !== undefined && cfg.server !== state.server) {
        state.server = cfg.server;
        $("server-label").textContent = cfg.server
          ? cfg.server.replace(new RegExp(":" + DEFAULT_PORT + "$"), "")
          : "서버 미지정";
        $("nav").title = `${cfg.server} · 드래그해서 이동`;
        connect();
      }
      reportNavSize();
    },
  };

  if (window.qt && window.QWebChannel) {
    new QWebChannel(qt.webChannelTransport, (channel) => {
      bridge = channel.objects.bridge;
      bridge.getConfig((json) => App.configure(JSON.parse(json)));
    });
  } else {
    // 일반 브라우저에서 테스트: index.html?server=192.168.0.10:8765&theme=dark
    const q = new URLSearchParams(location.search);
    App.configure({ server: q.get("server") || "", theme: q.get("theme") || "light" });
  }
})();
