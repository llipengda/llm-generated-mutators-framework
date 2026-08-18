"use client";

import {
  AlertTriangle,
  Box,
  Braces,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Copy,
  Download,
  FileCode2,
  FileUp,
  Gauge,
  GitBranch,
  Hash,
  Layers3,
  Link2,
  Plus,
  Redo2,
  Shapes,
  Stethoscope,
  Trash2,
  Type,
  Undo2,
  X,
} from "lucide-react";
import dagre from "@dagrejs/dagre";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import DEMO_PIT from "./demoPit";
import type { DiagnosisReport, XmlLocation } from "../lib/datamodel-diagnoser";

const KIND_META: Record<string, { label: string; icon: typeof Box; color: string; description: string }> = {
  DataModel: { label: "DataModel", icon: Layers3, color: "blue", description: "可复用的协议数据模型。" },
  Block: { label: "Block", icon: Box, color: "slate", description: "按照 wire order 组织字段的容器。" },
  Number: { label: "Number", icon: Hash, color: "amber", description: "固定宽度的数值字段。" },
  String: { label: "String", icon: Type, color: "green", description: "ASCII、UTF-8 或 UTF-16 字符串字段。" },
  Blob: { label: "Blob", icon: Braces, color: "violet", description: "原始二进制数据。" },
  Choice: { label: "Choice", icon: GitBranch, color: "cyan", description: "在多个候选数据结构之间选择。" },
  Optional: { label: "Optional", icon: CircleDot, color: "orange", description: "expression 成立时包含的条件字段。" },
  Relation: { label: "Relation", icon: Link2, color: "rose", description: "字段之间的 size、count 或 offset 关系。" },
  MqttVarInt: { label: "MqttVarInt", icon: Gauge, color: "teal", description: "MQTT 可变字节整数。" },
};

const CHILD_TYPES = ["Block", "Number", "String", "Blob", "Choice", "Optional", "Relation"];
type Path = number[];
type LengthInfo = { minBits: number; maxBits: number | null };
type ActiveRelation = { sourceKey: string; targetKey: string } | null;
type ChoiceSelections = Record<string, number>;
type MergedAncestor = { field: Element; renderKey: string };

function modelNameOf(element: Element) {
  let current: Element | null = element;
  while (current && current.localName !== "DataModel") current = current.parentElement;
  return current?.getAttribute("name") || null;
}

function matchesDiagnosticLocation(element: Element, locations: XmlLocation[]) {
  const name = element.getAttribute("name");
  const model = modelNameOf(element);
  return locations.some((location) =>
    location.tag === element.localName &&
    (!location.name || location.name === name) &&
    (!location.model || location.model === model)
  );
}

function containsDiagnosticLocation(element: Element, byName: Map<string, Element>, locations: XmlLocation[], stack = new Set<string>()): boolean {
  if (matchesDiagnosticLocation(element, locations)) return true;
  const ref = element.getAttribute("ref");
  if (ref) {
    if (stack.has(ref)) return false;
    const target = byName.get(ref);
    if (!target) return false;
    const next = new Set(stack); next.add(ref);
    return children(target).some((child) => containsDiagnosticLocation(child, byName, locations, next));
  }
  return children(element).some((child) => containsDiagnosticLocation(child, byName, locations, stack));
}

function children(node: Element) { return Array.from(node.children); }
function nameOf(el: Element) { return el.getAttribute("name") || el.getAttribute("ref") || el.localName; }
function parsePit(xml: string) {
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  return doc.querySelector("parsererror") ? null : doc;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeLocation(value: unknown, doc: XMLDocument): XmlLocation | null {
  if (!isRecord(value)) return null;
  const descriptor = typeof value.element === "string" ? value.element.toLowerCase() : "";
  const suppliedName = typeof value.name === "string" ? value.name : null;
  const suppliedTag = typeof value.tag === "string" ? value.tag : null;
  const candidates = Array.from(doc.getElementsByTagName("*"));
  const inferred = candidates
    .filter((element) => {
      const name = element.getAttribute("name");
      return Boolean(name && descriptor.includes(name.toLowerCase()));
    })
    .sort((left, right) => (right.getAttribute("name")?.length ?? 0) - (left.getAttribute("name")?.length ?? 0))[0];
  const attributes = isRecord(value.attributes)
    ? Object.fromEntries(Object.entries(value.attributes).filter((entry): entry is [string, string] => typeof entry[1] === "string"))
    : inferred ? Object.fromEntries(Array.from(inferred.attributes).map((attribute) => [attribute.name, attribute.value])) : {};
  const line = Number(value.line);
  return {
    line: Number.isFinite(line) ? line : 0,
    tag: suppliedTag || inferred?.localName || "",
    name: suppliedName || inferred?.getAttribute("name") || null,
    model: typeof value.model === "string" ? value.model : inferred ? modelNameOf(inferred) : null,
    attributes,
  };
}

function normalizeDiagnosis(value: unknown, doc: XMLDocument): DiagnosisReport {
  if (!isRecord(value)) throw new Error("诊断结果必须是 JSON 对象。");
  const judgment = isRecord(value.llm_judgment) ? value.llm_judgment : null;
  if (!judgment || judgment.status === "error") {
    const message = judgment && typeof judgment.error === "string" ? judgment.error : "诊断结果中没有可用的 LLM 结论。";
    throw new Error(message);
  }
  const analysis = isRecord(judgment.analysis) ? judgment.analysis : null;
  const rootCauses = analysis && Array.isArray(analysis.root_causes) ? analysis.root_causes.filter(isRecord) : [];
  const priority = analysis && Array.isArray(analysis.priority_order) ? analysis.priority_order.map(String) : [];
  const rankedCause = priority.map((id) => rootCauses.find((cause) => cause.id === id)).find(Boolean) || rootCauses[0];
  const legacyCause = isRecord(judgment.root_cause) ? judgment.root_cause : null;
  const cause = rankedCause || legacyCause;
  if (!cause) throw new Error("诊断结果中没有 root cause。");
  const locations = Array.isArray(cause.xml_locations)
    ? cause.xml_locations.map((location) => normalizeLocation(location, doc)).filter((location): location is XmlLocation => Boolean(location))
    : [];
  const classification = ["root_cause", "contributing_factor", "symptom", "uncertain"].includes(String(cause.classification))
    ? cause.classification as "root_cause" | "contributing_factor" | "symptom" | "uncertain"
    : "uncertain";
  const categories = ["reference", "endianness", "layout", "choice", "cardinality", "boundary", "other"];
  const category = categories.includes(String(cause.category)) ? cause.category as "reference" | "endianness" | "layout" | "choice" | "cardinality" | "boundary" | "other" : "other";
  return {
    datamodel: typeof value.datamodel === "string" ? value.datamodel : "uploaded-datamodel.xml",
    logs_analyzed: Number.isFinite(Number(value.logs_analyzed)) ? Number(value.logs_analyzed) : 0,
    cross_log_summary: [],
    reports: [],
    static_diagnostics: [],
    llm_judgment: {
      model: typeof judgment.model === "string" ? judgment.model : "unknown",
      root_cause: {
        title: typeof cause.title === "string" ? cause.title : "未命名根因",
        classification,
        category,
        confidence: Number.isFinite(Number(cause.confidence)) ? Number(cause.confidence) : 0,
        affected_seeds: Array.isArray(cause.affected_seeds) ? cause.affected_seeds.map(String) : [],
        xml_locations: locations,
        reasoning: typeof cause.reasoning === "string" ? cause.reasoning : "",
        evidence: Array.isArray(cause.evidence) ? cause.evidence.map(String) : [],
        suggested_fix: typeof cause.suggested_fix === "string" ? cause.suggested_fix : null,
        verification: typeof cause.verification === "string" ? cause.verification : "",
      },
    },
  };
}
function serialize(doc: XMLDocument) {
  const xml = new XMLSerializer().serializeToString(doc);
  return xml.startsWith("<?xml") ? xml : `<?xml version="1.0" encoding="utf-8"?>\n${xml}`;
}
function cloneDoc(doc: XMLDocument) { return parsePit(serialize(doc))!; }
function getAtPath(doc: XMLDocument, path: Path | null) {
  if (!path) return null;
  let current: Element | null = doc.documentElement;
  for (const index of path) current = current ? children(current)[index] ?? null : null;
  return current;
}
function pathFor(element: Element): Path {
  const path: number[] = [];
  let current: Element | null = element;
  while (current?.parentElement) {
    path.unshift(children(current.parentElement).indexOf(current));
    current = current.parentElement;
  }
  return path;
}
function modelsOf(doc: XMLDocument) {
  return Array.from(doc.getElementsByTagNameNS("*", "DataModel"));
}
function findPacketStructure(doc: XMLDocument) {
  const models = modelsOf(doc);
  const byName = new Map(models.map((model) => [model.getAttribute("name") || "", model]));
  const entry = models.find((model) => /_packet_array$/i.test(model.getAttribute("name") || "")) || null;
  return { entry, byName };
}

function relationSummary(field: Element) {
  const ref = field.getAttribute("ref");
  if (ref) return { icon: Link2, text: ref, kind: "ref" };
  const relation = Array.from(field.getElementsByTagNameNS("*", "Relation"))[0];
  if (relation) return { icon: Link2, text: `${relation.getAttribute("type") || "relation"} → ${relation.getAttribute("of") || "field"}`, kind: "relation" };
  if (field.localName === "Optional") return { icon: CircleDot, text: `if ${field.getAttribute("src") || "expression"}`, kind: "condition" };
  return null;
}

function addLengths(parts: LengthInfo[]): LengthInfo {
  return parts.reduce((sum, part) => ({
    minBits: sum.minBits + part.minBits,
    maxBits: sum.maxBits === null || part.maxBits === null ? null : sum.maxBits + part.maxBits,
  }), { minBits: 0, maxBits: 0 as number | null });
}

function lengthOf(element: Element, byName: Map<string, Element>, stack = new Set<string>()): LengthInfo {
  const ref = element.getAttribute("ref");
  if (ref) {
    if (stack.has(ref)) return { minBits: 0, maxBits: null };
    const target = byName.get(ref);
    if (!target) return { minBits: 0, maxBits: null };
    const next = new Set(stack); next.add(ref);
    return lengthOf(target, byName, next);
  }
  if (element.localName === "Relation") return { minBits: 0, maxBits: 0 };
  if (element.localName === "Number") {
    const bits = Number(element.getAttribute("size"));
    return Number.isFinite(bits) && bits > 0 ? { minBits: bits, maxBits: bits } : { minBits: 0, maxBits: null };
  }
  if (element.localName === "MqttVarInt") return { minBits: 8, maxBits: 32 };
  const length = Number(element.getAttribute("length"));
  if (Number.isFinite(length) && length >= 0) return { minBits: length * 8, maxBits: length * 8 };
  if (element.localName === "String" && element.hasAttribute("value")) {
    const bytes = new TextEncoder().encode(element.getAttribute("value") || "").length + (element.getAttribute("nullTerminated") === "true" ? 1 : 0);
    return { minBits: bytes * 8, maxBits: bytes * 8 };
  }
  if (["String", "Blob"].includes(element.localName)) return { minBits: 0, maxBits: null };

  const childLengths = children(element).filter((child) => child.localName !== "Relation").map((child) => lengthOf(child, byName, stack));
  let result: LengthInfo;
  if (element.localName === "Choice" && childLengths.length) {
    result = {
      minBits: Math.min(...childLengths.map((item) => item.minBits)),
      maxBits: childLengths.some((item) => item.maxBits === null) ? null : Math.max(...childLengths.map((item) => item.maxBits as number)),
    };
  } else result = addLengths(childLengths);

  const minOccurs = Number(element.getAttribute("minOccurs") || "1");
  const maxRaw = element.getAttribute("maxOccurs");
  const maxOccurs = maxRaw === "unbounded" ? null : Number(maxRaw || "1");
  if (element.localName === "Optional") return { minBits: 0, maxBits: result.maxBits };
  return {
    minBits: result.minBits * (Number.isFinite(minOccurs) ? minOccurs : 1),
    maxBits: result.maxBits === null || maxOccurs === null || !Number.isFinite(maxOccurs) ? null : result.maxBits * maxOccurs,
  };
}

function fixedLength(element: Element, byName: Map<string, Element>): { value: number; unit: "BIT" | "BYTE" } | null {
  const info = lengthOf(element, byName);
  if (info.maxBits === null || info.minBits !== info.maxBits || info.minBits <= 0) return null;
  if (element.localName === "Number" && element.hasAttribute("size")) return { value: Number(element.getAttribute("size")), unit: "BIT" };
  if (element.hasAttribute("length")) return { value: Number(element.getAttribute("length")), unit: "BYTE" };
  if (element.localName === "String" && element.hasAttribute("value")) return { value: info.minBits / 8, unit: "BYTE" };
  return info.minBits % 8 === 0 ? { value: info.minBits / 8, unit: "BYTE" } : { value: info.minBits, unit: "BIT" };
}

function escapeTokenText(value: string) {
  let escaped = "";
  for (const character of value) {
    const codePoint = character.codePointAt(0)!;
    if (character === "\\") escaped += "\\\\";
    else if (character === '"') escaped += '\\"';
    else if (character === "\0") escaped += "\\0";
    else if (character === "\b") escaped += "\\b";
    else if (character === "\t") escaped += "\\t";
    else if (character === "\n") escaped += "\\n";
    else if (character === "\v") escaped += "\\v";
    else if (character === "\f") escaped += "\\f";
    else if (character === "\r") escaped += "\\r";
    else if (
      codePoint === 0x20 ||
      (codePoint >= 0x01 && codePoint <= 0x1f) ||
      (codePoint >= 0x7f && codePoint <= 0x9f) ||
      codePoint === 0x00a0 || codePoint === 0x1680 ||
      (codePoint >= 0x2000 && codePoint <= 0x200f) ||
      codePoint === 0x2028 || codePoint === 0x2029 || codePoint === 0x202f ||
      codePoint === 0x205f || codePoint === 0x2060 || codePoint === 0x3000 || codePoint === 0xfeff
    ) {
      escaped += codePoint <= 0xff
        ? `\\x${codePoint.toString(16).toUpperCase().padStart(2, "0")}`
        : `\\u${codePoint.toString(16).toUpperCase().padStart(4, "0")}`;
    } else escaped += character;
  }
  return escaped;
}

function tokenValueLabel(field: Element) {
  if (field.getAttribute("token") !== "true" || !field.hasAttribute("value")) return null;
  const value = field.getAttribute("value") || "";
  return field.localName === "Number" ? value : `"${escapeTokenText(value)}"`;
}

function occurrenceRange(field: Element) {
  if (!field.hasAttribute("minOccurs") && !field.hasAttribute("maxOccurs")) return null;
  const min = field.getAttribute("minOccurs");
  const max = field.getAttribute("maxOccurs");
  const label = min && max ? `${min}..${max}` : min ? `min ${min}` : `max ${max}`;
  return { min, max, label };
}

function estimateVisualWeight(field: Element, byName: Map<string, Element>, stack = new Set<string>(), depth = 0): number {
  const ref = field.getAttribute("ref");
  if (ref) {
    if (stack.has(ref)) return 1;
    const target = byName.get(ref);
    if (!target) return 1;
    const nextStack = new Set(stack); nextStack.add(ref);
    return estimateVisualWeight(target, byName, nextStack, depth);
  }
  const nested = children(field).filter((child) => child.localName !== "Relation");
  if (nested.length === 0) return 1;
  if (field.localName === "Choice") return estimateVisualWeight(nested[0], byName, stack, depth) + .2;
  if (nested.length === 1 && !field.getElementsByTagNameNS("*", "Relation").length) return estimateVisualWeight(nested[0], byName, stack, depth) + .15;
  if (depth >= 2) return 1.15;
  const weights = nested.map((child) => estimateVisualWeight(child, byName, stack, depth + 1));
  if (weights.length > 3) {
    const columns = [0, 0];
    weights.forEach((weight) => { const target = columns[0] <= columns[1] ? 0 : 1; columns[target] += weight; });
    return Math.max(...columns) + .55;
  }
  return weights.reduce((sum, weight) => sum + weight, .55);
}

function verticalColumns<T>(items: T[], weightOf: (item: T) => number) {
  if (items.length <= 3) return [items];
  const columns: T[][] = [[], []];
  const weights = [0, 0];
  items.forEach((item) => {
    const target = weights[0] <= weights[1] ? 0 : 1;
    columns[target].push(item);
    weights[target] += weightOf(item);
  });
  return columns;
}

function preferredHeaderWidth(head: HTMLElement) {
  const style = getComputedStyle(head);
  const directChildren = Array.from(head.children).filter((child): child is HTMLElement => child instanceof HTMLElement && !child.classList.contains("field-spacer"));
  const gap = Number.parseFloat(style.columnGap || style.gap) || 0;
  let width = (Number.parseFloat(style.paddingLeft) || 0) + (Number.parseFloat(style.paddingRight) || 0) + gap * Math.max(0, directChildren.length - 1);
  directChildren.forEach((child) => {
    if (!child.classList.contains("field-copy")) {
      width += child.getBoundingClientRect().width;
      return;
    }
    const title = child.querySelector<HTMLElement>(":scope > strong");
    const type = child.querySelector<HTMLElement>(":scope > small");
    const mergedPath = child.querySelector<HTMLElement>(":scope > .merged-path");
    const titleWidth = title?.scrollWidth || 0;
    const typeWidth = type?.scrollWidth || 0;
    width += mergedPath ? Math.max(mergedPath.scrollWidth, titleWidth + typeWidth + 7) : Math.max(titleWidth, typeWidth);
  });
  return width + 18;
}

function preferredItemWidth(item: HTMLElement) {
  const head = item.querySelector<HTMLElement>(":scope > .inline-field > .inline-field-head");
  let width = head ? preferredHeaderWidth(head) : 0;
  item.querySelectorAll<HTMLElement>("[data-adaptive-grid][data-minimum-width]").forEach((grid) => {
    width = Math.max(width, Number.parseFloat(grid.dataset.minimumWidth || "0") || 0);
  });
  return width;
}

function AdaptiveFieldGrid({ className, twoColumns, children }: { className: string; twoColumns: boolean; children: ReactNode }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [singleColumn, setSingleColumn] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let frame = 0;
    const checkFit = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const items = Array.from(container.querySelectorAll<HTMLElement>(":scope > .flow-column > .flow-item"));
        const itemWidths = items.map(preferredItemWidth);
        const style = getComputedStyle(container);
        const horizontalPadding = (Number.parseFloat(style.paddingLeft) || 0) + (Number.parseFloat(style.paddingRight) || 0);
        const minimumWidth = Math.ceil(Math.max(0, ...itemWidths) + horizontalPadding);
        if (container.dataset.minimumWidth !== String(minimumWidth)) container.dataset.minimumWidth = String(minimumWidth);
        const columnWidth = (container.clientWidth - 9) / 2;
        const shouldUseSingleColumn = twoColumns && itemWidths.some((width) => width > columnWidth);
        setSingleColumn((current) => current === shouldUseSingleColumn ? current : shouldUseSingleColumn);
      });
    };
    const resizeObserver = new ResizeObserver(checkFit);
    const mutationObserver = new MutationObserver(checkFit);
    resizeObserver.observe(container);
    container.querySelectorAll<HTMLElement>(":scope > .flow-column > .flow-item > .inline-field > .inline-field-head").forEach((head) => resizeObserver.observe(head));
    mutationObserver.observe(container, { attributes: true, attributeFilter: ["data-minimum-width"], childList: true, characterData: true, subtree: true });
    document.fonts?.ready.then(checkFit);
    checkFit();
    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      mutationObserver.disconnect();
    };
  }, [twoColumns]);

  return <div ref={containerRef} data-adaptive-grid className={`${className} ${twoColumns ? "two-columns" : ""} ${singleColumn ? "adaptive-single-column" : ""}`}>{children}</div>;
}

function InlineField({ field, byName, stack, onSelect, activeRelation, onRelationChange, choiceSelections, onChoiceChange, expandedFields, onToggleExpanded, diagnosticLocations, renderKey, depth = 0, mergedAncestors = [] }: {
  field: Element;
  byName: Map<string, Element>;
  stack: Set<string>;
  onSelect: (field: Element) => void;
  activeRelation: ActiveRelation;
  onRelationChange: (relation: ActiveRelation) => void;
  choiceSelections: ChoiceSelections;
  onChoiceChange: (key: string, index: number) => void;
  expandedFields: Set<string>;
  onToggleExpanded: (key: string) => void;
  diagnosticLocations: XmlLocation[];
  renderKey: string;
  depth?: number;
  mergedAncestors?: MergedAncestor[];
}) {
  const meta = KIND_META[field.localName] || { label: field.localName, icon: Shapes, color: "slate", description: "Peach 扩展元素。" };
  const Icon = meta.icon;
  const relation = relationSummary(field);
  const RelationIcon = relation?.icon;
  const ref = field.getAttribute("ref");
  const target = ref ? byName.get(ref) : null;
  const circular = Boolean(ref && stack.has(ref));
  const ownChildren = children(field).filter((child) => child.localName !== "Relation");
  const nested = target && !circular ? children(target) : ownChildren;
  const nextStack = new Set(stack);
  if (ref) nextStack.add(ref);
  const isChoice = field.localName === "Choice";
  const length = fixedLength(field, byName);
  const tokenValue = tokenValueLabel(field);
  const occurrence = occurrenceRange(field);
  const relationElement = Array.from(field.getElementsByTagNameNS("*", "Relation"))[0];
  const relationTargetName = relationElement?.getAttribute("of") || "";
  const isRelationSource = activeRelation?.sourceKey === renderKey;
  const isRelationTarget = activeRelation?.targetKey === renderKey;
  const nestedContainerKey = `${renderKey}/${target ? "ref" : "children"}`;
  const diagnosticChoiceIndex = diagnosticLocations.length && isChoice ? nested.findIndex((child) => containsDiagnosticLocation(child, byName, diagnosticLocations, nextStack)) : -1;
  const selectedChoiceIndex = diagnosticChoiceIndex >= 0 ? diagnosticChoiceIndex : Math.min(choiceSelections[renderKey] ?? 0, Math.max(0, nested.length - 1));
  const visibleNested = isChoice ? nested.map((child, index) => ({ child, index })).filter(({ index }) => index === selectedChoiceIndex) : nested.map((child, index) => ({ child, index }));
  const isGroup = visibleNested.length > 0 && !circular;
  const mergedIsRelationTarget = mergedAncestors.some((ancestor) => activeRelation?.targetKey === ancestor.renderKey);
  const isDiagnosed = matchesDiagnosticLocation(field, diagnosticLocations) || mergedAncestors.some((ancestor) => matchesDiagnosticLocation(ancestor.field, diagnosticLocations));
  const containsDiagnosis = diagnosticLocations.length > 0 && containsDiagnosticLocation(field, byName, diagnosticLocations, stack);
  // Keep every container visible while diagnosing so off-path containers can
  // be collapsed, including shallow and single-child blocks normally flattened.
  if (isGroup && visibleNested.length === 1 && !relationElement && diagnosticLocations.length === 0) {
    const [{ child, index }] = visibleNested;
    return <InlineField field={child} byName={byName} stack={nextStack} onSelect={onSelect} activeRelation={activeRelation} onRelationChange={onRelationChange} choiceSelections={choiceSelections} onChoiceChange={onChoiceChange} expandedFields={expandedFields} onToggleExpanded={onToggleExpanded} diagnosticLocations={diagnosticLocations} renderKey={`${nestedContainerKey}/${index}`} depth={depth} mergedAncestors={[...mergedAncestors, { field, renderKey }]} />;
  }
  const canCollapse = isGroup && (depth >= 2 || diagnosticLocations.length > 0);
  const collapsed = canCollapse && (diagnosticLocations.length > 0 ? !containsDiagnosis : !expandedFields.has(renderKey));
  const activateRelation = (button: HTMLElement) => {
    if (!relationElement || !relationTargetName) return;
    const sourceWrapper = button.parentElement;
    let scope = sourceWrapper?.parentElement?.closest<HTMLElement>(".root-children, .inline-children, .choice-selected") || sourceWrapper?.parentElement || null;
    while (scope) {
      const directMatch = Array.from(scope.children)
        .find((candidate) => candidate instanceof HTMLElement && candidate.dataset.fieldName === relationTargetName && candidate.dataset.renderKey !== renderKey) as HTMLElement | undefined;
      const match = directMatch || Array.from(scope.querySelectorAll<HTMLElement>("[data-field-name]"))
        .find((candidate) => candidate.dataset.fieldName === relationTargetName && candidate.dataset.renderKey !== renderKey);
      if (match?.dataset.renderKey) {
        onRelationChange({ sourceKey: renderKey, targetKey: match.dataset.renderKey });
        return;
      }
      const parentField = scope.closest<HTMLElement>(".inline-field");
      scope = parentField?.parentElement?.closest<HTMLElement>(".root-children, .inline-children, .choice-selected") || parentField?.parentElement || null;
    }
  };
  return (
    <div data-render-key={renderKey} data-field-name={field.getAttribute("name") || ""} className={`inline-field ${isGroup ? "wire-group" : "wire-leaf"} depth-${Math.min(depth, 4)} ${collapsed ? "is-collapsed" : ""} ${isChoice ? "inline-choice" : ""} ${occurrence ? "inline-array" : ""} ${target ? "inline-ref" : ""} ${isRelationTarget || mergedIsRelationTarget ? "relation-target-highlight" : ""} ${isDiagnosed ? "diagnostic-highlight" : ""}`}>
      <div
        role="button"
        tabIndex={0}
        className={`inline-field-head ${isRelationSource ? "relation-source-active" : ""}`}
        onClick={() => onSelect(field)}
        onKeyDown={(event) => { if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); onSelect(field); } }}
        onMouseEnter={(event) => activateRelation(event.currentTarget)}
        onMouseLeave={() => isRelationSource && onRelationChange(null)}
        onFocus={(event) => activateRelation(event.currentTarget)}
        onBlur={() => isRelationSource && onRelationChange(null)}
      >
        <span className={`kind-icon kind-${meta.color}`}><Icon size={14} /></span>
        <span className={`field-copy ${mergedAncestors.length > 0 ? "has-merged-path" : ""}`}>
          {mergedAncestors.length > 0 && <span className="merged-path">{mergedAncestors.map((ancestor) => {
            const ancestorOccurrence = occurrenceRange(ancestor.field);
            const ancestorCondition = ancestor.field.localName === "Optional" ? `if ${ancestor.field.getAttribute("src") || "expression"}` : null;
            const ancestorDetails = [ancestorOccurrence?.label, ancestorCondition].filter(Boolean) as string[];
            const ancestorRef = ancestor.field.getAttribute("ref");
            const ancestorTarget = ancestorRef ? byName.get(ancestorRef) : null;
            const ancestorChoices = ancestor.field.localName === "Choice" ? children(ancestorTarget || ancestor.field).filter((child) => child.localName !== "Relation") : [];
            const ancestorChoiceIndex = Math.min(choiceSelections[ancestor.renderKey] ?? 0, Math.max(0, ancestorChoices.length - 1));
            return <span className="merged-segment" key={ancestor.renderKey}>
              <button type="button" data-render-key={ancestor.renderKey} data-field-name={ancestor.field.getAttribute("name") || ""} title={`编辑 ${nameOf(ancestor.field)}${ancestorCondition ? ` · ${ancestorCondition}` : ""}`} onClick={(event) => { event.stopPropagation(); onSelect(ancestor.field); }}>{ancestorOccurrence && <Layers3 className="merged-array-icon" size={12} />}{nameOf(ancestor.field)}{ancestorDetails.map((detail) => <em key={detail}>{detail}</em>)}</button>
              {ancestorChoices.length > 0 && <select className="merged-choice-select" aria-label={`${nameOf(ancestor.field)} 的 Choice 选项`} value={ancestorChoiceIndex} onClick={(event) => event.stopPropagation()} onChange={(event) => { event.stopPropagation(); onChoiceChange(ancestor.renderKey, Number(event.target.value)); }}>{ancestorChoices.map((choice, index) => <option key={`${nameOf(choice)}-${index}`} value={index}>{nameOf(choice)}</option>)}</select>}
              <i>/</i>
            </span>;
          })}</span>}
          <strong>{nameOf(field)}</strong><small>{meta.label}</small>
        </span>
        {canCollapse && <button type="button" className="collapse-toggle collapse-summary" aria-expanded={!collapsed} title={`${collapsed ? "展开" : "收起"} ${nameOf(field)}`} onClick={(event) => { event.stopPropagation(); onToggleExpanded(renderKey); }}>{collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}<span>{collapsed ? `${visibleNested.length} 个字段` : "收起"}</span></button>}
        <span className="field-spacer" />
        {field.getAttribute("token") === "true" && <span className="fixed-mark">TOKEN</span>}
        {tokenValue !== null && <span className="token-value" title={`value=${tokenValue}`}><span>value</span><code>{tokenValue}</code></span>}
        {occurrence && <span className="occurs-badge" title={[occurrence.min && `minOccurs=${occurrence.min}`, occurrence.max && `maxOccurs=${occurrence.max}`].filter(Boolean).join("; ")}><span className="occurs-icon"><Layers3 size={15} strokeWidth={2.4} /></span><span className="occurs-copy"><span>ARRAY</span><strong>{occurrence.label}</strong></span></span>}
        {relation && RelationIcon && <span className={`relation-chip relation-${relation.kind}`} title={relation.text}><RelationIcon size={11} /><span>{relation.text}</span></span>}
        {length && <span className={`length-badge length-${length.unit.toLowerCase()}`}><strong>{length.value}</strong><span>{length.unit}</span></span>}
      </div>
      {circular && <div className="circular-note"><AlertTriangle size={12} />检测到指向 {ref} 的循环引用，已停止展开。</div>}
      {isChoice && nested.length > 0 && !circular && !collapsed && (
        <label className="choice-control">
          <span><GitBranch size={12} />Choice 选项</span>
          <select value={selectedChoiceIndex} onChange={(event) => onChoiceChange(renderKey, Number(event.target.value))}>
            {nested.map((child, index) => <option key={`${nameOf(child)}-${index}`} value={index}>{nameOf(child)}{child.getAttribute("ref") ? ` · ${child.getAttribute("ref")}` : ""}</option>)}
          </select>
        </label>
      )}
      {visibleNested.length > 0 && !circular && !collapsed && (
        <AdaptiveFieldGrid className={isChoice ? "choice-selected" : "inline-children"} twoColumns={visibleNested.length > 3}>
          {verticalColumns(visibleNested, ({ child }) => estimateVisualWeight(child, byName, nextStack, depth + 1)).map((column, columnIndex) => <div className="flow-column" key={`column-${columnIndex}`}>{column.map(({ child, index }) => <div className="flow-item" style={{ order: index }} key={`${child.localName}-${nameOf(child)}-${index}`}><InlineField field={child} byName={byName} stack={nextStack} onSelect={onSelect} activeRelation={activeRelation} onRelationChange={onRelationChange} choiceSelections={choiceSelections} onChoiceChange={onChoiceChange} expandedFields={expandedFields} onToggleExpanded={onToggleExpanded} diagnosticLocations={diagnosticLocations} renderKey={`${nestedContainerKey}/${index}`} depth={depth + 1} /></div>)}</div>)}
        </AdaptiveFieldGrid>
      )}
      {target && !circular && <div className="inline-ref-label"><Link2 size={10} />{ref} · inline 展开</div>}
    </div>
  );
}

function ProtocolCanvas({ entry, byName, onSelect, activeRelation, onRelationChange, choiceSelections, onChoiceChange, expandedFields, onToggleExpanded, diagnosticLocations }: { entry: Element; byName: Map<string, Element>; onSelect: (field: Element) => void; activeRelation: ActiveRelation; onRelationChange: (relation: ActiveRelation) => void; choiceSelections: ChoiceSelections; onChoiceChange: (key: string, index: number) => void; expandedFields: Set<string>; onToggleExpanded: (key: string) => void; diagnosticLocations: XmlLocation[] }) {
  const entryName = entry.getAttribute("name") || "packet_array";
  const length = fixedLength(entry, byName);
  const entryChildren = children(entry);
  return (
    <div className="root-model">
      <button className="root-model-head" onClick={() => onSelect(entry)}>
        <span className="root-index"><Layers3 size={18} /></span>
        <span><strong>{entryName}</strong></span>
        <span className="field-spacer" />
        {length && <span className={`length-badge root-length length-${length.unit.toLowerCase()}`}><strong>{length.value}</strong><span>{length.unit}</span></span>}
      </button>
      <AdaptiveFieldGrid className="root-children" twoColumns={entryChildren.length > 3}>
        {verticalColumns(entryChildren.map((field, index) => ({ field, index })), ({ field }) => estimateVisualWeight(field, byName, new Set([entryName]))).map((column, columnIndex) => <div className="flow-column" key={`root-column-${columnIndex}`}>{column.map(({ field, index }) => <div className="flow-item" style={{ order: index }} key={`${field.localName}-${nameOf(field)}-${index}`}><InlineField field={field} byName={byName} stack={new Set([entryName])} onSelect={onSelect} activeRelation={activeRelation} onRelationChange={onRelationChange} choiceSelections={choiceSelections} onChoiceChange={onChoiceChange} expandedFields={expandedFields} onToggleExpanded={onToggleExpanded} diagnosticLocations={diagnosticLocations} renderKey={`entry/${index}`} /></div>)}</div>)}
      </AdaptiveFieldGrid>
    </div>
  );
}

function resolvedTreeChildren(field: Element, byName: Map<string, Element>, stack: Set<string>) {
  const ref = field.getAttribute("ref");
  const target = ref && !stack.has(ref) ? byName.get(ref) : null;
  return children(target || field).filter((child) => child.localName !== "Relation");
}

type TopologyNodeData = {
  label: string;
  kind: string;
  color: string;
  fieldPath: Path;
  active: boolean;
  diagnosed: boolean;
  isArray: boolean;
  isRef: boolean;
  circular: boolean;
  hasChildren: boolean;
  open: boolean;
  onToggle: (id: string) => void;
};
type TopologyNode = Node<TopologyNodeData, "pitNode">;

function PitTopologyNode({ id, data }: NodeProps<TopologyNode>) {
  const meta = KIND_META[data.kind] || { label: data.kind, icon: Shapes, color: "slate", description: "Peach 扩展元素。" };
  return (
    <div className={`flow-pit-node kind-${data.color} ${data.active ? "is-active" : ""} ${data.diagnosed ? "is-diagnosed" : ""}`} data-node-label={`${data.label} · ${meta.label}${data.diagnosed ? " · 诊断命中" : ""}${data.circular ? " · 循环引用" : ""}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      {data.isRef && <span className="flow-node-dot is-ref" />}
      {data.isArray && <span className="flow-node-dot is-array" />}
      {data.hasChildren && <button className="flow-node-toggle nodrag nopan" aria-label={data.open ? `折叠 ${data.label}` : `展开 ${data.label}`} onClick={(event) => { event.stopPropagation(); data.onToggle(id); }}>{data.open ? <ChevronDown size={10} /> : <ChevronRight size={10} />}</button>}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

const TOPOLOGY_NODE_TYPES = { pitNode: PitTopologyNode };

function buildTopologyGraph(entry: Element, byName: Map<string, Element>, selectedNodeId: string, openOverrides: Record<string, boolean>, onToggle: (id: string) => void, diagnosticLocations: XmlLocation[]) {
  const nodes: TopologyNode[] = [];
  const edges: Edge[] = [];
  const entryName = entry.getAttribute("name") || "packet_array";
  const walk = (field: Element, id: string, parentId: string | null, depth: number, stack: Set<string>) => {
    const ref = field.getAttribute("ref");
    const circular = Boolean(ref && stack.has(ref));
    const nested = circular ? [] : resolvedTreeChildren(field, byName, stack);
    const onDiagnosticPath = diagnosticLocations.length > 0 && containsDiagnosticLocation(field, byName, diagnosticLocations, stack);
    const open = diagnosticLocations.length > 0 ? onDiagnosticPath : (openOverrides[id] ?? true);
    const meta = KIND_META[field.localName] || { label: field.localName, icon: Shapes, color: "slate", description: "Peach 扩展元素。" };
    nodes.push({
      id,
      type: "pitNode",
      position: { x: 0, y: 0 },
      data: {
        label: nameOf(field),
        kind: field.localName,
        color: meta.color,
        fieldPath: pathFor(field),
        active: id === selectedNodeId,
        diagnosed: matchesDiagnosticLocation(field, diagnosticLocations),
        isArray: Boolean(occurrenceRange(field)),
        isRef: Boolean(ref),
        circular,
        hasChildren: nested.length > 0,
        open,
        onToggle,
      },
    });
    if (parentId) edges.push({ id: `${parentId}->${id}`, source: parentId, target: id, type: "bezier", style: { stroke: "#8098aa", strokeWidth: 1.35, opacity: .82 } });
    if (!open) return;
    const nextStack = new Set(stack);
    if (ref) nextStack.add(ref);
    nested.forEach((child, index) => walk(child, `${id}/${index}`, id, depth + 1, nextStack));
  };
  walk(entry, "entry", null, 0, new Set([entryName]));

  const graph = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "LR", nodesep: 16, ranksep: 44, marginx: 20, marginy: 20 });
  nodes.forEach((node) => graph.setNode(node.id, { width: 30, height: 30 }));
  edges.forEach((edge) => graph.setEdge(edge.source, edge.target));
  dagre.layout(graph);
  nodes.forEach((node) => {
    const position = graph.node(node.id);
    node.position = { x: position.x - 15, y: position.y - 15 };
  });
  return { nodes, edges };
}

function TopologyGraph({ entry, byName, selectedNodeId, onSelect, diagnosticLocations }: { entry: Element; byName: Map<string, Element>; selectedNodeId: string; onSelect: (path: Path, nodeId: string) => void; diagnosticLocations: XmlLocation[] }) {
  const [openOverrides, setOpenOverrides] = useState<Record<string, boolean>>({});
  const toggle = useCallback((id: string) => setOpenOverrides((current) => ({ ...current, [id]: !(current[id] ?? true) })), []);
  const { nodes, edges } = useMemo(() => buildTopologyGraph(entry, byName, selectedNodeId, openOverrides, toggle, diagnosticLocations), [entry, byName, selectedNodeId, openOverrides, toggle, diagnosticLocations]);
  return (
    <ReactFlow<TopologyNode, Edge>
      nodes={nodes}
      edges={edges}
      nodeTypes={TOPOLOGY_NODE_TYPES}
      onNodeClick={(_, node) => onSelect(node.data.fieldPath, node.id)}
      nodesDraggable={false}
      nodesConnectable={false}
      edgesFocusable={false}
      fitView
      fitViewOptions={{ padding: .24, minZoom: .24, maxZoom: 1.3 }}
      minZoom={.16}
      maxZoom={2.2}
      panOnScroll
      panOnScrollSpeed={.8}
      zoomOnScroll={false}
      zoomOnPinch
      onlyRenderVisibleElements
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#c8d3dc" />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}

function ProtocolNodeCanvas({ field, byName, onEdit, activeRelation, onRelationChange, choiceSelections, onChoiceChange, expandedFields, onToggleExpanded, diagnosticLocations }: { field: Element; byName: Map<string, Element>; onEdit: (field: Element) => void; activeRelation: ActiveRelation; onRelationChange: (relation: ActiveRelation) => void; choiceSelections: ChoiceSelections; onChoiceChange: (key: string, index: number) => void; expandedFields: Set<string>; onToggleExpanded: (key: string) => void; diagnosticLocations: XmlLocation[] }) {
  const meta = KIND_META[field.localName] || { label: field.localName, icon: Shapes, color: "slate", description: "Peach 扩展元素。" };
  const Icon = meta.icon;
  const length = fixedLength(field, byName);
  const ref = field.getAttribute("ref");
  const target = ref ? byName.get(ref) : null;
  const nested = children(target || field).filter((child) => child.localName !== "Relation");
  const visibleFields = nested.length > 0 ? nested : [field];
  const rootStack = new Set<string>();
  if (field.localName === "DataModel" && field.getAttribute("name")) rootStack.add(field.getAttribute("name")!);
  if (ref) rootStack.add(ref);
  const renderPrefix = `tree-root/${pathFor(field).join("-") || "entry"}`;
  return (
    <section className="tree-canvas-panel" aria-live="polite">
      <div className="root-model tree-root-model">
        <button className="root-model-head tree-root-model-head" onClick={() => onEdit(field)}>
          <span className={`root-index kind-${meta.color}`}><Icon size={17} /></span>
          <span><strong>{nameOf(field)}</strong><em>{meta.label}{ref ? ` · ref ${ref}` : ""}</em></span>
          <span className="field-spacer" />
          <span className="tree-root-edit"><FileCode2 size={12} />编辑</span>
          {length && <span className={`length-badge root-length length-${length.unit.toLowerCase()}`}><strong>{length.value}</strong><span>{length.unit}</span></span>}
        </button>
        <AdaptiveFieldGrid className="root-children tree-root-children" twoColumns={visibleFields.length > 3}>
          {verticalColumns(visibleFields.map((child, index) => ({ child, index })), ({ child }) => estimateVisualWeight(child, byName, rootStack)).map((column, columnIndex) => <div className="flow-column" key={`tree-column-${columnIndex}`}>{column.map(({ child, index }) => <div className="flow-item" style={{ order: index }} key={`${child.localName}-${nameOf(child)}-${index}`}><InlineField field={child} byName={byName} stack={rootStack} onSelect={onEdit} activeRelation={activeRelation} onRelationChange={onRelationChange} choiceSelections={choiceSelections} onChoiceChange={onChoiceChange} expandedFields={expandedFields} onToggleExpanded={onToggleExpanded} diagnosticLocations={diagnosticLocations} renderKey={`${renderPrefix}/${index}`} /></div>)}</div>)}
        </AdaptiveFieldGrid>
      </div>
    </section>
  );
}

function ProtocolTree({ entry, byName, selectedPath, selectedNodeId, onSelect, onEdit, activeRelation, onRelationChange, choiceSelections, onChoiceChange, expandedFields, onToggleExpanded, diagnosticLocations }: { entry: Element; byName: Map<string, Element>; selectedPath: Path | null; selectedNodeId: string; onSelect: (path: Path, nodeId: string) => void; onEdit: (field: Element) => void; activeRelation: ActiveRelation; onRelationChange: (relation: ActiveRelation) => void; choiceSelections: ChoiceSelections; onChoiceChange: (key: string, index: number) => void; expandedFields: Set<string>; onToggleExpanded: (key: string) => void; diagnosticLocations: XmlLocation[] }) {
  const effectiveSelectedPath = selectedPath || pathFor(entry);
  const selected = getAtPath(entry.ownerDocument, effectiveSelectedPath) || entry;
  return (
    <div className="tree-workspace">
      <aside className="structure-tree-panel">
        <div className="structure-tree-head"><GitBranch size={14} /><span>结构树</span></div>
        <div className="structure-tree-scroll"><TopologyGraph entry={entry} byName={byName} selectedNodeId={selectedNodeId} onSelect={onSelect} diagnosticLocations={diagnosticLocations} /></div>
      </aside>
      <ProtocolNodeCanvas field={selected} byName={byName} onEdit={onEdit} activeRelation={activeRelation} onRelationChange={onRelationChange} choiceSelections={choiceSelections} onChoiceChange={onChoiceChange} expandedFields={expandedFields} onToggleExpanded={onToggleExpanded} diagnosticLocations={diagnosticLocations} />
    </div>
  );
}

function DiagnosisPanel({ report, fileNames, onClear }: { report: DiagnosisReport; fileNames: string[]; onClear: () => void }) {
  const rootCause = report.llm_judgment?.root_cause;
  return (
    <section className="diagnosis-panel" aria-live="polite">
      <div className="diagnosis-head">
        <div className="diagnosis-title-icon"><Stethoscope size={18} /></div>
        <div><h2>测试结果诊断</h2></div>
        <button className="diagnosis-clear" onClick={onClear}>清除诊断</button>
      </div>
        <div className="diagnosis-files">诊断文件：{fileNames.join("、")}</div>
      {!rootCause ? (
        <div className="diagnosis-empty"><CheckCircle2 size={20} /><div><strong>诊断结果中没有单一根因</strong><span>请重新生成并上传有效的诊断 JSON。</span></div></div>
      ) : (
        <div className="diagnosis-grid">
          <article className="diagnosis-card">
            <div className="diagnosis-card-top"><code>{rootCause.category}</code></div>
            <h3>{rootCause.title}</h3>
            <p className="diagnosis-reasoning">{rootCause.reasoning}</p>
            <div className="diagnosis-seed">测试：{rootCause.affected_seeds.join("、") || "未指定"}</div>
            {rootCause.xml_locations.length > 0 && <div className="diagnosis-locations">{rootCause.xml_locations.map((location, locationIndex) => <span key={`${location.line}-${locationIndex}`}><FileCode2 size={11} />第 {location.line} 行 · {location.model ? `${location.model} / ` : ""}{location.tag}{location.name ? ` “${location.name}”` : ""}</span>)}</div>}
            {rootCause.evidence.length > 0 && <ul>{rootCause.evidence.map((evidence, evidenceIndex) => <li key={evidenceIndex}>{evidence}</li>)}</ul>}
            {rootCause.suggested_fix && <div className="diagnosis-fix"><strong>建议修复</strong>{rootCause.suggested_fix}</div>}
            <div className="diagnosis-verification"><strong>验证方式</strong>{rootCause.verification}</div>
          </article>
        </div>
      )}
    </section>
  );
}

export default function Home() {
  const [doc, setDoc] = useState<XMLDocument | null>(null);
  const [fileName, setFileName] = useState("mqtt-example.xml");
  const [selectedPath, setSelectedPath] = useState<Path | null>(null);
  const [history, setHistory] = useState<string[]>([]);
  const [future, setFuture] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [activeRelation, setActiveRelation] = useState<ActiveRelation>(null);
  const [choiceSelections, setChoiceSelections] = useState<ChoiceSelections>({});
  const [expandedFields, setExpandedFields] = useState<Set<string>>(() => new Set());
  const [viewMode, setViewMode] = useState<"canvas" | "tree">("canvas");
  const [treeSelectedPath, setTreeSelectedPath] = useState<Path | null>(null);
  const [treeSelectedNodeId, setTreeSelectedNodeId] = useState("entry");
  const [addType, setAddType] = useState("Block");
  const [diagnosis, setDiagnosis] = useState<DiagnosisReport | null>(null);
  const [diagnosisFiles, setDiagnosisFiles] = useState<string[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);
  const resultInput = useRef<HTMLInputElement>(null);
  const diagnosisPanel = useRef<HTMLDivElement>(null);

  // DOMParser is browser-only; load the bundled demo after hydration.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => setDoc(parsePit(DEMO_PIT)), []);

  const structure = useMemo(() => doc ? findPacketStructure(doc) : null, [doc]);
  const selected = doc ? getAtPath(doc, selectedPath) : null;
  const entryName = structure?.entry?.getAttribute("name") || "entry_not_found";
  const protocolName = entryName.replace(/_packet_array$/i, "") || "protocol";
  const diagnosticLocations = useMemo(() => {
    const all = diagnosis?.llm_judgment?.root_cause?.xml_locations ?? [];
    const seen = new Set<string>();
    return all.filter((location) => {
      const key = `${location.model}:${location.tag}:${location.name}:${location.line}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [diagnosis]);

  const commit = useCallback((mutator: (draft: XMLDocument) => void) => {
    if (!doc) return;
    const before = serialize(doc);
    const draft = cloneDoc(doc);
    mutator(draft);
    setHistory((items) => [...items.slice(-39), before]);
    setFuture([]);
    setDoc(draft);
    setDirty(true);
    setDiagnosis(null);
    setDiagnosisFiles([]);
  }, [doc]);

  const updateAttribute = (name: string, value: string) => commit((draft) => {
    const el = getAtPath(draft, selectedPath);
    if (!el) return;
    if (value === "") el.removeAttribute(name); else el.setAttribute(name, value);
  });

  const addAttribute = () => {
    const name = window.prompt("Attribute 名称（例如 length、ref、minOccurs）");
    if (!name?.trim()) return;
    const value = window.prompt(`${name.trim()} 的 value`, "value");
    if (value !== null) updateAttribute(name.trim(), value);
  };

  const changeElementType = (type: string) => commit((draft) => {
    const el = getAtPath(draft, selectedPath);
    if (!el || el.localName === "DataModel") return;
    const replacement = draft.createElementNS(el.namespaceURI, type);
    Array.from(el.attributes).forEach((attr) => replacement.setAttributeNS(attr.namespaceURI, attr.name, attr.value));
    while (el.firstChild) replacement.appendChild(el.firstChild);
    el.parentNode?.replaceChild(replacement, el);
  });

  const addChild = () => commit((draft) => {
    const parent = getAtPath(draft, selectedPath);
    if (!parent) return;
    const child = draft.createElementNS(parent.namespaceURI, addType);
    if (addType !== "Relation") child.setAttribute("name", `new_${addType.toLowerCase()}`);
    if (addType === "Number") child.setAttribute("size", "8");
    if (addType === "Relation") { child.setAttribute("type", "size"); child.setAttribute("of", "target_field"); }
    parent.appendChild(child);
  });

  const duplicateSelected = () => commit((draft) => {
    const el = getAtPath(draft, selectedPath);
    if (!el?.parentElement) return;
    const copy = el.cloneNode(true) as Element;
    if (copy.hasAttribute("name")) copy.setAttribute("name", `${copy.getAttribute("name")}_copy`);
    el.parentElement.insertBefore(copy, el.nextSibling);
  });

  const removeSelected = () => {
    if (!selected || selected.localName === "DataModel") return;
    commit((draft) => getAtPath(draft, selectedPath)?.remove());
    setSelectedPath(null);
    setActiveRelation(null);
    setChoiceSelections({});
    setExpandedFields(new Set());
    setTreeSelectedPath(null);
    setTreeSelectedNodeId("entry");
  };

  const undo = () => {
    if (!doc || !history.length) return;
    setFuture((items) => [serialize(doc), ...items]);
    setDoc(parsePit(history[history.length - 1]));
    setHistory((items) => items.slice(0, -1));
    setSelectedPath(null);
    setActiveRelation(null);
    setChoiceSelections({});
    setExpandedFields(new Set());
    setTreeSelectedPath(null);
    setTreeSelectedNodeId("entry");
  };
  const redo = () => {
    if (!doc || !future.length) return;
    setHistory((items) => [...items, serialize(doc)]);
    setDoc(parsePit(future[0]));
    setFuture((items) => items.slice(1));
    setSelectedPath(null);
    setActiveRelation(null);
    setChoiceSelections({});
    setExpandedFields(new Set());
    setTreeSelectedPath(null);
    setTreeSelectedNodeId("entry");
  };

  const loadFile = async (file?: File) => {
    if (!file) return;
    const parsed = parsePit(await file.text());
    if (!parsed || parsed.documentElement.localName !== "Peach") {
      setError("无法读取该文件，请确认它是有效的 Peach Pit 文件。" );
      return;
    }
    const nextStructure = findPacketStructure(parsed);
    if (!nextStructure.entry) {
      setError("未找到 *_packet_array 入口，该文件不符合当前支持的 Pit 结构。" );
      return;
    }
    setDoc(parsed);
    setFileName(file.name);
    setHistory([]);
    setFuture([]);
    setDirty(false);
    setSelectedPath(null);
    setActiveRelation(null);
    setChoiceSelections({});
    setExpandedFields(new Set());
    setTreeSelectedPath(null);
    setTreeSelectedNodeId("entry");
    setDiagnosis(null);
    setDiagnosisFiles([]);
    setError("");
  };

  const loadDiagnosis = async (file?: File) => {
    if (!doc || !file) return;
    setError("");
    try {
      const payload: unknown = JSON.parse(await file.text());
      setDiagnosis(normalizeDiagnosis(payload, doc));
      setDiagnosisFiles([file.name]);
      setViewMode("canvas");
      setSelectedPath(null);
      setActiveRelation(null);
      setExpandedFields(new Set());
      requestAnimationFrame(() => diagnosisPanel.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "诊断结果导入失败，请检查 JSON 格式后重试。");
    }
  };

  const download = () => {
    if (!doc) return;
    const url = URL.createObjectURL(new Blob([serialize(doc)], { type: "application/xml;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = fileName.replace(/\.(xml|pit)$/i, "") + "-edited.xml";
    link.click();
    URL.revokeObjectURL(url);
    setDirty(false);
  };

  const selectElement = (element: Element) => setSelectedPath(pathFor(element));
  const changeChoice = (key: string, index: number) => {
    setChoiceSelections((current) => ({ ...current, [key]: index }));
    setActiveRelation(null);
    setExpandedFields(new Set());
  };
  const toggleExpanded = (key: string) => setExpandedFields((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const attributes = selected ? Array.from(selected.attributes).filter((attr) => !attr.name.startsWith("xmlns")) : [];
  const selectedMeta = selected ? KIND_META[selected.localName] || { label: selected.localName, icon: Shapes, color: "slate", description: "Peach 扩展元素。" } : null;

  return (
    <main className="app-shell">
      <input ref={fileInput} type="file" accept=".xml,.pit,text/xml,application/xml" hidden onChange={(event) => loadFile(event.target.files?.[0])} />
      <input ref={resultInput} type="file" accept=".json,application/json" hidden onChange={(event) => { void loadDiagnosis(event.target.files?.[0]); event.target.value = ""; }} />
      <header className="topbar">
        <div className="file-status"><FileCode2 size={15} /><span className="file-name">{fileName}</span>{dirty && <span className="dirty-pill">未保存</span>}</div>
        <div className="toolbar">
          <button className="icon-button" aria-label="撤销" disabled={!history.length} onClick={undo}><Undo2 size={17} /></button>
          <button className="icon-button" aria-label="重做" disabled={!future.length} onClick={redo}><Redo2 size={17} /></button>
          <span className="toolbar-divider" />
          <button className="secondary-button" onClick={() => fileInput.current?.click()}><FileUp size={16} /> 打开文件</button>
          <button className="diagnose-button" disabled={!doc} onClick={() => resultInput.current?.click()}><Stethoscope size={16} /> 上传诊断结果</button>
          <button className="primary-button compact" onClick={download}><Download size={16} /> 导出 Pit</button>
        </div>
      </header>

      {error && <div className="error-banner"><AlertTriangle size={16} />{error}<button onClick={() => setError("")}><X size={15} /></button></div>}

      {doc && structure && (
        <div className="protocol-workspace">
          <section className="protocol-board">
            <div className="board-head">
              <div><h1>{protocolName.toUpperCase()} <span>DataModel</span></h1></div>
              <div className="view-switch" role="group" aria-label="展示模式">
                <button className={viewMode === "canvas" ? "is-active" : ""} aria-pressed={viewMode === "canvas"} onClick={() => setViewMode("canvas")}><Layers3 size={14} />协议画布</button>
                <button className={viewMode === "tree" ? "is-active" : ""} aria-pressed={viewMode === "tree"} onClick={() => setViewMode("tree")}><GitBranch size={14} />树形结构</button>
              </div>
            </div>
            {structure.entry ? viewMode === "canvas" ? <ProtocolCanvas entry={structure.entry} byName={structure.byName} onSelect={selectElement} activeRelation={activeRelation} onRelationChange={setActiveRelation} choiceSelections={choiceSelections} onChoiceChange={changeChoice} expandedFields={expandedFields} onToggleExpanded={toggleExpanded} diagnosticLocations={diagnosticLocations} /> : <ProtocolTree entry={structure.entry} byName={structure.byName} selectedPath={treeSelectedPath} selectedNodeId={treeSelectedNodeId} onSelect={(path, nodeId) => { setTreeSelectedPath(path); setTreeSelectedNodeId(nodeId); }} onEdit={selectElement} activeRelation={activeRelation} onRelationChange={setActiveRelation} choiceSelections={choiceSelections} onChoiceChange={changeChoice} expandedFields={expandedFields} onToggleExpanded={toggleExpanded} diagnosticLocations={diagnosticLocations} /> : <div className="empty-packets"><GitBranch size={24} /><strong>未找到协议入口</strong><span>文件必须包含以 _packet_array 结尾的 DataModel。</span></div>}
          </section>
          {diagnosis && <div ref={diagnosisPanel}><DiagnosisPanel report={diagnosis} fileNames={diagnosisFiles} onClear={() => { setDiagnosis(null); setDiagnosisFiles([]); }} /></div>}
        </div>
      )}

      {selected && selectedMeta && (
        <div className="drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setSelectedPath(null)}>
          <aside className="inspector">
            <button className="drawer-close" aria-label="关闭属性面板" onClick={() => setSelectedPath(null)}><X size={17} /></button>
            <div className="inspector-header">
              <div className={`large-kind kind-${selectedMeta.color}`}><selectedMeta.icon size={19} /></div>
              <div><span>{selectedMeta.label}</span><h2>{nameOf(selected)}</h2></div>
            </div>
            <div className="form-section">
              <div className="section-label">字段设置</div>
              {selected.localName !== "DataModel" && (
                <label className="field-label"><span>元素类型 <em>字段类型</em></span><select value={selected.localName} onChange={(event) => changeElementType(event.target.value)}>{CHILD_TYPES.concat(selected.localName).filter((v, i, a) => a.indexOf(v) === i).map((type) => <option key={type}>{type}</option>)}</select></label>
              )}
              {attributes.map((attr) => (
                <label className="field-label" key={attr.name}><span>{attr.name}<em>{attributeHelp(attr.name)}</em></span>{attr.name === "token" ? <select value={attr.value} onChange={(event) => updateAttribute(attr.name, event.target.value)}><option value="true">true · 不可变异</option><option value="false">false · 可变 value</option></select> : <input value={attr.value} onChange={(event) => updateAttribute(attr.name, event.target.value)} />}</label>
              ))}
              {!selected.hasAttribute("name") && selected.localName !== "Relation" && <button className="add-attribute" onClick={() => updateAttribute("name", "unnamed_field")}><Plus size={14} /> 添加 name</button>}
              <button className="add-attribute" onClick={addAttribute}><Plus size={14} /> 添加 Attribute</button>
            </div>
            {!['Number', 'String', 'Blob', 'Relation'].includes(selected.localName) && (
              <div className="form-section"><div className="section-label">添加子字段</div><div className="add-row"><select value={addType} onChange={(event) => setAddType(event.target.value)}>{CHILD_TYPES.map((type) => <option key={type}>{KIND_META[type]?.label || type}</option>)}</select><button onClick={addChild}><Plus size={16} /> 添加</button></div></div>
            )}
            <div className="inspector-actions"><button onClick={duplicateSelected}><Copy size={15} /> 复制</button>{selected.localName !== "DataModel" && <button className="danger-action" onClick={removeSelected}><Trash2 size={15} /> 删除</button>}</div>
          </aside>
        </div>
      )}
    </main>
  );
}

function attributeHelp(name: string) {
  const help: Record<string, string> = {
    name: "字段名称", ref: "引用的 DataModel", size: "长度（bit）", length: "长度（byte）",
    value: "默认值", token: "是否不可变异", type: "类型", minOccurs: "最小出现次数",
    maxOccurs: "最大出现次数", src: "条件的来源字段", expression: "条件表达式", of: "Relation 的目标字段",
  };
  return help[name] || "Peach 元素 Attribute";
}
