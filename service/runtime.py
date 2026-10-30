"""MaaFramework 运行时：连接设备、加载资源、启动 Agent、按队列执行任务。

所有方法都是同步阻塞的，Web 层应在线程池中调用。
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from maa.agent_client import AgentClient
from maa.context import ContextEventSink
from maa.controller import Controller
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker, TaskerEventSink

from controllers import ControllerError, create_controller, list_devices
from pi import InterfaceError, ProjectInterface
from png import encode_png
from settings import deep_merge

CLIENT_NAME = "MaaSquadWebService"
CLIENT_VERSION = "1.0.0"

EmitFunc = Callable[[str, dict[str, Any]], None]


class ServiceError(Exception):
    """带 HTTP 状态码的业务错误。"""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _now() -> float:
    return round(time.time(), 3)


@dataclass
class TaskRecord:
    name: str
    entry: str
    pipeline_override: dict[str, Any]
    status: str = "pending"  # pending / running / succeeded / failed / stopped / skipped
    task_id: Optional[int] = None
    nodes: list[str] = field(default_factory=list)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entry": self.entry,
            "status": self.status,
            "task_id": self.task_id,
            "nodes": self.nodes,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class Run:
    id: str
    source: str
    tasks: list[TaskRecord]
    stop_on_failure: bool = False
    status: str = "queued"  # queued / running / succeeded / failed / stopped / cancelled
    error: Optional[str] = None
    created_at: float = field(default_factory=_now)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    stop_requested: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def finished(self) -> bool:
        return self.status in ("succeeded", "failed", "stopped", "cancelled")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "status": self.status,
            "error": self.error,
            "stop_on_failure": self.stop_on_failure,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tasks": [t.to_dict() for t in self.tasks],
        }


class _TaskerSink(TaskerEventSink):
    def __init__(self, callback: Callable[[str, dict[str, Any]], None]):
        super().__init__()
        self._callback = callback

    def on_raw_notification(self, tasker: Tasker, msg: str, details: dict[str, Any]) -> None:
        self._callback(msg, details)


class _ContextSink(ContextEventSink):
    def __init__(self, callback: Callable[[str, dict[str, Any]], None]):
        super().__init__()
        self._callback = callback

    def on_raw_notification(self, context, msg: str, details: dict[str, Any]) -> None:
        self._callback(msg, details)


_NODE_EVENT_PREFIXES = ("Node.PipelineNode", "Node.Recognition.", "Node.Action.")


class MaaRuntime:
    def __init__(self, pi: ProjectInterface, settings: dict[str, Any], user_dir: Path, emit: Optional[EmitFunc] = None):
        self.pi = pi
        self.settings = settings
        self.user_dir = Path(user_dir).resolve()
        self.emit: EmitFunc = emit or (lambda event, data: None)

        self._conn_lock = threading.RLock()
        self._run_lock = threading.RLock()
        self._controller: Optional[Controller] = None
        self._resource: Optional[Resource] = None
        self._tasker: Optional[Tasker] = None
        self._sinks: list[Any] = []
        self._agents: list[tuple[AgentClient, Optional[subprocess.Popen]]] = []
        self._controller_cfg: Optional[dict[str, Any]] = None
        self._resource_cfg: Optional[dict[str, Any]] = None
        self._controller_type: Optional[str] = None

        self._queue: "queue.Queue[Optional[Run]]" = queue.Queue()
        self._runs: "OrderedDict[str, Run]" = OrderedDict()
        self._current: Optional[Run] = None
        self._worker = threading.Thread(target=self._work, name="maa-run-worker", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ 状态

    @property
    def connected(self) -> bool:
        return self._tasker is not None

    def status(self) -> dict[str, Any]:
        with self._run_lock:
            current = self._current.id if self._current else None
            queued = sum(1 for r in self._runs.values() if r.status == "queued")
        return {
            "connected": self.connected,
            "controller": self._controller_cfg["name"] if self._controller_cfg else None,
            "controller_type": self._controller_type,
            "resource": self._resource_cfg["name"] if self._resource_cfg else None,
            "agents": len(self._agents),
            "running_run": current,
            "queued_runs": queued,
            "interface": {"name": self.pi.name, "version": self.pi.version},
            "maafw_version": Library.version(),
        }

    def devices(self, kind: str) -> list[dict[str, Any]]:
        try:
            return list_devices(kind)
        except ControllerError as e:
            raise ServiceError(str(e)) from e

    # ------------------------------------------------------------------ 连接

    def connect(self, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """连接控制器并加载资源。params 可包含 controller、resource、controller_type、adb、win32、debug。"""
        params = params or {}
        maa = self.settings["maa"]
        merged = deep_merge({"adb": maa["adb"], "win32": maa["win32"]}, params)

        with self._conn_lock:
            if self._busy():
                raise ServiceError("有任务正在执行或排队，请先停止后再重新连接", 409, "busy")
            try:
                ctrl_cfg = self.pi.get_controller(params.get("controller") or maa["controller"] or None)
                res_cfg = self.pi.get_resource(params.get("resource") or maa["resource"] or None, ctrl_cfg["name"])
            except InterfaceError as e:
                raise ServiceError(str(e)) from e

            self._disconnect_locked()

            try:
                controller = create_controller(ctrl_cfg, merged)
            except (ControllerError, RuntimeError) as e:
                raise ServiceError(f"创建控制器失败: {e}", 502, "controller_error") from e
            if not controller.post_connection().wait().succeeded:
                raise ServiceError("控制器连接失败，请检查设备是否在线", 502, "controller_error")

            resource = Resource()
            for path in self.pi.resource_paths(res_cfg, ctrl_cfg):
                if not path.exists():
                    raise ServiceError(f"资源目录不存在: {path}", 500, "resource_error")
                if not resource.post_bundle(path).wait().succeeded:
                    raise ServiceError(f"资源加载失败: {path}", 500, "resource_error")

            agents = self._start_agents(resource, ctrl_cfg, res_cfg)

            tasker = Tasker()
            if not tasker.bind(resource, controller) or not tasker.inited:
                self._stop_agents(agents)
                raise ServiceError("Tasker 初始化失败", 500, "tasker_error")

            sinks = [_TaskerSink(self._on_notification), _ContextSink(self._on_notification)]
            tasker.add_sink(sinks[0])
            tasker.add_context_sink(sinks[1])

            self._controller, self._resource, self._tasker = controller, resource, tasker
            self._sinks, self._agents = sinks, agents
            self._controller_cfg, self._resource_cfg = ctrl_cfg, res_cfg
            self._controller_type = merged.get("controller_type") or ctrl_cfg.get("type")

        status = self.status()
        self.emit("status", status)
        return status

    def disconnect(self) -> dict[str, Any]:
        with self._conn_lock:
            if self._busy():
                raise ServiceError("有任务正在执行或排队，请先停止", 409, "busy")
            self._disconnect_locked()
        status = self.status()
        self.emit("status", status)
        return status

    def _disconnect_locked(self) -> None:
        if self._tasker is not None and self._tasker.running:
            self._tasker.post_stop().wait()
        self._stop_agents(self._agents)
        self._tasker = self._resource = self._controller = None
        self._sinks, self._agents = [], []
        self._controller_cfg = self._resource_cfg = self._controller_type = None

    def _start_agents(self, resource: Resource, ctrl_cfg: dict[str, Any], res_cfg: dict[str, Any]):
        agent_settings = self.settings["maa"]["agent"]
        if not self.pi.agents or not agent_settings["enabled"]:
            return []

        env = dict(os.environ)
        env.update(
            {
                "PI_INTERFACE_VERSION": "v2.5.0",
                "PI_CLIENT_NAME": CLIENT_NAME,
                "PI_CLIENT_VERSION": CLIENT_VERSION,
                "PI_CLIENT_LANGUAGE": "zh_cn",
                "PI_CLIENT_MAAFW_VERSION": Library.version(),
                "PI_VERSION": self.pi.version,
                "PI_USER_DATA_DIR": str(self.user_dir),
                "PI_CONTROLLER": json.dumps(ctrl_cfg, ensure_ascii=False, separators=(",", ":")),
                "PI_RESOURCE": json.dumps(res_cfg, ensure_ascii=False, separators=(",", ":")),
            }
        )

        started: list[tuple[AgentClient, Optional[subprocess.Popen]]] = []
        try:
            for cfg in self.pi.agents:
                client = AgentClient(cfg.get("identifier") or None)
                client.set_timeout(int(agent_settings["timeout"]))
                if not client.bind(resource):
                    raise ServiceError("AgentClient 绑定资源失败", 500, "agent_error")

                exe = cfg["child_exec"]
                if exe in ("python", "python3", "py"):
                    exe = sys.executable  # 与服务使用同一 Python 环境
                args = list(cfg.get("child_args", []))
                cwd = self._agent_cwd(args)
                try:
                    proc = subprocess.Popen([exe, *args, client.identifier or ""], cwd=cwd, env=env)
                except OSError as e:
                    raise ServiceError(f"启动 Agent 失败: {exe}: {e}", 500, "agent_error") from e
                started.append((client, proc))

                if not client.connect():
                    raise ServiceError(f"Agent 连接超时: {exe} {' '.join(args)}", 502, "agent_error")
        except Exception:
            self._stop_agents(started)
            raise
        return started

    def _agent_cwd(self, args: list[str]) -> Path:
        """Agent 的 CWD 为 interface.json 所在目录；开发环境（assets/interface.json）下回退到仓库根目录。"""
        base = self.pi.base_dir
        script = next((a for a in args if a.endswith(".py")), None)
        if script and not (base / script).exists() and (base.parent / script).exists():
            return base.parent
        return base

    @staticmethod
    def _stop_agents(agents) -> None:
        for client, proc in agents:
            try:
                client.disconnect()
            except Exception:
                pass
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def _require_connected(self) -> None:
        if self.connected:
            return
        if self.settings["maa"]["auto_connect"]:
            self.connect()
            return
        raise ServiceError("尚未连接设备，请先调用 connect", 409, "not_connected")

    def screenshot(self) -> bytes:
        self._require_connected()
        controller = self._controller
        job = controller.post_screencap().wait()
        if not job.succeeded:
            raise ServiceError("截图失败", 502, "controller_error")
        return encode_png(job.get())

    # ------------------------------------------------------------------ 任务

    def submit(self, body: dict[str, Any], source: str = "api") -> Run:
        """提交一次运行。

        body 支持:
          - {"tasks": [{"name": "任务名", "option": {...}}, "任务名", ...]}
          - {"preset": "预设名"}
          - {"stop_on_failure": true}
        """
        if not isinstance(body, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        # 持有连接锁，避免与 connect / disconnect 交错
        with self._conn_lock:
            self._require_connected()
            return self._enqueue(self._build_run(body, source))

    def _build_run(self, body: dict[str, Any], source: str) -> Run:
        items = body.get("tasks")
        if body.get("preset"):
            if items:
                raise ServiceError("tasks 与 preset 只能二选一")
            try:
                items = self.pi.expand_preset(body["preset"])
            except InterfaceError as e:
                raise ServiceError(str(e), 404, "not_found") from e
        if not isinstance(items, list) or not items:
            raise ServiceError("tasks 必须是非空数组")

        controller = self._controller_cfg["name"]
        resource = self._resource_cfg["name"]
        records = []
        for item in items:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ServiceError("tasks 的元素必须是任务名或 {name, option}")
            try:
                entry, override = self.pi.build_task(item["name"], item.get("option"), controller, resource)
            except InterfaceError as e:
                raise ServiceError(str(e)) from e
            records.append(TaskRecord(name=item["name"], entry=entry, pipeline_override=override))

        return Run(id=uuid.uuid4().hex[:12], source=source, tasks=records, stop_on_failure=bool(body.get("stop_on_failure")))

    def submit_entry(self, body: dict[str, Any], source: str = "api") -> Run:
        """直接按 pipeline 入口执行，需开启 security.allow_raw_entry。"""
        if not self.settings["security"]["allow_raw_entry"]:
            raise ServiceError("未开启 security.allow_raw_entry，不允许直接执行 pipeline 入口", 403, "forbidden")
        entry = body.get("entry") if isinstance(body, dict) else None
        override = body.get("pipeline_override", {}) if isinstance(body, dict) else None
        if not isinstance(entry, str) or not entry:
            raise ServiceError("entry 必须是非空字符串")
        if not isinstance(override, dict):
            raise ServiceError("pipeline_override 必须是对象")
        with self._conn_lock:
            self._require_connected()
            if entry not in self._resource.node_list:
                raise ServiceError(f"资源中不存在节点: {entry}", 404, "not_found")
            record = TaskRecord(name=entry, entry=entry, pipeline_override=override)
            return self._enqueue(Run(id=uuid.uuid4().hex[:12], source=source, tasks=[record]))

    def _enqueue(self, run: Run) -> Run:
        with self._run_lock:
            self._runs[run.id] = run
            self._trim_history()
            # 先发出 queued 事件再入队，避免工作线程抢先把状态改为 running
            self.emit("run", run.to_dict())
        self._queue.put(run)
        return run

    def _trim_history(self) -> None:
        limit = max(int(self.settings["maa"]["history_limit"]), 1)
        finished = [rid for rid, r in self._runs.items() if r.finished]
        for rid in finished[: max(len(finished) - limit, 0)]:
            del self._runs[rid]

    def list_runs(self) -> list[dict[str, Any]]:
        with self._run_lock:
            return [r.to_dict() for r in reversed(self._runs.values())]

    def get_run(self, run_id: str) -> Run:
        with self._run_lock:
            run = self._runs.get(run_id)
        if run is None:
            raise ServiceError(f"未找到运行记录: {run_id}", 404, "not_found")
        return run

    def cancel(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        with self._run_lock:
            if run.status == "queued":
                self._finish(run, "cancelled")
                return run
            if run.status != "running":
                return run
            run.stop_requested = True
        self._post_stop()
        return run

    def stop(self) -> dict[str, Any]:
        """停止当前运行并取消所有排队中的运行。"""
        cancelled = []
        with self._run_lock:
            for run in self._runs.values():
                if run.status == "queued":
                    self._finish(run, "cancelled")
                    cancelled.append(run.id)
            current = self._current
            if current is not None:
                current.stop_requested = True
        if current is not None:
            self._post_stop()
        return {"stopped": current.id if current else None, "cancelled": cancelled}

    def _post_stop(self) -> None:
        tasker = self._tasker
        if tasker is not None:
            tasker.post_stop().wait()

    def _busy(self) -> bool:
        with self._run_lock:
            return self._current is not None or any(r.status == "queued" for r in self._runs.values())

    def _finish(self, run: Run, status: str, error: Optional[str] = None) -> None:
        run.status = status
        run.error = error
        run.finished_at = _now()
        for t in run.tasks:
            if t.status == "pending":
                t.status = "skipped"
        # 先发出事件再标记结束，保证等待中的请求（wait=true）返回时，最终状态事件已经排在响应之前
        self.emit("run", run.to_dict())
        run.done.set()

    def _work(self) -> None:
        while True:
            run = self._queue.get()
            if run is None:
                return
            with self._run_lock:
                if run.status != "queued":
                    continue
                run.status = "running"
                run.started_at = _now()
                self._current = run
            self.emit("run", run.to_dict())
            try:
                self._execute(run)
            except Exception as e:  # 防止工作线程意外退出
                with self._run_lock:
                    self._finish(run, "failed", f"内部错误: {e}")
            finally:
                with self._run_lock:
                    self._current = None

    def _execute(self, run: Run) -> None:
        tasker = self._tasker
        if tasker is None:
            with self._run_lock:
                self._finish(run, "failed", "设备已断开")
            return

        any_failed = False
        for record in run.tasks:
            if run.stop_requested:
                break
            record.status = "running"
            record.started_at = _now()
            self.emit("run", run.to_dict())

            job = tasker.post_task(record.entry, record.pipeline_override)
            record.task_id = job.job_id
            job.wait()
            detail = job.get()
            if detail is not None:
                record.nodes = [n.name for n in detail.nodes if n is not None][:500]

            record.finished_at = _now()
            if run.stop_requested:
                record.status = "stopped"
            elif job.succeeded:
                record.status = "succeeded"
            else:
                record.status = "failed"
                any_failed = True
            self.emit("run", run.to_dict())
            if any_failed and run.stop_on_failure:
                break

        with self._run_lock:
            if run.stop_requested:
                self._finish(run, "stopped")
            else:
                self._finish(run, "failed" if any_failed else "succeeded")

    def _on_notification(self, msg: str, details: dict[str, Any]) -> None:
        events = self.settings["events"]
        if msg.startswith("Tasker.Task"):
            if events["task"]:
                self.emit("maa", {"msg": msg, "details": details})
        elif msg.startswith(_NODE_EVENT_PREFIXES):
            if events["node"]:
                self.emit("maa", {"msg": msg, "details": details})

    # ------------------------------------------------------------------ 退出

    def close(self) -> None:
        self.stop()
        self._queue.put(None)
        self._worker.join(timeout=10)
        with self._conn_lock:
            self._disconnect_locked()
