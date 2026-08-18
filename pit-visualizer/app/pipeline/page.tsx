"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, FileArchive, GripHorizontal, LoaderCircle, Play, Sparkles, Terminal, Wrench } from "lucide-react";

const API = process.env.NEXT_PUBLIC_PEACH_API_URL || "http://127.0.0.1:8000";

type Job = {
  id: string; protocol: string; status: string; phase: string; message: string;
  packet_types: string[]; progress: { completed: number; total: number };
  attempts: { datamodel: number; mutator: number }; disabled_mutators: string[];
  artifact: string | null;
};
type EventItem = { id: number; time: string; level: string; message: string; data?: Record<string, unknown> };

const STAGES = ["上传输入", "消息类型", "DataModel", "DataModel 验证", "Mutator", "Mutator 验证", "产物"];

function stageIndex(job: Job | null) {
  if (!job) return 0;
  if (job.phase.includes("packet")) return 1;
  if (job.phase === "datamodel_generation") return 2;
  if (job.phase.includes("datamodel")) return 3;
  if (job.phase === "mutator_generation") return 4;
  if (job.phase.includes("mutator")) return 5;
  return job.status === "completed" ? 6 : 2;
}

export default function PipelinePage() {
  const [job, setJob] = useState<Job | null>(null);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [protocol, setProtocol] = useState("");
  const [packetTypes, setPacketTypes] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const specs = useRef<HTMLInputElement>(null);
  const seeds = useRef<HTMLInputElement>(null);
  const jobId = job?.id;

  const refresh = async (id?: string) => {
    const response = await fetch(id ? `${API}/api/jobs/${id}` : `${API}/api/jobs/active`);
    if (!response.ok) throw new Error("无法读取本地任务状态。");
    const payload = await response.json() as { job: Job | null };
    setJob(payload.job);
    if (payload.job?.status === "awaiting_packet_types") setPacketTypes(payload.job.packet_types);
  };

  useEffect(() => { void Promise.resolve().then(refresh).catch((cause: Error) => setError(cause.message)); }, []);
  useEffect(() => {
    if (!jobId) return;
    void fetch(`${API}/api/jobs/${jobId}/events/history`).then(async (response) => {
      if (!response.ok) throw new Error("无法读取运行输出历史。");
      return response.json() as Promise<{ events: EventItem[] }>;
    }).then((payload) => setEvents(payload.events.slice(-1000))).catch((cause: Error) => setError(cause.message));
    const source = new EventSource(`${API}/api/jobs/${jobId}/events`);
    source.addEventListener("pipeline", (raw) => {
      const event = JSON.parse((raw as MessageEvent).data) as EventItem;
      setEvents((items) => [...items.filter((item) => item.id !== event.id), event].sort((left, right) => left.id - right.id).slice(-1000));
      void refresh(jobId).catch((cause: Error) => setError(cause.message));
    });
    return () => source.close();
  }, [jobId]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setError("");
    const specificationFiles = Array.from(specs.current?.files || []);
    const seedFiles = Array.from(seeds.current?.files || []);
    if (!protocol.trim() || !specificationFiles.length || !seedFiles.length) {
      setError("请填写协议名称，并上传规范和至少一个种子文件。"); return;
    }
    setSubmitting(true);
    try {
      const form = new FormData(); form.append("protocol", protocol);
      specificationFiles.forEach((file) => form.append("specifications", file));
      seedFiles.forEach((file) => form.append("seeds", file));
      const response = await fetch(`${API}/api/jobs`, { method: "POST", body: form });
      const payload = await response.json() as { job?: Job; detail?: string };
      if (!response.ok || !payload.job) throw new Error(payload.detail || "无法创建任务。");
      setEvents([]); setJob(payload.job);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法创建任务。"); }
    finally { setSubmitting(false); }
  };

  const confirmTypes = async () => {
    if (!job) return;
    const cleaned = packetTypes.map((type) => type.trim()).filter(Boolean);
    const response = await fetch(`${API}/api/jobs/${job.id}/packet-types`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ packet_types: cleaned }) });
    if (!response.ok) { setError("消息类型保存失败。"); return; }
    await refresh(job.id);
  };

  const cancel = async () => { if (job) { await fetch(`${API}/api/jobs/${job.id}/cancel`, { method: "POST" }); await refresh(job.id); } };
  const resume = async () => { if (job) { await fetch(`${API}/api/jobs/${job.id}/resume`, { method: "POST" }); await refresh(job.id); } };
  const startFresh = async () => {
    if (!job) return;
    const response = await fetch(`${API}/api/jobs/${job.id}/start-fresh`, { method: "POST" });
    if (!response.ok) { const payload = await response.json() as { detail?: string }; setError(payload.detail || "无法开始新会话。"); return; }
    setEvents([]); setPacketTypes([]); setProtocol(""); setJob(null);
  };
  const progress = job?.progress.total ? Math.round(job.progress.completed / job.progress.total * 100) : 0;

  return <main className={`pipeline-shell${job ? " has-runtime-console" : ""}`}>
    <header className="pipeline-topbar"><Link href="/" className="pipeline-brand"><Sparkles size={17} />Pit Studio <span>Pipeline</span></Link>{job && <div className="pipeline-header-actions">{job.status === "paused" && <button className="primary-button" onClick={resume}>恢复任务</button>}{!["running", "cancelling"].includes(job.status) && <button className="secondary-button" onClick={startFresh}>开始新任务</button>}<button className="secondary-button" onClick={cancel} disabled={["completed", "failed", "cancelled", "cancelling"].includes(job.status)}>{job.status === "cancelling" ? "正在中断" : "中断任务"}</button></div>}</header>
    {error && <div className="pipeline-error"><AlertTriangle size={16} />{error}</div>}
    {!job ? <section className="pipeline-workspace pipeline-setup">
      <aside className="pipeline-sidebar"><div className="pipeline-side-title"><span>本地任务</span><h1>Peach Pipeline</h1><p>上传规范和种子后，流程会在本机执行。</p></div><StageList active={0} /></aside>
      <section className="pipeline-main-card pipeline-setup-card"><div className="pipeline-card-head"><div><span>第一步</span><h2>创建协议任务</h2></div></div><form className="pipeline-form" onSubmit={submit}>
        <label>协议名称<input value={protocol} onChange={(event) => setProtocol(event.target.value)} placeholder="例如 mqtt" required /></label>
        <label>规范文档 <em>支持多个 PDF 或 TXT</em><input ref={specs} type="file" accept=".pdf,.txt,text/plain,application/pdf" multiple required /></label>
        <label>测试种子 <em>可多选文件或选择文件夹</em><input ref={seeds} type="file" multiple required /></label>
        <button className="primary-button pipeline-start" disabled={submitting}>{submitting ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}{submitting ? "正在创建任务" : "开始生成"}</button>
      </form></section>
    </section> : <><section className="pipeline-workspace">
      <aside className="pipeline-sidebar"><div className="pipeline-side-title"><span>协议任务</span><h1>{job.protocol.toUpperCase()}</h1><Status job={job} /></div><StageList active={stageIndex(job)} /></aside>
      <div className="pipeline-content"><section className="pipeline-main-card">
        <div className="pipeline-card-head"><div><span>当前工作</span><h2>{job.message}</h2></div>{job.status === "running" && <LoaderCircle className="spin pipeline-loader" size={24} />}</div>
        {job.status === "awaiting_packet_types" && <PacketTypeEditor types={packetTypes} setTypes={setPacketTypes} onConfirm={confirmTypes} />}
        {job.status === "awaiting_datamodel_edit" && <div className="manual-repair"><Wrench size={25} /><div><h3>需要人工修复 DataModel</h3><p>诊断信息和命中的字段会在 Pit Studio 中显示。保存后会自动重新验证。</p></div><Link className="primary-button" href={`/?job=${job.id}`}>打开 Pit Studio</Link></div>}
        {job.phase === "mutator_generation" && <div className="progress-wrap"><div><span>Mutator 生成进度</span><strong>{job.progress.completed}/{job.progress.total}</strong></div><div className="progress-track"><i style={{ width: `${progress}%` }} /></div></div>}
        {job.status === "completed" && <a className="primary-button pipeline-download" href={`${API}/api/jobs/${job.id}/artifact`}><FileArchive size={16} />下载产物 ZIP</a>}
        {job.disabled_mutators.length > 0 && <div className="disabled-mutators"><AlertTriangle size={15} />已禁用：{job.disabled_mutators.join("、")}</div>}
      </section></div>
    </section><RuntimeConsole events={events} /></>}
  </main>;
}

function Status({ job }: { job: Job }) { const text = job.status === "completed" ? "已完成" : job.status.startsWith("awaiting") ? "等待操作" : job.status === "failed" ? "失败" : job.status === "paused" ? "已暂停" : job.status === "cancelling" ? "正在中断" : "运行中"; return <span className={`job-status ${job.status}`}>{text}</span>; }
function StageList({ active }: { active: number }) { return <ol className="pipeline-stages">{STAGES.map((stage, index) => <li key={stage} className={index <= active ? "done" : ""}><i>{index < active ? <CheckCircle2 size={14} /> : index + 1}</i><span>{stage}</span></li>)}</ol>; }
function PacketTypeEditor({ types, setTypes, onConfirm }: { types: string[]; setTypes: (types: string[]) => void; onConfirm: () => void }) { return <div className="packet-editor"><p>确认将要建模和生成 Mutator 的消息类型。</p>{types.map((type, index) => <div className="packet-row" key={`${type}-${index}`}><input value={type} onChange={(event) => setTypes(types.map((item, itemIndex) => itemIndex === index ? event.target.value : item))} /><button type="button" onClick={() => setTypes(types.filter((_, itemIndex) => itemIndex !== index))}>移除</button></div>)}<button type="button" className="add-type" onClick={() => setTypes([...types, "NEW_PACKET"])}>+ 添加消息类型</button><button className="primary-button" onClick={onConfirm}>确认并生成 DataModel</button></div>; }
function RuntimeConsole({ events }: { events: EventItem[] }) {
  const scroll = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(380);
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => { if (!collapsed) scroll.current?.scrollTo({ top: scroll.current.scrollHeight }); }, [events, collapsed]);

  const resizeFrom = (startY: number, startHeight: number) => {
    const move = (event: PointerEvent) => setHeight(Math.max(180, Math.min(window.innerHeight - 90, startHeight + startY - event.clientY)));
    const stop = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop);
  };

  return <section className={`runtime-console${collapsed ? " is-collapsed" : ""}`} style={{ height: collapsed ? 55 : height }}>
    <div className="runtime-resize-handle" role="separator" aria-label="调整运行输出面板高度" aria-orientation="horizontal" tabIndex={collapsed ? -1 : 0} onPointerDown={(event) => { event.preventDefault(); resizeFrom(event.clientY, height); }} onKeyDown={(event) => { if (event.key === "ArrowUp") setHeight((value) => Math.min(window.innerHeight - 90, value + 24)); if (event.key === "ArrowDown") setHeight((value) => Math.max(180, value - 24)); }}><GripHorizontal size={18} /></div>
    <div className="runtime-console-head"><Terminal size={17} /><h2>运行输出</h2><span>工具调用、LLM 输出与校验结果</span><button type="button" className="runtime-collapse" aria-expanded={!collapsed} aria-label={collapsed ? "展开运行输出" : "折叠运行输出"} onClick={() => setCollapsed((value) => !value)}>{collapsed ? <ChevronUp size={18} /> : <ChevronDown size={18} />}</button></div>
    {!collapsed && <div className="runtime-console-scroll" ref={scroll}>{events.length === 0 ? <p>等待本地服务输出…</p> : events.map((event) => <RuntimeLine key={event.id} event={event} />)}</div>}
  </section>;
}
function RuntimeLine({ event }: { event: EventItem }) { const kind = String(event.data?.kind || ""); const output = typeof event.data?.output === "string" ? event.data.output : ""; const time = new Date(event.time).toLocaleTimeString(); if (kind === "llm_output" || kind === "validation_result") return <details className={`runtime-result event-${event.level}`} open><summary><time>{time}</time><strong>{event.message}</strong></summary><pre>{output || "没有输出。"}</pre></details>; return <div className={`runtime-line event-${event.level}`}><time>{time}</time><span>{event.message}</span></div>; }
