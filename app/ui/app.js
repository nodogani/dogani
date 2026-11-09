/*
 * 오버레이 UI 로직. 같은 페이지를 세 가지 창으로 쓴다 (state.mode)
 * - nav : 네비게이션 바 (하나뿐). 공간 이름 ▾ 를 누르면 공간 메뉴(공용 / 개인), 할일 날짜 이동, 할일·달력 버튼
 * - note: 할일 창. 고른 공간의 그날 노트 (CodeMirror 라이브 프리뷰, 쓰기 권한 있을 때만 편집)
 * - cal : 달력 창. 고른 공간의 월 달력 + 일정 파일 편집 (개인 공간이면 공용 일정도 같이 표시)
 * 창 이동/크기/창끼리 연락은 QWebChannel 의 bridge 객체로 파이썬에 요청
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MD = window.ObsidianMarkdown;
  const CAL = window.DoganiCalendar;
  const { WD, pad, iso, parseISO, addMonths } = CAL;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  const dayLabel = (s) => {
    const d = parseISO(s);
    return `${String(d.getFullYear()).slice(2)}${pad(d.getMonth() + 1)}${pad(d.getDate())} ${WD[d.getDay()]}`;
  };

  const state = {
    mode: "nav",       // nav | note | cal
    space: null,       // { key, name, server, prefix, kind: me|team|peer, status }
    spaces: [],        // nav: 메뉴 목록
    team: null,        // { server, prefix } 개인 공간 달력에 같이 그릴 공용 일정
    base: "",          // "http://ip:port/api" 또는 ".../api/team"
    today: iso(new Date()),
    noteDate: iso(new Date()),
    calYM: iso(new Date()).slice(0, 7),
    calView: "month",  // month | doc
    status: "offline", // online | offline | paused | blocked | team_only
    es: null,
    retryTimer: null,
    writable: false,
    ownEvents: [],
    teamEvents: [],
    sched: null,       // 일정 색인 (CAL.index)
  };
  let bridge = null;

  // ------------------------------------------------------------ server
  async function api(path, body, base) {
    const opts = { cache: "no-store" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch((base || state.base) + path, opts);
    const res = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(res.message || res.error || "HTTP " + r.status);
      err.code = res.error || "http";
      throw err;
    }
    return res;
  }

  const STATUS_TEXT = {
    offline: "연결할 수 없습니다.",
    paused: "상대방이 공유를 일시 중지했습니다.",
    blocked: "상대방이 접속을 차단했습니다.",
    team_only: "팀원에게만 공유하는 노트입니다. (트레이 > 팀스페이스 > 팀 참여)",
    not_member: "팀 로그인이 필요합니다. (트레이 > 팀스페이스)",
    client_only: "접속 키가 맞지 않습니다. (Dogani 버전 확인)",
    conflict: "그 사이 다른 곳(옵시디언·다른 팀원)에서 수정되어 저장하지 않았습니다. 다시 불러옵니다.",
    read_only: "편집 권한이 없습니다.",
    write_failed: "저장에 실패했습니다.",
  };
  const errorText = (e) => STATUS_TEXT[e.code] || STATUS_TEXT[state.status] || STATUS_TEXT.offline;

  function setStatus(status) {
    if (state.status === status) return;
    state.status = status;
    if (bridge && state.space) bridge.setStatus(state.space.key, status);
  }

  function connect() {
    clearTimeout(state.retryTimer);
    if (state.es) state.es.close();
    state.es = null;
    state.status = "";
    setStatus("offline");
    const sp = state.space;
    state.base = sp && sp.server ? "http://" + sp.server + sp.prefix : "";
    state.ownEvents = [];
    rebuildSched();
    if (!state.base) return;

    const es = new EventSource(state.base + "/events");
    const applyPaused = (e) => {
      setStatus(JSON.parse(e.data).paused ? "paused" : "online");
      refresh();
    };
    es.addEventListener("hello", (e) => {
      applyPaused(e);
      loadSchedule();
      api("/info").then((info) => {
        setWritable(!!info.writable);
        if (sp.kind === "peer" && info.name && bridge) bridge.setName(sp.key, info.name);
      }).catch(() => {});
    });
    es.addEventListener("status", applyPaused);
    es.addEventListener("schedule", () => loadSchedule());
    es.addEventListener("changed", (e) => {
      const dates = JSON.parse(e.data).dates || [];
      if (state.mode === "note" && dates.includes(state.noteDate)) note.reloadIfIdle();
      if (state.mode === "cal" && dates.some((d) => d.startsWith(state.calYM))) loadMonth();
    });
    es.addEventListener("reload", () => { loadSchedule(); refresh(); });
    es.onerror = () => {
      if (state.es !== es) return;
      // 브라우저가 자동 재연결을 포기한 경우(CLOSED: 403 등)에는 이유를 확인하고 직접 재시도
      if (es.readyState === EventSource.CLOSED) {
        api("/info")
          .then(() => api("/month?ym=" + state.calYM))   // 정보는 되는데 이벤트가 막힘 = 팀원 전용 등
          .then(() => setStatus("offline"))
          .catch((e) => setStatus(["blocked", "team_only", "not_member"].includes(e.code)
            ? (e.code === "blocked" ? "blocked" : "team_only") : "offline"))
          .finally(() => { refresh(); state.retryTimer = setTimeout(connect, 5000); });
      } else {
        setStatus("offline");
      }
    };
    state.es = es;
  }

  function refresh() {
    if (state.mode === "note") note.reloadIfIdle();
    else if (state.mode === "cal") { if (state.calView === "doc") doc.reloadIfIdle(); else loadMonth(); }
  }

  // ------------------------------------------------------------ schedule (내 공간 일정 + 공용 일정)
  async function loadSchedule() {
    try {
      state.ownEvents = (await api("/schedule")).events || [];
    } catch (e) {
      return; // 예전 버전 Dogani(일정 API 없음) 이거나 연결 끊김: 기존 것 유지
    }
    rebuildSched();
  }

  async function loadTeamSchedule() {
    const t = state.team;
    if (!t || !state.space || state.space.kind === "team" || state.mode === "nav") {
      if (state.teamEvents.length) { state.teamEvents = []; rebuildSched(); }
      return;
    }
    try {
      const evs = (await api("/schedule", undefined, "http://" + t.server + t.prefix)).events || [];
      state.teamEvents = evs.map((ev) => ({ ...ev, team: true }));
    } catch (e) {
      return;
    }
    rebuildSched();
  }

  function rebuildSched() {
    state.sched = CAL.index(state.ownEvents.concat(state.teamEvents));
    if (state.mode === "cal" && state.calView === "month") loadMonth();
    if (state.mode === "note") renderSchedRow();
  }

  // ------------------------------------------------------------ 문서 편집 칸 (할일 노트 / 일정 파일 공용)
  /**
   * o = { body, footer, key(): 지금 보여줄 문서 키, fetch(key), post(key, content, baseMtime, base),
   *       empty(doc): 문서 없을 때 HTML, loaded(doc), progress: bool }
   */
  function DocPane(o) {
    const p = { doc: null, key: null, lines: null, eol: "\n", editing: false, editor: null };
    const host = document.createElement("div");
    host.className = "lp-host";
    let chain = Promise.resolve(true);
    let timer = null;

    function show(text, keepScroll) {
      if (!p.editor) {
        p.editor = window.DoganiEditor.create(host, {
          doc: text,
          readOnly: !state.writable,
          tableHtml: MD.tableHtml,
          onChange(text, immediate) {
            p.lines = text.split("\n");
            if (o.progress) setProgress(...countTasks(p.lines));
            clearTimeout(timer);
            timer = setTimeout(p.save, 700);
            if (immediate) p.save();   // 체크박스 클릭은 바로 저장
          },
          onFocus() { if (state.writable) p.editing = true; },
          onBlur() { if (p.editing) { p.editing = false; p.save(); } },
          onSave: () => p.save(),
          openUrl: (url) => (bridge ? bridge.openUrl(url) : window.open(url)),
        });
      } else if (p.editor.getDoc() !== text) {
        p.editor.setDoc(text, keepScroll);
      }
      p.editor.setReadOnly(!state.writable);
      o.body.classList.add("has-editor");
      o.body.replaceChildren(host);
    }

    p.message = (html) => {
      p.lines = null;
      o.body.classList.remove("has-editor");
      o.body.innerHTML = `<div class="empty">${html}</div>`;
      if (o.progress) $("note-bar").parentElement.parentElement.classList.add("no-tasks");
      o.footer.textContent = "";
    };

    p.load = async (keepScroll) => {
      const key = o.key();
      let d;
      try {
        d = await o.fetch(key);
      } catch (e) {
        if (key !== o.key()) return;
        p.doc = null;
        p.message(`${esc(errorText(e))}<br><small>${esc(state.space ? state.space.server : "")}</small>`);
        return;
      }
      if (key !== o.key() || p.editing) return;
      p.doc = d;
      p.key = key;
      if (!d.exists) {
        p.message(o.empty(d));
        if (o.loaded) o.loaded(d);
        return;
      }
      p.eol = d.content.includes("\r\n") ? "\r\n" : "\n";
      p.lines = d.content.split(/\r\n|\n/);
      show(p.lines.join("\n"), keepScroll);
      if (o.loaded) o.loaded(d);
      const t = new Date(d.mtime);
      o.footer.textContent =
        `마지막 수정 ${t.getMonth() + 1}/${t.getDate()} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
    };

    p.reloadIfIdle = () => { if (!p.editing) p.load(true); };

    /** 저장은 순서대로 하나씩 (자동 저장이 겹치면 mtime 충돌이 나므로). 공간·날짜는 부른 시점 기준 */
    p.save = () => {
      clearTimeout(timer);
      if (!p.lines || p.key == null) return chain;
      const content = p.lines.join(p.eol);
      const key = p.key, base = state.base;
      chain = chain.then(async () => {
        const target = p.doc && p.doc.exists && p.key === key ? p.doc : null;
        if (target && target.content === content) return true;
        try {
          const d = await o.post(key, content, target ? target.mtime : null, base);
          if (p.key === key && state.base === base) {
            p.doc = d;
            if (o.loaded) o.loaded(d);
            o.footer.textContent = "저장됨";
          }
          return true;
        } catch (e) {
          if (state.base === base) {
            o.footer.textContent = errorText(e);
            if (e.code === "conflict" && !p.editing) p.load(true);
          }
          return false;
        }
      });
      return chain;
    };

    p.exit = () => {
      if (p.editor && p.editor.hasFocus()) p.editor.blur();   // onBlur 에서 저장
      else if (p.editing) { p.editing = false; p.save(); }
    };

    /** 문서가 없는 날짜/파일: 빈 문서로 작성 시작 (첫 저장 때 파일 생성) */
    p.startBlank = () => {
      if (!state.writable || p.lines || !p.doc || p.doc.exists || p.doc.no_dir) return false;
      p.lines = [""];
      p.eol = "\n";
      p.key = o.key();
      show("", false);
      p.editor.focus(0);
      return true;
    };

    p.setReadOnly = (r) => { if (p.editor) p.editor.setReadOnly(r); };

    o.body.addEventListener("click", (e) => {
      const copy = e.target.closest(".copy-prev");
      if (copy) return copyPrev(copy.dataset.prev);
      if (state.status === "online") p.startBlank();
    });
    return p;
  }

  // ------------------------------------------------------------ note (할일 창)
  const note = DocPane({
    body: $("note-body"),
    footer: $("note-footer"),
    progress: true,
    key: () => state.base + "|" + state.noteDate,
    fetch: (key) => api(`/note?date=${key.split("|")[1]}`),
    post: (key, content, base_mtime, base) => api("/note", { date: key.split("|")[1], content, base_mtime }, base),
    empty(d) {
      if (d.no_dir) {
        return state.writable && state.space.kind === "me"
          ? "todo 폴더가 지정되지 않았습니다.<br>트레이 &gt; todo 경로 &gt; 찾아보기..."
          : "상대방이 todo 폴더를 지정하지 않았습니다.";
      }
      return state.writable ? `이 날짜의 노트가 없습니다.<br>클릭해서 작성${copyButton(d)}` : "이 날짜의 노트가 없습니다.";
    },
    loaded(d) {
      if (d.exists) {
        setProgress(d.done, d.total);
        if (!d.content.trim() && copyButton(d)) $("note-body").insertAdjacentHTML("afterbegin", `<div class="empty">${copyButton(d)}</div>`);
      }
      reportNoteDate(d.title);
    },
  });

  function renderSchedRow() {
    const row = $("note-sched");
    const html = CAL.dayEvents(state.noteDate, state.sched);
    row.innerHTML = html;
    row.hidden = !html;
  }

  /** 노트가 없거나 비었을 때: 직전 노트를 그대로 복사하는 버튼 */
  function copyButton(d) {
    if (!state.writable || !d || !d.prev || d.no_dir) return "";
    return `<button class="copy-prev" data-prev="${d.prev}">어제 노트 복사 <span>(${dayLabel(d.prev)})</span></button>`;
  }

  async function copyPrev(prev) {
    const date = state.noteDate;
    const target = note.doc;
    try {
      const src = await api(`/note?date=${prev}`);
      if (!src.exists) throw Object.assign(new Error(), { code: "write_failed" });
      await api("/note", { date, content: src.content, base_mtime: target && target.exists ? target.mtime : null });
    } catch (e) {
      $("note-footer").textContent = errorText(e);
      return;
    }
    if (state.noteDate === date) note.load(false);
  }

  function setProgress(done, total) {
    $("note-bar").parentElement.parentElement.classList.toggle("no-tasks", total === 0);
    $("note-count").textContent = total ? `${done}/${total}` : "";
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

  function reportNoteDate(title) {
    const t = title || dayLabel(state.noteDate);
    $("note-title").textContent = t;
    $("note-title").classList.toggle("is-today", state.noteDate === state.today);
    if (bridge) bridge.noteDateChanged(state.noteDate, t, state.noteDate === state.today);
  }

  function showNote(date) {
    note.exit();
    state.noteDate = date;
    setProgress(0, 0);
    renderSchedRow();
    reportNoteDate();
    note.load(false);
  }

  // ------------------------------------------------------------ calendar (달력 창)
  async function loadMonth() {
    const ym = state.calYM;
    const [y, m] = ym.split("-").map(Number);
    $("cal-title").textContent = `${y}년 ${m}월`;
    let days = {};
    let failed = false;
    try {
      days = (await api(`/month?ym=${ym}`)).days;
    } catch (e) {
      failed = errorText(e);
    }
    if (ym !== state.calYM) return;
    const r = CAL.renderMonth({ ym, days, idx: state.sched, today: state.today, selected: state.noteDate });
    $("cal-grid").innerHTML = r.html;
    CAL.fitCells($("cal-grid"));
    $("cal-footer").textContent = failed ? failed : `노트 ${r.notes}개 · 완료 ${r.done}/${r.total}`;
  }

  const doc = DocPane({
    body: $("doc-body"),
    footer: $("doc-footer"),
    key: () => state.base + "|sched",
    fetch: () => api("/sched-doc"),
    post: (key, content, base_mtime, base) => api("/sched-doc", { content, base_mtime }, base),
    empty: (d) => (d.no_dir ? "todo 폴더가 지정되지 않았습니다." : state.writable ? "일정 파일이 없습니다.<br>클릭해서 작성" : "일정 파일이 없습니다."),
    loaded: (d) => { $("doc-title").textContent = d.title || "일정"; },
  });

  function setCalView(view) {
    if (view !== "doc") doc.exit();
    state.calView = view;
    $("view-cal").hidden = view !== "month";
    $("view-doc").hidden = view !== "doc";
    if (view === "doc") doc.load(false);
    else loadMonth();
  }

  // ------------------------------------------------------------ nav (네비게이션 바 + 공간 메뉴)
  const GROUPS = [["team", "공용"], ["me", "개인"], ["member", ""], ["other", "주소로 추가"]];

  function renderNav(cfg) {
    const sp = state.space;
    if (!sp) return;
    const dot = $("status-dot");
    for (const s of ["online", "paused", "blocked"]) dot.classList.toggle(s, (sp.status === "team_only" ? "blocked" : sp.status) === s);
    $("space-name").textContent = sp.name;
    $("nav").title = `${sp.kind === "team" ? "공용 공간" : sp.kind === "me" ? "내 노트" : sp.server} · 드래그해서 이동`;
    renderMenu(cfg);
    reportNavSize();
  }

  function renderMenu(cfg) {
    const cur = state.space && state.space.key;
    let html = "";
    for (const [group, title] of GROUPS) {
      const items = state.spaces.filter((s) => s.group === group);
      if (group === "team" && !items.length) {
        html += `<div class="sm-title">공용</div>`;
        html += `<button class="sm-item sm-action" data-action="join_team"><span class="sm-glyph">+</span><span class="sm-name">팀 참여하기...</span></button>`;
        if ((cfg || {}).teamState === "none") {
          html += `<button class="sm-item sm-action" data-action="start_host"><span class="sm-glyph">⌂</span><span class="sm-name">새 팀 만들기 (이 PC 가 호스트)</span></button>`;
        }
        continue;
      }
      if (!items.length) continue;
      if (title) html += `<div class="sm-title">${title}</div>`;
      for (const s of items) {
        const st = s.status === "team_only" ? "blocked" : s.status;
        html += `<button class="sm-item${s.key === cur ? " active" : ""}" data-key="${esc(s.key)}"${s.disabled ? " disabled" : ""}>` +
          `<span class="sm-name">${esc(s.name)}${s.kind === "me" ? ' <em>나</em>' : ""}</span>` +
          (s.sub ? `<span class="sm-sub">${esc(s.sub)}</span>` : "") +
          `<span class="dot ${st}"></span></button>`;
      }
    }
    html += `<div class="sm-sep"></div>`;
    html += `<button class="sm-item sm-action" data-action="rename"><span class="sm-glyph">✎</span><span class="sm-name">내 이름 바꾸기</span></button>`;
    html += `<button class="sm-item sm-action" data-action="add_server"><span class="sm-glyph">+</span><span class="sm-name">주소로 노트 추가...</span></button>`;
    $("space-menu").innerHTML = html;
  }

  function setMenu(open) {
    $("space-menu").hidden = !open;
    $("space-btn").classList.toggle("active", open);
    reportNavSize();
  }

  function setOpen(open) {
    $("btn-note").classList.toggle("active", !!open.note);
    $("btn-cal").classList.toggle("active", !!open.cal);
  }

  function reportNavSize() {
    if (state.mode !== "nav") return;
    requestAnimationFrame(() => {
      // 그림자가 잘리지 않도록 #root 여백까지 포함한 크기
      const cs = getComputedStyle($("root"));
      const nav = $("nav").getBoundingClientRect();
      let w = nav.width, h = nav.height;
      if (!$("space-menu").hidden) {
        const m = $("space-menu").getBoundingClientRect();
        w = Math.max(w, m.width);
        h = m.bottom - nav.top;
      }
      w += parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
      h += parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
      if (bridge) bridge.setNavSize(Math.ceil(w), Math.ceil(h));
    });
  }

  // ------------------------------------------------------------ edit permission
  function setWritable(w) {
    state.writable = w;
    document.body.classList.toggle("writable", w);
    if (!w) { note.exit(); doc.exit(); }
    note.setReadOnly(!w);
    doc.setReadOnly(!w);
    $("cal-edit").hidden = false;
    refresh();
  }

  function checkDateRollover() {
    const now = iso(new Date());
    if (now === state.today) return;
    const prev = state.today;
    state.today = now;
    // 오늘 노트를 보고 있었다면 자정에 다음 날로 넘어간다 (편집 중이면 끝난 뒤에)
    if (state.mode === "note" && state.noteDate === prev && !note.editing) showNote(now);
    if (state.mode === "cal") loadMonth();
  }

  // ------------------------------------------------------------ events
  $("space-btn").addEventListener("click", () => setMenu($("space-menu").hidden));
  $("space-menu").addEventListener("click", (e) => {
    const item = e.target.closest(".sm-item");
    if (!item || item.disabled) return;
    setMenu(false);
    if (item.dataset.action) { if (bridge) bridge.action(item.dataset.action); return; }
    if (bridge) bridge.selectSpace(item.dataset.key);
  });
  window.addEventListener("blur", () => { if (state.mode === "nav" && !$("space-menu").hidden) setMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("space-menu").hidden) setMenu(false); });

  $("btn-note").addEventListener("click", () => { setMenu(false); if (bridge) bridge.togglePanel("note"); });
  $("btn-cal").addEventListener("click", () => { setMenu(false); if (bridge) bridge.togglePanel("cal"); });
  // 화살표는 토/일·쉬는 날을 건너뜀 (그 날 노트는 달력에서 눌러서 열기). 할일 창에 전달
  $("note-prev").addEventListener("click", () => bridge && bridge.stepNote(-1));
  $("note-next").addEventListener("click", () => bridge && bridge.stepNote(1));
  $("note-today").addEventListener("click", () => bridge && bridge.stepNote(0));

  for (const btn of document.querySelectorAll(".close-btn")) {
    btn.addEventListener("click", () => {
      note.exit();
      doc.exit();
      // 닫기 전에 저장이 끝나도록
      Promise.all([note.save(), doc.save()]).then(() => { if (bridge) bridge.closePanel(); });
    });
  }
  // 편집 영역 밖(바 등)을 누르거나 창이 포커스를 잃으면 편집 끝
  document.addEventListener("mousedown", (e) => {
    if (note.editing && !e.target.closest("#note-body")) note.exit();
    if (doc.editing && !e.target.closest("#doc-body")) doc.exit();
  });
  window.addEventListener("blur", () => { note.exit(); doc.exit(); });

  $("cal-prev").addEventListener("click", () => { state.calYM = addMonths(state.calYM, -1); loadMonth(); });
  $("cal-next").addEventListener("click", () => { state.calYM = addMonths(state.calYM, 1); loadMonth(); });
  $("cal-today").addEventListener("click", () => { state.calYM = state.today.slice(0, 7); loadMonth(); });
  $("cal-edit").addEventListener("click", () => setCalView("doc"));
  $("doc-back").addEventListener("click", () => doc.save().then(() => setCalView("month")));
  $("cal-grid").addEventListener("click", (e) => {
    const cell = e.target.closest(".cal-day");
    if (!cell) return;
    state.noteDate = cell.dataset.date;
    for (const c of $("cal-grid").querySelectorAll(".cal-day.selected")) c.classList.remove("selected");
    cell.classList.add("selected");
    if (bridge) bridge.openNote(cell.dataset.date);
  });

  // 드래그 이동: 네비게이션 바, 창 위쪽 막대
  for (const el of [$("nav"), ...document.querySelectorAll(".panel-bar")]) {
    el.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || e.target.closest("button")) return;
      e.preventDefault();
      if (bridge) bridge.startMove();
    });
  }
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
  setInterval(loadTeamSchedule, 60 * 1000);
  let fitTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => { if (state.mode === "cal" && state.calView === "month") CAL.fitCells($("cal-grid")); }, 100);
  });

  // ------------------------------------------------------------ init
  const sameSpace = (a, b) => a && b && a.key === b.key && a.server === b.server && a.prefix === b.prefix;

  window.App = {
    /**
     * 파이썬에서 호출 (바뀐 값만 와도 됨)
     * { mode, theme, space, team, date, noteDate,  nav: spaces, open, noteLabel, me, teamState }
     */
    configure(cfg) {
      if (cfg.theme) {
        document.body.classList.toggle("theme-light", cfg.theme !== "dark");
        document.body.classList.toggle("theme-dark", cfg.theme === "dark");
      }
      let first = false;
      if (cfg.mode && cfg.mode !== state.mode) {
        state.mode = cfg.mode;
        first = true;
        for (const m of ["nav", "note", "cal"]) document.body.classList.toggle("mode-" + m, m === cfg.mode);
        const panel = cfg.mode !== "nav";
        $("panel").hidden = !panel;
        $("view-note").hidden = cfg.mode !== "note";
        $("view-cal").hidden = cfg.mode !== "cal";
        $("view-doc").hidden = true;
        document.body.classList.toggle("expanded", panel);
        if (bridge) bridge.setExpanded(panel);
      }
      if (cfg.spaces) state.spaces = cfg.spaces;
      if (cfg.team !== undefined) state.team = cfg.team;
      let moved = false;
      if (cfg.space) {
        moved = !sameSpace(cfg.space, state.space);
        if (moved) { note.exit(); doc.exit(); }
        state.space = cfg.space;
        document.body.classList.toggle("space-team", cfg.space.kind === "team");
      }

      if (state.mode === "nav") {
        if (cfg.open) setOpen(cfg.open);
        renderNav(cfg);
        return;
      }
      if (moved) {
        state.writable = false;
        document.body.classList.remove("writable");
        connect();
        loadTeamSchedule();
      } else if (cfg.team !== undefined) {
        loadTeamSchedule();
      }
      if (state.mode === "note" && (first || moved)) showNote(first ? cfg.date || state.today : state.noteDate);
      if (state.mode === "cal") {
        if (cfg.noteDate) state.noteDate = cfg.noteDate;
        if (first) { state.calYM = state.noteDate.slice(0, 7); setCalView("month"); }
        else if (moved) setCalView(state.calView);
        else if (cfg.noteDate && state.calView === "month") loadMonth();
      }
    },
    /** 달력 창에서 고른 날짜를 할일 창에 표시 */
    showDate(date) {
      if (state.mode === "note") showNote(date);
    },
    /** 네비게이션 바의 ‹ › 오늘 (step: -1 / 1 / 0) */
    step(step) {
      if (state.mode !== "note") return;
      showNote(step === 0 ? state.today : CAL.nextWorkday(state.noteDate, step, state.sched));
    },
  };

  if (window.qt && window.QWebChannel) {
    new QWebChannel(qt.webChannelTransport, (channel) => {
      bridge = channel.objects.bridge;
      bridge.getConfig((json) => App.configure(JSON.parse(json)));
    });
  } else {
    // 일반 브라우저에서 테스트: index.html?mode=note&server=192.168.0.10:8765&prefix=/api&theme=dark
    const q = new URLSearchParams(location.search);
    App.configure({
      mode: q.get("mode") || "note", theme: q.get("theme") || "light",
      space: { key: "test", name: q.get("server") || "test", server: q.get("server") || "", prefix: q.get("prefix") || "/api", kind: "peer", status: "offline" },
    });
  }
})();
