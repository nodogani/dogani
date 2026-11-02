/*
 * 옵시디언 읽기 보기(Reading view)에 최대한 맞춘 경량 마크다운 렌더러 (외부 라이브러리 없음, 사내망 오프라인용)
 *
 * 지원: 제목, 체크리스트(중첩/완료 취소선), 순서/비순서 목록, 인용, 콜아웃, 코드블록, 표, 구분선,
 *       **굵게** *기울임* ~~취소선~~ ==하이라이트== `코드` #태그 [[위키링크]] ![[임베드]] [링크](url) %%주석%%
 * 줄바꿈: 옵시디언 기본값(Strict line breaks 꺼짐)처럼 한 줄 개행도 <br>로 표시
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

  const inlineLines = (lines) => lines.map(inline).join("<br>");

  // ------------------------------------------------------------ block helpers
  const BLANK = /^\s*$/;
  const HEADING = /^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
  const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const FENCE = /^[ \t]*(`{3,}|~{3,})[ \t]*([^`\s]*)/;
  const QUOTE = /^[ \t]{0,3}>/;
  const LIST = /^([ \t]*)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$/;
  const TASK = /^\[(.)\](?:[ \t]+(.*))?$/;
  const TABLE_SEP = /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;

  const isList = (line) => LIST.test(line) && !HR.test(line);
  const isBlockStart = (line) =>
    HEADING.test(line) || HR.test(line) || FENCE.test(line) || QUOTE.test(line) || isList(line);

  const indentWidth = (ws) => {
    let w = 0;
    for (const ch of ws) w += ch === "\t" ? 4 : 1;
    return w;
  };

  // ------------------------------------------------------------ lists
  function parseList(lines, i) {
    const root = { indent: -1, children: [] };
    const stack = [root];
    let last = null;

    while (i < lines.length) {
      const line = lines[i];
      if (isList(line)) {
        const m = line.match(LIST);
        const indent = indentWidth(m[1]);
        while (stack.length > 1 && stack[stack.length - 1].indent >= indent) stack.pop();
        let rest = m[3] || "";
        let task = null;
        const t = rest.match(TASK);
        if (t) {
          task = t[1];
          rest = t[2] || "";
        }
        const item = {
          indent,
          ordered: /\d/.test(m[2]),
          number: parseInt(m[2], 10),
          task,
          lines: [rest],
          children: [],
        };
        stack[stack.length - 1].children.push(item);
        stack.push(item);
        last = item;
        i++;
        continue;
      }
      if (BLANK.test(line) || isBlockStart(line)) break;
      // 들여쓴 이어지는 줄 / lazy continuation 은 직전 항목에 붙인다
      last.lines.push(line.trim());
      i++;
    }
    return { html: renderItems(root.children), next: i };
  }

  function renderItems(items) {
    let html = "";
    let k = 0;
    while (k < items.length) {
      const ordered = items[k].ordered;
      const start = items[k].number;
      let run = "";
      while (k < items.length && items[k].ordered === ordered) {
        run += renderItem(items[k]);
        k++;
      }
      html += ordered
        ? `<ol${start !== 1 ? ` start="${start}"` : ""}>${run}</ol>`
        : `<ul>${run}</ul>`;
    }
    return html;
  }

  function renderItem(item) {
    const body = inlineLines(item.lines);
    const children = item.children.length ? renderItems(item.children) : "";
    if (item.task === null) return `<li>${body}${children}</li>`;
    const checked = item.task !== " ";
    const cls = "task-list-item" + (checked ? " is-checked" : "");
    return (
      `<li class="${cls}" data-task="${esc(item.task)}">` +
      `<span class="task-checkbox${checked ? " checked" : ""}"></span>` +
      `<span class="task-text">${body}</span>${children}</li>`
    );
  }

  // ------------------------------------------------------------ table
  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  }

  function parseTable(lines, i) {
    const head = splitRow(lines[i]);
    const aligns = splitRow(lines[i + 1]).map((c) =>
      c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : "");
    const cell = (tag, text, idx) =>
      `<${tag}${aligns[idx] ? ` style="text-align:${aligns[idx]}"` : ""}>${inline(text || "")}</${tag}>`;
    let html = "<table><thead><tr>" + head.map((c, k) => cell("th", c, k)).join("") + "</tr></thead><tbody>";
    i += 2;
    while (i < lines.length && !BLANK.test(lines[i]) && lines[i].includes("|")) {
      const row = splitRow(lines[i]);
      html += "<tr>" + head.map((_, k) => cell("td", row[k], k)).join("") + "</tr>";
      i++;
    }
    return { html: html + "</tbody></table>", next: i };
  }

  // ------------------------------------------------------------ blocks
  function renderBlocks(lines) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let m;

      if (BLANK.test(line)) {
        i++;
        continue;
      }

      if ((m = line.match(FENCE))) {
        const fence = m[1];
        const lang = m[2];
        const code = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) code.push(lines[i++]);
        i++;
        out.push(`<pre><code${lang ? ` class="language-${esc(lang)}"` : ""}>${esc(code.join("\n"))}</code></pre>`);
        continue;
      }

      if ((m = line.match(HEADING))) {
        const level = m[1].length;
        out.push(`<h${level}>${inline(m[2])}</h${level}>`);
        i++;
        continue;
      }

      if (HR.test(line)) {
        out.push("<hr>");
        i++;
        continue;
      }

      if (QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length && QUOTE.test(lines[i])) {
          inner.push(lines[i].replace(/^[ \t]{0,3}>[ \t]?/, ""));
          i++;
        }
        const callout = inner[0].match(/^\[!([\w-]+)\]([+-]?)[ \t]*(.*)$/);
        if (callout) {
          const type = callout[1].toLowerCase();
          const title = callout[3] || type.charAt(0).toUpperCase() + type.slice(1);
          out.push(
            `<div class="callout" data-callout="${esc(type)}">` +
              `<div class="callout-title">${inline(title)}</div>` +
              `<div class="callout-content">${renderBlocks(inner.slice(1))}</div></div>`);
        } else {
          out.push(`<blockquote>${renderBlocks(inner)}</blockquote>`);
        }
        continue;
      }

      if (isList(line)) {
        const r = parseList(lines, i);
        out.push(r.html);
        i = r.next;
        continue;
      }

      if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        const r = parseTable(lines, i);
        out.push(r.html);
        i = r.next;
        continue;
      }

      const para = [];
      while (i < lines.length && !BLANK.test(lines[i]) && !isBlockStart(lines[i])) {
        para.push(lines[i].trim());
        i++;
      }
      out.push(`<p>${inlineLines(para)}</p>`);
    }
    return out.join("\n");
  }

  function render(md) {
    let text = md.replace(/\r\n?/g, "\n");
    // YAML frontmatter 숨김
    text = text.replace(/^---\n[\s\S]*?\n---(?:\n|$)/, "");
    // 옵시디언 주석 %% ... %% 숨김
    text = text.replace(/%%[\s\S]*?%%/g, "");
    return renderBlocks(text.split("\n"));
  }

  global.ObsidianMarkdown = { render, inline };
})(window);
