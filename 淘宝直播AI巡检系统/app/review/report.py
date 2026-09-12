"""复盘报告：每场直播 Markdown 报告 + 周报聚合

每场报告内容：场次信息 / 统计指标 / 高亮时刻清单（可点回看）/ 话术清单 / 新话术
周报内容：主播维度汇总（场次、时长、高亮、话术新增）、类别分布、高频话术 TOP
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from ..db import Store
from ..config import SHANGHAI, now_shanghai, resolve

log = logging.getLogger("report")


def _hms(ms: int) -> str:
    ms = max(int(ms), 0)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, _ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_duration(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}分{s}秒" if m < 60 else f"{m // 60}小时{m % 60}分"


def _cat_stats(rows) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["category"]] = out.get(r["category"], 0) + 1
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def _md_cell(value: str) -> str:
    """完整保留文本，同时保证换行和竖线不会破坏 Markdown 表格。"""
    return str(value or "").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _report_metric(value, unit: str = "", decimals: int = 0) -> str:
    if value is None:
        return "暂无"
    return f"{float(value):,.{decimals}f}{unit}"


def _intelligence_evidence_sources(context) -> dict[tuple[str, str], object]:
    sources: dict[tuple[str, str], object] = {}
    for item in context.transcripts:
        sources[("transcript", item.segment_id)] = item.to_dict()
    for item in context.peak_context:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("peak_id") or item.get("source_id") or "")
        if source_id:
            sources[("peak", source_id)] = item
    metrics = context.metrics if isinstance(context.metrics, dict) else {}
    windows = metrics.get("metric_windows")
    if isinstance(windows, list):
        for item in windows:
            if not isinstance(item, dict):
                continue
            source_id = str(
                item.get("source_id") or item.get("window_id")
                or item.get("id") or "")
            if source_id:
                sources[("metric_window", source_id)] = item
    series = metrics.get("series")
    if isinstance(series, dict):
        for metric_name, values in series.items():
            source = {
                "metric_name": str(metric_name),
                "window_start_ms": context.window_start_ms,
                "window_end_ms": context.window_end_ms,
                "series": values,
            }
            sources.setdefault(("metric_window", str(metric_name)), source)
            sources.setdefault(("metric_window", f"M:{metric_name}"), source)
    return sources


def _intelligence_result_evidence_keys(result) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    groups = (
        result.observations,
        result.reusable_talktracks,
        result.action_experiments,
    )
    for group in groups:
        for item in group:
            for evidence in item.evidence:
                keys.add((str(evidence.source_type), str(evidence.source_id)))
    return keys


def _intelligence_evidence_markdown(
        items: list[object],
        evidence_sources: dict[tuple[str, str], object] | None = None) -> str:
    refs: list[str] = []
    for item in items:
        source_type = str(getattr(item, "source_type", "") or "")
        source_id = str(getattr(item, "source_id", "") or "")
        if not source_type or not source_id:
            continue
        label = f"{source_type}:{source_id}"
        if evidence_sources is not None:
            source = evidence_sources.get((source_type, source_id))
            if source is None:
                raise ValueError(f"missing frozen evidence source: {label}")
            detail = json.dumps(
                source, ensure_ascii=False, allow_nan=False,
                sort_keys=True, separators=(",", ":"),
            )
            label += f"（{detail}）"
        refs.append(label)
    return "、".join(refs) or "暂无"


def _intelligence_rejection_markdown(items: object) -> list[str]:
    if not isinstance(items, list):
        raise ValueError("intelligence rejected artifact must be an array")
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("intelligence rejection must be an object")
        code = str(item.get("code") or "").strip()
        if not code:
            raise ValueError("intelligence rejection requires code")
        item_type = str(item.get("item_type") or "").strip()
        raw_index = item.get("index")
        if raw_index is not None and (
                not isinstance(raw_index, int) or isinstance(raw_index, bool)):
            raise ValueError("intelligence rejection index must be an integer")
        detail = str(item.get("detail") or "").strip()
        target = (
            f"{item_type} #{raw_index}"
            if item_type and raw_index is not None else item_type
        )
        suffix = "；".join(part for part in (target, detail) if part)
        lines.append(f"- `{code}`" + (f"；{suffix}" if suffix else ""))
    return lines


def render_hourly_intelligence_markdown(
        result, *, evidence_sources: dict[tuple[str, str], object] | None = None,
        rejected_details: object = None) -> str:
    """Render the full validated hourly artifact without reparsing model prose."""
    from ..intelligence.models import HourlyIntelligenceResult

    if not isinstance(result, HourlyIntelligenceResult):
        raise TypeError("result must be HourlyIntelligenceResult")
    lines = [
        "## 小时智能分析",
        "",
        f"- 任务：`{result.job_key}`",
        f"- 状态：`{result.status}`",
    ]
    if result.observations:
        lines += ["", "### 经营观察", ""]
        for index, item in enumerate(result.observations, 1):
            lines += [
                f"{index}. {item.statement}",
                f"   - 关系：{item.relation}",
                f"   - 场景：{item.scene or '未分类'}",
                f"   - 平台场次：{item.live_id or '暂无'}",
                f"   - 观察来源：{item.source_id or '暂无'}",
                f"   - 证据：{_intelligence_evidence_markdown(item.evidence, evidence_sources)}",
            ]
    if result.reusable_talktracks:
        lines += ["", "### 高质可复用话术", ""]
        for index, item in enumerate(result.reusable_talktracks, 1):
            lines += [
                f"{index}. 场景：{item.scene}",
                f"   - 主播原话：{item.original_text}",
                f"   - 可直接复用：{item.reusable_script}",
                f"   - 证据：{_intelligence_evidence_markdown(item.evidence, evidence_sources)}",
            ]
    if result.action_experiments:
        lines += ["", "### 下一小时行动实验", ""]
        for index, item in enumerate(result.action_experiments, 1):
            lines += [
                f"{index}. `{item.experiment_id}`：{item.issue}",
                f"   - 动作：{item.action}",
                f"   - 照读话术：{item.script}",
                f"   - 触发：{item.trigger}",
                f"   - 时长：{item.duration}",
                f"   - 验证：{item.metric_name}；{item.comparison}",
                f"   - 证据：{_intelligence_evidence_markdown(item.evidence, evidence_sources)}",
            ]
    if rejected_details is not None:
        rejection_lines = _intelligence_rejection_markdown(rejected_details)
        detailed_codes = {
            str(item.get("code") or "") for item in rejected_details
            if isinstance(item, dict)
        }
        rejection_lines.extend(
            f"- `{reason}`" for reason in result.rejected_reasons
            if reason not in detailed_codes
        )
    else:
        rejection_lines = [f"- `{reason}`" for reason in result.rejected_reasons]
    if rejection_lines:
        lines += ["", "### 被拒项（审计）", ""]
        lines += rejection_lines
    return "\n".join(lines).rstrip() + "\n"


def _stream_hourly_intelligence_markdown(store: Store, stream_id: int) -> list[str]:
    """Load only frozen validated artifacts for the local stream report."""
    from ..intelligence.integrity import validated_result_from_artifact

    sections: list[str] = []
    jobs = store.query(
        """SELECT job_key,live_id,anchor_id,window_start_ms,window_end_ms,input_hash,status
           FROM intelligence_jobs
           WHERE task_type='hourly' AND stream_id=?
             AND status='ready'
           ORDER BY window_start_ms,window_end_ms,job_key""",
        (int(stream_id),),
    )
    for job in jobs:
        try:
            artifact = store.get_intelligence_artifact(str(job["job_key"]))
            result = validated_result_from_artifact(
                artifact or {},
                expected_job_key=str(job["job_key"]),
                expected_status=str(job["status"]),
            )
            context_raw = (artifact or {}).get("context_snapshot")
            if not context_raw:
                raise ValueError("hourly intelligence has no frozen context")
            from ..intelligence.context import intelligence_context_input_hash
            from ..intelligence.models import IntelligenceContext
            context = IntelligenceContext.from_dict(context_raw)
            if (intelligence_context_input_hash(context) != context.input_hash
                    or context.input_hash != str(job["input_hash"])
                    or context.stream_id != int(stream_id)
                    or context.live_id != str(job["live_id"])
                    or context.anchor_id != int(job["anchor_id"] or 0)
                    or context.window_start_ms != int(job["window_start_ms"])
                    or context.window_end_ms != int(job["window_end_ms"])):
                raise ValueError("frozen intelligence context does not match job")
            evidence_sources = _intelligence_evidence_sources(context)
            missing = _intelligence_result_evidence_keys(result) - set(evidence_sources)
            if missing:
                raise ValueError("frozen intelligence context misses result evidence")
            section = render_hourly_intelligence_markdown(
                result,
                evidence_sources=evidence_sources,
                rejected_details=(artifact or {}).get("rejected"),
            )
            coverage = (context.metrics or {}).get("media_coverage") or {}
            if isinstance(coverage, dict) and coverage.get("gaps"):
                from ..business_facts import format_media_coverage
                rendered_coverage = format_media_coverage(coverage)
                if rendered_coverage:
                    section = (
                        "## 录音覆盖与缺失时段\n\n"
                        + rendered_coverage + "\n\n" + section)
        except (TypeError, ValueError):
            log.warning(
                "小时智能产物损坏，已从 Markdown 隐藏：job=%s",
                str(job["job_key"]),
            )
            continue
        sections.append(section.rstrip())
    return sections


def _smart_minutes_markdown(items: list[dict], *, compact: bool = False) -> list[str]:
    from ..transcription.feishu import chapter_deep_link, note_doc_link
    lines: list[str] = []
    for index, item in enumerate(items, 1):
        url = str(item.get("minute_url") or "")
        if not url:
            continue
        start = int(item.get("window_start_ms") or 0)
        end = int(item.get("window_end_ms") or 0)
        lines += [
            f"### 小时 {index:02d} · {_hms(start)}–{_hms(end)}",
            "",
            "> AI基于主播原话生成，不代表平台经营数据。",
            "",
        ]
        summary = str(item.get("summary") or "").strip()
        if summary:
            lines += [summary[:500] if compact else summary, ""]
        chapters = item.get("chapters") or []
        for chapter in (chapters[:3] if compact else chapters):
            title = str(chapter.get("title") or "查看章节")
            lines.append(
                f"- [{title}]({chapter_deep_link(url, int(chapter.get('start_ms') or 0))})"
                + (f" — {chapter.get('summary')}" if chapter.get("summary") else "")
            )
        quotes = item.get("golden_quotes") or []
        if quotes:
            lines += ["", f"- 金句：{quotes[0]}"]
        note_url = note_doc_link(url, str(item.get("note_doc_token") or ""))
        if note_url:
            lines += ["", f"[打开智能会议纪要]({note_url})"]
        lines += [f"[查看完整逐字稿]({url})", ""]
    return lines


def _bound_platform_intelligence(store: Store, live_id: str, summary: dict):
    """返回与冻结日报及持久化上下文同时一致的 DeepSeek 结果。"""
    from ..intelligence.platform import (
        bound_platform_intelligence_from_summary,
        load_frozen_platform_intelligence,
    )

    try:
        bound_result, binding = bound_platform_intelligence_from_summary(summary)
        context, result, _rejected = load_frozen_platform_intelligence(
            store, str(live_id), expected_result=bound_result.to_dict())
        if context.input_hash != binding["input_hash"]:
            raise ValueError("formal daily report and platform context hash differ")
    except (KeyError, TypeError, ValueError):
        log.warning("全日智能产物损坏，已从 Markdown 关闭：identity=%s", live_id)
        return None
    if (result.status != "ready" or not result.full_analysis.strip()
            or not result.business_conclusions or not result.next_actions):
        log.warning("全日智能产物不完整，已从 Markdown 关闭：identity=%s", live_id)
        return None
    return result


def write_recovery_metrics_markdown(cfg: dict, store: Store, stream_id: int) -> Path | None:
    """把登录中断期间补抓的分钟趋势独立落入 Markdown。"""
    rows = store.query(
        """SELECT window_start_ms,window_end_ms,recovery_metrics_json
           FROM transcription_jobs
           WHERE stream_id=? AND recovery_metrics_status IN ('running','writing','ready')
             AND recovery_metrics_json NOT IN ('','{}')
           ORDER BY window_start_ms""",
        (int(stream_id),),
    )
    if not rows:
        return None
    lines = [
        f"# 场次 #{int(stream_id)} 登录中断历史趋势补抓",
        "",
        "> 本文件仅保留平台可查的历史分钟趋势。缺少小时边界快照时，不用恢复时累计数倒推或平均拆分。",
        "",
        "| 媒体窗口 | 平均在线 | 最高在线 | 商品点击 | 成交金额 | 来源 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        try:
            metrics = json.loads(str(row["recovery_metrics_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        start_ms = int(row["window_start_ms"] or 0)
        end_ms = int(row["window_end_ms"] or 0)
        label = (f"{start_ms // 3_600_000:02d}:{start_ms // 60_000 % 60:02d}"
                 f"–{end_ms // 3_600_000:02d}:{end_ms // 60_000 % 60:02d}")
        def show(key: str, decimals: int = 0) -> str:
            value = metrics.get(key)
            if value is None:
                return "暂无"
            return f"{float(value):,.{decimals}f}"
        lines.append(
            f"| {label} | {show('uv_avg', 1)} | {show('max_online_uv')} | "
            f"{show('ipv_total')} | {show('pay_amt', 2)} | "
            f"{str(metrics.get('source') or '千牛历史分钟趋势')} |"
        )
    out_dir = resolve((cfg.get("report", {}) or {}).get("out_dir", "data/reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"stream_{int(stream_id):04d}_auth_recovery_metrics.md"
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out


def generate_platform_report(cfg: dict, store: Store, live_id: str,
                             summary: dict | None = None) -> Path:
    """生成一个业务经营日的唯一日报，技术碎片只留审计。"""
    if summary is None:
        from .platform import build_platform_review_summary
        summary = build_platform_review_summary(cfg, store, live_id)
    platform = summary.get("display_metrics") or {}
    anchors = summary.get("anchors") or []
    issues = summary.get("data_issues") or []
    state = "完整" if summary.get("data_state") == "complete" else "部分"
    identity = str(summary.get("business_session_key") or summary.get("live_id") or live_id)
    intelligence = _bound_platform_intelligence(store, identity, summary)
    viewer_value = platform.get("viewer_uv")
    viewer_label = "当日观看人数"
    viewer_unit = " 人"
    if viewer_value is None and platform.get("viewer_uv_segment_sum") is not None:
        viewer_value = platform.get("viewer_uv_segment_sum")
        viewer_label = "当日观看人次（分段累计，非去重）"
        viewer_unit = " 人次"
    lines = [
        "# 直播经营日报",
        "",
        f"- 经营日：`{identity}`",
        f"- 时间：{summary.get('started_at') or '暂无'} ~ {summary.get('ended_at') or '暂无'}",
        f"- 有效录像时长：{_fmt_duration(float(summary.get('duration_sec') or 0))}",
        f"- 主播数：{len(anchors)}",
        f"- 官方数据状态：{state}（缺失值保持“暂无”，不按录像时长分摊）",
        "",
        "## 全日核心数据",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 当日成交金额 | {_report_metric(platform.get('pay_amt'), ' 元', 2)} |",
        f"| {viewer_label} | {_report_metric(viewer_value, viewer_unit)} |",
        f"| 最高在线 | {_report_metric(platform.get('max_online_uv'), ' 人')} |",
        f"| 当日成交人数 | {_report_metric(platform.get('buyer_cnt'), ' 人')} |",
        f"| 当日成交订单 | {_report_metric(platform.get('order_cnt'), ' 单')} |",
        f"| 当日成交件数 | {_report_metric(platform.get('item_qty'), ' 件')} |",
        "",
        f"> 来源：{platform.get('source_scope') or platform.get('source') or '平台冻结总账暂未返回'}。",
    ]
    lines += ["", "## DeepSeek 全日经营分析", ""]
    if intelligence is None:
        lines.append("（尚无与本日报绑定的完整分析）")
    else:
        # 完整分析是日报长文正文，不做卡片字数截断。
        lines += [intelligence.full_analysis.strip(), "", "### 经营结论", ""]
        lines += [f"{index}. {item}" for index, item in enumerate(
            intelligence.business_conclusions, 1)]

    lines += [
        "", "## 各主播经营表现对比", "",
        "| 主播 | 上钟时长 | 成交金额 | 观看口径 | 成交口径 | 订单 | 件数 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for anchor in anchors:
        metrics = anchor.get("metrics") or {}
        if metrics.get("look_uv") is not None:
            viewers = _report_metric(metrics.get("look_uv"), " 人")
        elif metrics.get("look_uv_segment_sum") is not None:
            viewers = _report_metric(metrics.get("look_uv_segment_sum"), " 人次") + "（分段累计，非去重）"
        else:
            viewers = "暂无"
        if metrics.get("pay_byr_cnt") is not None:
            buyers = _report_metric(metrics.get("pay_byr_cnt"), " 人")
        elif metrics.get("pay_byr_cnt_segment_sum") is not None:
            buyers = _report_metric(metrics.get("pay_byr_cnt_segment_sum"), " 人次") + "（分段累计，非去重）"
        else:
            buyers = "暂无"
        lines.append(
            f"| {_md_cell(anchor.get('anchor_name') or '未知主播')} | "
            f"{_fmt_duration(float(metrics.get('on_air_duration_sec') or anchor.get('duration_sec') or 0))} | "
            f"{_report_metric(metrics.get('pay_amt'), ' 元', 2)} | {viewers} | {buyers} | "
            f"{_report_metric(metrics.get('pay_ord_cnt'), ' 单')} | "
            f"{_report_metric(metrics.get('pay_itm_qty'), ' 件')} |"
        )

    trends = summary.get("hourly_trends") or []
    lines += [
        "", "## 小时经营趋势", "",
        "| 时段 | 主播 | 成交金额 | 新增观看 | 平均在线 | 最高在线 | 成交人数 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    if trends:
        for row in trends:
            try:
                start = datetime.fromtimestamp(
                    int(row.get("window_start_ms") or 0) / 1000, SHANGHAI)
                end = datetime.fromtimestamp(
                    int(row.get("window_end_ms") or 0) / 1000, SHANGHAI)
                period = f"{start:%H:%M}–{end:%H:%M}"
            except (TypeError, ValueError, OSError):
                period = "时间暂无"
            lines.append(
                f"| {period} | {_md_cell(row.get('anchor_name') or '未知主播')} | "
                f"{_report_metric(row.get('pay_amt'), ' 元', 2)} | "
                f"{_report_metric(row.get('viewer_uv'), ' 人')} | "
                f"{_report_metric(row.get('avg_online'), ' 人', 1)} | "
                f"{_report_metric(row.get('max_online'), ' 人')} | "
                f"{_report_metric(row.get('buyer_cnt'), ' 人')} |"
            )
    else:
        lines.append("| 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 |")

    coverage_lines: list[str] = []
    from ..business_facts import format_media_coverage
    for row in trends:
        coverage = row.get("media_coverage") or {}
        if not isinstance(coverage, dict) or not coverage.get("gaps"):
            continue
        rendered = format_media_coverage(coverage)
        if not rendered:
            continue
        try:
            start = datetime.fromtimestamp(
                int(row.get("window_start_ms") or 0) / 1000, SHANGHAI)
            end = datetime.fromtimestamp(
                int(row.get("window_end_ms") or 0) / 1000, SHANGHAI)
            period = f"{start:%H:%M}–{end:%H:%M}"
        except (TypeError, ValueError, OSError):
            period = "时间暂无"
        coverage_lines += [f"### {period}", "", rendered, ""]
    if coverage_lines:
        lines += ["", "## 录音覆盖与缺失时段", "", *coverage_lines]

    quotes: list[tuple[str, str, str]] = []
    seen_quotes: set[str] = set()
    for anchor in anchors:
        for minute in anchor.get("smart_minutes") or []:
            for raw_quote in minute.get("golden_quotes") or []:
                quote = str(raw_quote or "").strip()
                if not quote or quote in seen_quotes:
                    continue
                seen_quotes.add(quote)
                quotes.append((str(anchor.get("anchor_name") or "未知主播"),
                               quote, str(minute.get("minute_url") or "")))
                if len(quotes) >= 3:
                    break
            if len(quotes) >= 3:
                break
        if len(quotes) >= 3:
            break
    lines += ["", "## 飞书妙记金句（最多 3 条）", ""]
    if quotes:
        for index, (name, quote, url) in enumerate(quotes, 1):
            suffix = f" [查看妙记]({url})" if url else ""
            lines.append(f"{index}. **{name}**：{quote}{suffix}")
    else:
        lines.append("（暂无完整金句）")

    lines += ["", "## 下一场行动（最多 3 项）", ""]
    if intelligence is not None:
        lines += [f"{index}. {item}" for index, item in enumerate(
            intelligence.next_actions[:3], 1)]
    else:
        lines.append("（尚无与本日报绑定的行动建议）")

    lines += ["", "## 数据来源与异常", ""]
    if issues:
        lines += [f"- {issue}" for issue in issues]
    else:
        lines.append("- 平台冻结总账、主播上下钟指标、小时冻结事实和飞书妙记均已返回。")
    lines += [
        "- 人数类多片段指标不直接相加为去重人数；分段人次会单独标注。",
        "- DeepSeek 可做完整经营分析，但不得改写官方经营数据；相关关系不写成因果。",
        "", "## 审计附录", "",
        "本地技术碎片只用于审计与回听，不作为正式复盘单位：",
        "",
    ]
    for stream_id in summary.get("scope_stream_ids") or []:
        lines.append(f"- 本地录像 #{int(stream_id)}")

    out_dir = resolve((cfg.get("report", {}) or {}).get("out_dir", "data/reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_identity = "".join(
        ch for ch in identity if ch.isalnum() or ch in "-_") or "unknown"
    out = out_dir / f"daily_{safe_identity}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("直播经营日报已生成: %s", out)
    return out


def generate_stream_report(cfg: dict, store: Store, stream_id: int,
                           data_problems: list[str] | None = None) -> Path:
    stream = store.get_stream(stream_id)
    if not stream:
        raise ValueError(f"stream #{stream_id} 不存在")
    anchor = store.get_anchor(stream["anchor_id"])
    a_name = anchor["name"] if anchor else f"主播#{stream['anchor_id']}"

    transcripts = store.get_transcripts(stream_id)
    highlights = store.get_highlights(stream_id)
    visible_highlights = [h for h in highlights if h["kind"] in ("data_association", "quality")]
    occurrences = store.query(
        """SELECT o.*, CASE WHEN o.id=(
               SELECT MIN(first.id) FROM talktrack_occurrences first
               WHERE first.anchor_id=o.anchor_id AND first.category=o.category
                 AND first.norm_text=o.norm_text
           ) THEN 1 ELSE 0 END AS is_new
           FROM talktrack_occurrences o WHERE o.stream_id=? ORDER BY o.id""",
        (stream_id,),
    )

    total_chars = sum(len(t["text"]) for t in transcripts)
    cat_stats = _cat_stats(occurrences)
    new_list = [row for row in occurrences if row["is_new"]]

    day_seq = store.day_seq(stream_id)
    started = stream["started_at"] or ""
    if day_seq > 0:
        m, d = int(started[5:7]), int(started[8:10])
        day_label = f"{m}月{d}日第 {day_seq} 场"
    else:
        day_label = f"#{stream_id}"

    lines = [
        f"# 直播复盘报告 — {a_name}",
        "",
        f"- 场次：{day_label}",
        f"- 开始：{stream['started_at']}　结束：{stream['ended_at'] or '-'}",
        f"- 时长：{_fmt_duration(stream['duration_sec'] or 0)}",
        f"- 录像：`{stream['file_path']}`",
        f"- 转写：{len(transcripts)} 句 / {total_chars} 字　精选高亮：{len(visible_highlights)} 个",
        f"- 本场新增话术：{len(new_list)} 条",
        "",
    ]
    if data_problems:
        lines += ["> ⚠️ **经营数据质量门禁未完全通过**：" + "；".join(data_problems),
                  "> 缺失字段不会按 0 展示；AI 只能描述已核验事实，不得评价表现或推断因果。", ""]
    # 平台场次总账标注：淘宝把一天直播算作一个平台场次，本地可能拆成多场；
    # 拆分场次只显示平台整场最终值（订单数等），本场下方数据仍是本地估算。
    try:
        ps = store.query("SELECT * FROM platform_sessions WHERE live_id=?",
                         (stream["live_id"] or "",))
        if ps:
            n_local = store.query(
                """SELECT COUNT(*) c FROM streams WHERE live_id=?
                   AND status!='interrupted' AND file_path!=''""",
                (stream["live_id"] or "",))[0]["c"]
            if n_local and int(n_local) > 1:
                p = ps[0]
                def _pv(key: str, digits: int = 0) -> str:
                    value = p[key]
                    if value is None:
                        return "暂无"
                    return f"{float(value):,.{digits}f}"
                lines += [
                    f"> 📌 本场属于平台场次（liveId {stream['live_id']}，"
                    f"{p['started_at']} ~ {p['ended_at']}）：平台整场成交 "
                    f"{_pv('pay_amt', 2)} 元 / {_pv('order_cnt')} 单 / "
                    f"观看 {_pv('viewer_uv')} 人。",
                    "> 本场为平台场次的拆分时段，下方本场数据为本地估算，订单数以平台整场为准。",
                    "",
                ]
    except Exception:
        pass
    lines += ["## 话术类别分布（本场）", "",
        "| 类别 | 条数 |",
        "| --- | --- |",
    ]
    lines += [f"| {k} | {v} |" for k, v in cat_stats.items()] or ["| - | 0 |"]

    # 高亮展示：只展示「数据驱动高亮」的短句摘录（每条带真实时间点）；
    # 关键词+声学高亮仅作话术入库的内部信号，不再进入报告展示（2026-08-03 用户要求）
    from ..asr.clean import DISPLAY_FALLBACK, persisted_display_record_text
    from ..highlight.peak import (format_peak_evidence, has_data_peak_reason,
                                  parse_peak_meta)
    peak_hls = [h for h in highlights if has_data_peak_reason(h["reasons"])]
    lines += ["", "## 峰值时刻话术（数据驱动高亮）", ""]
    lines += ["> 以下为语义补全展示版短句摘录；摘录失败或无法可靠还原的峰值不会重复展示，原始 ASR 见转写文件。", ""]
    if peak_hls:
        for h in sorted(peak_hls, key=lambda x: -x["score"]):
            reasons = "、".join(json.loads(h["reasons"]) if isinstance(h["reasons"], str) else h["reasons"])
            lines += [f"### {reasons}（{h['score']:.1f}分）", ""]
            meta = parse_peak_meta(h["peak_meta"] or {})
            evidence = format_peak_evidence(meta, cfg=cfg) if meta else []
            if evidence:
                lines += ["> **时间关联（非因果，仅表示先后）**：", ""]
                for ev in evidence:
                    lines.append(f"> - {ev}")
                lines.append("")
            lines += [f"> 数据关联窗口：{_hms(h['start_ms'])} ~ {_hms(h['end_ms'])}"
                      + ("" if evidence else "（暂无合格时间关联）"), ""]
            body = persisted_display_record_text(
                cfg, h, meta_field="peak_meta", max_chars=360)
            if body:
                lines.append(f"- {body}")
            lines.append("")
    else:
        lines += ["（本场无显著经营数据峰值）"]

    lines += ["", "## 优质可复用话术（内容质量榜）", "",
              "> 本榜只评价表达是否完整、具体、可复用；不代表它导致了任何经营结果。", ""]
    quality_hls = [h for h in highlights if h["kind"] == "quality"]
    if quality_hls:
        for index, h in enumerate(quality_hls[:5], 1):
            try:
                meta = json.loads(h["quality_meta"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            body = persisted_display_record_text(
                cfg, h, meta_field="quality_meta", max_chars=220)
            if not body:
                continue
            rationale = "、".join(str(x) for x in (meta.get("rationale") or [])[:3]) or "达到综合质量线"
            lines += [
                f"### {index}. {meta.get('category') or '优质话术'}"
                f"（{meta.get('quality_score') or '暂无'}分）",
                "", f"- 时间：{_hms(h['start_ms'])}", f"- 入选：{rationale}",
                f"- 话术：{body}", "",
            ]
    else:
        lines += ["（本场暂无达到质量线的可复用话术）"]

    lines += ["", "## 话术库更新（本场新增 TOP）", ""]
    if new_list:
        lines += ["| 类别 | 话术 |", "| --- | --- |"]
        for t in new_list[:20]:
            body = persisted_display_record_text(
                cfg, t, field="text", max_chars=500)
            if body:
                lines.append(f"| {t['category']} | {_md_cell(body)} |")
    else:
        lines += ["（本场没有新话术，多为复用历史话术）"]

    lines += ["", "## 历史话术库参考（明确不属于本场事实）", "",
              "> 以下仅为该主播历史累计库，用于对照；不得据此判断本场说过这些话。", ""]
    top = store.get_talktracks(anchor_id=stream["anchor_id"])[:10]
    if top:
        lines += ["| 次数 | 类别 | 话术 |", "| --- | --- | --- |"]
        for t in top:
            body = persisted_display_record_text(
                cfg, t, field="text", max_chars=500)
            if body:
                lines.append(
                    f"| {t['use_count']} | {t['category']} | {_md_cell(body)} |")

    # ---- 数据表现（千牛经营数据）----
    from ..metrics.qianniu import get_metrics, format_metrics_text
    metrics = get_metrics(store, stream_id)
    if metrics:
        lines += ["", "## 📊 经营数据事实（千牛只读数据源）", "",
                  format_metrics_text(metrics), ""]

    # ---- AI 复盘分析（大模型生成）----
    ai_review = ""
    if (cfg.get("transcription", {}) or {}).get("enabled"):
        from ..transcription.models import TranscriptionResult
        smart_items: list[dict] = []
        for job in store.get_stream_transcription_jobs(stream_id):
            result = TranscriptionResult.from_json(job["result_json"])
            artifact = result.smart if result else None
            if not artifact or not artifact.minute_url:
                continue
            smart_items.append({
                "window_start_ms": int(job["window_start_ms"] or 0),
                "window_end_ms": int(job["window_end_ms"] or 0),
                "minute_url": artifact.minute_url,
                "summary": artifact.summary,
                "chapters": [
                    {"start_ms": c.start_ms, "end_ms": c.end_ms,
                     "title": c.title, "summary": c.summary}
                    for c in artifact.chapters
                ],
                "golden_quotes": list(artifact.golden_quotes),
            })
        if smart_items:
            lines += ["", "---", "", "## 妙记与智能纪要", ""]
            lines += _smart_minutes_markdown(smart_items, compact=False)
    else:
        from .ai_report import generate_ai_review
        ai_review = generate_ai_review(cfg, store, stream_id)
        lines += ["", "---", "", "## AI 复盘分析", "", ai_review]

    hourly_intelligence = _stream_hourly_intelligence_markdown(store, stream_id)
    if hourly_intelligence:
        lines += ["", "---", ""]
        for index, section in enumerate(hourly_intelligence):
            if index:
                lines += ["", "---", ""]
            lines.append(section)

    out_dir = resolve(cfg["report"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"stream_{stream_id:04d}_{a_name}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    store.save_review(stream_id, str(out), ai_review=ai_review)
    log.info("复盘报告已生成: %s", out)
    return out


def _frozen_daily_revenue(store: Store, since: str) -> dict[str, dict]:
    """按开播日归属的平台冻结成交（2026-08-08：口径与复盘卡/数据大屏一致）。

    每天只播一场（早 6 至次日凌晨）时，一天的成交=该场官方冻结总账；
    跨天场次归开播日。只统计 display_metrics.pay_amt 已冻结回填的整场。
    """
    out: dict[str, dict] = {}
    for row in store.query("SELECT live_id, payload FROM platform_reviews"):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        started = str(payload.get("started_at") or "")
        day = started[:10]
        if not day or day < since:
            continue
        metrics = payload.get("display_metrics") or {}
        pay = metrics.get("pay_amt")
        if not isinstance(pay, (int, float)):
            continue
        item = out.setdefault(day, {"pay_amt": 0.0})
        item["pay_amt"] += float(pay)
        viewers = metrics.get("viewer_uv")
        if isinstance(viewers, (int, float)):
            item["viewer_uv"] = item.get("viewer_uv", 0) + float(viewers)
    return out


def generate_weekly_report(cfg: dict, store: Store, days: int | None = None) -> Path:
    days = days or int(cfg.get("report", {}).get("week_days", 7))
    since = (now_shanghai() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    streams = store.query(
        "SELECT * FROM streams WHERE started_at>=? AND status='reported' ORDER BY anchor_id, started_at",
        (since,),
    )
    anchors = store.query("SELECT * FROM anchors")

    lines = [
        f"# 周度巡检汇总（近 {days} 天）",
        "",
        f"- 生成时间：{now_shanghai().strftime('%Y-%m-%d %H:%M')}",
        f"- 场次总数：{len(streams)}",
        "",
        "## 主播维度",
        "",
        "| 主播 | 场次 | 总时长 | 高亮数 | 新增话术 | 复用话术 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for a in anchors:
        a_streams = [s for s in streams if s["anchor_id"] == a["id"]]
        if not a_streams:
            continue
        n = len(a_streams)
        hours = sum(s["duration_sec"] or 0 for s in a_streams) / 3600
        hl = sum(store.query(
            """SELECT COUNT(*) c FROM highlights WHERE stream_id=? AND (
                   kind IN ('data_association','quality') OR peak_meta NOT IN ('','{}')
               )""",
            (s["id"],))[0]["c"]
                 for s in a_streams)
        ids = tuple(s["id"] for s in a_streams)
        ph = ",".join("?" * len(ids))
        tt = store.query(
            f"""SELECT o.id,o.stream_id,CASE WHEN o.id=(
                    SELECT MIN(first.id) FROM talktrack_occurrences first
                    WHERE first.anchor_id=o.anchor_id AND first.category=o.category
                      AND first.norm_text=o.norm_text
                ) THEN 1 ELSE 0 END AS is_new
                FROM talktrack_occurrences o
                WHERE o.anchor_id=? AND o.stream_id IN ({ph})""",
            (a["id"], *ids),
        )
        added = sum(1 for t in tt if t["is_new"])
        reused = sum(1 for t in tt if not t["is_new"])
        lines.append(f"| {a['name']} | {n} | {hours:.1f}h | {hl} | {added} | {reused} |")

    lines += ["", "## 每日经营数据（近 7 天）", ""]
    from ..metrics import daily as daily_metrics
    daily_metrics.ensure_table(store)
    # 成交金额以"当天开播场次的平台冻结总账"为准（每天只播一场=单场冻结；
    # 跨天场次归开播日），与数据大屏/复盘卡同口径；其余列保留每日经营接口。
    frozen_daily = _frozen_daily_revenue(
        store, (now_shanghai() - timedelta(days=days)).strftime("%Y-%m-%d"))
    d_rows = store.query(
        "SELECT * FROM daily_metrics WHERE date>=? ORDER BY date DESC LIMIT 7",
        ((now_shanghai() - timedelta(days=days)).strftime("%Y%m%d"),),
    )
    if d_rows:
        lines += ["| 日期 | 观看人数 | 成交金额(元) | 成交人数 | 新增会员 | 会员成交人数 |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for r in d_rows:
            if r["data_state"] == "pending":
                lines.append(
                    f"| {r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:]} "
                    "| 待结算 | 待结算 | 待结算 | 待结算 | 待结算 |")
                continue
            def _v(r, k):
                v = r[k]
                if v in (None, ""):
                    return "-"
                if k in ("pay_amt", "pay_amt_mbr"):
                    return f"{v:,.0f}"
                return f"{v:,}"
            lines.append(f"| {r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:]} "
                         f"| {_v(r,'look_uv')} | {_v(r,'pay_amt')} | {_v(r,'pay_byr_cnt')} "
                         f"| {_v(r,'mbr_cnt_incr')} | {_v(r,'pay_byr_cnt_mbr')} |")
        # 成交金额列按冻结口径重写（当天开播场次的官方冻结总账；无冻结值时保留原值）
        day_frozen = {
            day.replace("-", ""): item
            for day, item in frozen_daily.items()
        }
        for index, r in enumerate(d_rows):
            frozen = day_frozen.get(r["date"])
            if frozen is None:
                continue
            row_line = lines[len(lines) - len(d_rows) + index]
            day_label = row_line.split("|")[1]
            cells = [cell.strip() for cell in row_line.split("|")]
            # cells: ['', 日期, 观看, 成交金额, 人数, 会员, 会员成交, '']
            if len(cells) >= 6:
                if "viewer_uv" in frozen:
                    cells[2] = f"{frozen['viewer_uv']:,.0f}"
                cells[3] = f"{frozen['pay_amt']:,.0f}"
                lines[len(lines) - len(d_rows) + index] = "| " + " | ".join(cells[1:-1]) + " |"
        # 7 日合计（只统计有数据的列）；成交金额合计用冻结口径
        sums = {}
        for r in d_rows:
            if r["data_state"] != "complete":
                continue
            for k in ("look_uv", "pay_byr_cnt", "mbr_cnt_incr", "pay_byr_cnt_mbr"):
                v = r[k]
                if v is not None:
                    sums[k] = sums.get(k, 0) + v
        frozen_sum = sum(
            item["pay_amt"] for item in frozen_daily.values())
        if frozen_sum > 0:
            sums["pay_amt"] = frozen_sum
        if sums:
            def _s(k, fmt="int"):
                if k not in sums:
                    return "-"
                return f"{sums[k]:,.0f}" if fmt == "int" else f"{sums[k]:,.0f}"
            lines.append(f"| **合计** | {_s('look_uv')} | {_s('pay_amt')} | {_s('pay_byr_cnt')} "
                         f"| {_s('mbr_cnt_incr')} | {_s('pay_byr_cnt_mbr')} |")
    else:
        lines += ["（暂无每日数据，运行后每日自动抓取）"]

    lines += ["", "## 主播单日表现（最新一天）", ""]
    from ..metrics import daibo as daibo_metrics
    daibo_metrics.ensure_table(store)
    last_d = store.query(
        "SELECT MAX(date) d FROM daibo_daily WHERE look_uv>0"
    )[0]["d"]
    if last_d:
        d_rows = store.query(
            "SELECT * FROM daibo_daily WHERE date=? ORDER BY look_uv DESC", (last_d,))
        lines += [f"日期：{last_d[:4]}-{last_d[4:6]}-{last_d[6:]}", "",
                  "| 主播 | 观看人数 | 成交金额(元) | 转化率 | 客单价 | 观看时长 | 新增粉丝 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in d_rows:
            def _dv(k, pct=False, money=False):
                v = r[k]
                if v is None:
                    return "-"
                if pct:
                    return f"{v:.2f}%"
                if money:
                    return f"{v:,.0f}"
                return f"{v:,.0f}"
            lt = r["look_time_sec"]
            lt_s = (f"{int(lt)//3600}h{int(lt)%3600//60}m"
                    if lt is not None else "-")
            lines.append(f"| {r['daibo_name']} | {_dv('look_uv')} | {_dv('pay_amt', money=True)} "
                         f"| {_dv('cvr_pay', pct=True)} | {_dv('atv', money=True)} "
                         f"| {lt_s} | {_dv('atn_uv')} |")
        pending_days = [r["date"] for r in store.query(
            """SELECT date FROM daily_metrics
               WHERE data_state='pending' AND date>? ORDER BY date""",
            (last_d,),
        )]
        if pending_days:
            pretty = "、".join(f"{d[4:6]}-{d[6:8]}" for d in pending_days)
            lines += ["", f"> 主播单日数据待补抓：{pretty} 尚未形成可用结算值。"]
    else:
        lines += ["（暂无主播数据，运行后每日自动抓取）"]

    lines += ["", "## 话术类别分布（本周）", ""]
    cat_stats = {
        row["category"]: row["n"] for row in store.query(
            """SELECT o.category,COUNT(*) n FROM talktrack_occurrences o
               JOIN streams s ON s.id=o.stream_id
               WHERE s.status='reported' AND s.started_at>=?
               GROUP BY o.category ORDER BY n DESC""", (since,))
    }
    lines += ["| 类别 | 条数 |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in sorted(cat_stats.items(), key=lambda x: -x[1])] or ["| - | 0 |"]

    lines += ["", "## 高频话术 TOP 20（本周）", ""]
    rows = store.query(
        """SELECT o.anchor_id,a.name anchor_name,o.category,o.norm_text,
                  MAX(o.text) text,COUNT(*) period_count
           FROM talktrack_occurrences o
           JOIN streams s ON s.id=o.stream_id
           JOIN anchors a ON a.id=o.anchor_id
           WHERE s.status='reported' AND s.started_at>=?
             AND o.text_provenance='display_repaired'
           GROUP BY o.anchor_id,o.category,o.norm_text
           ORDER BY period_count DESC,MAX(o.id) DESC LIMIT 20""", (since,))
    if rows:
        from ..asr.clean import (OUTWARD_DISPLAY_REPAIRED,
                                 outward_display_text)
        lines += ["| 次数 | 主播 | 类别 | 话术 |", "| --- | --- | --- | --- |"]
        for t in rows:
            body = outward_display_text(
                t["text"], cfg, provenance=OUTWARD_DISPLAY_REPAIRED,
                max_chars=500)
            if not body:
                continue
            lines.append(
                f"| {t['period_count']} | {t['anchor_name']} | {t['category']} | "
                f"{_md_cell(body)} |"
            )

    out_dir = resolve(cfg["report"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"weekly_{now_shanghai().strftime('%Y%m%d')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("周报已生成: %s", out)
    return out
