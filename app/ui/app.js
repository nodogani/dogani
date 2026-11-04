/*
 * 오버레이 UI 로직 (창 하나 = 노트 하나: 내 노트 또는 다른 사람)
 * - 서버의 SSE(/api/events)를 구독해서 노트가 바뀌면 즉시 다시 그린다
 * - 내 노트(writable)는 옵시디언 라이브 프리뷰처럼 클릭한 줄만 원문으로 바꿔 그 자리에서 편집
 * - 창 이동/크기/펼침 등 네이티브 동작은 QWebChannel 의 bridge 객체로 파이썬에 요청
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const WD = ["일", "월", "화", "수", "목", "금", "토"];
  const DEFAULT_PORT = "8765";
  const MD = window.ObsidianMarkdown;

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
    label: "",         // 네비게이션 바 이름 (내 노트 / 상대 PC 이름)
    mine: false,
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
      api("/api/info").then((info) => {
        setWritable(!!info.writable);
        if (!state.mine && info.name) {
          setLabel(info.name);
          if (bridge) bridge.setName(info.name);
        }
      }).catch(() => {});
    });
    es.addEventListener("status", applyPaused);
    es.addEventListener("changed", (e) => {
      const dates = JSON.parse(e.data).dates || [];
      pulse();
      // 편집 중에는 화면을 덮어쓰지 않는다 (저장 시 서버가 충돌 검사)
      if (state.view === "note" && !state.editing && dates.includes(state.noteDate)) loadNote(true);
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
  function showMessage(html) {
    state.lines = null;
    $("note-body").innerHTML = `<div class="empty">${html}</div>`;
    $("note-progress").hidden = true;
    $("note-footer").textContent = "";
  }

  async function loadNote(keepScroll) {
    const date = state.noteDate;
    const body = $("note-body");
    $("note-title").textContent = dayLabel(date);
    $("note-title").classList.toggle("is-today", date === state.today);
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
        showMessage(state.writable ? "이 날짜의 노트가 없습니다.<br>클릭해서 작성" : "이 날짜의 노트가 없습니다.");
      }
      return;
    }
    $("note-title").textContent = note.title;
    state.eol = note.content.includes("\r\n") ? "\r\n" : "\n";
    state.lines = note.content.split(/\r\n|\n/);
    renderNote(keepScroll ? body.scrollTop : 0);
    setProgress(note.done, note.total);
    const t = new Date(note.mtime);
    $("note-footer").textContent =
      `마지막 수정 ${t.getMonth() + 1}/${t.getDate()} ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
  }

  /** state.lines 를 화면에 그린다. 편집 중인 단위는 원문 textarea 로. */
  function renderNote(scrollTop) {
    const body = $("note-body");
    const scroll = scrollTop === undefined ? body.scrollTop : scrollTop;
    state.units = MD.units(state.lines);
    const frag = document.createDocumentFragment();
    let editor = null;
    for (const u of state.units) {
      const div = document.createElement("div");
      div.dataset.start = u.start;
      if (u.style) div.setAttribute("style", u.style);
      if (u.callout) div.dataset.callout = u.callout;
      if (state.active && state.active.start === u.start) {
        div.className = u.rawCls + " lp-active";
        if (u.guides) div.innerHTML = u.guides;
        editor = document.createElement("textarea");
        editor.className = "lp-editor";
        editor.rows = 1;
        editor.spellcheck = false;
        editor.value = state.active.block
          ? state.lines.slice(u.start, u.end + 1).join("\n")
          : state.lines[u.start].slice(state.active.indent.length);
        div.append(editor);
        bindEditor(editor);
      } else {
        div.className = u.cls;
        div.innerHTML = u.html;
      }
      frag.append(div);
    }
    body.replaceChildren(frag);
    if (editor) autosize(editor);
    body.scrollTop = scroll;
    return editor;
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
  }

  // ------------------------------------------------------------ edit (내 노트 전용)
  function setWritable(w) {
    state.writable = w;
    document.body.classList.toggle("writable", w);
    if (!w) exitEdit();
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

  const unitAt = (line) => state.units.find((u) => u.start <= line && line <= u.end) || state.units[state.units.length - 1];

  /** line 줄의 col 위치(원문 기준)에 커서를 두고 그 줄(단위)을 원문 편집 상태로 */
  function activate(line, col) {
    if (!state.writable || !state.lines) return;
    state.units = MD.units(state.lines);
    line = Math.max(0, Math.min(line, state.lines.length - 1));
    const u = unitAt(line);
    state.active = { start: u.start, end: u.end, block: !!u.block, indent: u.indent || "", list: !!u.list };
    state.editing = true;
    $("note-body").classList.add("editing");
    const ed = renderNote();
    let pos;
    if (u.block) {
      pos = 0;
      for (let k = u.start; k < line; k++) pos += state.lines[k].length + 1;
      pos += Math.min(col, state.lines[line].length);
    } else {
      pos = Math.max(0, Math.min(col - state.active.indent.length, ed.value.length));
    }
    ed.focus({ preventScroll: true });
    ed.setSelectionRange(pos, pos);
    ed.scrollIntoView({ block: "nearest" });
  }

  function exitEdit() {
    if (!state.editing) return;
    state.editing = false;
    state.active = null;
    $("note-body").classList.remove("editing");
    if (state.lines) renderNote();
    save();
  }

  function autosize(ed) {
    ed.style.height = "auto";
    ed.style.height = ed.scrollHeight + "px";
  }

  // 커서가 textarea 의 첫/마지막 화면 줄에 있는지 (자동 줄바꿈 포함) — 위/아래 화살표로 옆 줄 이동 판단
  const mirror = document.createElement("div");
  mirror.className = "lp-mirror";
  document.body.append(mirror);
  function rowHeight(ed, text) {
    const cs = getComputedStyle(ed);
    mirror.style.width = ed.clientWidth + "px";
    mirror.style.font = cs.font;
    mirror.style.lineHeight = cs.lineHeight;
    mirror.style.tabSize = cs.tabSize;
    mirror.textContent = text + "​";
    return mirror.offsetHeight;
  }

  /** 원문 줄의 목록 접두어(들여쓰기 + 기호 + 체크박스) */
  const LIST_PREFIX = /^([ \t]*)([-*+]|(\d{1,9})([.)]))([ \t]+)(\[.\][ \t]+)?/;

  function bindEditor(ed) {
    const a = state.active;
    const rawLine = () => a.indent + ed.value;   // 단일 줄 단위의 원문
    const caretCol = () => a.indent.length + ed.selectionStart;

    ed.addEventListener("input", () => {
      if (a.block) {
        const parts = ed.value.replace(/\r\n?/g, "\n").split("\n");
        state.lines.splice(a.start, a.end - a.start + 1, ...parts);
        const delta = a.start + parts.length - 1 - a.end;
        a.end += delta;
        if (delta) shiftUnits(a.start, delta);
      } else if (/[\r\n]/.test(ed.value)) {
        // 여러 줄 붙여넣기 → 줄을 나눠서 넣고 마지막 줄에서 계속
        const after = ed.value.length - ed.selectionEnd;
        const parts = rawLine().replace(/\r\n?/g, "\n").split("\n");
        state.lines.splice(a.start, 1, ...parts);
        changed();
        const last = a.start + parts.length - 1;
        return activate(last, parts[parts.length - 1].length - after);
      } else {
        state.lines[a.start] = rawLine();
      }
      autosize(ed);
      changed();
    });

    ed.addEventListener("keydown", (e) => {
      if (e.isComposing || e.keyCode === 229) return; // 한글 조합 중
      const s = ed.selectionStart, end = ed.selectionEnd;
      const key = e.key;

      if (e.ctrlKey && key.toLowerCase() === "s") {
        e.preventDefault();
        save();
      } else if (key === "Escape") {
        e.preventDefault();
        exitEdit();
      } else if (key === "Enter" && !a.block) {
        e.preventDefault();
        enter(e.shiftKey);
      } else if (key === "Backspace" && !a.block && s === 0 && end === 0) {
        e.preventDefault();
        if (a.indent) return indentLine(-1);
        if (a.start === 0) return;
        const prev = state.lines[a.start - 1];
        state.lines.splice(a.start - 1, 2, prev + rawLine());
        changed();
        activate(a.start - 1, prev.length);
      } else if (key === "Delete" && !a.block && s === ed.value.length && end === s && a.start + 1 < state.lines.length) {
        e.preventDefault();
        const len = rawLine().length;
        state.lines.splice(a.start, 2, rawLine() + state.lines[a.start + 1]);
        changed();
        activate(a.start, len);
      } else if (key === "Tab") {
        e.preventDefault();
        if (a.list) indentLine(e.shiftKey ? -1 : 1);
        else if (!e.shiftKey) document.execCommand("insertText", false, "\t");
      } else if ((key === "ArrowUp" || key === "ArrowDown") && !e.shiftKey && s === end) {
        const up = key === "ArrowUp";
        const edge = up
          ? rowHeight(ed, ed.value.slice(0, s)) <= rowHeight(ed, "")
          : rowHeight(ed, ed.value.slice(0, s)) >= rowHeight(ed, ed.value);
        if (!edge) return;
        const target = up ? a.start - 1 : a.end + 1;
        if (target < 0 || target >= state.lines.length) return;
        e.preventDefault();
        // 같은 열 위치로 (블록이면 블록 안 그 줄의 열)
        let col = caretCol();
        if (a.block) col = s - ed.value.lastIndexOf("\n", s - 1) - 1;
        activate(target, col);
      }
    });

    /** Enter: 줄 나누기. 목록이면 옵시디언처럼 다음 줄에 기호 이어 붙이기 */
    function enter(plain) {
      const line = rawLine();
      const col = caretCol();
      const before = line.slice(0, col);
      const after = line.slice(a.indent.length + ed.selectionEnd);
      const m = before.match(LIST_PREFIX);
      if (m && !plain && before === m[0] && !after.trim()) {
        // 빈 목록 항목에서 Enter: 들여쓰기 한 단계 빼고, 맨 앞이면 목록 끝내기
        if (m[1]) return indentLine(-1);
        state.lines[a.start] = "";
        changed();
        return activate(a.start, 0);
      }
      let prefix = "";
      if (m && !plain) {
        const marker = m[3] ? `${+m[3] + 1}${m[4]}` : m[2];
        prefix = m[1] + marker + m[5] + (m[6] ? "[ ] " : "");
      } else if (a.list) {
        prefix = a.indent; // 이어지는 줄은 들여쓰기 유지
      }
      state.lines.splice(a.start, 1, before, prefix + after);
      changed();
      activate(a.start + 1, prefix.length);
    }

    /** 목록 들여쓰기 +1 / -1 단계 (탭 문자, 스페이스 들여쓰기면 4칸) */
    function indentLine(dir) {
      const col = caretCol();
      let ind = a.indent;
      let delta;
      if (dir > 0) {
        const unit = ind.includes(" ") && !ind.includes("\t") ? "    " : "\t";
        ind += unit;
        delta = unit.length;
      } else {
        if (!ind) return;
        const cut = ind.endsWith("\t") ? 1 : Math.min(4, ind.length - ind.trimEnd().length || ind.length);
        ind = ind.slice(0, ind.length - cut);
        delta = -cut;
      }
      state.lines[a.start] = ind + ed.value;
      changed();
      activate(a.start, col + delta);
    }
  }

  /** 블록 편집으로 줄 수가 바뀌면 뒤쪽 단위의 줄 번호를 밀어준다 (다시 그리면 편집 중 undo 가 날아가므로) */
  function shiftUnits(after, delta) {
    for (const u of state.units) {
      if (u.start > after) { u.start += delta; u.end += delta; }
      else if (u.start === after) u.end = state.active.end;
    }
    for (const el of $("note-body").querySelectorAll("[data-start]")) {
      if (+el.dataset.start > after) el.dataset.start = +el.dataset.start + delta;
    }
  }

  // 체크박스 클릭 → 그 줄의 체크 상태만 바꿔서 바로 저장
  function toggleTask(line) {
    const re = /^((?:[ \t]{0,3}>[ \t]?)*[ \t]*(?:[-*+]|\d+[.)])[ \t]+\[)(.)(\])/;
    if (!re.test(state.lines[line])) return;
    state.lines[line] = state.lines[line].replace(re, (_, p, c, q) => p + (c === " " ? "x" : " ") + q);
    const [done, total] = countTasks(state.lines);
    setProgress(done, total);
    if (state.editing) {
      // 편집 중이면 현재 편집 줄을 유지한 채 다시 그림
      const ed = document.querySelector(".lp-editor");
      const pos = ed ? ed.selectionStart : 0;
      const ed2 = renderNote();
      if (ed2) { ed2.focus({ preventScroll: true }); ed2.setSelectionRange(pos, pos); }
    } else {
      renderNote();
    }
    save();
  }

  /** 클릭 위치 → 렌더링된 글자 기준 오프셋 → 원문 열 위치 (기호는 건너뛰며 글자 맞추기) */
  function clickToCol(e, el, u) {
    const raw = state.lines[u.start];
    const textEl = el.querySelector(".lp-text");
    if (!textEl) return raw.length;
    let offset = null;
    const r = document.caretRangeFromPoint(e.clientX, e.clientY);
    if (r && textEl.contains(r.startContainer)) {
      const pre = document.createRange();
      pre.setStart(textEl, 0);
      pre.setEnd(r.startContainer, r.startOffset);
      offset = pre.toString().length;
    }
    const rendered = textEl.textContent;
    if (offset === null) {
      const rect = textEl.getBoundingClientRect();
      offset = e.clientX < rect.left && e.clientY < rect.bottom ? 0 : rendered.length;
    }
    const src = raw.slice(u.textStart || 0);
    let i = 0, j = 0;
    while (j < offset && i < src.length) {
      if (src[i] === rendered[j]) j++;
      i++;
    }
    return (u.textStart || 0) + i;
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
    $("cal-footer").textContent = failed ? failed : `노트 ${notes}개 · 완료 ${sumDone}/${sumTotal}`;
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

  $("note-body").addEventListener("mousedown", (e) => {
    // 체크박스를 눌러도 편집 중인 줄의 포커스가 빠지지 않게
    if (state.writable && e.target.closest(".task-checkbox")) e.preventDefault();
  });
  $("note-body").addEventListener("click", (e) => {
    if (!state.writable || state.status !== "online" || e.target.closest(".lp-editor")) return;
    const box = e.target.closest(".task-checkbox");
    const el = e.target.closest("[data-start]");
    if (box && el) return toggleTask(+el.dataset.start);
    if (e.target.closest("a")) return;
    if (!state.editing && String(window.getSelection())) return; // 글자 선택(복사)은 편집 안 함

    if (!state.lines) {
      // 노트가 없는 날짜: 빈 노트로 작성 시작 (첫 저장 때 파일 생성)
      if (!state.note || state.note.exists || state.note.no_dir) return;
      state.lines = [""];
      state.eol = "\n";
      return activate(0, 0);
    }
    if (!el) {
      // 본문 아래 빈 곳: 마지막 줄 끝
      const last = state.lines.length - 1;
      return activate(last, state.lines[last].length);
    }
    const u = state.units.find((x) => x.start === +el.dataset.start);
    if (!u) return;
    if (u.block) return activate(u.start, state.lines[u.start].length);
    activate(u.start, clickToCol(e, el, u));
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
