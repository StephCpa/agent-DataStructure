// 公共构件：配色、字体、段落与表格构建器（DM-1 深青配色，正式报告 Profile A）
const {
  Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, AlignmentType, WidthType, BorderStyle, ShadingType, PageBreak,
} = require("docx");

// DM-1 Deep Cyan（正文页使用降饱和的表格色，封面用亮色）
const C = {
  heading: "162235",     // 标题深藏青
  body: "000000",        // 正文纯黑（Profile A 正式文档）
  secondary: "5A6678",   // 次要说明
  accent: "1B6B7A",      // 表头 / 线条（DM-1 表格 token，已降饱和）
  accentBright: "37DCF2",// 封面亮青
  tableHeaderText: "FFFFFF",
  innerLine: "C8DDE2",
  surface: "EDF3F5",
  codeBg: "F3F6F8",
  noteGray: "888888",
};

const FONT_BODY = { ascii: "Times New Roman", eastAsia: "SimSun" };
const FONT_HEAD = { ascii: "Arial", eastAsia: "SimHei" };
const FONT_CODE = { ascii: "Consolas", eastAsia: "SimSun" };

const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: NB, bottom: NB, left: NB, right: NB };
const allNoBorders = { top: NB, bottom: NB, left: NB, right: NB, insideHorizontal: NB, insideVertical: NB };

// ── 标题 ─────────────────────────────────────────────
function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    keepNext: true,
    spacing: { before: 400, after: 200, line: 312 },
    children: [new TextRun({ text, bold: true, size: 32, color: C.heading, font: FONT_HEAD })],
  });
}
function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    keepNext: true,
    spacing: { before: 280, after: 140, line: 312 },
    children: [new TextRun({ text, bold: true, size: 28, color: C.heading, font: FONT_HEAD })],
  });
}
function h3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    keepNext: true,
    spacing: { before: 220, after: 100, line: 312 },
    children: [new TextRun({ text, bold: true, size: 24, color: C.heading, font: FONT_HEAD })],
  });
}

// ── 正文段落 ─────────────────────────────────────────
function body(text, opts) {
  const o = opts || {};
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    indent: { firstLine: 480 },
    spacing: { line: 312, after: o.after != null ? o.after : 60 },
    keepNext: !!o.keepNext,
    children: [new TextRun({ text, size: 24, color: C.body, font: FONT_BODY, bold: !!o.bold })],
  });
}
// 多段正文快捷方式
function bodies(arr) { return arr.map((t) => body(t)); }

// 无首行缩进的说明行（用于紧贴表格/代码块的补充说明）
function lead(text, opts) {
  const o = opts || {};
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    indent: { firstLine: 480 },
    spacing: { line: 312, before: o.before != null ? o.before : 60, after: o.after != null ? o.after : 40 },
    keepNext: true,
    children: [new TextRun({ text, size: 24, color: C.body, font: FONT_BODY })],
  });
}

function note(text) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    spacing: { line: 312, before: 60, after: 120 },
    children: [new TextRun({ text, italics: true, size: 18, color: C.noteGray, font: FONT_BODY })],
  });
}

// 编号步骤段（以“n.”开头的正文段，保持首行缩进）
function step(n, text) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    indent: { firstLine: 480 },
    spacing: { line: 312, after: 40 },
    children: [
      new TextRun({ text: n + " ", size: 24, color: C.body, font: FONT_BODY, bold: true }),
      new TextRun({ text, size: 24, color: C.body, font: FONT_BODY }),
    ],
  });
}

// ── 表格 ─────────────────────────────────────────────
let tableCounter = 0;
function tblTitle(text) {
  tableCounter += 1;
  return new Paragraph({
    alignment: AlignmentType.CENTER,
    keepNext: true,
    spacing: { before: 160, after: 60, line: 312 },
    children: [new TextRun({ text: "表 " + tableCounter + "  " + text, bold: true, size: 21, color: C.heading, font: FONT_HEAD })],
  });
}

function cellPara(text, o) {
  return new Paragraph({
    alignment: o && o.center ? AlignmentType.CENTER : AlignmentType.LEFT,
    spacing: { line: 276, before: 20, after: 20 },
    children: [new TextRun({
      text: String(text),
      bold: !!(o && o.bold),
      size: (o && o.size) || 20,
      color: (o && o.color) || C.body,
      font: FONT_BODY,
    })],
  });
}

// headers: string[]；rows: string[][]；widths: number[] 百分比
function tbl(headers, rows, widths, opts) {
  const o = opts || {};
  const fs = o.fontSize || 20;
  const headerRow = new TableRow({
    tableHeader: true,
    cantSplit: true,
    children: headers.map((t, i) => new TableCell({
      children: [cellPara(t, { bold: true, size: fs, color: C.tableHeaderText, center: true })],
      shading: { type: ShadingType.CLEAR, fill: C.accent },
      margins: { top: 70, bottom: 70, left: 110, right: 110 },
      width: widths ? { size: widths[i], type: WidthType.PERCENTAGE } : undefined,
    })),
  });
  const dataRows = rows.map((r, ri) => new TableRow({
    cantSplit: true,
    children: r.map((t, i) => new TableCell({
      children: [cellPara(t, { size: fs, bold: i === 0 && !!o.firstColBold })],
      shading: o.zebra && ri % 2 === 1 ? { type: ShadingType.CLEAR, fill: C.surface } : undefined,
      margins: { top: 60, bottom: 60, left: 110, right: 110 },
      width: widths ? { size: widths[i], type: WidthType.PERCENTAGE } : undefined,
    })),
  }));
  return new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 6, color: C.accent },
      bottom: { style: BorderStyle.SINGLE, size: 6, color: C.accent },
      left: NB, right: NB,
      insideHorizontal: { style: BorderStyle.SINGLE, size: 2, color: C.innerLine },
      insideVertical: { style: BorderStyle.SINGLE, size: 2, color: C.innerLine },
    },
    rows: [headerRow, ...dataRows],
  });
}

// ── 代码块 ───────────────────────────────────────────
let codeCounter = 0;
function codeBlock(lines, title) {
  const out = [];
  if (title) {
    codeCounter += 1;
    out.push(new Paragraph({
      alignment: AlignmentType.CENTER,
      keepNext: true,
      spacing: { before: 160, after: 60, line: 312 },
      children: [new TextRun({ text: "代码 " + codeCounter + "  " + title, bold: true, size: 21, color: C.heading, font: FONT_HEAD })],
    }));
  }
  lines.forEach((ln, i) => {
    out.push(new Paragraph({
      shading: { type: ShadingType.CLEAR, fill: C.codeBg },
      border: { left: { style: BorderStyle.SINGLE, size: 8, color: C.accent, space: 8 } },
      indent: { left: 240, right: 160 },
      spacing: { line: 252, lineRule: "atLeast", before: i === 0 ? 80 : 0, after: i === lines.length - 1 ? 80 : 0 },
      children: [new TextRun({ text: ln.length ? ln : " ", size: 17, color: "1A2530", font: FONT_CODE })],
    }));
  });
  return out;
}

function pageBreakPara() {
  return new Paragraph({ children: [new PageBreak()] });
}

module.exports = {
  C, FONT_BODY, FONT_HEAD, FONT_CODE, NB, noBorders, allNoBorders,
  h1, h2, h3, body, bodies, lead, note, step, tbl, tblTitle, codeBlock, pageBreakPara,
};
