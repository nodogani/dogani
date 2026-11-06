/*
 * Dogani 라이브 프리뷰 편집기 (CodeMirror 6 — 옵시디언 편집기와 같은 기반)
 *
 * 옵시디언 라이브 프리뷰 흉내:
 *  - 노트 전체가 편집기 하나. 커서/선택이 닿은 서식만 원문 기호(**, ==, [[ ]], # ...)가 보이고 나머지는 렌더링
 *  - 체크박스·글머리표·들여쓰기 가이드·구분선은 위젯, 표는 커서가 밖에 있으면 표로
 *  - 편집기에 포커스가 없으면(읽기 전용 포함) 전부 렌더링된 상태
 *
 * window.DoganiEditor.create(parent, opts) 로 생성 (app.js 에서 사용). 빌드: app/editor/build.bat
 */
import { EditorState, StateField, StateEffect, Compartment, Transaction } from "@codemirror/state";
import { EditorView, Decoration, ViewPlugin, WidgetType, keymap, drawSelection } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentMore, indentLess, insertTab } from "@codemirror/commands";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { syntaxTree, ensureSyntaxTree, indentUnit } from "@codemirror/language";

// ------------------------------------------------------------ focus (상태에 넣어야 표 위젯 StateField 도 반응)
const setFocus = StateEffect.define();
const focusField = StateField.define({
  create: () => false,
  update: (v, tr) => {
    for (const e of tr.effects) if (e.is(setFocus)) v = e.value;
    return v;
  },
});
const focusSync = EditorView.focusChangeEffect.of((_, focusing) => setFocus.of(focusing));

/** 선택(커서)이 [from, to] 에 닿는지. 포커스가 없으면 항상 false → 전부 렌더링 */
function toucher(state) {
  const focused = state.field(focusField, false);
  const ranges = state.selection.ranges;
  return (from, to) => focused && ranges.some((r) => r.from <= to && r.to >= from);
}

// ------------------------------------------------------------ widgets
class Checkbox extends WidgetType {
  constructor(checked) { super(); this.checked = checked; }
  eq(o) { return o.checked === this.checked; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-task-wrap";
    s.innerHTML = `<span class="task-checkbox${this.checked ? " checked" : ""}"></span>`;
    return s;
  }
  ignoreEvent() { return false; }
}
class Bullet extends WidgetType {
  eq() { return true; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-bullet-wrap";
    s.innerHTML = '<span class="lp-bullet"></span>';
    return s;
  }
}
/** 목록 앞 들여쓰기(탭/스페이스) → 단계마다 고정 폭 + 가이드선 (옵시디언처럼) */
class Indent extends WidgetType {
  constructor(lvl) { super(); this.lvl = lvl; }
  eq(o) { return o.lvl === this.lvl; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-indent";
    s.style.width = `calc(${this.lvl} * var(--ind))`;
    for (let k = 0; k < this.lvl; k++) s.insertAdjacentHTML("beforeend", `<span class="cm-guide" style="--k:${k}"></span>`);
    return s;
  }
}
class Rule extends WidgetType {
  eq() { return true; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-hr";
    return s;
  }
}
class Label extends WidgetType {
  constructor(text, cls) { super(); this.text = text; this.cls = cls; }
  eq(o) { return o.text === this.text && o.cls === this.cls; }
  toDOM() {
    const s = document.createElement("span");
    s.className = this.cls;
    s.textContent = this.text;
    return s;
  }
}
class Table extends WidgetType {
  constructor(text, html, from) { super(); this.text = text; this.html = html; this.from = from; }
  eq(o) { return o.text === this.text; }
  toDOM(view) {
    const d = document.createElement("div");
    d.className = "cm-table-widget";
    d.innerHTML = this.html(this.text.split("\n"));
    d.addEventListener("mousedown", (e) => {
      if (!view.state.facet(EditorView.editable) || e.target.closest("a")) return;
      e.preventDefault();
      view.dispatch({ selection: { anchor: this.from } });
      view.focus();
    });
    return d;
  }
  ignoreEvent() { return true; }
}

const hide = Decoration.replace({});
const line = (cls, attrs) => Decoration.line({ class: cls, attributes: attrs });
const mark = (cls, attrs) => Decoration.mark({ class: cls, attributes: attrs });

// ------------------------------------------------------------ frontmatter
function frontmatterEnd(doc) {
  if (doc.lines < 2 || doc.line(1).text !== "---") return -1;
  for (let n = 2; n <= doc.lines; n++) if (doc.line(n).text === "---") return doc.line(n).to;
  return -1;
}

// ------------------------------------------------------------ inline (lezer 가 모르는 옵시디언 문법)
const OBS_INLINE = [
  // %%주석%%: 커서가 닿을 때만 흐리게 보임
  { re: /%%.*?%%/g, run: (d, m, f, t, touched) => d.push((touched(f, t) ? mark("cm-comment") : hide).range(f, t)) },
  // ==하이라이트==
  { re: /==(?=\S)(.+?)==/g, run: (d, m, f, t, touched) => {
    d.push(mark("cm-highlight").range(f, t));
    if (!touched(f, t)) d.push(hide.range(f, f + 2), hide.range(t - 2, t));
  } },
  // [[위키링크|별칭]] / ![[임베드]]
  { re: /(!?)\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]/g, run: (d, m, f, t, touched) => {
    const embed = !!m[1];
    const cls = embed ? "cm-embed" : "cm-ilink";
    if (touched(f, t)) {
      d.push(mark(cls).range(f, t));
      return;
    }
    const open = f + m[1].length + 2;
    const textFrom = m[3] ? open + m[2].length + 1 : open;
    d.push(hide.range(f, textFrom), mark(cls, { title: m[2] }).range(textFrom, t - 2), hide.range(t - 2, t));
  } },
  // #태그 (숫자만은 태그 아님)
  { re: /(^|\s)(#(?:[\p{L}\p{N}_\-/])*[\p{L}_\-/](?:[\p{L}\p{N}_\-/])*)/gu, run: (d, m, f, t) => {
    d.push(mark("cm-tag").range(f + m[1].length, t));
  } },
];

const HEADINGS = { ATXHeading1: 1, ATXHeading2: 2, ATXHeading3: 3, ATXHeading4: 4, ATXHeading5: 5, ATXHeading6: 6 };
const INLINE_CLS = { Emphasis: "cm-em", StrongEmphasis: "cm-strong", Strikethrough: "cm-strike", InlineCode: "cm-inline-code" };
const INLINE_MARKS = { Emphasis: "EmphasisMark", StrongEmphasis: "EmphasisMark", Strikethrough: "StrikethroughMark", InlineCode: "CodeMark" };
const CALLOUT = /^\s*>\s*\[!([\w-]+)\][+-]?[ \t]*/;

function build(view) {
  const { state } = view;
  const doc = state.doc;
  const touched = toucher(state);
  const lineTouched = (pos) => { const l = doc.lineAt(pos); return touched(l.from, l.to); };
  const decos = [];
  const atomic = [];
  const skip = [];          // 정규식 인라인을 적용하지 않을 범위 (코드, 표, URL ...)
  const fm = frontmatterEnd(doc);

  if (fm > 0) {
    skip.push([0, fm]);
    for (let p = 0; p <= fm;) { const l = doc.lineAt(p); decos.push(line("cm-fm").range(l.from)); p = l.to + 1; }
  }

  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from, to,
      enter(node) {
        const name = node.name;
        if (name === "Document") return;
        if (node.to <= fm) return false;

        if (HEADINGS[name]) {
          const l = doc.lineAt(node.from);
          decos.push(line(`cm-h cm-h${HEADINGS[name]}`).range(l.from));
          if (!touched(l.from, l.to)) {
            const c = node.node.cursor();
            let first = true;
            if (c.firstChild()) do {
              if (c.name !== "HeaderMark") continue;
              // 앞쪽 "## " 은 공백까지, 뒤쪽 닫는 " ##" 은 앞 공백까지
              if (first) decos.push(hide.range(c.from, Math.min(c.to + 1, l.to)));
              else decos.push(hide.range(Math.max(l.from, c.from - 1), c.to));
              first = false;
            } while (c.nextSibling());
          }
          return;
        }
        if (name === "SetextHeading1" || name === "SetextHeading2") {
          const l = doc.lineAt(node.from);
          decos.push(line(`cm-h cm-h${name.endsWith("1") ? 1 : 2}`).range(l.from));
          return;
        }

        if (INLINE_CLS[name]) {
          decos.push(mark(INLINE_CLS[name]).range(node.from, node.to));
          if (name === "InlineCode") skip.push([node.from, node.to]);
          if (!touched(node.from, node.to)) {
            const c = node.node.cursor();
            if (c.firstChild()) do { if (c.name === INLINE_MARKS[name]) decos.push(hide.range(c.from, c.to)); } while (c.nextSibling());
          }
          return;
        }

        if (name === "Link" || name === "Image") {
          const url = node.node.getChild("URL");
          if (!url) return;      // [x] 같은 참조 링크 모양은 그대로
          const href = doc.sliceString(url.from, url.to);
          skip.push([node.from, node.to]);
          const marks = node.node.getChildren("LinkMark");
          if (marks.length < 2) return false;
          const textFrom = marks[0].to, textTo = marks[1].from;
          if (touched(node.from, node.to)) {
            decos.push(mark("cm-link").range(textFrom, Math.max(textFrom, textTo)));
          } else if (name === "Image") {
            decos.push(Decoration.replace({ widget: new Label(doc.sliceString(textFrom, textTo) || "이미지", "cm-embed") }).range(node.from, node.to));
          } else if (textTo > textFrom) {
            decos.push(hide.range(node.from, textFrom),
              mark("cm-link cm-link-rendered", { "data-href": href, title: href }).range(textFrom, textTo),
              hide.range(textTo, node.to));
          }
          return false;
        }
        if (name === "URL") {
          // 맨 URL (자동 링크)
          skip.push([node.from, node.to]);
          decos.push(mark("cm-link cm-url", { "data-href": doc.sliceString(node.from, node.to) }).range(node.from, node.to));
          return;
        }

        if (name === "HorizontalRule") {
          if (!lineTouched(node.from)) decos.push(Decoration.replace({ widget: new Rule() }).range(node.from, node.to));
          return;
        }

        if (name === "Blockquote") {
          if (node.node.parent && node.node.parent.name !== "Document" && hasAncestor(node.node, "Blockquote")) return;
          const first = doc.lineAt(node.from);
          const c = first.text.match(CALLOUT);
          const type = c && c[1].toLowerCase();
          for (let p = node.from; p <= node.to;) {
            const l = doc.lineAt(p);
            const depth = (l.text.match(/^(\s*>\s?)+/) || [""])[0].split(">").length - 1;
            const cls = type ? "cm-callout" + (l.number === first.number ? " cm-callout-title" : "") : "cm-quote";
            decos.push(line(cls, { style: `--q:${Math.max(depth, 1)}`, ...(type ? { "data-callout": type } : {}) }).range(l.from));
            p = l.to + 1;
          }
          if (type && !touched(first.from, first.to)) {
            const start = first.from + c[0].length;
            if (start >= first.to) {
              decos.push(Decoration.replace({ widget: new Label(type.charAt(0).toUpperCase() + type.slice(1), "") }).range(first.from, first.to));
            } else {
              decos.push(hide.range(first.from, start));
            }
          }
          return;
        }
        if (name === "QuoteMark") {
          if (!lineTouched(node.from)) {
            const next = doc.sliceString(node.to, node.to + 1);
            decos.push(hide.range(node.from, next === " " ? node.to + 1 : node.to));
          }
          return;
        }

        if (name === "ListItem") {
          const l = doc.lineAt(node.from);
          const lvl = listDepth(node.node);
          const ws = l.text.match(/^[ \t]*/)[0].length;
          const quote = /^\s*>/.test(l.text);
          if (!quote) {
            if (ws) {
              decos.push(Decoration.replace({ widget: new Indent(lvl) }).range(l.from, l.from + ws));
              atomic.push([l.from, l.from + ws]);
            }
            decos.push(line("cm-li", { style: `--lvl:${lvl}` }).range(l.from));
          }
          const markNode = node.node.getChild("ListMark");
          const task = node.node.getChild("Task");
          const tm = task && task.getChild("TaskMarker");
          if (markNode && tm) {
            const checked = doc.sliceString(tm.from + 1, tm.to - 1) !== " ";
            const end = doc.sliceString(tm.to, tm.to + 1) === " " ? tm.to + 1 : tm.to;
            decos.push(line("cm-task" + (checked ? " is-checked" : "")).range(l.from));
            if (!touched(markNode.from, end)) {
              decos.push(Decoration.replace({ widget: new Checkbox(checked) }).range(markNode.from, end));
            }
            if (checked && end < l.to) decos.push(mark("cm-checked-text").range(end, l.to));
          } else if (markNode) {
            const ordered = /\d/.test(doc.sliceString(markNode.from, markNode.to));
            const end = doc.sliceString(markNode.to, markNode.to + 1) === " " ? markNode.to + 1 : markNode.to;
            if (ordered) decos.push(mark("cm-num").range(markNode.from, markNode.to));
            else if (!touched(markNode.from, end)) decos.push(Decoration.replace({ widget: new Bullet() }).range(markNode.from, end));
            if (!quote) decos.push(line(ordered ? "cm-li-ol" : "cm-li-ul").range(l.from));
          }
          return;
        }

        if (name === "FencedCode" || name === "CodeBlock") {
          skip.push([node.from, node.to]);
          const active = touched(node.from, node.to);
          const firstL = doc.lineAt(node.from).number, lastL = doc.lineAt(node.to).number;
          for (let n = firstL; n <= lastL; n++) {
            const l = doc.line(n);
            let cls = "cm-codeblock";
            if (n === firstL) cls += " cm-codeblock-begin";
            if (n === lastL) cls += " cm-codeblock-end";
            const fence = name === "FencedCode" && (n === firstL || (n === lastL && /^\s*(```|~~~)/.test(l.text)));
            if (fence) {
              cls += " cm-fence";
              if (!active && l.to > l.from) {
                const info = n === firstL ? l.text.replace(/^\s*(`{3,}|~{3,})\s*/, "") : "";
                decos.push(Decoration.replace(info ? { widget: new Label(info, "cm-code-info") } : {}).range(l.from, l.to));
              }
            }
            decos.push(line(cls).range(l.from));
          }
          return false;
        }

        if (name === "Table") {
          skip.push([node.from, node.to]);
          for (let p = node.from; p <= node.to;) { const l = doc.lineAt(p); decos.push(line("cm-table-raw").range(l.from)); p = l.to + 1; }
          return false;
        }
        if (name === "HTMLBlock" || name === "CommentBlock") {
          skip.push([node.from, node.to]);
          return false;
        }
      },
    });

    // 옵시디언 전용 인라인 문법: 줄 단위 정규식
    for (let p = from; p <= to;) {
      const l = doc.lineAt(p);
      p = l.to + 1;
      if (!l.text || inRanges(skip, l.from, l.to, true)) continue;
      for (const rule of OBS_INLINE) {
        rule.re.lastIndex = 0;
        let m;
        while ((m = rule.re.exec(l.text))) {
          const f = l.from + m.index, t = f + m[0].length;
          if (!m[0].length) { rule.re.lastIndex++; continue; }
          if (inRanges(skip, f, t, false)) continue;
          rule.run(decos, m, f, t, touched);
          if (rule.re === OBS_INLINE[0].re) skip.push([f, t]);
        }
      }
    }
  }

  return { decorations: Decoration.set(decos, true), atomic: Decoration.set(atomic.map(([f, t]) => hide.range(f, t)), true) };
}

function inRanges(ranges, from, to, whole) {
  return ranges.some(([f, t]) => (whole ? f <= from && to <= t : f < to && from < t));
}
function hasAncestor(node, name) {
  for (let p = node.parent; p; p = p.parent) if (p.name === name) return true;
  return false;
}
function listDepth(node) {
  let d = 0;
  for (let p = node.parent; p; p = p.parent) if (p.name === "ListItem") d++;
  return d;
}

const livePreview = ViewPlugin.fromClass(class {
  constructor(view) { Object.assign(this, build(view)); }
  update(u) {
    if (u.docChanged || u.viewportChanged || u.selectionSet || u.focusChanged ||
        u.transactions.some((tr) => tr.effects.some((e) => e.is(setFocus))) ||
        syntaxTree(u.startState) !== syntaxTree(u.state)) {
      Object.assign(this, build(u.view));
    }
  }
}, {
  decorations: (v) => v.decorations,
  provide: (p) => EditorView.atomicRanges.of((view) => (view.plugin(p) ? view.plugin(p).atomic : Decoration.none)),
});

// ------------------------------------------------------------ table (블록 위젯은 StateField 에서만 가능)
function tableField(html) {
  const make = (state) => {
    const touched = toucher(state);
    const tree = ensureSyntaxTree(state, state.doc.length, 200) || syntaxTree(state);
    const decos = [];
    tree.iterate({
      enter(node) {
        if (node.name !== "Table") return;
        const from = state.doc.lineAt(node.from).from, to = state.doc.lineAt(node.to).to;
        if (!touched(from, to)) {
          decos.push(Decoration.replace({ widget: new Table(state.doc.sliceString(from, to), html, from), block: true }).range(from, to));
        }
        return false;
      },
    });
    return Decoration.set(decos, true);
  };
  return StateField.define({
    create: make,
    update: (v, tr) => (tr.docChanged || tr.selection || tr.effects.some((e) => e.is(setFocus)) ? make(tr.state) : v),
    provide: (f) => EditorView.decorations.from(f),
  });
}

// ------------------------------------------------------------ commands
const isListLine = (state) => /^\s*(?:>\s*)*([-*+]|\d+[.)])(\s|$)/.test(state.doc.lineAt(state.selection.main.head).text);
const tab = (view) => (isListLine(view.state) || !view.state.selection.main.empty ? indentMore(view) : insertTab(view));

function toggleTaskAt(view, pos) {
  const l = view.state.doc.lineAt(pos);
  const m = l.text.match(/^((?:[ \t]{0,3}>[ \t]?)*[ \t]*(?:[-*+]|\d+[.)])[ \t]+\[)(.)\]/);
  if (!m) return false;
  const at = l.from + m[1].length;
  view.dispatch({ changes: { from: at, to: at + 1, insert: m[2] === " " ? "x" : " " }, userEvent: "input.toggle" });
  return true;
}

/** 탭/스페이스 들여쓰기 중 노트가 이미 쓰고 있는 것 */
function detectIndent(text) {
  if (/^\t+\S/m.test(text)) return "\t";
  if (/^ {4}(?:[-*+]|\d+[.)])\s/m.test(text)) return "    ";
  if (/^ {2}(?:[-*+]|\d+[.)])\s/m.test(text)) return "  ";
  return "\t";
}

// ------------------------------------------------------------ public
function create(parent, opts) {
  const ro = new Compartment();
  const ind = new Compartment();
  const readOnlyExt = (r) => [EditorState.readOnly.of(r), EditorView.editable.of(!r)];
  const call = (name, ...a) => opts[name] && opts[name](...a);
  const table = tableField(opts.tableHtml || ((l) => l.join("<br>")));

  const extensions = (text, r) => [
    focusField, focusSync,
    history(),
    drawSelection(),
    EditorView.lineWrapping,
    markdown({ base: markdownLanguage }),
    ind.of(indentUnit.of(detectIndent(text))),
    EditorState.tabSize.of(4),
    keymap.of([
      { key: "Mod-s", run: () => { call("onSave"); return true; } },
      { key: "Escape", run: (v) => { v.contentDOM.blur(); return true; } },
      { key: "Tab", run: tab, shift: indentLess },
      ...historyKeymap,
      ...defaultKeymap,
    ]),
    livePreview,
    table,
    ro.of(readOnlyExt(r)),
    EditorView.updateListener.of((u) => {
      if (u.docChanged) call("onChange", u.state.doc.toString(), u.transactions.some((tr) => tr.isUserEvent("input.toggle")));
      if (u.focusChanged) call(u.view.hasFocus ? "onFocus" : "onBlur");
    }),
    EditorView.domEventHandlers({
      mousedown(e, view) {
        const box = e.target.closest(".cm-task-wrap");
        if (box) {
          e.preventDefault();
          if (view.state.facet(EditorView.editable)) toggleTaskAt(view, view.posAtDOM(box));
          return true;
        }
        const link = e.target.closest("[data-href]");
        if (link && e.button === 0 && (link.classList.contains("cm-link-rendered") || e.ctrlKey || !view.state.facet(EditorView.editable))) {
          e.preventDefault();
          call("openUrl", link.dataset.href);
          return true;
        }
        return false;
      },
    }),
  ];

  let readOnly = !!opts.readOnly;
  const view = new EditorView({ parent, state: EditorState.create({ doc: opts.doc || "", extensions: extensions(opts.doc || "", readOnly) }) });

  return {
    view,
    getDoc: () => view.state.doc.toString(),
    /** 문서 통째로 교체 (날짜 이동·외부 변경). 실행 취소 기록도 새로 */
    setDoc(text, keepScroll) {
      const top = view.scrollDOM.scrollTop;
      view.setState(EditorState.create({ doc: text, extensions: extensions(text, readOnly) }));
      view.scrollDOM.scrollTop = keepScroll ? top : 0;
    },
    setReadOnly(r) {
      readOnly = !!r;
      view.dispatch({ effects: ro.reconfigure(readOnlyExt(readOnly)) });
    },
    focus(pos) {
      const at = pos === undefined ? view.state.doc.length : Math.min(pos, view.state.doc.length);
      view.dispatch({ selection: { anchor: at }, scrollIntoView: true });
      view.focus();
    },
    blur() { view.contentDOM.blur(); },
    hasFocus: () => view.hasFocus,
  };
}

window.DoganiEditor = { create };
