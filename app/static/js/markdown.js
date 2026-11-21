/**
 * Purpose: Lightweight, XSS-safe Markdown renderer supporting paragraphs, code blocks, inline code, emphasis, and lists.
 * Role: component
 * Patterns: none
 * Depends on: none
 * Exposes: escapeHtml, renderMarkdown
 * Tests: none
 * Invariants: Escapes all raw HTML before formatting so untrusted model output cannot inject executable DOM.
 */

export function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderInline(escaped) {
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

export function renderMarkdown(rawText) {
  const text = String(rawText ?? "").replace(/\r\n/g, "\n");
  if (!text.trim()) return "";

  const codeBlocks = [];
  const placeholderText = text.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_m, _lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(`<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
    return `@@CODEBLOCK_${idx}@@`;
  });

  const blocks = placeholderText.split(/\n{2,}/);
  const htmlParts = blocks.map((block) => {
    const trimmed = block.trim();
    if (!trimmed) return "";

    const codeMatch = trimmed.match(/^@@CODEBLOCK_(\d+)@@$/);
    if (codeMatch) {
      return codeBlocks[Number(codeMatch[1])] || "";
    }

    const lines = trimmed.split("\n");
    if (lines.every((l) => /^[-*]\s+/.test(l.trim()))) {
      const items = lines
        .map((l) => `<li>${renderInline(escapeHtml(l.trim().replace(/^[-*]\s+/, "")))}</li>`)
        .join("");
      return `<ul>${items}</ul>`;
    }

    if (lines.every((l) => /^\d+\.\s+/.test(l.trim()))) {
      const items = lines
        .map((l) => `<li>${renderInline(escapeHtml(l.trim().replace(/^\d+\.\s+/, "")))}</li>`)
        .join("");
      return `<ol>${items}</ol>`;
    }

    const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch && lines.length === 1) {
      const level = Math.min(4, headingMatch[1].length + 2);
      return `<h${level}>${renderInline(escapeHtml(headingMatch[2]))}</h${level}>`;
    }

    const formatted = lines.map((l) => renderInline(escapeHtml(l))).join("<br />");
    return `<p>${formatted}</p>`;
  });

  return htmlParts
    .join("")
    .replace(/@@CODEBLOCK_(\d+)@@/g, (_m, idx) => codeBlocks[Number(idx)] || "");
}
