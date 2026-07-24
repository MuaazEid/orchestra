/* Orchestra — minimal Markdown renderer (no dependencies, no network).
 *
 * Why not marked.js + DOMPurify from a CDN: Orchestra's entire premise is
 * that it runs with no cloud and no network. A UI that blocks on jsdelivr
 * is a UI that breaks on a plane, behind a corporate proxy, or on the
 * air-gapped machine this project was built for. So: ~200 lines, vendored.
 *
 * Safety model — escape first, then build:
 *   1. The whole source is HTML-escaped BEFORE any markup is generated.
 *   2. Every tag in the output is emitted by this file, never by the model.
 *   3. The only attacker-controlled attribute is a link href, which must
 *      match SAFE_URL (http/https/mailto/relative) or the link is dropped.
 * There is no path by which model output becomes live HTML.
 */
(function (global) {
  'use strict';

  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ESCAPES[c];
    });
  }

  var SAFE_URL = /^(?:https?:\/\/|mailto:|#|\/)[^\s]*$/i;

  function safeUrl(raw) {
    var url = raw.trim().replace(/&amp;/g, '&');
    if (!SAFE_URL.test(url)) return null;
    return url.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
  }

  function link(href, text) {
    return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
  }

  /* ── Inline span formatting ─────────────────────────────────────── */
  function inline(s) {
    // [text](url) — the optional "title" is already escaped to &quot;
    s = s.replace(/\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g,
      function (whole, text, href) {
        var url = safeUrl(href);
        return url ? link(url, text || url) : whole;
      });

    // Bare URLs. Anything we emitted above sits after '"' or '>', never
    // after whitespace, so this pass cannot double-wrap its own output.
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, function (whole, pre, raw) {
      var url = safeUrl(raw);
      return url ? pre + link(url, raw) : whole;
    });

    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^\w])__([^_]+)__(?!\w)/g, '$1<strong>$2</strong>');
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    return s;
  }

  /* ── Block elements ─────────────────────────────────────────────── */
  function codeBlock(fence) {
    var lang = fence.lang || 'text';
    return '<div class="code-block" data-lang="' + escapeHtml(lang) + '">' +
      '<div class="code-bar"><span class="code-lang">' + escapeHtml(lang) + '</span>' +
      '<button type="button" class="code-copy" data-copy-code>Copy</button></div>' +
      '<pre><code>' + fence.code + '</code></pre></div>';
  }

  function cells(line) {
    return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|');
  }

  function looksLikeTable(lines) {
    return lines.length >= 2 &&
      lines[0].indexOf('|') !== -1 &&
      /^[\s|:-]+$/.test(lines[1]) && lines[1].indexOf('-') !== -1;
  }

  function table(lines) {
    var head = cells(lines[0]);
    var aligns = cells(lines[1]).map(function (c) {
      var t = c.trim();
      if (t.charAt(0) === ':' && t.slice(-1) === ':') return 'center';
      return t.slice(-1) === ':' ? 'right' : 'left';
    });
    var html = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
    head.forEach(function (c, i) {
      html += '<th style="text-align:' + (aligns[i] || 'left') + '">' + inline(c.trim()) + '</th>';
    });
    html += '</tr></thead><tbody>';
    lines.slice(2).forEach(function (row) {
      if (!row.trim()) return;
      html += '<tr>';
      cells(row).forEach(function (c, i) {
        html += '<td style="text-align:' + (aligns[i] || 'left') + '">' + inline(c.trim()) + '</td>';
      });
      html += '</tr>';
    });
    return html + '</tbody></table></div>';
  }

  var ITEM = /^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$/;

  function listItem(text) {
    // GitHub-style task list, rendered read-only.
    var task = text.match(/^\[([ xX])\]\s+(.*)$/);
    if (task) {
      return '<span class="md-task' + (task[1] === ' ' ? '' : ' done') + '">' +
        (task[1] === ' ' ? '\u25A2' : '\u2611') + '</span> ' + inline(task[2]);
    }
    return inline(text);
  }

  function list(lines) {
    var tag = /^\s*\d+[.)]\s/.test(lines[0]) ? 'ol' : 'ul';
    var html = '<' + tag + ' class="md-list">';
    var itemOpen = false, subTag = null;

    lines.forEach(function (raw) {
      var m = raw.match(ITEM);
      if (!m) {                                   // wrapped continuation line
        if (itemOpen) html += ' ' + inline(raw.trim());
        return;
      }
      var content = listItem(m[2]);
      if (m[1].length >= 2) {                     // one level of nesting
        if (!subTag) {
          subTag = /^\s*\d+[.)]\s/.test(raw) ? 'ol' : 'ul';
          html += '<' + subTag + ' class="md-list">';
        }
        html += '<li>' + content + '</li>';
        return;
      }
      if (subTag) { html += '</' + subTag + '>'; subTag = null; }
      if (itemOpen) html += '</li>';
      html += '<li>' + content;
      itemOpen = true;
    });

    if (subTag) html += '</' + subTag + '>';
    if (itemOpen) html += '</li>';
    return html + '</' + tag + '>';
  }

  function block(b) {
    if (/^\u0000F\d+\u0000$/.test(b)) return b;             // fenced code
    var lines = b.split('\n');

    var heading = lines[0].match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      var level = heading[1].length;
      var html = '<h' + level + ' class="md-h md-h' + level + '">' +
        inline(heading[2].trim()) + '</h' + level + '>';
      var rest = lines.slice(1).join('\n').trim();
      return rest ? html + block(rest) : html;
    }

    if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(b.trim())) return '<hr class="md-hr">';

    if (lines.every(function (l) { return /^\s*&gt;/.test(l); })) {
      var inner = lines.map(function (l) {
        return l.trim().replace(/^&gt;\s?/, '');
      }).join('\n').trim();
      return '<blockquote class="md-quote">' + (inner ? block(inner) : '') + '</blockquote>';
    }

    if (looksLikeTable(lines)) return table(lines);
    if (ITEM.test(lines[0])) return list(lines);

    return '<p>' + inline(b).replace(/\n/g, '<br>') + '</p>';
  }

  /* ── Entry point ────────────────────────────────────────────────── */
  function render(src) {
    var fences = [], codes = [];
    var s = escapeHtml(String(src == null ? '' : src).replace(/\r\n?/g, '\n'));

    // Pull fenced blocks out first so nothing else rewrites their contents.
    s = s.replace(/(^|\n)```([\w+#.-]*)[ \t]*\n([\s\S]*?)(?:\n```|$)/g,
      function (whole, pre, lang, code) {
        fences.push({ lang: lang, code: code.replace(/\n+$/, '') });
        return pre + '\u0000F' + (fences.length - 1) + '\u0000';
      });

    s = s.replace(/`([^`\n]+)`/g, function (whole, code) {
      codes.push(code);
      return '\u0000C' + (codes.length - 1) + '\u0000';
    });

    var html = s.split(/\n{2,}/)
      .map(function (b) { return b.trim(); })
      .filter(Boolean)
      .map(block)
      .join('\n');

    html = html.replace(/\u0000C(\d+)\u0000/g, function (whole, i) {
      return '<code class="md-inline-code">' + codes[+i] + '</code>';
    });
    return html.replace(/\u0000F(\d+)\u0000/g, function (whole, i) {
      return codeBlock(fences[+i]);
    });
  }

  global.MD = { render: render, escapeHtml: escapeHtml };
})(typeof window !== 'undefined' ? window : globalThis);
