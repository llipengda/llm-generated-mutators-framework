"""Local HTTP control plane for the Peach generation workflow.

This server is deliberately loopback-only.  It reuses the Python pipeline and
the locally installed Docker/Mono Peach prerequisites; it is not a hosted API.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import signal
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / ".web_runs"
RUNS_DIR.mkdir(exist_ok=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str, fallback: str) -> str:
    name = Path(value).name
    return name if name and name not in {".", ".."} else fallback


def safe_protocol(value: str) -> str:
    protocol = value.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", protocol):
        raise HTTPException(422, "协议名称只能包含小写字母、数字、连字符和下划线，且必须以字母开头。")
    return protocol


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "任务不存在或任务状态已损坏。") from exc


class PacketTypesPayload(BaseModel):
    packet_types: list[str]


class DataModelPayload(BaseModel):
    xml: str


def run_worker_process(entry: Callable[..., None], args: tuple[Any, ...]) -> None:
    """Run one pipeline action in its own process group for hard interruption."""
    os.setsid()
    entry(*args)


class JobManager:
    """A single-job local queue with durable snapshots and SSE events."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events_changed = threading.Condition(self.lock)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.processes: dict[str, multiprocessing.Process] = {}
        self.active_id: str | None = None
        self._load_jobs()

    def _load_jobs(self) -> None:
        for path in sorted(RUNS_DIR.glob("*/job.json"), key=lambda p: p.stat().st_mtime):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            job_id = str(job.get("id", ""))
            if not job_id:
                continue
            if job.get("status") in {"running", "cancelling"}:
                job["status"] = "paused"
                job["message"] = "本地服务已重启；请继续任务。"
                self._write_job(job)
            self.jobs[job_id] = job
            event_path = self.job_dir(job_id) / "events.jsonl"
            lines: list[dict[str, Any]] = []
            if event_path.exists():
                for line in event_path.read_text(encoding="utf-8").splitlines():
                    try:
                        lines.append(json.loads(line))
                    except ValueError:
                        pass
            self.events[job_id] = lines
            if job.get("status") not in {"completed", "failed", "cancelled"}:
                self.active_id = job_id

    def job_dir(self, job_id: str) -> Path:
        return RUNS_DIR / job_id

    def _write_job(self, job: dict[str, Any]) -> None:
        path = self.job_dir(job["id"]) / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def emit(self, job_id: str, level: str, message: str, **data: Any) -> None:
        with self.lock:
            history = self.event_history(job_id)
            event = {
                "id": (history[-1]["id"] if history else 0) + 1,
                "time": now(),
                "level": level,
                "message": message,
                "data": data,
            }
            self.events[job_id].append(event)
            with (self.job_dir(job_id) / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def event_history(self, job_id: str) -> list[dict[str, Any]]:
        event_path = self.job_dir(job_id) / "events.jsonl"
        if not event_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in event_path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def snapshot(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            path = self.job_dir(job_id) / "job.json"
            if not path.exists():
                raise HTTPException(404, "任务不存在。")
            job = read_json(path)
            self.jobs[job_id] = job
            return dict(job)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            job = self.snapshot(job_id)
            job.update(changes)
            job["updated_at"] = now()
            self._write_job(job)
            return job

    def create(self, protocol: str, specs: list[UploadFile], seeds: list[UploadFile]) -> dict[str, Any]:
        with self.lock:
            if self.active_id:
                active = self.snapshot(self.active_id)
                if active and active.get("status") not in {"completed", "failed", "cancelled"}:
                    raise HTTPException(409, "已有活动任务，请先完成、取消或恢复该任务。")
            job_id = uuid.uuid4().hex
            work = self.job_dir(job_id)
            spec_dir, seed_dir = work / "inputs/specs", work / "inputs/seeds"
            spec_dir.mkdir(parents=True)
            seed_dir.mkdir(parents=True)
            job = {
                "id": job_id,
                "protocol": protocol,
                "status": "running",
                "phase": "packet_types",
                "message": "正在提取消息类型。",
                "created_at": now(),
                "updated_at": now(),
                "packet_types": [],
                "progress": {"completed": 0, "total": 0},
                "attempts": {"datamodel": 0, "mutator": 0},
                "disabled_mutators": [],
                "artifact": None,
            }
            self.jobs[job_id] = job
            self.events[job_id] = []
            self.active_id = job_id
            self._write_job(job)
        self._save_uploads(specs, spec_dir, "specification")
        self._save_uploads(seeds, seed_dir, "seed")
        self.emit(job_id, "info", "已接收上传文件。", specifications=len(specs), seeds=len(seeds))
        return job

    @staticmethod
    def _save_uploads(files: list[UploadFile], destination: Path, label: str) -> None:
        if not files:
            raise HTTPException(422, f"请至少上传一个{label}文件。")
        for index, upload in enumerate(files, start=1):
            filename = safe_name(upload.filename or "", f"{label}-{index}")
            target = destination / filename
            suffix = 1
            while target.exists():
                target = destination / f"{target.stem}-{suffix}{target.suffix}"
                suffix += 1
            with target.open("wb") as output:
                shutil.copyfileobj(upload.file, output)

    def start(self, job_id: str, entry: Callable[..., None], *args: Any) -> None:
        context = multiprocessing.get_context("fork")
        process = context.Process(
            target=run_worker_process,
            args=(entry, args),
            name=f"peach-web-{job_id[:8]}",
            daemon=True,
        )
        process.start()
        self.processes[job_id] = process

    def interrupt(self, job_id: str) -> None:
        process = self.processes.get(job_id)
        if not process or not process.is_alive():
            return
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                try:
                    if os.getpgid(process.pid) == process.pid:
                        break
                except ProcessLookupError:
                    return
                time.sleep(0.01)
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.join(timeout=1.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        except ProcessLookupError:
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)

    def finish(self, job_id: str, status: str, message: str) -> None:
        self.update(job_id, status=status, message=message)
        self.emit(job_id, "success" if status == "completed" else "error", message)
        if status in {"completed", "failed", "cancelled"}:
            with self.lock:
                if self.active_id == job_id:
                    self.active_id = None


manager = JobManager()


class WebPipelineRunner:
    """Executes existing Peach stages while replacing terminal prompts with web states."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.job = manager.snapshot(job_id)
        self.protocol = self.job["protocol"]
        self.work = manager.job_dir(job_id)
        self.pipeline: Any = None

    def _pipeline(self) -> Any:
        if self.pipeline is not None:
            return self.pipeline
        from config import build_config_from_args, load_env
        import log as log_module
        import pipeline.base as base_module
        import pipeline.peach as peach_module
        import ui as ui_module
        from pipeline.peach import PeachPipeline

        specs = sorted((self.work / "inputs/specs").iterdir())
        build_config_from_args(
            self.protocol,
            str(self.work / "inputs/seeds"),
            [str(path) for path in specs],
            fixer=False,
            state_dir=str(self.work / "pipeline_state"),
        )
        load_env()
        # The existing stage methods ask through these imported functions.  A
        # web task has explicit API pauses instead, so all non-essential CLI
        # prompts receive their deterministic web defaults.
        peach_module.ask_regenerate = lambda *_args: True
        peach_module.ask_select_types = lambda packet_types, _protocol: list(packet_types)
        peach_module.ask_skip_verification = lambda *_args: False
        peach_module.ask_reuse_diagnosis = lambda *_args: False
        base_module.ask_resume_state = lambda *_args: True
        base_module.ask_after_fix_failure = lambda *_args: "exit"
        # Preserve terminal behavior, while sending concise tool activity,
        # final LLM replies, and complete validator output to the browser.
        ui = ui_module.UI
        if not hasattr(ui, "_web_original_result_markdown"):
            ui._web_original_result_markdown = ui.result_markdown
            ui._web_original_run_with_live_output = ui.run_with_live_output

        def result_markdown(step_title: str, content: str) -> None:
            ui._web_original_result_markdown(step_title, content)
            manager.emit(
                self.job_id,
                "info",
                f"LLM 输出：{step_title}",
                kind="llm_output",
                step=step_title,
                output=str(content),
            )

        def run_with_live_output(cmd: list[str], *, title: str = "", max_lines: int = 20):
            manager.emit(self.job_id, "info", title or "正在运行验证。", kind="validation_start")
            result = ui._web_original_run_with_live_output(cmd, title=title, max_lines=max_lines)
            manager.emit(
                self.job_id,
                "success" if result.returncode == 0 else "error",
                f"{title or '验证'} 已结束。",
                kind="validation_result",
                title=title,
                returncode=result.returncode,
                output=result.stdout,
            )
            return result

        ui.result_markdown = staticmethod(result_markdown)
        ui.run_with_live_output = staticmethod(run_with_live_output)
        log_module.set_runtime_listener(
            lambda message: manager.emit(self.job_id, "info", message, kind="runtime_log")
        )
        self.pipeline = PeachPipeline()
        return self.pipeline

    def _interrupted(self) -> bool:
        """Honor an interrupt between safe pipeline operations."""
        status = manager.snapshot(self.job_id).get("status")
        if status == "cancelling":
            manager.finish(self.job_id, "cancelled", "任务已中断。")
            return True
        return status == "cancelled"

    def _run_step(self, label: str, action: Callable[[], None]) -> bool:
        if self._interrupted():
            return False
        manager.update(self.job_id, status="running", message=label)
        manager.emit(self.job_id, "info", label)
        started = time.monotonic()
        action()
        if self._interrupted():
            return False
        manager.emit(self.job_id, "success", f"{label} 完成。", elapsed_seconds=round(time.monotonic() - started, 1))
        return True

    def extract_packet_types(self) -> None:
        try:
            pipeline = self._pipeline()
            if not self._run_step("正在从规范中提取消息类型。", pipeline.step_1_packet_types_extraction):
                return
            packet_types = list(pipeline.state.get("packet_types") or [])
            manager.update(
                self.job_id,
                status="awaiting_packet_types",
                phase="packet_types",
                message="请确认消息类型。",
                packet_types=packet_types,
            )
            manager.emit(self.job_id, "warning", "消息类型已提取，等待确认。", packet_types=packet_types)
        except Exception as exc:
            self._fail(exc)

    def continue_after_packet_types(self, packet_types: list[str]) -> None:
        try:
            pipeline = self._pipeline()
            pipeline.state["packet_types"] = packet_types
            pipeline.save_state()
            if not self._run_step("正在生成 DataModel。", pipeline.step_2_datamodel_generation):
                return
            manager.update(self.job_id, phase="datamodel_validation", message="正在验证 DataModel。")
            self._validate_datamodel()
        except Exception as exc:
            self._fail(exc)

    def _validate_datamodel(self) -> None:
        pipeline = self._pipeline()
        for attempt in range(0, 4):
            if self._interrupted():
                return
            passed, output = pipeline.verify_datamodel()
            if self._interrupted():
                return
            if passed:
                manager.emit(self.job_id, "success", "DataModel 验证通过。")
                self._generate_mutators()
                return
            if attempt == 3:
                pipeline.diagnose_datamodel_failure(output)
                if self._interrupted():
                    return
                manager.update(
                    self.job_id,
                    status="awaiting_datamodel_edit",
                    phase="datamodel_manual_edit",
                    message="DataModel 自动修复三次后仍未通过，请在 Pit Studio 中修复。",
                    attempts={**manager.snapshot(self.job_id)["attempts"], "datamodel": 3},
                )
                manager.emit(self.job_id, "error", "DataModel 自动修复已耗尽，等待人工编辑。")
                return
            pipeline.diagnose_datamodel_failure(output)
            if self._interrupted():
                return
            self._apply_datamodel_fix(output)
            if self._interrupted():
                return
            attempts = dict(manager.snapshot(self.job_id)["attempts"])
            attempts["datamodel"] = attempt + 1
            manager.update(self.job_id, attempts=attempts)
            manager.emit(self.job_id, "warning", f"DataModel 自动修复 {attempt + 1}/3。")

    def _apply_datamodel_fix(self, output: str) -> None:
        pipeline = self._pipeline()
        from pathlib import Path

        diagnosis_path = Path("./llm/peach") / pipeline.protocol_lower / "datamodel_diagnosis.json"
        prompt = f"""
Repair the current {pipeline.protocol_name} Peach DataModel using only the completed diagnosis report and the current DataModel.
First read {diagnosis_path}, then read ./llm/peach/{pipeline.protocol_lower}/datamodel.xml, and write the repaired XML to that same path. Do not read validator logs or call RFC_Search. Do not simplify the DataModel.
"""
        pipeline.call_agent(prompt, "Web: DataModel Auto-fix", agent_graph=pipeline.datamodel_autofix_agent_graph)

    def verify_manual_datamodel(self) -> None:
        try:
            if self._interrupted():
                return
            manager.update(self.job_id, status="running", message="正在验证人工修复的 DataModel。")
            pipeline = self._pipeline()
            passed, output = pipeline.verify_datamodel()
            if self._interrupted():
                return
            if not passed:
                pipeline.diagnose_datamodel_failure(output)
                if self._interrupted():
                    return
                manager.update(self.job_id, status="awaiting_datamodel_edit", message="验证仍未通过，请继续修复。")
                manager.emit(self.job_id, "error", "人工修复后的 DataModel 仍未通过验证。")
                return
            manager.emit(self.job_id, "success", "人工修复后的 DataModel 验证通过。")
            self._generate_mutators()
        except Exception as exc:
            self._fail(exc)

    def _generate_mutators(self) -> None:
        pipeline = self._pipeline()
        packet_types = list(pipeline.state.get("packet_types") or [])
        manager.update(self.job_id, phase="mutator_generation", progress={"completed": 0, "total": len(packet_types)})
        if not self._run_step("正在生成 Mutator。", pipeline.step_4_mutator_generation):
            return
        manager.update(self.job_id, progress={"completed": len(packet_types), "total": len(packet_types)})
        self._validate_mutators()

    def _validate_mutators(self) -> None:
        pipeline = self._pipeline()
        manager.update(self.job_id, phase="mutator_validation", message="正在验证 Mutator。")
        # step_5 already owns a three-attempt fix/verify loop.  Its terminal
        # prompt is replaced above with "exit", after which the web runner
        # disables only the classes that still have failure logs.
        pipeline.step_5_mutator_validation_and_fix()
        if self._interrupted():
            return
        failures = self._mutator_failures(pipeline)
        if not failures:
            self._compile_and_package()
            return
        attempts = dict(manager.snapshot(self.job_id)["attempts"])
        attempts["mutator"] = 3
        manager.update(self.job_id, attempts=attempts)
        disabled = self._disable_failed_mutators(pipeline, failures)
        manager.update(self.job_id, disabled_mutators=disabled)
        manager.emit(self.job_id, "warning", "已禁用无法修复的 Mutator，并重新验证其余 Mutator。", disabled=disabled)
        pipeline.step_5_mutator_validation_and_fix()
        if self._interrupted():
            return
        remaining = self._mutator_failures(pipeline)
        if remaining:
            raise RuntimeError("禁用失败 Mutator 后，剩余 Mutator 仍未通过验证。")
        self._compile_and_package()

    @staticmethod
    def _mutator_failures(pipeline: Any) -> list[str]:
        base = Path("llm/peach") / pipeline.protocol_lower / "mutator_test_logs"
        names: set[str] = set()
        for folder in (base / "error", base / "fail"):
            if folder.exists():
                names.update(path.stem for path in folder.glob("*.log"))
        return sorted(names)

    @staticmethod
    def _disable_failed_mutators(pipeline: Any, classes: list[str]) -> list[str]:
        mutator_dir = Path("llm/peach") / pipeline.protocol_lower / "Mutators"
        disabled: list[str] = []
        for class_name in classes:
            for source in mutator_dir.glob("*.cs"):
                content = source.read_text(encoding="utf-8")
                match = re.search(rf"(?m)^\\s*public\\s+class\\s+{re.escape(class_name)}\\b", content)
                if not match or f"disabled by Peach Web: {class_name}" in content:
                    continue
                start = match.start()
                line_start = content.rfind("\n", 0, start) + 1
                while line_start > 0:
                    previous = content.rfind("\n", 0, line_start - 1) + 1
                    line = content[previous:line_start].strip()
                    if line.startswith("[") or not line:
                        start = previous
                        line_start = previous
                    else:
                        break
                open_brace = content.find("{", match.end())
                if open_brace < 0:
                    continue
                depth, end = 0, open_brace
                for index in range(open_brace, len(content)):
                    if content[index] == "{":
                        depth += 1
                    elif content[index] == "}":
                        depth -= 1
                        if depth == 0:
                            end = index + 1
                            break
                replacement = f"#if false // disabled by Peach Web: {class_name}\n" + content[start:end] + "\n#endif\n"
                source.write_text(content[:start] + replacement + content[end:], encoding="utf-8")
                disabled.append(class_name)
                break
        return disabled

    def _compile_and_package(self) -> None:
        pipeline = self._pipeline()
        if not self._run_step("正在编译最终 DLL。", pipeline.step_final_compile):
            return
        protocol_dir = ROOT / "llm/peach" / pipeline.protocol_lower
        archive = self.work / f"{pipeline.protocol_lower}-peach-artifacts-{self.job_id[:8]}.zip"
        summary = {
            "protocol": self.protocol,
            "job_id": self.job_id,
            "completed_at": now(),
            "disabled_mutators": manager.snapshot(self.job_id).get("disabled_mutators", []),
            "token_usage": pipeline.state.get("token_usage_total", {}),
        }
        report_dir = self.work / "reports"
        report_dir.mkdir(exist_ok=True)
        (report_dir / "validation-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in [protocol_dir / "datamodel.xml", protocol_dir / f"{pipeline.protocol_upper}.dll"]:
                if path.exists():
                    bundle.write(path, path.name)
            for path in (protocol_dir / "Mutators").glob("*.cs"):
                bundle.write(path, f"Mutators/{path.name}")
            diagnosis = protocol_dir / "datamodel_diagnosis.json"
            if diagnosis.exists():
                bundle.write(diagnosis, "reports/datamodel_diagnosis.json")
            bundle.write(report_dir / "validation-summary.json", "reports/validation-summary.json")
            bundle.writestr("manifest.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        manager.update(self.job_id, artifact=archive.name, phase="completed")
        manager.finish(self.job_id, "completed", "Peach 产物已生成，可以下载。")

    def _fail(self, exc: Exception) -> None:
        manager.finish(self.job_id, "failed", f"任务失败：{exc}")


app = FastAPI(title="Peach Pipeline Local API")
app.add_middleware(
    CORSMiddleware,
    # Vite/vinext may choose 3001+ when 3000 is already in use. Keep this
    # local-only while allowing the development server's selected port.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


@app.get("/api/jobs/active")
def active_job() -> JSONResponse:
    if not manager.active_id:
        return JSONResponse({"job": None})
    return JSONResponse({"job": manager.snapshot(manager.active_id)})


@app.post("/api/jobs")
async def create_job(
    protocol: str = Form(...),
    specifications: list[UploadFile] = File(...),
    seeds: list[UploadFile] = File(...),
) -> JSONResponse:
    job = manager.create(safe_protocol(protocol), specifications, seeds)
    runner = WebPipelineRunner(job["id"])
    manager.start(job["id"], runner.extract_packet_types)
    return JSONResponse({"job": job}, status_code=201)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    return JSONResponse({"job": manager.snapshot(job_id)})


@app.get("/api/jobs/{job_id}/events")
def stream_events(job_id: str, after: int = 0) -> StreamingResponse:
    manager.snapshot(job_id)

    def generate():
        cursor = after
        idle_ticks = 0
        while True:
            pending = [event for event in manager.event_history(job_id) if event["id"] > cursor]
            for event in pending:
                cursor = event["id"]
                yield f"id: {cursor}\nevent: pipeline\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if pending:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 30:
                    yield ": keepalive\n\n"
                    idle_ticks = 0
            if manager.snapshot(job_id).get("status") in {"completed", "failed", "cancelled"} and not pending:
                return
            time.sleep(0.5)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/events/history")
def event_history(job_id: str) -> JSONResponse:
    manager.snapshot(job_id)
    return JSONResponse({"events": manager.event_history(job_id)})


@app.put("/api/jobs/{job_id}/packet-types")
def confirm_packet_types(job_id: str, payload: PacketTypesPayload) -> JSONResponse:
    job = manager.snapshot(job_id)
    if job.get("status") != "awaiting_packet_types":
        raise HTTPException(409, "当前任务不在消息类型确认阶段。")
    types = [item.strip() for item in payload.packet_types if item.strip()]
    if not types:
        raise HTTPException(422, "至少保留一个消息类型。")
    manager.update(job_id, packet_types=types, status="running", phase="datamodel_generation")
    manager.emit(job_id, "info", "消息类型已确认。", packet_types=types)
    manager.start(job_id, WebPipelineRunner(job_id).continue_after_packet_types, types)
    return JSONResponse({"job": manager.snapshot(job_id)})


@app.get("/api/jobs/{job_id}/datamodel")
def get_datamodel(job_id: str) -> JSONResponse:
    job = manager.snapshot(job_id)
    protocol_dir = ROOT / "llm/peach" / job["protocol"]
    model = protocol_dir / "datamodel.xml"
    if not model.exists():
        raise HTTPException(404, "DataModel 尚未生成。")
    diagnosis = protocol_dir / "datamodel_diagnosis.json"
    return JSONResponse({"xml": model.read_text(encoding="utf-8"), "diagnosis": json.loads(diagnosis.read_text(encoding="utf-8")) if diagnosis.exists() else None})


@app.put("/api/jobs/{job_id}/datamodel")
def save_datamodel(job_id: str, payload: DataModelPayload) -> JSONResponse:
    job = manager.snapshot(job_id)
    if "<Peach" not in payload.xml or f'{job["protocol"]}_packet_array' not in payload.xml:
        raise HTTPException(422, "DataModel 必须包含 Peach 根节点和协议 packet_array 入口。")
    target = ROOT / "llm/peach" / job["protocol"] / "datamodel.xml"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".xml.tmp")
    temp.write_text(payload.xml, encoding="utf-8")
    os.replace(temp, target)
    manager.emit(job_id, "info", "已保存人工编辑的 DataModel。")
    return JSONResponse({"saved": True})


@app.post("/api/jobs/{job_id}/datamodel/verify")
def verify_datamodel(job_id: str) -> JSONResponse:
    job = manager.snapshot(job_id)
    if job.get("status") != "awaiting_datamodel_edit":
        raise HTTPException(409, "当前任务不等待 DataModel 人工修复。")
    manager.start(job_id, WebPipelineRunner(job_id).verify_manual_datamodel)
    return JSONResponse({"started": True})


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> JSONResponse:
    job = manager.snapshot(job_id)
    if job.get("status") == "running":
        manager.update(job_id, status="cancelling", message="正在立即中断任务。")
        manager.emit(job_id, "warning", "正在立即中断任务。")
        manager.interrupt(job_id)
        manager.finish(job_id, "cancelled", "任务已中断。")
        return JSONResponse({"cancelled": True})
    if job.get("status") not in {"completed", "failed", "cancelled"}:
        manager.finish(job_id, "cancelled", "任务已中断。")
    return JSONResponse({"cancelled": True})


@app.post("/api/jobs/{job_id}/start-fresh")
def start_fresh(job_id: str) -> JSONResponse:
    """Close a non-running session so the UI can begin a clean upload flow."""
    job = manager.snapshot(job_id)
    if job.get("status") in {"running", "cancelling"}:
        raise HTTPException(409, "任务仍在运行。请先取消任务，并等待当前操作结束。")
    if job.get("status") not in {"completed", "failed", "cancelled"}:
        manager.finish(job_id, "cancelled", "已开始新的会话。")
    return JSONResponse({"ready": True})


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> JSONResponse:
    job = manager.snapshot(job_id)
    if job.get("status") != "paused":
        raise HTTPException(409, "当前任务不需要恢复。")
    runner = WebPipelineRunner(job_id)
    phase = str(job.get("phase", ""))
    if phase == "packet_types":
        if job.get("packet_types"):
            manager.update(job_id, status="awaiting_packet_types", message="请确认消息类型。")
        else:
            manager.update(job_id, status="running", message="正在重新提取消息类型。")
            manager.start(job_id, runner.extract_packet_types)
    elif phase == "datamodel_manual_edit":
        manager.update(job_id, status="awaiting_datamodel_edit", message="请继续人工修复 DataModel。")
    else:
        packet_types = list(job.get("packet_types") or [])
        if packet_types:
            manager.update(job_id, status="running", message="正在从最近的安全阶段继续任务。")
            manager.start(job_id, runner.continue_after_packet_types, packet_types)
        else:
            manager.update(job_id, status="running", phase="packet_types", message="正在重新提取消息类型。")
            manager.start(job_id, runner.extract_packet_types)
    manager.emit(job_id, "info", "任务已恢复。")
    return JSONResponse({"job": manager.snapshot(job_id)})


@app.get("/api/jobs/{job_id}/artifact")
def download_artifact(job_id: str) -> FileResponse:
    job = manager.snapshot(job_id)
    if job.get("status") != "completed" or not job.get("artifact"):
        raise HTTPException(409, "产物尚未完成。")
    path = manager.job_dir(job_id) / job["artifact"]
    if not path.exists():
        raise HTTPException(404, "产物文件不存在。")
    return FileResponse(path, filename=path.name, media_type="application/zip")
