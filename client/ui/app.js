/*
 * 오버레이 UI 로직
 * - 서버의 SSE(/api/events)를 구독해서 노트가 바뀌면 즉시 다시 그린다
 * - 기본은 읽기 전용. 서버 PC 에서 실행한 클라이언트(서버 주인)만 편집/체크박스 토글 가능
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
    writable: false,   // 서버가 편집을 허용하는지 (서버 PC 에서 접속한 경우)
    note: null,        // 현재 표시 중인 노트 응답
    editing: false,
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
    offline: "서버에 연결할 수 없습니다.",
    paused: "서버에서 공유를 일시 중지했습니다.",
    blocked: "서버에서 접속이 차단되었습니다.",
    client_only: "서버 접속 키가 맞지 않습니다.",
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
      api("/api/info").then((info) => setWritable(!!info.writable)).catch(() => {});
    });
    es.addEventListener("status", applyPaused);
    es.addEventListener("changed", (e) => {
      const dates = JSON.parse(e.data).dates || [];
      pulse();
      // 편집 중에는 화면을 덮어쓰지 않는다 (저장 시 서버가 충돌 검사)
      if (state.view === "note" && !state.editing && (dates.includes(state.noteDate) || dates.includes(state.shownDate))) loadNote(true);
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
    if (state.view === "note") { if (!state.editing) loadNote(true); }
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
      note = await api(`/api/note?date=${date}`);
    } catch (e) {
      if (date !== state.noteDate) return;
      state.note = null;
      body.innerHTML = `<div class="empty">${errorText(e)}<br>${state.server || "(서버 미지정)"}</div>`;
      $("note-progress").hidden = true;
      $("note-footer").textContent = "";
      return;
    }
    if (date !== state.noteDate) return;

    state.shownDate = note.date;
    state.note = note;
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
    closeEditor();
    state.noteDate = date;
    setView("note");
    loadNote(false);
  }

  // ------------------------------------------------------------ edit (서버 주인 전용)
  function setWritable(w) {
    state.writable = w;
    document.body.classList.toggle("writable", w);
    if (!w && state.editing) closeEditor();
  }

  // 노트가 없는 날짜면 편집 대상은 그 날짜의 새 노트
  const editTarget = () => (state.note && state.note.exists ? state.note : null);

  // 저장은 순서대로 하나씩 (자동 저장이 겹치면 mtime 충돌이 나므로)
  let saveChain = Promise.resolve(true);
  let saveTimer = null;

  function save(content) {
    saveChain = saveChain.then(async () => {
      const target = editTarget();
      if (target && target.content === content) return true;
      try {
        state.note = await api("/api/note", {
          date: target ? target.date : state.noteDate,
          content,
          base_mtime: target ? target.mtime : null,
        });
        $("note-footer").textContent = "저장됨";
        setProgress(state.note.done, state.note.total);
        return true;
      } catch (e) {
        $("note-footer").textContent = errorText(e);
        return false;
      }
    });
    return saveChain;
  }

  /** 옵시디언처럼 본문을 클릭하면 그 자리에서 바로 원문 편집. caretText: 클릭한 블록의 글자(커서 위치 찾기용) */
  function openEditor(caretText, offsetY) {
    if (!state.writable || state.status !== "online" || state.editing) return;
    const ed = $("note-editor");
    const t = editTarget();
    state.editing = true;
    ed.value = t ? t.content : "";
    $("note-body").hidden = true;
    ed.hidden = false;
    let pos = ed.value.length;
    if (caretText) {
      const idx = ed.value.indexOf(caretText);
      if (idx >= 0) pos = ed.value.indexOf("\n", idx) < 0 ? ed.value.length : ed.value.indexOf("\n", idx);
    }
    ed.focus();
    ed.setSelectionRange(pos, pos);
    // 클릭한 줄이 화면에서 같은 높이에 오도록 스크롤
    const line = ed.value.slice(0, pos).split("\n").length - 1;
    const lh = parseFloat(getComputedStyle(ed).lineHeight) || 20;
    ed.scrollTop = Math.max(0, line * lh - (offsetY || 0));
  }

  function closeEditor() {
    if (!state.editing) return;
    clearTimeout(saveTimer);
    const ed = $("note-editor");
    state.editing = false;
    save(ed.value).then(() => {
      ed.hidden = true;
      $("note-body").hidden = false;
      loadNote(true);
    });
  }

  // 읽기 화면에서 체크박스 클릭 → 원본의 같은 순번 체크박스 토글 (서버 count_tasks 와 같은 규칙)
  async function toggleTask(box) {
    const target = editTarget();
    if (!target) return;
    const boxes = [...$("note-body").querySelectorAll(".task-checkbox")];
    const idx = boxes.indexOf(box);
    const lines = target.content.split("\n");
    const taskRe = /^(\s*(?:[-*+]|\d+[.)])\s+\[)(.)(\])/;
    let inFence = false, hit = -1, count = 0;
    lines.forEach((line, i) => {
      if (/^\s*(```|~~~)/.test(line)) { inFence = !inFence; return; }
      if (inFence || !taskRe.test(line)) return;
      if (count === idx) hit = i;
      count++;
    });
    // 인용/콜아웃 안 체크박스 등으로 순번이 어긋나면 엉뚱한 줄을 건드리지 않도록 포기
    if (hit < 0 || count !== boxes.length) {
      $("note-footer").textContent = "이 체크박스는 편집 버튼으로 수정해주세요.";
      return;
    }
    lines[hit] = lines[hit].replace(taskRe, (_, a, c, b) => a + (c === " " ? "x" : " ") + b);
    if (await save(lines.join("\n"))) loadNote(true);
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
    if (view !== "note") closeEditor();
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
  $("note-editor").addEventListener("input", () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => save($("note-editor").value), 700);
  });
  $("note-editor").addEventListener("blur", closeEditor);
  $("note-editor").addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key.toLowerCase() === "s") {
      e.preventDefault();
      clearTimeout(saveTimer);
      save($("note-editor").value);
    } else if (e.key === "Escape") {
      e.preventDefault();
      $("note-editor").blur();
    } else if (e.key === "Tab") {
      e.preventDefault();
      document.execCommand("insertText", false, "\t");
    }
  });
  $("note-body").addEventListener("click", (e) => {
    if (!state.writable) return;
    const box = e.target.closest(".task-checkbox");
    if (box) return toggleTask(box);
    if (e.target.closest("a") || String(window.getSelection())) return; // 링크 클릭·글자 선택은 편집 안 함
    const block = e.target.closest("li, p, h1, h2, h3, h4, h5, h6, td, th, pre, blockquote");
    // 블록의 첫 줄 글자 일부로 원문 위치를 찾는다 (렌더링된 글자라 마크다운 기호는 빠져 있음)
    const text = block ? (block.firstChild && block.firstChild.textContent || block.textContent).trim().split("\n")[0].slice(0, 20) : "";
    const body = $("note-body");
    const offsetY = block ? block.getBoundingClientRect().top - body.getBoundingClientRect().top : 0;
    openEditor(text, offsetY);
  });
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
