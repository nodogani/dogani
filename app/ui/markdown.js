/*
 * 옵시디언 라이브 프리뷰(Live Preview)에 맞춘 경량 마크다운 렌더러 (외부 라이브러리 없음, 사내망 오프라인용)
 *
 * 원문 한 줄 = 화면 한 줄로 그린다. 그래서 편집할 때 클릭한 줄만 원문(서식 깨진 텍스트)으로 바꾸고
 * 나머지 줄은 보기 화면과 똑같이 유지할 수 있다. (코드블록·표·frontmatter 는 여러 줄을 한 덩어리로)
 *
 * 지원: 제목, 체크리스트(중첩/완료 취소선), 순서/비순서 목록, 인용, 콜아웃, 코드블록, 표, 구분선,
 *       **굵게** *기울임* ~~취소선~~ ==하이라이트== `코드` #태그 [[위키링크]] ![[임베드]] [링크](url) %%주석%%
 * 보안: 원문 HTML 태그는 렌더링하지 않고 이스케이프
 */
(function (global) {
  "use strict";

  const esc = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  // ------------------------------------------------------------ inline
  function inline(src) {
    const slots = [];
    const keep = (html) => "\u0000" + (slots.push(html) - 1) + "\u0000";
    let s = src;

    s = s.replace(/(`+)(.+?)\1(?!`)/g, (_, __, code) => keep(`<code>${esc(code)}</code>`));
    s = s.replace(/%%.*?%%/g, () => keep(""));  // 옵시디언 주석은 숨김
    s = esc(s);

    s = s.replace(/!\[\[([^\]]+?)\]\]/g, (_, target) =>
      keep(`<span class="embed" title="${target}">${target.split("|")[0]}</span>`));
    s = s.replace(/\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]/g, (_, target, alias) =>
      keep(`<span class="internal-link" title="${target}">${alias || target}</span>`));
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, alt) =>
      keep(`<span class="embed">${alt || "이미지"}</span>`));
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, text, url) =>
      /^(https?:|mailto:)/i.test(url)
        ? keep(`<a class="external-link" href="${url}">${text}</a>`)
        : keep(`<span class="internal-link">${text}</span>`));
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<]+[^\s<.,;:!?)\]'"])/g, (_, pre, url) =>
      pre + keep(`<a class="external-link" href="${url}">${url}</a>`));

    s = s.replace(/\*\*(?=\S)(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^\w])__(?=\S)(.+?)__(?!\w)/g, "$1<strong>$2</strong>");
    s = s.replace(/\*(?=[^\s*])([^*]+?)\*/g, "<em>$1</em>");
    s = s.replace(/(^|[^\w])_(?=[^\s_])([^_]+?)_(?!\w)/g, "$1<em>$2</em>");
    s = s.replace(/~~(?=\S)(.+?)~~/g, "<del>$1</del>");
    s = s.replace(/==(?=\S)(.+?)==/g, "<mark>$1</mark>");
    // 태그: 숫자만으로 된 것(#123)은 태그가 아님
    s = s.replace(/(^|\s)#((?:[\p{L}\p{N}_\-/])*[\p{L}_\-/](?:[\p{L}\p{N}_\-/])*)/gu, (_, pre, tag) =>
      pre + keep(`<span class="tag">#${tag}</span>`));

    for (let guard = 0; guard < 5 && s.includes("\u0000"); guard++) {
      s = s.replace(/\u0000(\d+)\u0000/g, (_, n) => slots[+n]);
    }
    return s;
  }

  // ------------------------------------------------------------ patterns
  const BLANK = /^\s*$/;
  const HEADING = /^(#{1,6})[ \t]+/;
  const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const FENCE = /^[ \t]*(`{3,}|~{3,})[ \t]*([^`\s]*)/;
  const QUOTE = /^((?:[ \t]{0,3}>[ \t]?)+)/;
  const LIST = /^([ \t]*)([-*+]|\d{1,9}[.)])([ \t]+|$)/;
  const TASK = /^\[(.)\](?:[ \t]+|$)/;
  const TABLE_SEP = /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;
  const CALLOUT = /^\[!([\w-]+)\][+-]?[ \t]*/;

  const isList = (line) => LIST.test(line) && !HR.test(line);
  const indentWidth = (ws) => {
    let w = 0;
    for (const ch of ws) w += ch === "\t" ? 4 : 1;
    return w;
  };
  const guides = (lvl) => {
    let h = "";
    for (let k = 0; k < lvl; k++) h += `<span class="lp-guide" style="--k:${k}"></span>`;
    return h;
  };

  // ------------------------------------------------------------ table
  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  }

  function tableHtml(lines) {
    const head = splitRow(lines[0]);
    const aligns = splitRow(lines[1]).map((c) =>
      c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : "");
    const cell = (tag, text, idx) =>
      `<${tag}${aligns[idx] ? ` style="text-align:${aligns[idx]}"` : ""}>${inline(text || "")}</${tag}>`;
    let html = "<table><thead><tr>" + head.map((c, k) => cell("th", c, k)).join("") + "</tr></thead><tbody>";
    for (const line of lines.slice(2)) {
      const row = splitRow(line);
      html += "<tr>" + head.map((_, k) => cell("td", row[k], k)).join("") + "</tr>";
    }
    return html + "</tbody></table>";
  }

  // ------------------------------------------------------------ units
  /**
   * 원문 줄 배열 → 화면 단위 배열.
   * unit: { start, end, cls, style, html, block, indent, textStart, list }
   *  - cls/style/html : 보기 상태의 div
   *  - rawCls         : 편집 중(원문 표시)일 때 div 클래스
   *  - indent         : 편집 시 숨겨두는 앞쪽 들여쓰기 (목록만, 나머지는 "")
   *  - textStart      : 원문에서 .lp-text 에 그려진 글자가 시작하는 위치 (클릭 위치 → 커서 위치 계산용)
   *  - task           : 체크박스 줄이면 true
   */
  function units(lines) {
    const out = [];
    const stack = [];   // 목록 들여쓰기 폭 스택
    let callout = null; // 진행 중인 콜아웃 종류
    let i = 0;

    // YAML frontmatter (옵시디언 속성)
    if (lines[0] === "---") {
      let j = 1;
      while (j < lines.length && lines[j] !== "---") j++;
      if (j < lines.length) {
        out.push({ start: 0, end: j, block: true, cls: "lp-props", rawCls: "lp-raw-block lp-mono",
                   html: `<pre>${esc(lines.slice(1, j).join("\n")) || " "}</pre>` });
        i = j + 1;
      }
    }

    while (i < lines.length) {
      const line = lines[i];
      let m;

      // 코드블록
      if ((m = line.match(FENCE))) {
        let j = i + 1;
        while (j < lines.length && !lines[j].trim().startsWith(m[1])) j++;
        const end = Math.min(j, lines.length - 1);
        const code = lines.slice(i + 1, j).join("\n");
        out.push({ start: i, end, block: true, cls: "lp-code", rawCls: "lp-raw-block lp-mono lp-code-raw",
                   html: `<pre><code${m[2] ? ` class="language-${esc(m[2])}"` : ""}>${esc(code) || " "}</code></pre>` });
        stack.length = 0;
        callout = null;
        i = end + 1;
        continue;
      }

      // 여러 줄 주석 %% ... %% (숨김 대신 흐리게 한 줄로)
      if (/^\s*%%/.test(line) && (line.match(/%%/g) || []).length === 1) {
        let j = i + 1;
        while (j < lines.length && !lines[j].includes("%%")) j++;
        const end = Math.min(j, lines.length - 1);
        out.push({ start: i, end, block: true, cls: "lp-line lp-comment", rawCls: "lp-raw-block",
                   html: `<span class="lp-text">%% 주석 ${end - i + 1}줄 %%</span>` });
        i = end + 1;
        continue;
      }

      // 표
      if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        let j = i + 2;
        while (j < lines.length && !BLANK.test(lines[j]) && lines[j].includes("|")) j++;
        out.push({ start: i, end: j - 1, block: true, cls: "lp-table", rawCls: "lp-raw-block",
                   html: tableHtml(lines.slice(i, j)) });
        stack.length = 0;
        callout = null;
        i = j;
        continue;
      }

      out.push(lineUnit(line, i));
      i++;
    }
    return out;

    function lineUnit(line, i) {
      const u = { start: i, end: i, indent: "", textStart: 0 };

      if (BLANK.test(line)) {
        callout = null;
        return Object.assign(u, { cls: "lp-line lp-blank", rawCls: "lp-line", html: "" });
      }

      let m;
      if ((m = line.match(QUOTE))) {
        const depth = (m[1].match(/>/g) || []).length;
        let rest = line.slice(m[1].length);
        let start = m[1].length;
        let cls = "lp-line lp-quote";
        const c = rest.match(CALLOUT);
        if (c && depth === 1) {
          callout = c[1].toLowerCase();
          start += c[0].length;
          rest = rest.slice(c[0].length) || callout.charAt(0).toUpperCase() + callout.slice(1);
          cls = "lp-line lp-callout lp-callout-title";
        } else if (callout) {
          cls = "lp-line lp-callout";
        }
        stack.length = 0;
        // 인용 안의 목록/체크박스도 그려준다 (들여쓰기 단계는 무시)
        const inner = innerContent(rest);
        return Object.assign(u, {
          cls: cls + inner.cls, rawCls: cls.replace(" lp-callout-title", ""),
          callout: cls.includes("lp-callout") ? callout : undefined,
          style: `--q:${depth}`, html: inner.html, textStart: start + inner.textStart, task: inner.task,
        });
      }
      callout = null;

      if ((m = line.match(HEADING))) {
        stack.length = 0;
        const level = m[1].length;
        const text = line.slice(m[0].length).replace(/[ \t]+#+[ \t]*$/, "");
        return Object.assign(u, { cls: `lp-line lp-h lp-h${level}`, rawCls: `lp-line lp-h lp-h${level}`,
                                  html: `<span class="lp-text">${inline(text)}</span>`, textStart: m[0].length });
      }

      if (HR.test(line)) {
        stack.length = 0;
        return Object.assign(u, { cls: "lp-line lp-hr", rawCls: "lp-line", html: "<hr>" });
      }

      if (isList(line)) {
        const ws = line.match(/^[ \t]*/)[0];
        const w = indentWidth(ws);
        while (stack.length && stack[stack.length - 1] > w) stack.pop();
        if (!stack.length || stack[stack.length - 1] < w) stack.push(w);
        const lvl = stack.length - 1;
        const inner = innerContent(line.slice(ws.length));
        return Object.assign(u, {
          cls: "lp-line lp-li" + inner.cls, rawCls: "lp-line lp-li-raw", style: `--lvl:${lvl}`, list: true,
          html: guides(lvl) + inner.html, guides: guides(lvl), indent: ws, textStart: ws.length + inner.textStart,
          task: inner.task,
        });
      }

      // 목록 항목 아래 들여쓴 이어지는 줄
      if (stack.length && /^[ \t]+\S/.test(line)) {
        const ws = line.match(/^[ \t]*/)[0];
        const lvl = stack.length - 1;
        return Object.assign(u, {
          cls: "lp-line lp-li lp-cont", rawCls: "lp-line lp-li lp-cont", style: `--lvl:${lvl}`, list: true,
          html: guides(lvl) + `<span class="lp-text">${inline(line.slice(ws.length))}</span>`,
          guides: guides(lvl), indent: ws, textStart: ws.length,
        });
      }

      stack.length = 0;
      return Object.assign(u, { cls: "lp-line", rawCls: "lp-line", html: `<span class="lp-text">${inline(line)}</span>` });
    }
  }

  /** 들여쓰기·인용 기호를 뗀 한 줄 내용: 목록 기호/체크박스/제목 + 본문 */
  function innerContent(text) {
    let m = text.match(LIST);
    if (m && !HR.test(text)) {
      let rest = text.slice(m[0].length);
      let start = m[0].length;
      const t = rest.match(TASK);
      if (t) {
        const checked = t[1] !== " ";
        start += t[0].length;
        return {
          cls: " lp-mk lp-task" + (checked ? " is-checked" : ""), textStart: start, task: true,
          html: `<span class="task-checkbox${checked ? " checked" : ""}"></span>` +
                `<span class="lp-text">${inline(text.slice(start))}</span>`,
        };
      }
      const ordered = /\d/.test(m[2]);
      const marker = ordered ? `<span class="lp-num">${esc(m[2])}</span>` : `<span class="lp-bullet"></span>`;
      return { cls: " lp-mk", textStart: start, html: marker + `<span class="lp-text">${inline(rest)}</span>` };
    }
    if ((m = text.match(HEADING))) {
      return { cls: ` lp-h lp-h${m[1].length}`, textStart: m[0].length,
               html: `<span class="lp-text">${inline(text.slice(m[0].length))}</span>` };
    }
    return { cls: "", textStart: 0, html: `<span class="lp-text">${inline(text)}</span>` };
  }

  global.ObsidianMarkdown = { units, inline };
})(window);
