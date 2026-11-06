/*
 * 표 위젯용 경량 마크다운 HTML 변환 (외부 라이브러리 없음, 사내망 오프라인용)
 * 편집기 본문은 vendor/editor.js (CodeMirror 라이브 프리뷰) 가 그리고, 여기는 표 칸 안의 인라인 서식만 담당.
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

  global.ObsidianMarkdown = { inline, tableHtml };
})(window);
