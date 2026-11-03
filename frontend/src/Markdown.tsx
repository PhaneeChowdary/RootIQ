import type { ReactNode } from "react";

/** Inline Markdown the agents use: **bold**, *italic* and `code`. Builds React nodes, never raw HTML. */
export function Inline({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\s][^*]*\*)/g);
  return (
    <>
      {parts.map((part, i) => {
        if (/^(\*\*|__).+(\*\*|__)$/.test(part)) return <strong key={i}>{part.slice(2, -2)}</strong>;
        if (/^`.+`$/.test(part)) return <code key={i}>{part.slice(1, -1)}</code>;
        if (/^\*.+\*$/.test(part)) return <em key={i}>{part.slice(1, -1)}</em>;
        return part;
      })}
    </>
  );
}

type Item = { text: string; children: string[] };
type Block = { kind: "p"; text: string } | { kind: "h"; text: string } | { kind: "ul" | "ol"; items: Item[] };

const BULLET = /^(\s*)(?:[-*•]|(\d+)[.)])\s+(.*)$/;

/** Block Markdown from model output: paragraphs, headings and bullet or numbered lists (one level of nesting). */
export function Markdown({ text, className = "" }: { text: string; className?: string }) {
  const blocks: Block[] = [];
  for (const raw of text.replace(/\r/g, "").split("\n")) {
    const line = raw.trimEnd();
    const last = blocks[blocks.length - 1];
    if (!line.trim()) { blocks.push({ kind: "p", text: "" }); continue; }
    const heading = line.match(/^\s*#{1,6}\s+(.*)$/);
    if (heading) { blocks.push({ kind: "h", text: heading[1] }); continue; }
    const bullet = line.match(BULLET);
    if (bullet) {
      const [, indent, number, body] = bullet;
      const kind = number ? "ol" : "ul";
      if (indent.length >= 2 && last && (last.kind === "ul" || last.kind === "ol") && last.items.length) {
        last.items[last.items.length - 1].children.push(body);
      } else if (last && last.kind === kind) {
        last.items.push({ text: body, children: [] });
      } else {
        blocks.push({ kind, items: [{ text: body, children: [] }] });
      }
      continue;
    }
    if (last && (last.kind === "ul" || last.kind === "ol") && /^\s{2,}/.test(line)) {
      const item = last.items[last.items.length - 1];
      item.text += ` ${line.trim()}`;
    } else if (last && last.kind === "p" && last.text) {
      last.text += ` ${line.trim()}`;
    } else {
      blocks.push({ kind: "p", text: line.trim() });
    }
  }
  // A blank line between two list items of the same kind does not end the list.
  const merged: Block[] = [];
  for (const block of blocks) {
    if (block.kind === "p" && !block.text) continue;
    const prev = merged[merged.length - 1];
    if (prev && (block.kind === "ul" || block.kind === "ol") && prev.kind === block.kind) prev.items.push(...block.items);
    else merged.push(block);
  }
  return (
    <div className={`md ${className}`.trim()}>
      {merged.map((block, i): ReactNode => {
        if (block.kind === "h") return <h5 key={i}><Inline text={block.text} /></h5>;
        if (block.kind === "p") return <p key={i}><Inline text={block.text} /></p>;
        const items = block.items.map((item, j) => (
          <li key={j}>
            <Inline text={item.text} />
            {item.children.length ? <ul>{item.children.map((child, k) => <li key={k}><Inline text={child} /></li>)}</ul> : null}
          </li>
        ));
        return block.kind === "ol" ? <ol key={i}>{items}</ol> : <ul key={i}>{items}</ul>;
      })}
    </div>
  );
}

/** Change model Markdown into plain text, the same way as the backend (backend/app/agent/plaintext.py).
 *  Customer emails and Jira comments show plain text, so the edit boxes show plain text too. */
export function plainText(text: string) {
  return text
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "")
    .replace(/^([ \t]*)[*+][ \t]+/gm, "$1- ")
    .replace(/\*\*([\s\S]+?)\*\*|__([\s\S]+?)__/g, (_m, a, b) => a ?? b)
    .replace(/(^|[^\w*])\*(?!\s)([^*\n]+?)\*(?!\w)/g, "$1$2")
    .replace(/`([^`\n]+)`/g, "$1")
    .replace(/^```[^\n]*\n?|^```\s*$/gm, "")
    .replace(/\[([^\]]+)\]\((\S+?)\)/g, "$1 ($2)")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
