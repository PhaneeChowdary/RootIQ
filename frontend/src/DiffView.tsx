import { useMemo, useState } from "react";
import { ActionIcon } from "./icons";

/** A code diff from the Code agent: split  or unified, with line numbers and the changed words marked.
 * The app never applies it. A person reviews it and applies it in the normal code review. */

type Kind = "ctx" | "add" | "del" | "hunk";
type Row = { kind: Kind; text: string; old?: number; new?: number; partner?: string };
type Segment = { text: string; changed: boolean };
type Mode = "split" | "unified";

const MODE_KEY = "rootiq.diffView";

function readMode(): Mode {
  try {
    return localStorage.getItem(MODE_KEY) === "unified" ? "unified" : "split";
  } catch {
    return "split";
  }
}

/** The rows of a unified diff, with the old and new line numbers. Each removed line knows the added line that
 * replaces it, and the other way round, so the changed words can be marked. */
export function parseDiff(diff: string): { rows: Row[]; added: number; removed: number } {
  const rows: Row[] = [];
  let oldLine = 0;
  let newLine = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("--- ") || line.startsWith("+++ ")) continue;
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(line);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      rows.push({ kind: "hunk", text: line });
    } else if (line.startsWith("+")) {
      rows.push({ kind: "add", text: line.slice(1), new: newLine++ });
    } else if (line.startsWith("-")) {
      rows.push({ kind: "del", text: line.slice(1), old: oldLine++ });
    } else if (line.startsWith(" ")) {  // a blank context line is " "; an empty string is only the end of the text
      rows.push({ kind: "ctx", text: line.slice(1), old: oldLine++, new: newLine++ });
    }
  }
  for (let i = 0; i < rows.length; ) {  // pair each block of removed lines with the added lines after it
    if (rows[i].kind !== "del") { i++; continue; }
    let j = i;
    while (j < rows.length && rows[j].kind === "del") j++;
    let k = j;
    while (k < rows.length && rows[k].kind === "add") k++;
    for (let n = 0; n < Math.min(j - i, k - j); n++) {
      rows[i + n].partner = rows[j + n].text;
      rows[j + n].partner = rows[i + n].text;
    }
    i = k;
  }
  return { rows, added: rows.filter((r) => r.kind === "add").length, removed: rows.filter((r) => r.kind === "del").length };
}

/** The parts of `text` that are not in `other`, by words: a longest common subsequence of the tokens. */
export function changedWords(text: string, other: string): Segment[] {
  const a = text.match(/\s+|\w+|[^\w\s]/g) ?? [];
  const b = other.match(/\s+|\w+|[^\w\s]/g) ?? [];
  if (a.length * b.length > 160_000) return [{ text, changed: true }];
  const lcs = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out: Segment[] = [];
  let i = 0;
  let j = 0;
  const push = (t: string, changed: boolean) => {
    const last = out[out.length - 1];
    if (last && last.changed === changed) last.text += t;
    else out.push({ text: t, changed });
  };
  while (i < a.length) {
    if (j < b.length && a[i] === b[j]) { push(a[i], false); i++; j++; }
    else if (j < b.length && lcs[i][j + 1] >= lcs[i + 1][j]) j++;
    else { push(a[i], !/^\s+$/.test(a[i])); i++; }
  }
  // A line that is mostly new is marked as a whole by its row colour: word marks would only add noise.
  const kept = out.filter((s) => !s.changed).reduce((n, s) => n + s.text.trim().length, 0);
  if (kept < text.trim().length * 0.5) return [{ text, changed: false }];
  // One mark across the spaces between two changed words.
  for (let k = out.length - 2; k > 0; k--) {
    if (!out[k].changed && /^\s+$/.test(out[k].text) && out[k - 1].changed && out[k + 1].changed) {
      out.splice(k - 1, 3, { text: out[k - 1].text + out[k].text + out[k + 1].text, changed: true });
    }
  }
  return out;
}

/** The sign and the code of one line. The indentation becomes padding, so a wrapped line stays under its code. */
function Code({ row }: { row: Row | null }) {
  if (!row) return <><td className="dv-sign dv-empty" /><td className="dv-code dv-empty" /></>;
  const indent = /^[ \t]*/.exec(row.text)![0];
  const width = [...indent].reduce((n, c) => n + (c === "\t" ? 4 : 1), 0);
  const segments = row.partner !== undefined && row.kind !== "ctx" ? changedWords(row.text, row.partner) : [{ text: row.text, changed: false }];
  let skip = indent.length;  // the indentation is padding now: drop it from the first segments
  const shown = segments.map((seg) => {
    const cut = Math.min(skip, seg.text.length);
    skip -= cut;
    return { ...seg, text: seg.text.slice(cut) };
  }).filter((seg) => seg.text);
  return (
    <>
      <td className={`dv-sign dv-${row.kind}`} aria-hidden="true">{row.kind === "add" ? "+" : row.kind === "del" ? "-" : ""}</td>
      <td className={`dv-code dv-${row.kind}`} style={{ paddingLeft: `calc(${width}ch + 2px)` }}>
        {shown.length ? shown.map((seg, i) => (seg.changed ? <mark key={i}>{seg.text}</mark> : <span key={i}>{seg.text}</span>)) : " "}
      </td>
    </>
  );
}

const Num = ({ n, kind }: { n?: number; kind?: Kind }) => <td className={`dv-num${kind ? ` dv-${kind}` : ""}`}>{n ?? ""}</td>;

function splitPairs(rows: Row[]): (Row | [Row | null, Row | null])[] {
  const out: (Row | [Row | null, Row | null])[] = [];
  for (let i = 0; i < rows.length; ) {
    const row = rows[i];
    if (row.kind === "hunk") { out.push(row); i++; continue; }
    if (row.kind === "ctx") { out.push([row, row]); i++; continue; }
    const dels: Row[] = [];
    const adds: Row[] = [];
    while (i < rows.length && rows[i].kind === "del") dels.push(rows[i++]);
    while (i < rows.length && rows[i].kind === "add") adds.push(rows[i++]);
    for (let n = 0; n < Math.max(dels.length, adds.length); n++) out.push([dels[n] ?? null, adds[n] ?? null]);
  }
  return out;
}

export function DiffView({ diff, path }: { diff: string; path: string }) {
  const [mode, setMode] = useState<Mode>(readMode);
  const [copied, setCopied] = useState(false);
  const { rows, added, removed } = useMemo(() => parseDiff(diff), [diff]);
  const choose = (next: Mode) => {
    setMode(next);
    try { localStorage.setItem(MODE_KEY, next); } catch { /* the choice is only for this page */ }
  };
  const copy = () => {
    void navigator.clipboard?.writeText(diff + "\n").then(() => { setCopied(true); window.setTimeout(() => setCopied(false), 1600); });
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([diff + "\n"], { type: "text/x-diff" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: `${path.replace(/[\/]/g, "_") || "change"}.patch` });
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="diff-view">
      <div className="dv-head">
        <span className="dv-file"><ActionIcon kind="diff" size={14} /><span className="dv-path">{path}</span>
          <span className="dv-count add">+{added}</span><span className="dv-count del">-{removed}</span></span>
        <span className="dv-tools">
          <span className="dv-toggle" role="group" aria-label="Diff layout">
            <button aria-pressed={mode === "split"} className={mode === "split" ? "active" : ""} onClick={() => choose("split")} type="button"><ActionIcon kind="split" size={13} />Split</button>
            <button aria-pressed={mode === "unified"} className={mode === "unified" ? "active" : ""} onClick={() => choose("unified")} type="button"><ActionIcon kind="unified" size={13} />Unified</button>
          </span>
          <button className="text-action" onClick={copy} type="button"><ActionIcon kind={copied ? "check" : "copy"} size={13} />{copied ? "Copied" : "Copy"}</button>
          <button className="text-action" onClick={download} type="button"><ActionIcon kind="download" size={13} />.patch</button>
        </span>
      </div>
      <div className="dv-scroll">
        <table className={`dv-table ${mode}`}>
          {mode === "split"
            ? <colgroup><col className="dv-col-num" /><col className="dv-col-sign" /><col /><col className="dv-col-num" /><col className="dv-col-sign" /><col /></colgroup>
            : <colgroup><col className="dv-col-num" /><col className="dv-col-num" /><col className="dv-col-sign" /><col /></colgroup>}
          <tbody>
            {mode === "split"
              ? splitPairs(rows).map((item, i) => Array.isArray(item) ? (
                  <tr key={i}>
                    <Num n={item[0]?.old} kind={item[0]?.kind} /><Code row={item[0]} />
                    <Num n={item[1]?.new} kind={item[1]?.kind} /><Code row={item[1]} />
                  </tr>
                ) : (
                  <tr className="dv-hunk-row" key={i}><td colSpan={6}>{item.text}</td></tr>
                ))
              : rows.map((row, i) => row.kind === "hunk" ? (
                  <tr className="dv-hunk-row" key={i}><td colSpan={4}>{row.text}</td></tr>
                ) : (
                  <tr key={i}><Num n={row.old} kind={row.kind} /><Num n={row.new} kind={row.kind} /><Code row={row} /></tr>
                ))}
          </tbody>
        </table>
      </div>
      <p className="dv-foot muted"><ActionIcon kind="lock" size={12} />Proposal only: RootIQ never changes your code. Review it, then apply it in your normal code review.</p>
    </div>
  );
}
