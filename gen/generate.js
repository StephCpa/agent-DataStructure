// 主装配：ASG 详细设计说明书
const {
  Document, Packer, Paragraph, TextRun, Header, Footer,
  AlignmentType, PageNumber, NumberFormat, SectionType, TableOfContents, PageBreak,
} = require("docx");
const fs = require("fs");

const { C, FONT_BODY, FONT_HEAD } = require("./helpers");
const { buildCoverR1, COVER_P } = require("./cover");
const { ch1, ch2 } = require("./ch01");
const { ch3 } = require("./ch03");
const { ch4 } = require("./ch04");
const { ch5 } = require("./ch05");
const { ch6 } = require("./ch06");
const { ch7 } = require("./ch07");
const { ch8, ch9, ch10 } = require("./ch08");
const { ch11, ch12, ch13, appendix } = require("./ch11");

const pgSize = { width: 11906, height: 16838 };
const pgMargin = { top: 1440, bottom: 1440, left: 1701, right: 1417 };

// ── 页脚（页码）与页眉 ──
function pageNumFooter() {
  return new Footer({
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: [PageNumber.CURRENT], size: 18, color: C.secondary, font: FONT_BODY })],
    })],
  });
}
function docHeader() {
  return new Header({
    children: [new Paragraph({
      alignment: AlignmentType.RIGHT,
      border: { bottom: { style: "single", size: 2, color: C.innerLine, space: 4 } },
      children: [new TextRun({ text: "面向大模型智能体的数据结构详细设计说明书（ASG V1.0）", size: 16, color: C.secondary, font: FONT_BODY })],
    })],
  });
}

// ── 前置部分：摘要 + 目录 ──
function frontMatter() {
  const out = [];
  out.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 240, after: 300 },
    children: [new TextRun({ text: "摘  要", bold: true, size: 32, color: C.heading, font: FONT_HEAD })],
  }));
  out.push(new Paragraph({
    alignment: AlignmentType.JUSTIFIED, indent: { firstLine: 480 }, spacing: { line: 312, after: 80 },
    children: [new TextRun({ text: "本说明书依据《面向大模型智能体的数据结构研究与设计报告》（2026-10-24）开展详细设计，将其中提出的 Agent 状态图（ASG）抽象数据类型细化为可实施的工程规格。内容覆盖：模块划分与信任边界；十一种类型化记录的字段级数据结构定义与关系边类型系统；PostgreSQL 物理模式（含实现请求去重、预算约束与不可变性的具体约束）；observe、assess、revise、invalidate、prepare_effect、dispatch、resolve_effect、project_context、retire_or_purge 九个核心操作的接口契约；效果、尝试、预留、授权、任务五组状态机的转移表；串行提交、支持电路求值、失效传播与读取闸门、上下文投影、原子预留、outbox 执行、对账、删除传播与确定性回放九个核心算法；并发事务边界与不变量 I1 至 I10 的机制映射；授权规范化摘要、权限检查点、数据标签传播与删除域设计；错误码目录、复杂度边界、对抗测试用例集与阶段 A 至 E 实施计划。", size: 24, color: C.body, font: FONT_BODY })],
  }));
  out.push(new Paragraph({
    alignment: AlignmentType.JUSTIFIED, indent: { firstLine: 480 }, spacing: { line: 312, after: 80 },
    children: [new TextRun({ text: "本说明书实现范围对应研究设计报告的阶段 A（语义内核）与阶段 B（执行闭环），并为阶段 C（证据与上下文）预留接口；不包含模型选型、检索算法本体、跨工作空间分布式事务与差分隐私机制。全部设计遵循研究设计报告的边界声明：历史日志是系统接纳记录的依据而非外部真值证明；投影带水位并以纪元闸门约束关键读取；外部效果一律经执行网关并区分失败与未知；删除以墓碑门控阻止复活。本文不含任何虚构的实验结果或性能数字。", size: 24, color: C.body, font: FONT_BODY })],
  }));
  out.push(new Paragraph({
    alignment: AlignmentType.JUSTIFIED, indent: { firstLine: 480 }, spacing: { line: 312, after: 200 },
    children: [
      new TextRun({ text: "关键词：", bold: true, size: 24, color: C.body, font: FONT_BODY }),
      new TextRun({ text: "Agent 状态图；证据依赖；失效传播；执行恢复；授权绑定；资源预留；上下文清单；确定性回放", size: 24, color: C.body, font: FONT_BODY }),
      new PageBreak(),
    ],
  }));
  // 摘要页结束，目录另起一页（分页符挂接在关键词段落末尾）

  out.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 240, after: 300 },
    children: [new TextRun({ text: "目  录", bold: true, size: 32, color: C.heading, font: FONT_HEAD })],
  }));
  out.push(new TableOfContents("Table of Contents", {
    hyperlink: true,
    headingStyleRange: "1-3",
  }));
  out.push(new Paragraph({
    spacing: { before: 200 },
    children: [new TextRun({
      text: "注：本目录由域代码生成。编辑文档后如需刷新页码，请在目录上右键选择更新域并更新整个目录。",
      italics: true, size: 18, color: C.noteGray, font: FONT_BODY,
    })],
  }));
  return out;
}

// ── 装配 ──
const bodyChildren = [
  ...ch1(), ...ch2(), ...ch3(), ...ch4(), ...ch5(),
  ...ch6(), ...ch7(), ...ch8(), ...ch9(), ...ch10(),
  ...ch11(), ...ch12(), ...ch13(), ...appendix(),
];

const doc = new Document({
  creator: "ASG Design Team",
  title: "面向大模型智能体的数据结构详细设计说明书",
  description: "ASG（Agent 状态图）详细设计 V1.0，依据 2026-10-24 研究设计报告编制",
  styles: {
    default: {
      document: {
        run: { font: FONT_BODY, size: 24, color: C.body },
        paragraph: { spacing: { line: 312 } },
      },
      heading1: {
        run: { font: FONT_HEAD, size: 32, bold: true, color: C.heading },
        paragraph: { spacing: { before: 400, after: 200, line: 312 }, outlineLevel: 0 },
      },
      heading2: {
        run: { font: FONT_HEAD, size: 28, bold: true, color: C.heading },
        paragraph: { spacing: { before: 280, after: 140, line: 312 }, outlineLevel: 1 },
      },
      heading3: {
        run: { font: FONT_HEAD, size: 24, bold: true, color: C.heading },
        paragraph: { spacing: { before: 220, after: 100, line: 312 }, outlineLevel: 2 },
      },
    },
  },
  sections: [
    {
      // 第 1 节：封面（边距为 0，无页眉页脚页码）
      properties: {
        page: { size: pgSize, margin: { top: 0, bottom: 0, left: 0, right: 0 } },
      },
      children: buildCoverR1({
        title: "面向大模型智能体的数据结构详细设计说明书",
        subtitle: "Agent 状态图（ASG）V1.0 语义内核与执行闭环",
        englishLabel: "ASG DETAILED DESIGN",
        metaLines: [
          "依据文档：《面向大模型智能体的数据结构研究与设计报告》",
          "文档版本：V1.0（详细设计）",
          "编制日期：2026 年 10 月 24 日",
          "适用阶段：阶段 A 语义内核 / 阶段 B 执行闭环",
        ],
        footerLeft: "ASG 详细设计说明书",
        footerRight: "V1.0 / 2026-10",
        palette: COVER_P,
      }),
    },
    {
      // 第 2 节：前置部分（摘要 + 目录），罗马页码
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: pgSize, margin: pgMargin, pageNumbers: { start: 1, formatType: NumberFormat.UPPER_ROMAN } },
      },
      footers: { default: pageNumFooter() },
      children: frontMatter(),
    },
    {
      // 第 3 节：正文，阿拉伯页码从 1 起
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: pgSize, margin: pgMargin, pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL } },
      },
      headers: { default: docHeader() },
      footers: { default: pageNumFooter() },
      children: bodyChildren,
    },
  ],
});

const OUT = "ASG_Detailed_Design_V1.0.docx";
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(OUT, buf);
  console.log("written:", OUT, buf.length, "bytes");
}).catch((e) => {
  console.error("FAILED:", e);
  process.exit(1);
});
