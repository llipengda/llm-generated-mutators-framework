export type XmlLocation = {
  line: number;
  tag: string;
  name: string | null;
  model: string | null;
  attributes: Record<string, string>;
};

export type Diagnostic = {
  code: string;
  severity: "error" | "warning" | "summary";
  confidence: number;
  message: string;
  evidence: string[];
  log_lines: number[];
  xml_locations: XmlLocation[];
};

export type DiagnosisReport = {
  datamodel: string;
  logs_analyzed: number;
  cross_log_summary: Array<{
    code: string;
    severity: string;
    confidence: number;
    xml_locations: XmlLocation[];
    seeds: string[];
  }>;
  reports: Array<{ log: string; seed: string; diagnostics: Diagnostic[] }>;
  static_diagnostics: Diagnostic[];
  llm_judgment?: {
    model: string;
    root_cause: LlmRootCause | null;
  };
};

export type LlmRootCause = {
  title: string;
  classification: "root_cause" | "contributing_factor" | "symptom" | "uncertain";
  category: "reference" | "endianness" | "layout" | "choice" | "cardinality" | "boundary" | "other";
  confidence: number;
  affected_seeds: string[];
  xml_locations: XmlLocation[];
  reasoning: string;
  evidence: string[];
  suggested_fix: string | null;
  verification: string;
};

type IndexedNode = XmlLocation & { depth: number };
type LogEvent = {
  line: number;
  depth: number;
  tag: string;
  name: string;
  offset: number;
  total: number;
  sizeBytes: number | null;
  value: number | null;
};

function xmlLocation(node: IndexedNode): XmlLocation {
  return { line: node.line, tag: node.tag, name: node.name, model: node.model, attributes: node.attributes };
}

const TREE_RE = /^(.*?)(DataModel|Block|Choice|Array|Optional|Number|Blob|String) '([^']+)', Bytes: (\d+)\/(\d+)/;
const SIZE_RE = /Size: (\d+) bytes? \| (\d+) bits/;
const VALUE_RE = /Value: (-?\d+)(?: \(0x[0-9A-Fa-f]+\))?/;
const BUFFER_RE = /Length is (\d+) bits but buffer only has (\d+) bits left/;
const REFERENCE_RE = /Referenced element '([^']+)' not found/;
const BINDING_RE = /Unable to resolve binding '([^']+)' attached to '([^']+)'/;
const OPTIONAL_PATH_RE = /Optional 'Optional '([^']+)''/;
const PARSE_FAILURE_RE = /Failed to parse file '([^']+)':/;
const ROUNDTRIP_RE = /Parsed bytes do not match original file for '([^']+)'/;

function decodeXml(value: string) {
  return value
    .replaceAll("&quot;", '"')
    .replaceAll("&apos;", "'")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&amp;", "&");
}

function indexXml(xml: string) {
  const nodes: IndexedNode[] = [];
  const stack: Array<{ tag: string; name: string | null }> = [];
  const lines = xml.split(/\r?\n/);
  lines.forEach((line, lineIndex) => {
    const tagPattern = /<\s*(?!\/|\?|!)(?:[\w.-]+:)?([\w.-]+)\b([^<>]*?)(\/?)>/g;
    let match: RegExpExecArray | null;
    while ((match = tagPattern.exec(line))) {
      const tag = match[1];
      const rawAttributes = match[2];
      const attributes: Record<string, string> = {};
      const attributePattern = /(?:^|\s)(?:[\w.-]+:)?([\w.-]+)\s*=\s*("([^"]*)"|'([^']*)')/g;
      let attribute: RegExpExecArray | null;
      while ((attribute = attributePattern.exec(rawAttributes))) {
        attributes[attribute[1]] = decodeXml(attribute[3] ?? attribute[4] ?? "");
      }
      const parentModel = [...stack].reverse().find((item) => item.tag === "DataModel")?.name ?? null;
      const name = attributes.name ?? null;
      nodes.push({
        line: lineIndex + 1,
        tag,
        name,
        model: tag === "DataModel" ? name : parentModel,
        attributes,
        depth: stack.length,
      });
      if (match[3] !== "/") stack.push({ tag, name });
    }
    const closePattern = /<\s*\/\s*(?:[\w.-]+:)?([\w.-]+)\s*>/g;
    while (closePattern.exec(line)) stack.pop();
  });
  const locate = (name: string | null, tag?: string) => {
    if (!name) return [];
    const named = nodes.filter((node) => node.name === name);
    const tagged = tag ? named.filter((node) => node.tag === tag) : [];
    return (tagged.length ? tagged : named).slice(0, 6).map(xmlLocation);
  };
  const locateRuntimePath = (runtimePath: string, tag?: string) => {
    for (const part of runtimePath.split(".").filter(Boolean).reverse()) {
      const locations = locate(part, tag);
      if (locations.length) return locations;
    }
    return [];
  };
  return { nodes, locate, locateRuntimePath };
}

function parseEvents(lines: string[]): LogEvent[] {
  const events: LogEvent[] = [];
  lines.forEach((line, index) => {
    const match = TREE_RE.exec(line);
    if (!match) return;
    const event: LogEvent = {
      line: index + 1,
      depth: (match[1].match(/\|/g) || []).length,
      tag: match[2],
      name: match[3],
      offset: Number(match[4]),
      total: Number(match[5]),
      sizeBytes: null,
      value: null,
    };
    for (const following of lines.slice(index + 1, index + 5)) {
      if (TREE_RE.test(following)) break;
      const size = SIZE_RE.exec(following);
      const value = VALUE_RE.exec(following);
      if (size) event.sizeBytes = Number(size[1]);
      if (value) event.value = Number(value[1]);
    }
    events.push(event);
  });
  return events;
}

function deduplicate(items: Diagnostic[]) {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = `${item.code}\0${item.message}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function hexAfter(lines: string[], heading: string) {
  const start = lines.map((line, index) => line.trim() === heading ? index : -1).filter((index) => index >= 0).at(-1);
  if (start === undefined) return null;
  const bytes: number[] = [];
  for (const line of lines.slice(start + 1)) {
    const text = line.trim();
    if (!/^(?:[0-9A-Fa-f]{2})(?:\s+[0-9A-Fa-f]{2})*$/.test(text)) break;
    bytes.push(...text.split(/\s+/).map((token) => Number.parseInt(token, 16)));
  }
  return bytes.length ? new Uint8Array(bytes) : null;
}

function diagnoseLog(name: string, content: string, xmlIndex: ReturnType<typeof indexXml>) {
  const lines = content.split(/\r?\n/);
  const events = parseEvents(lines);
  const diagnostics: Diagnostic[] = [];
  const raw = hexAfter(lines, "Bytes:") ?? hexAfter(lines, "Original Bytes:");
  const original = hexAfter(lines, "Original Bytes:");
  const parsed = hexAfter(lines, "Parsed   Bytes:");
  const eofGroups = new Map<string, { event: LogEvent; count: number }>();
  const nearestEvent = (line: number) => [...events].reverse().find((event) => event.line <= line) ?? null;
  const seed = [...lines].reverse().map((line) => PARSE_FAILURE_RE.exec(line) || ROUNDTRIP_RE.exec(line)).find(Boolean)?.[1] ?? name.replace(/\.log$/i, "");
  const add = (diagnostic: Diagnostic) => diagnostics.push(diagnostic);

  lines.forEach((line, index) => {
    const reference = REFERENCE_RE.exec(line);
    if (reference) {
      const runtimePath = OPTIONAL_PATH_RE.exec(line)?.[1] ?? "";
      add({
        code: "unresolved_runtime_reference",
        severity: "error",
        confidence: 1,
        message: `元素“${runtimePath || "未知路径"}”无法解析引用“${reference[1]}”。`,
        evidence: ["运行时引用未找到。"],
        log_lines: [index + 1],
        xml_locations: xmlIndex.locateRuntimePath(runtimePath, "Optional"),
      });
    }
    const binding = BINDING_RE.exec(line);
    if (binding) {
      add({
        code: "unresolved_relation_binding",
        severity: "error",
        confidence: 1,
        message: `元素“${binding[2]}”无法解析 Relation 绑定“${binding[1]}”。`,
        evidence: ["失败的 size/count 绑定通常会使目标元素无法确定长度。"],
        log_lines: [index + 1],
        xml_locations: xmlIndex.locateRuntimePath(binding[2]),
      });
    }
    if (/Failed: Element is unsized\./.test(line)) {
      const event = nearestEvent(index + 1);
      if (event) add({
        code: "unsized_element",
        severity: "error",
        confidence: .99,
        message: `${event.tag}“${event.name}”在后面仍有数据时无法确定长度。`,
        evidence: ["这通常由失效的 size Relation 或非末尾的无界字段引起。"],
        log_lines: [index + 1],
        xml_locations: xmlIndex.locate(event.name, event.tag),
      });
    }
    const buffer = BUFFER_RE.exec(line);
    if (buffer) {
      const event = nearestEvent(index + 1);
      const wanted = Number(buffer[1]);
      const remaining = Number(buffer[2]);
      const lengthEvent = [...events].reverse().find((candidate) => candidate.line < index + 1 && candidate.tag === "Number" && candidate.name.toLowerCase().includes("length") && candidate.value !== null && [2, 4, 8].includes(candidate.sizeBytes ?? 0));
      if (lengthEvent && raw && lengthEvent.sizeBytes && lengthEvent.offset + lengthEvent.sizeBytes <= raw.length) {
        const encoded = raw.slice(lengthEvent.offset, lengthEvent.offset + lengthEvent.sizeBytes);
        const little = [...encoded].reduce((sum, byte, byteIndex) => sum + byte * 2 ** (8 * byteIndex), 0);
        const big = [...encoded].reduce((sum, byte) => sum * 256 + byte, 0);
        const parsedValue = wanted / 8;
        const alternate = parsedValue === little ? big : little;
        if (parsedValue !== alternate && alternate === remaining / 8) {
          add({
            code: "probable_endianness_mismatch",
            severity: "error",
            confidence: .99,
            message: `长度字段“${lengthEvent.name}”很可能使用了错误的字节序。`,
            evidence: [`原始字节：${[...encoded].map((byte) => byte.toString(16).padStart(2, "0")).join(" ")}。`, `当前长度=${parsedValue}，相反字节序=${alternate}，恰好等于剩余字节数。`],
            log_lines: [lengthEvent.line, index + 1],
            xml_locations: xmlIndex.locate(lengthEvent.name),
          });
        } else {
          add({
            code: "unexpected_end_of_input",
            severity: "error",
            confidence: .96,
            message: `元素“${event?.name ?? "未知字段"}”需要 ${buffer[1]} bit，但只剩 ${buffer[2]} bit。`,
            evidence: [event ? `最远解析到第 ${event.offset}/${event.total} byte。` : "日志中没有可用的字段位置。"],
            log_lines: [index + 1],
            xml_locations: event ? xmlIndex.locate(event.name, event.tag) : [],
          });
        }
      } else {
      add({
        code: "unexpected_end_of_input",
        severity: "error",
        confidence: .96,
        message: `元素“${event?.name ?? "未知字段"}”需要 ${buffer[1]} bit，但只剩 ${buffer[2]} bit。`,
        evidence: [event ? `最远解析到第 ${event.offset}/${event.total} byte。` : "日志中没有可用的字段位置。"],
        log_lines: [index + 1],
        xml_locations: event ? xmlIndex.locate(event.name, event.tag) : [],
      });
      }
      if (event && remaining === 0) {
        const key = `${event.offset}:${event.total}:${event.name}`;
        const group = eofGroups.get(key) ?? { event, count: 0 };
        group.count += 1;
        eofGroups.set(key, group);
      }
      const branch = [...events].reverse().find((candidate) => candidate.line < (event?.line ?? index + 1) && candidate.tag === "DataModel");
      const choice = branch && [...events].reverse().find((candidate) => candidate.line < branch.line && candidate.tag === "Choice" && candidate.depth < branch.depth);
      const token = branch && events.find((candidate) => candidate.line > branch.line && candidate.line < index + 1 && ["type", "packet_type", "submessage_id"].includes(candidate.name) && candidate.value !== null);
      if (branch && choice && token && !lines.slice(token.line - 1, index + 1).some((candidate) => /Token did not match/.test(candidate))) {
        add({
          code: "matched_choice_branch_failed",
          severity: "error",
          confidence: .92,
          message: `Choice 分支“${branch.name}”的 token 已匹配，但随后在“${event?.name ?? "未知字段"}”附近失败。`,
          evidence: [`判别字段“${token.name}”在第 ${token.offset} byte 匹配，分支随后到达第 ${event?.offset ?? "?"} byte。`],
          log_lines: [branch.line, index + 1],
          xml_locations: xmlIndex.locate(branch.name),
        });
      }
    }
    if (/No valid children were found/.test(line)) {
      const choice = [...events].reverse().find((event) => event.line <= index + 1 && event.tag === "Choice");
      if (choice) add({
        code: "choice_has_no_valid_branch",
        severity: "error",
        confidence: .97,
        message: `Choice“${choice.name}”在第 ${choice.offset}/${choice.total} byte 没有可用分支。`,
        evidence: ["可能存在重复边界错误、缺少变体或无法识别的判别字段。"],
        log_lines: [choice.line, index + 1],
        xml_locations: xmlIndex.locate(choice.name, "Choice"),
      });
    }
  });

  eofGroups.forEach(({ event, count }) => {
    if (count < 2) return;
    add({
      code: "all_choice_branches_hit_eof",
      severity: "error",
      confidence: .94,
      message: `${count} 个候选分支都要求在 EOF 处读取“${event.name}”；模型可能缺少空分支或允许的重复次数过少。`,
      evidence: [`EOF 位于第 ${event.offset}/${event.total} byte。`],
      log_lines: [],
      xml_locations: xmlIndex.locate(event.name),
    });
  });

  const parseFailureLine = lines.findIndex((line) => PARSE_FAILURE_RE.test(line));
  if (parseFailureLine >= 0) {
    const furthest = events.reduce<LogEvent | null>((best, event) => !best || event.offset > best.offset ? event : best, null);
    add({
      code: "parse_failed",
      severity: "summary",
      confidence: 1,
      message: `测试种子“${seed}”解析失败。`,
      evidence: [furthest ? `最远解析到第 ${furthest.offset}/${furthest.total} byte。` : "日志中没有字段偏移。"],
      log_lines: [parseFailureLine + 1],
      xml_locations: furthest ? xmlIndex.locate(furthest.name, furthest.tag) : [],
    });
  }
  if (lines.some((line) => ROUNDTRIP_RE.test(line))) {
    let firstDifference = -1;
    if (original && parsed) {
      const limit = Math.min(original.length, parsed.length);
      for (let index = 0; index < limit; index += 1) if (original[index] !== parsed[index]) { firstDifference = index; break; }
      if (firstDifference < 0) firstDifference = limit;
    }
    const covering = firstDifference >= 0 ? events.filter((event) => event.sizeBytes !== null && event.offset <= firstDifference && firstDifference < event.offset + event.sizeBytes) : [];
    const candidate = covering.at(-1);
    add({
      code: "roundtrip_bytes_mismatch",
      severity: "error",
      confidence: 1,
      message: `DataModel 重新序列化后的字节与原始测试数据不一致${candidate ? `，首个差异靠近“${candidate.name}”` : ""}。`,
      evidence: firstDifference >= 0 && original && parsed ? [`首个差异位于第 ${firstDifference} byte；原始长度=${original.length}，解析后长度=${parsed.length}。`] : ["日志报告了 round-trip 字节不匹配。"],
      log_lines: [],
      xml_locations: candidate ? xmlIndex.locate(candidate.name, candidate.tag) : [],
    });
  }
  return { log: name, seed, diagnostics: deduplicate(diagnostics) };
}

function staticDiagnostics(index: ReturnType<typeof indexXml>) {
  const diagnostics: Diagnostic[] = [];
  for (let i = 0; i < index.nodes.length; i += 1) {
    const node = index.nodes[i];
    if (!["Blob", "String", "Optional", "Block"].includes(node.tag)) continue;
    let end = i + 1;
    while (end < index.nodes.length && index.nodes[end].depth > node.depth) end += 1;
    const next = index.nodes[end];
    if (!next || next.depth !== node.depth) continue;
    const trailing = node.tag === "Blob" || node.tag === "String" ? node : index.nodes.slice(i + 1, end).filter((candidate) => candidate.tag !== "Relation").at(-1);
    if (!trailing || !["Blob", "String"].includes(trailing.tag) || trailing.attributes.length || trailing.attributes.lengthType) continue;
    diagnostics.push({
      code: "unbounded_element_before_sibling",
      severity: "warning",
      confidence: .88,
      message: `无界 ${node.tag}“${node.name ?? "未命名字段"}”后仍有同级字段，可能会吞掉后续字节。`,
      evidence: [`后续字段：${next.tag}“${next.name ?? "未命名字段"}”。`],
      log_lines: [],
      xml_locations: [xmlLocation(node)],
    });
  }
  return diagnostics;
}

export function diagnoseDatamodel(datamodel: string, logs: Array<{ name: string; content: string }>): DiagnosisReport {
  const index = indexXml(datamodel);
  const reports = logs.map((log) => diagnoseLog(log.name, log.content, index));
  const groups = new Map<string, DiagnosisReport["cross_log_summary"][number]>();
  reports.forEach((report) => report.diagnostics.filter((item) => item.severity !== "summary").forEach((item) => {
    const locations = item.xml_locations.map((location) => `${location.line}:${location.tag}:${location.name}`).join("|");
    const key = `${item.code}\0${locations || item.message}`;
    const group = groups.get(key) ?? { code: item.code, severity: item.severity, confidence: item.confidence, xml_locations: item.xml_locations, seeds: [] };
    group.confidence = Math.max(group.confidence, item.confidence);
    if (!group.seeds.includes(report.seed)) group.seeds.push(report.seed);
    groups.set(key, group);
  }));
  return {
    datamodel: "uploaded-datamodel.xml",
    logs_analyzed: reports.length,
    cross_log_summary: [...groups.values()].filter((group) => group.seeds.length >= 2).sort((a, b) => b.seeds.length - a.seeds.length || b.confidence - a.confidence),
    reports,
    static_diagnostics: staticDiagnostics(index),
  };
}
