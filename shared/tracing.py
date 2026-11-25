"""跨进程 trace（PRD 7.10 的 M4 实现）：W3C Trace Context + OTel span 树。

- trace_id 沿用 M1 口径（uuid4().hex，32 位 hex = 16 字节，即合法 W3C trace-id）；
  根 span 直接以它作 OTel trace-id，审计/agent_runs 里已有的 trace_id 与
  span 树天然可互查。
- 开关：tracing_enabled=False（默认）时全部助手退化为 no-op，
  opentelemetry 惰性导入——"trace 可丢弃，审计不可"。
- 采样：头部采样（TracerProvider sampler，按 trace_id 确定性）；未采样的根
  span 不会激活，delegate 也就不注入 traceparent，整条 trace 静默丢弃。
  异常全采需 collector 级尾采样（README 已知限制）。
- 导出：FileSpanExporter 按服务写 {trace_dir}/{service}.jsonl（dev/test 同机
  共享目录；console /api/traces/{trace_id} 汇总重建调用树）；OTLP 未接。
- 传播：委托出口统一在 shared/a2a_client.delegate——有活动 span 时把
  traceparent 写进请求 metadata；各服务端 start_server_span 从 metadata/头取
  父上下文（is_remote），checkpointer 线程不携带上下文，跨进程靠显式透传。
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from shared.config import get_settings

_TRACER: Any = None  # 惰性初始化的 opentelemetry.trace.Tracer


def new_trace_id() -> str:
    return uuid.uuid4().hex


def metadata_with_trace(trace_id: str, **extra: str) -> dict:
    meta = {"trace_id": trace_id, "ancestor_agents": "orchestrator"}
    meta.update(extra)
    return meta


def service_name() -> str:
    return os.environ.get("SERVICE_NAME") or get_settings().service_name or "unknown"


def enabled() -> bool:
    return get_settings().tracing_enabled


# ---------------------------------------------------------------------------
# traceparent（W3C Trace Context）：version-traceid-spanid-flags
# ---------------------------------------------------------------------------

def build_traceparent(trace_id_hex: str, span_id_hex: str) -> str:
    return f"00-{trace_id_hex}-{span_id_hex}-01"


def parse_traceparent(tp: str) -> tuple[str, str] | None:
    """合法返回 (trace_id, span_id)；不合法返回 None。"""
    try:
        version, tid, sid, _flags = tp.strip().split("-")
        if version != "00" or len(tid) != 32 or len(sid) != 16:
            return None
        int(tid, 16), int(sid, 16)
        return tid, sid
    except Exception:  # noqa: BLE001
        return None


def current_traceparent() -> str:
    """活动 span 存在且在采样中时返回 traceparent，否则空串。"""
    if not enabled():
        return ""
    from opentelemetry import trace

    span = trace.get_current_span()
    sc = span.get_span_context()
    if not (sc.is_valid and span.is_recording()):
        return ""
    return build_traceparent(f"{sc.trace_id:032x}", f"{sc.span_id:016x}")


def inject_trace_headers(headers: dict) -> dict:
    """给出站 HTTP 头补 traceparent（无活动 span 时原样返回）。"""
    tp = current_traceparent()
    if tp:
        headers = {**headers, "traceparent": tp}
    return headers


# ---------------------------------------------------------------------------
# 服务端 span
# ---------------------------------------------------------------------------

@contextmanager
def start_server_span(
    name: str,
    *,
    trace_id: str = "",
    parent_traceparent: str = "",
    attributes: dict | None = None,
) -> Iterator[Any]:
    """起一个服务端 span；tracing 关闭时整个 with 块退化为 no-op（yield None）。

    - parent_traceparent 非空 → 作为远端父上下文（跨进程父子关系由此建立）；
    - 否则 trace_id 齐全 → 以它为 trace-id 开新根（根 span 受头部采样控制）；
    - 都没有 → OTel 自开新 trace。
    """
    if not enabled():
        yield None
        return
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    tracer = _tracer()
    parent_ctx = None
    current = trace.get_current_span().get_span_context()
    parsed = parse_traceparent(parent_traceparent or "")
    if parsed:
        tid, sid = parsed
        parent_ctx = trace.set_span_in_context(
            NonRecordingSpan(SpanContext(
                trace_id=int(tid, 16), span_id=int(sid, 16),
                is_remote=True, trace_flags=TraceFlags(1),
            ))
        )
    elif not current.is_valid and trace_id and len(trace_id) == 32:
        # 本进程尚无上下文：锚定到调用方已生成的 trace_id（审计/agent_runs 同号）
        parent_ctx = trace.set_span_in_context(
            NonRecordingSpan(SpanContext(
                trace_id=int(trace_id, 16), span_id=int(uuid.uuid4().hex[:16], 16),
                is_remote=False, trace_flags=TraceFlags(1),
            ))
        )
    with tracer.start_as_current_span(
        name, context=parent_ctx, attributes=attributes or {},
    ) as span:
        yield span


def _tracer() -> Any:
    global _TRACER
    if _TRACER is None:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        s = get_settings()
        ratio = max(0.0, min(1.0, s.tracing_sample_ratio))
        d = Path(s.trace_dir)
        d.mkdir(parents=True, exist_ok=True)
        provider = TracerProvider(
            resource=Resource.create({
                "service.name": service_name(),
                "agentdock.env": s.env,
            }),
            # 直接按 trace_id 确定性掷签（不用 ParentBased：跨进程父上下文是
            # 显式锚定的已采样 SpanContext，ParentBased 会因此恒放行）。
            sampler=TraceIdRatioBased(ratio),
        )
        provider.add_span_processor(
            SimpleSpanProcessor(FileSpanExporter(d / f"{service_name()}.jsonl"))
        )
        # 不走全局 set_tracer_provider（只能设一次，测试/重初始化会静默失效）；
        # 本模块统一经由 _TRACER 使用 span，上下文传播靠 contextvars，与全局无关。
        _TRACER = provider.get_tracer("agentdock")
    return _TRACER


# ---------------------------------------------------------------------------
# 导出与读取（JSONL，按服务一文件；console 汇总重建树）
# ---------------------------------------------------------------------------

def _jsonable(attrs) -> dict:  # noqa: ANN001
    out: dict = {}
    for k, v in dict(attrs or {}).items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)[:500]
    return out


class FileSpanExporter:
    """把 ended span 追加写进本服务的 JSONL 文件（SimpleSpanProcessor 同步落盘）。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def export(self, spans) -> Any:  # noqa: ANN001
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                for span in spans:
                    fh.write(json.dumps(_row(span), ensure_ascii=False) + "\n")
            return SpanExportResult.SUCCESS
        except Exception:  # noqa: BLE001
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _row(span) -> dict:  # noqa: ANN001
    ctx = span.get_span_context()
    parent = span.parent
    end = span.end_time or span.start_time
    return {
        "service": service_name(),
        "name": span.name,
        "trace_id": f"{ctx.trace_id:032x}",
        "span_id": f"{ctx.span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent else "",
        "start_ms": int(span.start_time / 1e6),
        "end_ms": int(end / 1e6),
        "duration_ms": round((end - span.start_time) / 1e6, 1),
        "status": span.status.status_code.name,
        "attributes": _jsonable(span.attributes),
    }


def read_trace_spans(trace_id: str, trace_dir: str | None = None) -> list[dict]:
    """跨进程汇总：扫 trace_dir 下全部 *.jsonl，取该 trace_id 的 span 行。"""
    d = Path(trace_dir or get_settings().trace_dir)
    hits: list[dict] = []
    if not d.exists():
        return hits
    for f in sorted(d.glob("*.jsonl")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if trace_id not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("trace_id") == trace_id:
                hits.append(row)
    return hits


def build_span_tree(spans: list[dict]) -> dict:
    """把扁平 span 列表重建成调用树（根 = 无父或父不在集合内；跨进程丢父时兜底并标记）。"""
    by_id = {s["span_id"]: {**s, "children": []} for s in spans}
    roots: list[dict] = []
    for s in by_id.values():
        pid = s.get("parent_span_id") or ""
        if pid and pid in by_id:
            by_id[pid]["children"].append(s)
        else:
            roots.append(s)
    roots.sort(key=lambda s: s.get("start_ms", 0))
    if not roots:
        return {}
    if len(roots) == 1:
        return roots[0]
    root = roots[0]
    root["reparented"] = [r["span_id"] for r in roots[1:]]
    root["children"] = roots[1:] + list(root["children"])
    return root
