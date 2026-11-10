/*
 * 달력·일정 모듈
 * - 날짜 계산 도우미 (app.js 와 같이 씀)
 * - 일정 목록(/api/schedule) → 날짜별 색인, 쉬는 날(==하이라이트== 일정 + 토/일) 판정
 * - 월 달력 HTML 그리기, 태그별 색, 노트 위 일정 줄
 */
(function () {
  "use strict";

  const WD = ["일", "월", "화", "수", "목", "금", "토"];
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
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  /** 태그 → 색 (태그 이름 해시로 정해서 다시 켜도 같은 색) */
  function tagHue(tag) {
    let h = 2166136261;
    for (const ch of tag) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
    // 골든 앵글로 흩어서 비슷한 이름도 색이 멀어지게
    return Math.round(((h >>> 0) * 137.508) % 360);
  }
  const eventColor = (ev) => (ev.tags && ev.tags.length ? `hsl(${tagHue(ev.tags[0])} 70% 50%)` : "var(--accent)");

  /** 일정 목록 → { byDate: {iso: [ev]}, off: Set(iso), list } (ev.k = list 안 번호, 달력에서 data-k 로 찾음) */
  function index(events) {
    const byDate = {};
    const off = new Set();
    const list = events || [];
    list.forEach((ev, k) => { ev.k = k; });
    for (const ev of list) {
      let d = ev.start;
      for (let k = 0; d <= ev.end && k < 366; k++, d = addDays(d, 1)) {
        (byDate[d] = byDate[d] || []).push(ev);
        // 시작/끝 미정 일정의 페이드용 하루는 쉬는 날로 치지 않음
        const fake = (ev.open === "start" && d === ev.start) || (ev.open === "end" && d === ev.end);
        if (ev.holiday && !fake) off.add(d);
      }
    }
    return { byDate, off, list };
  }

  const isWeekend = (s) => { const w = parseISO(s).getDay(); return w === 0 || w === 6; };
  const isOff = (s, idx) => isWeekend(s) || !!(idx && idx.off.has(s));

  /** s 에서 dir(±1) 방향으로 다음 근무일 (토/일·쉬는 날 건너뜀) */
  function nextWorkday(s, dir, idx) {
    let d = addDays(s, dir);
    for (let k = 0; k < 60 && isOff(d, idx); k++) d = addDays(d, dir);
    return d;
  }

  function eventTip(ev) {
    const range = ev.open === "start" ? `~ ${ev.end} (시작 미정)`
      : ev.open === "end" ? `${ev.start} ~ (끝 미정)`
      : ev.start === ev.end ? ev.start : `${ev.start} ~ ${ev.end}`;
    return [
      `${ev.title}${ev.holiday ? " (쉬는 날)" : ""}`,
      `${range}${ev.time ? " " + timeText(ev) : ""}${ev.uncertain ? " (정확한 시각 미정)" : ""}`,
      ev.place && `장소: ${ev.place}`,
      ev.tags && ev.tags.length && ev.tags.map((t) => "#" + t).join(" "),
      ev.memo && `메모: ${ev.memo}`,
    ].filter(Boolean).join("\n");
  }

  const q = (ev) => (ev.uncertain ? "쯤" : "");  // TODO 로캘: 영어면 "~10:00" 등
  const timeText = (ev) => (ev.time || "") + q(ev);
  const shortTime = (ev) => (ev.time || "").split("-")[0] + q(ev);
  // 공용 일정(ev.team)은 점선 테두리 + 툴팁에 [공용] 표시
  const evAttrs = (ev, cls) => `class="${cls.concat(ev.team ? ["team"] : []).join(" ")}" data-k="${ev.k ?? ""}" style="--ev:${eventColor(ev)}" ` +
    `title="${esc((ev.team ? "[공용] " : "") + eventTip(ev))}"`;

  /** 노트 위 일정 줄용 칩 (시간·장소까지) */
  function chip(ev) {
    const cls = ["ev"];
    if (ev.important) cls.push("important");
    if (ev.holiday) cls.push("holiday");
    if (ev.uncertain) cls.push("unsure");
    const time = ev.time ? `<span class="ev-time">${esc(timeText(ev))}</span>` : "";
    const place = ev.place ? `<span class="ev-place">@${esc(ev.place)}</span>` : "";
    return `<span ${evAttrs(ev, cls)}>${time}${esc(ev.title)}${place}</span>`;
  }

  /** 달력 칸 하루짜리 일정: 굵게 아니고 시간 있으면 "● 15:00 제목", 아니면 칩 */
  function dayItem(ev) {
    const time = ev.time ? `<span class="ev-time">${esc(shortTime(ev))}</span>` : "";
    if (ev.time && !ev.important && !ev.holiday) {
      const cls = ["ev", "ev-dot"];
      if (ev.uncertain) cls.push("unsure");  // ○ 속 빈 점 + 옅은 글씨
      return `<span ${evAttrs(ev, cls)}><i></i>${time}${esc(ev.title)}</span>`;
    }
    const cls = ["ev"];
    if (ev.important) cls.push("important");
    if (ev.holiday) cls.push("holiday");
    if (ev.uncertain) cls.push("unsure");
    return `<span ${evAttrs(ev, cls)}>${time}${esc(ev.title)}</span>`;
  }

  /**
   * 기간 일정 한 주 구간: 시작 칸에만 여러 칸 너비로 그리고 나머지 칸은 자리만 비움.
   * head/tail = 이 구간에서 실제로 시작/끝나는지 (아니면 다음 주로 이어짐)
   */
  function rangeBar(ev, n, head, tail) {
    const cls = ["ev", "bar"];
    if (ev.italic) cls.push("arrow");
    if (ev.important) cls.push("important");
    if (ev.holiday) cls.push("holiday");
    if (head) cls.push("head");
    if (tail) cls.push("tail");
    // 시작/끝 미정: 그 끝 쪽 첫 칸에서 서서히 들어오거나(fade-in) 사라짐(fade-out)
    const fadeIn = ev.open === "start" && head, fadeOut = ev.open === "end" && tail;
    if (fadeIn) cls.push("fade-in");
    if (fadeOut) cls.push("fade-out");
    const cut = (head ? 5 : 0) + (tail ? 5 : 0);
    const style = `width:calc(${n} * 100% + ${n - 1} * var(--cal-gap) - ${cut}px);--n:${n}`;
    let label = `<span class="bar-label">${esc(ev.title)}</span>`;
    if (ev.italic) {
      label = (head && !fadeIn ? '<i class="ah l"></i>' : "") + '<i class="ln"></i>' + label +
        '<i class="ln"></i>' + (tail && !fadeOut ? '<i class="ah r"></i>' : "");
    }
    return `<span ${evAttrs(ev, cls).replace('style="', `style="${style};`)}>${label}</span>`;
  }

  /** 한 주(7일)의 기간 일정 줄 배치: { 날짜: [lane별 html] } */
  function layoutWeek(week, idx) {
    const seen = new Set(), multi = [];
    for (const d of week) {
      for (const ev of (idx && idx.byDate[d]) || []) {
        if (ev.start !== ev.end && !seen.has(ev)) { seen.add(ev); multi.push(ev); }
      }
    }
    multi.sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : a.end > b.end ? -1 : 1));
    const laneEnd = [];
    const cells = week.map(() => []);
    for (const ev of multi) {
      const s = Math.max(0, week.findIndex((d) => d >= ev.start));
      let e = week.length - 1;
      while (e > s && week[e] > ev.end) e--;
      let lane = laneEnd.findIndex((x) => x < s);
      if (lane < 0) lane = laneEnd.length;
      laneEnd[lane] = e;
      cells[s][lane] = rangeBar(ev, e - s + 1, week[s] === ev.start, week[e] === ev.end);
      for (let k = s + 1; k <= e; k++) cells[k][lane] = '<span class="ev spacer"></span>';
    }
    // 한 주의 모든 칸을 같은 줄 수로 채움 (띠 영역 높이가 같아야 옆 칸과 줄이 맞음)
    const n = laneEnd.length;
    return cells.map((c) => Array.from({ length: n }, (_, l) => c[l] || '<span class="ev spacer"></span>'));
  }

  /**
   * 월 달력 HTML.
   * opts: { ym, days: {iso: {done,total}}, idx, today, selected }
   * 반환: { html, notes, done, total }
   */
  function renderMonth({ ym, days, idx, today, selected }) {
    const [y, m] = ym.split("-").map(Number);
    let html = WD.map((w, k) => `<div class="wd${k === 0 ? " sun" : k === 6 ? " sat" : ""}">${w}</div>`).join("");
    const first = new Date(y, m - 1, 1);
    const cursor = new Date(y, m - 1, 1 - first.getDay());
    let notes = 0, done = 0, total = 0;
    let lanes = [];

    for (let k = 0; k < 42; k++) {
      const d = iso(cursor);
      if (k % 7 === 0) lanes = layoutWeek(Array.from({ length: 7 }, (_, i) => addDays(d, i)), idx);
      const info = days[d];
      const singles = ((idx && idx.byDate[d]) || []).filter((ev) => ev.start === ev.end);
      const cls = ["cal-day"];
      if (cursor.getMonth() !== m - 1) cls.push("other");
      if (cursor.getDay() === 0) cls.push("sun");
      if (cursor.getDay() === 6) cls.push("sat");
      if (idx && idx.off.has(d)) cls.push("off");
      if (d === today) cls.push("today");
      if (d === selected) cls.push("selected");
      let count = "";
      if (info) {
        notes++;
        done += info.done;
        total += info.total;
        const c = info.total === 0 ? "none" : info.done === info.total ? "done" : "";
        count = `<span class="count ${c}">${info.done}/${info.total}</span>`;
      }
      // 하루 일정은 위(남는 자리만, 넘치면 +N), 기간 띠는 칸 아래 고정 영역 (숨기지 않아 옆 칸과 줄이 항상 맞음)
      const lane = lanes[k % 7];
      const list = singles.length || lane.length ? `<div class="evs"><div class="singles">${singles.map(dayItem).join("")}</div>` +
        (lane.length ? `<div class="lanes">${lane.join("")}</div>` : "") + "</div>" : "";
      html += `<div class="${cls.join(" ")}" data-date="${d}"><span class="num">${cursor.getDate()}</span>${list}${count}</div>`;
      cursor.setDate(cursor.getDate() + 1);
    }
    return { html, notes, done, total };
  }

  /** 칸에 안 들어가는 하루 일정은 숨기고 "+N" 표시 (그린 뒤·크기 바뀐 뒤 호출). 기간 띠는 건드리지 않음 */
  function fitCells(grid) {
    for (const cell of grid.querySelectorAll(".cal-day")) {
      cell.querySelectorAll(".ev-more").forEach((el) => el.remove());
      const box = cell.querySelector(".singles");
      if (!box) continue;
      const items = [...box.children];
      for (const el of items) el.hidden = false;
      if (!items.length || box.scrollHeight <= box.clientHeight + 1) continue;
      const more = document.createElement("span");
      more.className = "ev ev-more";
      cell.querySelector(".num").after(more);  // +N 은 늘 날짜 숫자 오른쪽 (일정 칸 자리를 안 먹음)
      let hidden = 0;
      for (let k = items.length - 1; k >= 0; k--) {
        if (box.scrollHeight <= box.clientHeight + 1) break;
        items[k].hidden = true;
        hidden++;
      }
      more.textContent = `+${hidden}`;
      more.title = items.filter((el) => el.hidden).map((el) => el.title).join("\n\n");
    }
  }

  /** 노트 위에 보여줄 그날 일정 줄 HTML (없으면 "") */
  function dayEvents(d, idx) {
    const evs = (idx && idx.byDate[d]) || [];
    return evs.map(chip).join("");
  }

  window.doganiCalendar = {
    WD, pad, iso, parseISO, addDays, addMonths,
    index, isOff, nextWorkday, renderMonth, fitCells, dayEvents,
  };
})();
