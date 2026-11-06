/*
 * 오버레이 UI 로직 (창 하나 = 노트 하나: 내 노트 또는 다른 사람)
 * - 서버의 SSE(/api/events)를 구독해서 노트가 바뀌면 즉시 다시 그린다
 * - 노트 본문은 CodeMirror 라이브 프리뷰 편집기(vendor/editor.js) 하나. 내 노트만 편집 가능, 남의 노트는 읽기 전용
 * - 창 이동/크기/펼침 등 네이티브 동작은 QWebChannel 의 bridge 객체로 파이썬에 요청
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const DEFAULT_PORT = "8765";
  const MD = window.ObsidianMarkdown;
  const CAL = window.DoganiCalendar;
  const { WD, pad, iso, parseISO, addDays, addMonths } = CAL;

  const dayLabel = (s) => {
    const d = parseISO(s);
    return `${String(d.getFullYear()).slice(2)}${pad(d.getMonth() + 1)}${pad(d.getDate())} ${WD[d.getDay()]}`;
  };

  const state = {
    server: "",
    base: "",
    label: "",         // 네비게이션 바 이름 (내 노트 / 상대 PC 이름)
    mine: false,
    mode: "nav",       // 이 창의 역할: nav(네비게이션 바) | note(할일 창) | cal(달력 창)
    view: null,        // null | "note" | "cal"
    today: iso(new Date()),
    noteDate: iso(new Date()),
    calYM: iso(new Date()).slice(0, 7),
    status: "offline", // online | offline | paused | blocked
    es: null,
    retryTimer: null,
    writable: false,   // 서버가 편집을 허용하는지 (같은 PC = 내 노트)
    note: null,        // 현재 노트 응답
    lines: null,       // 현재 노트 원문 줄 배열 (편집 대상)
    eol: "\n",
    units: [],
    active: null,      // 편집 중인 단위 { start, end, block, indent, list }
    editing: false,
    sched: null,       // 일정 색인 (CAL.index) — 달력 표시, 쉬는 날 건너뛰기
  };
  let bridge = null;

  // ------------------------------------------------------------ server
  async function api(path, body) {
    const opts = { cache: "no-store" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(state.base + path, opts);
    const res = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(res.error || "HTTP " + r.status);
      err.code = res.error || "http";
      throw err;
    }
    return res;
  }

  const STATUS_TEXT = {
    offline: "연결할 수 없습니다.",
    paused: "상대방이 공유를 일시 중지했습니다.",
    blocked: "상대방이 접속을 차단했습니다.",
    client_only: "접속 키가 맞지 않습니다. (Dogani 버전 확인)",
    conflict: "그 사이 다른 곳에서 노트가 수정되어 저장하지 않았습니다.",
    read_only: "편집 권한이 없습니다.",
    write_failed: "저장에 실패했습니다.",
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

  function setLabel(text) {
    $("server-label").textContent = text;
    $("nav").title = `${state.server} · 드래그해서 이동`;
    reportNavSize();
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
    es.addEventListener("hello", (e) => {
      applyPaused(e);
      loadSchedule();
      api("/api/info").then((info) => {
        setWritable(!!info.writable);
        if (!state.mine && info.name) {
          setLabel(info.name);
          if (bridge) bridge.setName(info.name);
        }
      }).catch(() => {});
    });
    es.addEventListener("status", applyPaused);
    es.addEventListener("schedule", () => loadSchedule());
    es.addEventListener("changed", (e) => {
      const dates = JSON.parse(e.data).dates || [];
      pulse();
      // 편집 중에는 화면을 덮어쓰지 않는다 (저장 시 서버가 충돌 검사)
      if (state.view === "note" && !state.editing && dates.includes(state.noteDate)) loadNote(true);
      if (state.view === "cal" && dates.some((d) => d.startsWith(state.calYM))) loadMonth();
    });
    es.addEventListener("reload", () => { loadSchedule(); refresh(); });
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
    if (state.view === "note") { if (!state.editing) loadNote(true); }
    else if (state.view === "cal") loadMonth();
  }

  async function loadSchedule() {
    try {
      state.sched = CAL.index((await api("/api/schedule")).events);
    } catch (e) {
      return; // 예전 버전 Dogani(일정 API 없음) 이거나 연결 끊김: 기존 것 유지
    }
    if (state.view === "cal") loadMonth();
    if (state.view === "note") renderSchedRow();
  }

  // ------------------------------------------------------------ note view
  function renderSchedRow() {
    const row = $("note-sched");
    const html = CAL.dayEvents(state.noteDate, state.sched);
    row.innerHTML = html;
    row.hidden = !html;
  }

  /** 노트가 없거나 비었을 때: 직전 노트를 그대로 복사하는 버튼 */
  function copyButton(note) {
    if (!state.writable || !note || !note.prev || note.no_dir) return "";
    return `<button class="copy-prev" data-prev="${note.prev}">어제 노트 복사 <span>(${dayLabel(note.prev)})</span></button>`;
  }

  async function copyPrev(prev) {
    const date = state.noteDate;
    const target = state.note;
    try {
      const src = await api(`/api/note?date=${prev}`);
      if (!src.exists) throw Object.assign(new Error(), { code: "write_failed" });
      await api("/api/note", { date, content: src.content, base_mtime: target && target.exists ? target.mtime : null });
    } catch (e) {
      $("note-footer").textContent = errorText(e);
      return;
    }
    if (state.noteDate === date) loadNote(false);
  }

  function showMessage(html) {
    state.lines = null;
    const body = $("note-body");
    body.classList.remove("has-editor");
    body.innerHTML = `<div class="empty">${html}</div>`;
    $("note-progress").hidden = true;
    $("note-footer").textContent = "";
  }

  async function loadNote(keepScroll) {
    const date = state.noteDate;
    const body = $("note-body");
    $("note-title").textContent = dayLabel(date);
    $("note-title").classList.toggle("is-today", date === state.today);
    renderSchedRow();
    let note;
    try {
      note = await api(`/api/note?date=${date}`);
    } catch (e) {
      if (date !== state.noteDate) return;
      state.note = null;
      showMessage(`${errorText(e)}<br>${state.server || "(주소 미지정)"}`);
      return;
    }
    if (date !== state.noteDate || state.editing) return;

    state.note = note;
    if (!note.exists) {
      if (note.no_dir) {
        showMessage(state.writable
          ? "todo 폴더가 지정되지 않았습니다.<br>트레이 &gt; todo 경로 &gt; 찾아보기..."
          : "상대방이 todo 폴더를 지정하지 않았습니다.");
      } else {
        showMessage(state.writable ? `이 날짜의 노트가 없습니다.<br>클릭해서 작성${copyButton(note)}` : "이 날짜의 노트가 없습니다.");
      }
      return;
    }
    $("note-title").textContent = note.title;
    state.eol = note.content.includes("\r\n") ? "\r\n" : "\n";
    state.lines = note.content.split(/\r\n|\n/);
    showEditor(state.lines.join("\n"), keepScroll);
    if (!note.content.trim() && copyButton(note)) {
      body.insertAdjacentHTML("afterbegin", `<div class="empty">${copyButton(note)}</div>`);
    }
    setProgress(note.done, note.total);
    const t = new Date(note.mtime);
    $("note-footer").textContent =
      `마지막 수정 ${t.getMonth() + 1}/${t.getDate()} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
  }

  // ------------------------------------------------------------ editor (vendor/editor.js)
  let editor = null;
  const editorHost = document.createElement("div");
  editorHost.className = "lp-host";

  /** 노트 본문을 편집기에 표시. 같은 내용이면 그대로 둬서 커서·스크롤·실행 취소 유지 */
  function showEditor(text, keepScroll) {
    const body = $("note-body");
    if (!editor) {
      editor = window.DoganiEditor.create(editorHost, {
        doc: text,
        readOnly: !state.writable,
        tableHtml: MD.tableHtml,
        onChange(doc, immediate) {
          state.lines = doc.split("\n");
          changed();
          if (immediate) save();   // 체크박스 클릭은 바로 저장
        },
        onFocus() { if (state.writable) state.editing = true; },
        onBlur() { if (state.editing) { state.editing = false; save(); } },
        onSave: () => save(),
        openUrl: (url) => (bridge ? bridge.openUrl(url) : window.open(url)),
      });
    } else if (editor.getDoc() !== text) {
      editor.setDoc(text, keepScroll);
    }
    editor.setReadOnly(!state.writable);
    body.classList.add("has-editor");
    body.replaceChildren(editorHost);
  }

  function setProgress(done, total) {
    $("note-progress").hidden = total === 0;
    $("note-count").textContent = `${done}/${total}`;
    const bar = $("note-bar");
    bar.style.width = total ? `${(done / total) * 100}%` : "0";
    bar.classList.toggle("done", total > 0 && done === total);
  }

  function countTasks(lines) {
    let done = 0, total = 0, fence = false;
    for (const line of lines) {
      if (/^\s*(```|~~~)/.test(line)) { fence = !fence; continue; }
      const m = !fence && line.match(/^\s*(?:[-*+]|\d+[.)])\s+\[(.)\]/);
      if (m) { total++; if (m[1] !== " ") done++; }
    }
    return [done, total];
  }

  function showNote(date) {
    exitEdit();
    state.noteDate = date;
    setView("note");
    loadNote(false);
    if (bridge && state.mode === "note") bridge.noteDateChanged(date);
  }

  // ------------------------------------------------------------ edit (내 노트 전용)
  function setWritable(w) {
    state.writable = w;
    document.body.classList.toggle("writable", w);
    if (!w) exitEdit();
    if (editor) editor.setReadOnly(!w);
    if (state.view === "note" && !state.editing) loadNote(true);
  }

  // 저장은 순서대로 하나씩 (자동 저장이 겹치면 mtime 충돌이 나므로)
  let saveChain = Promise.resolve(true);
  let saveTimer = null;

  function save() {
    clearTimeout(saveTimer);
    if (!state.lines) return saveChain;
    const content = state.lines.join(state.eol);
    const date = state.noteDate;
    saveChain = saveChain.then(async () => {
      const target = state.note && state.note.exists && state.note.date === date ? state.note : null;
      if (target && target.content === content) return true;
      try {
        const note = await api("/api/note", { date, content, base_mtime: target ? target.mtime : null });
        if (state.noteDate === date) {
          state.note = note;
          $("note-title").textContent = note.title;
          $("note-footer").textContent = "저장됨";
        }
        return true;
      } catch (e) {
        $("note-footer").textContent = errorText(e);
        return false;
      }
    });
    return saveChain;
  }

  function changed() {
    const [done, total] = countTasks(state.lines);
    setProgress(done, total);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 700);
  }

  function exitEdit() {
    if (editor && editor.hasFocus()) editor.blur();   // onBlur 에서 저장
    else if (state.editing) { state.editing = false; save(); }
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

    const r = CAL.renderMonth({ ym, days, idx: state.sched, today: state.today, selected: state.noteDate });
    $("cal-grid").innerHTML = r.html;
    CAL.fitCells($("cal-grid"));
    $("cal-footer").textContent = failed ? failed : `노트 ${r.notes}개 · 완료 ${r.done}/${r.total}`;
  }

  // ------------------------------------------------------------ view / window
  function setView(view) {
    if (view !== "note") exitEdit();
    state.view = view;
    $("panel").hidden = !view;
    $("view-note").hidden = view !== "note";
    $("view-cal").hidden = view !== "cal";
    $("note-header").hidden = view !== "note";
    $("cal-header").hidden = view !== "cal";
    $("btn-note").classList.toggle("active", view === "note");
    $("btn-cal").classList.toggle("active", view === "cal");
    document.body.classList.toggle("expanded", !!view);
    if (bridge) bridge.setExpanded(!!view);
    if (!view) reportNavSize();
  }

  function reportNavSize() {
    requestAnimationFrame(() => {
      if (state.view) return;
      // 그림자가 잘리지 않도록 #root 여백까지 포함한 크기
      const r = $("nav").getBoundingClientRect();
      const cs = getComputedStyle($("root"));
      const w = r.width + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
      const h = r.height + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
      if (bridge) bridge.setNavSize(Math.ceil(w), Math.ceil(h));
    });
  }

  function checkDateRollover() {
    const now = iso(new Date());
    if (now === state.today) return;
    const prev = state.today;
    state.today = now;
    // 오늘 노트를 보고 있었다면 자정에 다음 날로 넘어간다 (편집 중이면 끝난 뒤에)
    if (state.noteDate === prev && !state.editing) {
      state.noteDate = now;
      if (state.view === "note") loadNote(false);
    }
    if (state.view === "cal") loadMonth();
  }

  /** 네비게이션 바 버튼 불빛 = 할일/달력 창이 열려 있는지 */
  function setOpen(open) {
    $("btn-note").classList.toggle("active", !!open.note);
    $("btn-cal").classList.toggle("active", !!open.cal);
  }

  function showCal() {
    state.calYM = state.noteDate.slice(0, 7);
    setView("cal");
    loadMonth();
  }

  // ------------------------------------------------------------ events
  // 네비게이션 바의 할일/달력 버튼은 각각 따로 창을 열고 닫는다 (브라우저 테스트에서는 한 창 안에서 전환)
  $("btn-note").addEventListener("click", () => {
    if (bridge) return bridge.togglePanel("note");
    if (state.view === "note") return setView(null);
    showNote(state.view ? state.noteDate : state.today);
  });
  $("btn-cal").addEventListener("click", () => {
    if (bridge) return bridge.togglePanel("cal");
    if (state.view === "cal") return setView(null);
    showCal();
  });
  $("btn-close").addEventListener("click", () => {
    exitEdit();
    // 닫기 전에 저장이 끝나도록
    saveChain.then(() => { if (bridge) bridge.closePanel(); });
  });
  // 화살표는 토/일·쉬는 날을 건너뜀 (그 날 노트는 달력에서 눌러서 열기)
  $("note-prev").addEventListener("click", () => showNote(CAL.nextWorkday(state.noteDate, -1, state.sched)));
  $("note-next").addEventListener("click", () => showNote(CAL.nextWorkday(state.noteDate, 1, state.sched)));
  $("note-today").addEventListener("click", () => showNote(state.today));

  $("note-body").addEventListener("click", (e) => {
    const copy = e.target.closest(".copy-prev");
    if (copy) return copyPrev(copy.dataset.prev);
    if (!state.writable || state.status !== "online" || state.lines) return;
    // 노트가 없는 날짜: 빈 노트로 작성 시작 (첫 저장 때 파일 생성)
    if (!state.note || state.note.exists || state.note.no_dir) return;
    state.lines = [""];
    state.eol = "\n";
    showEditor("", false);
    editor.focus(0);
  });
  // 노트 영역 밖(네비게이션 바 등)을 누르거나 창이 포커스를 잃으면 편집 끝
  document.addEventListener("mousedown", (e) => {
    if (state.editing && !e.target.closest("#note-body")) exitEdit();
  });
  window.addEventListener("blur", exitEdit);

  $("cal-prev").addEventListener("click", () => { state.calYM = addMonths(state.calYM, -1); loadMonth(); });
  $("cal-next").addEventListener("click", () => { state.calYM = addMonths(state.calYM, 1); loadMonth(); });
  $("cal-today").addEventListener("click", () => { state.calYM = state.today.slice(0, 7); loadMonth(); });
  $("cal-grid").addEventListener("click", (e) => {
    const cell = e.target.closest(".cal-day");
    if (!cell) return;
    if (bridge && state.mode === "cal") {
      state.noteDate = cell.dataset.date;
      for (const c of $("cal-grid").querySelectorAll(".cal-day.selected")) c.classList.remove("selected");
      cell.classList.add("selected");
      bridge.openNote(cell.dataset.date);
    } else {
      showNote(cell.dataset.date);
    }
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
  let fitTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => { if (state.view === "cal") CAL.fitCells($("cal-grid")); }, 100);
  });

  // ------------------------------------------------------------ init
  window.App = {
    /** 파이썬에서 호출: { server: "ip:port", theme: "light" | "dark", mine: bool, label: "내 노트" } */
    configure(cfg) {
      if (cfg.theme) {
        document.body.classList.toggle("theme-light", cfg.theme !== "dark");
        document.body.classList.toggle("theme-dark", cfg.theme === "dark");
      }
      if (cfg.mine !== undefined) {
        state.mine = !!cfg.mine;
        document.body.classList.toggle("mine", state.mine);
      }
      if (cfg.server !== undefined && cfg.server !== state.server) {
        state.server = cfg.server;
        setLabel(cfg.label || (cfg.server ? cfg.server.replace(new RegExp(":" + DEFAULT_PORT + "$"), "") : "주소 미지정"));
        connect();
      }
      if (cfg.open) setOpen(cfg.open);
      if (cfg.mode && cfg.mode !== state.mode) {
        state.mode = cfg.mode;
        document.body.classList.toggle("panel-window", cfg.mode !== "nav");
        if (cfg.mode === "note") showNote(cfg.date || state.today);
        else if (cfg.mode === "cal") showCal();
      }
      if (cfg.noteDate && state.mode === "cal") {
        state.noteDate = cfg.noteDate;
        loadMonth();
      }
      reportNavSize();
    },
    /** 파이썬에서 호출: 달력 창에서 고른 날짜를 할일 창에 표시 */
    showDate(date) {
      if (state.mode === "note") showNote(date);
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
