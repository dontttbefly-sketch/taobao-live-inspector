from __future__ import annotations

import csv
import io
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import PROJECT_ROOT, SHANGHAI, now_shanghai, resolve
from .lark_cli import run_lark_cli


_FEISHU_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_FEISHU_ROW_PREFIX_RE = re.compile(r"^\[row=(\d+)\]\s?(.*)$")
_FEISHU_RANGE_RE = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$", re.IGNORECASE)
_FEISHU_TIME_RE = re.compile(r"(?<!\d)(?:([01]?\d|2[0-4])(?::([0-5]\d))?)(?!\d)")
_FLAGSHIP_BACKGROUND = "#f53954"


def parse_shift(shift: str) -> tuple[int, int, int, int]:
    start, _, end = shift.strip().partition("-")
    sh, sm = start.strip().split(":")
    eh, em = end.strip().split(":")
    return int(sh), int(sm), int(eh), int(em)


def in_shift(shift: str, now: datetime | None = None) -> bool:
    if not shift:
        return False
    current = now or now_shanghai()
    sh, sm, eh, em = parse_shift(shift)
    minute = current.hour * 60 + current.minute
    start, end = sh * 60 + sm, eh * 60 + em
    return start <= minute < end if start <= end else minute >= start or minute < end


def _load_schedule(path: Path | str | None, now: datetime | None,
                   resolver: Callable[[str], Path]) -> dict:
    if path is not None:
        candidate = Path(path)
        path = candidate if candidate.is_absolute() else resolver(str(candidate))
    else:
        current = now or now_shanghai()
        business_day = current.date() - timedelta(days=1) if current.hour < 5 else current.date()
        path = None
        for candidate in sorted(resolver("schedules").glob("schedule_week_*.json"), reverse=True):
            match = re.fullmatch(r"schedule_week_(\d{8})\.json", candidate.name)
            if not match:
                continue
            try:
                start = date.fromisoformat(
                    f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}"
                )
            except ValueError:
                continue
            if start <= business_day <= start + timedelta(days=6):
                path = candidate
                break
        if path is None:
            return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def load_schedule(path: Path | str | None = None, now: datetime | None = None) -> dict:
    return _load_schedule(path, now, resolve)


def _schedule_week_monday(when: datetime) -> date:
    local = when.astimezone(SHANGHAI) if when.tzinfo else when.replace(tzinfo=SHANGHAI)
    business_day = local.date() - timedelta(days=1) if local.hour < 5 else local.date()
    return business_day - timedelta(days=business_day.weekday())


def _copy_schedule_days(schedule: Mapping[str, object] | object) -> dict[str, list[dict]]:
    copied: dict[str, list[dict]] = {}
    if not isinstance(schedule, Mapping):
        return copied
    for key, slots in schedule.items():
        if not isinstance(key, str) or not isinstance(slots, list):
            continue
        cleaned: list[dict] = []
        for slot in slots:
            if not isinstance(slot, Mapping):
                continue
            name = str(slot.get("anchor") or "").strip()
            hour = slot.get("hour")
            if not name or hour is None or isinstance(hour, bool):
                continue
            try:
                item: dict[str, object] = {"hour": int(hour), "anchor": name}
            except (TypeError, ValueError):
                continue
            for field in ("start_minute", "end_minute"):
                value = slot.get(field)
                if value is None or isinstance(value, bool):
                    continue
                try:
                    item[field] = int(value)
                except (TypeError, ValueError):
                    continue
            cleaned.append(item)
        if cleaned:
            copied[key] = cleaned
    return copied


def persist_weekly_schedule(
    schedule: Mapping[str, object],
    *,
    now: datetime | None = None,
    directory: Path | None = None,
    resolver: Callable[[str], Path] | None = None,
) -> Path | None:
    """把飞书排班原子写入本周 JSON，供极限词监听按命中时刻读取。"""
    incoming = _copy_schedule_days(schedule)
    if not incoming:
        return None
    folder = Path(directory) if directory is not None else (resolver or resolve)("schedules")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    path = folder / f"schedule_week_{_schedule_week_monday(now or now_shanghai()):%Y%m%d}.json"
    merged = incoming
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if isinstance(existing, Mapping):
            merged = _copy_schedule_days(existing)
            merged.update(incoming)
    encoded = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(encoded, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return path


def _annotated_csv_rows(annotated_csv: str) -> list[tuple[int, list[str]]]:
    """保留飞书 CSV 标注的真实行号，而不是按换行符猜坐标。"""
    result: list[tuple[int, list[str]]] = []
    for row in csv.reader(io.StringIO(str(annotated_csv or ""))):
        if not row:
            continue
        match = _FEISHU_ROW_PREFIX_RE.match(str(row[0]))
        if match is None:
            continue
        result.append((int(match.group(1)), [match.group(2), *row[1:]]))
    return result


def _cell_value(cell: object) -> str:
    return str(cell.get("value") or "").strip() if isinstance(cell, Mapping) else ""


def _cell_background(cell: object) -> str:
    if not isinstance(cell, Mapping):
        return ""
    styles = cell.get("cell_styles")
    value = styles.get("background_color") if isinstance(styles, Mapping) else ""
    return re.sub(r"\s+", "", str(value or "").lower())


def _is_flagship_cell(cell: object, marker: str) -> bool:
    value = _cell_value(cell)
    if marker and value == marker:
        return True
    return _cell_background(cell) in {
        _FLAGSHIP_BACKGROUND,
        "rgb(245,57,84)",
    }


def _time_to_business_minute(token: str, *, not_before: int) -> int | None:
    match = _FEISHU_TIME_RE.fullmatch(str(token).strip())
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour == 24 and minute:
        return None
    value = hour * 60 + minute
    while value < not_before:
        value += 24 * 60
    return value


def _cell_time_markers(cell: object, *, not_before: int) -> list[int]:
    markers: list[int] = []
    for match in _FEISHU_TIME_RE.finditer(_cell_value(cell)):
        value = _time_to_business_minute(match.group(0), not_before=not_before)
        if value is not None:
            markers.append(value)
            not_before = value
    return markers


def _slot_interval_from_cells(
        cells: list[tuple[int, int, int, object]],
        *, default_end: int) -> tuple[int, int]:
    """将一段连续红色格转为分钟级区间，时间文字优先于格子整点边界。"""
    default_start = cells[0][2]
    markers: list[tuple[int, int]] = []
    for position, _hour, _start, cell in cells:
        for minute in _cell_time_markers(cell, not_before=default_start):
            markers.append((position, minute))

    start, end = default_start, default_end
    if len(markers) >= 2:
        first = markers[0][1]
        last = markers[-1][1]
        if default_start <= first < default_end:
            start = first
        if start < last <= default_end:
            end = last
    elif markers:
        position, minute = markers[0]
        first_position, last_position = cells[0][0], cells[-1][0]
        if position == first_position and default_start <= minute < default_end:
            start = minute
        elif position == last_position and default_start < minute <= default_end:
            end = minute
    return start, end


def _append_feishu_slots(
        result: dict[str, list[dict]], *, day_key: str, row: list[object],
        name: str, hour_columns: list[tuple[int, int, int]], marker: str) -> None:
    active: list[tuple[int, int, int, object]] = []
    for position, (column_index, hour, minute) in enumerate(hour_columns):
        cell = row[column_index] if column_index < len(row) else {}
        if _is_flagship_cell(cell, marker):
            active.append((position, hour, minute, cell))

    cursor = 0
    while cursor < len(active):
        end = cursor + 1
        while end < len(active) and active[end][0] == active[end - 1][0] + 1:
            end += 1
        run = active[cursor:end]
        next_position = run[-1][0] + 1
        default_end = (hour_columns[next_position][2]
                       if next_position < len(hour_columns)
                       else run[-1][2] + 60)
        start_minute, end_minute = _slot_interval_from_cells(run, default_end=default_end)
        if end_minute <= start_minute:
            raise ValueError(
                f"飞书排班 {day_key} {name} 的旗舰店时间区间无效")
        result.setdefault(day_key, []).append({
            "hour": (start_minute % (24 * 60)) // 60,
            "start_minute": start_minute,
            "end_minute": end_minute,
            "anchor": name,
        })
        cursor = end


def _validate_feishu_slots(schedule: dict[str, list[dict]]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for day, slots in schedule.items():
        ordered = sorted(
            slots,
            key=lambda slot: (int(slot["start_minute"]), int(slot["end_minute"]),
                              str(slot["anchor"])),
        )
        for previous, current in zip(ordered, ordered[1:]):
            if int(current["start_minute"]) < int(previous["end_minute"]):
                raise ValueError(
                    f"飞书排班 {day} 存在重叠的旗舰店主播："
                    f"{previous['anchor']}、{current['anchor']}")
        if ordered:
            result[day] = ordered
    if not result:
        raise ValueError("飞书主播排班未找到旗舰店上播小时")
    return result


def parse_feishu_anchor_schedule_cells(
        ranges: list[Mapping[str, object]], *, live_marker: str = "旗舰店",
) -> dict[str, list[dict]]:
    """解析飞书带样式单元格；红色旗舰店格可携带半小时交接时间。"""
    schedule: dict[str, list[dict]] = {}
    marker = str(live_marker or "").strip()

    for range_data in ranges:
        cells = range_data.get("cells") if isinstance(range_data, Mapping) else None
        row_indices = range_data.get("row_indices") if isinstance(range_data, Mapping) else None
        col_indices = range_data.get("col_indices") if isinstance(range_data, Mapping) else None
        actual_range = str(range_data.get("actual_range") or "") if isinstance(range_data, Mapping) else ""
        if not isinstance(cells, list) or not isinstance(row_indices, list) or not isinstance(col_indices, list):
            raise ValueError("飞书主播排班单元格回包不完整")
        if not actual_range or len(cells) != len(row_indices):
            raise ValueError("飞书主播排班单元格坐标不完整")
        columns = {str(column).upper(): index for index, column in enumerate(col_indices)}
        date_column, name_column = columns.get("A"), columns.get("B")
        if date_column is None or name_column is None:
            raise ValueError("飞书主播排班缺少 A/B 列")

        day_key = ""
        hour_columns: list[tuple[int, int, int]] = []
        for row_index, raw_row in zip(row_indices, cells):
            if not isinstance(row_index, int) or not isinstance(raw_row, list):
                raise ValueError("飞书主播排班行坐标无效")
            first = _cell_value(raw_row[date_column] if date_column < len(raw_row) else {})
            date_match = _FEISHU_DATE_RE.fullmatch(first)
            if date_match:
                day_key = f"{int(date_match.group(1))}/{int(date_match.group(2))}"
                hour_columns = []
                continue
            if not day_key:
                continue
            if first == "开播时间":
                hour_columns = []
                offset = 0
                previous_hour: int | None = None
                for column_index, cell in enumerate(raw_row):
                    value = _cell_value(cell)
                    if not value.isdigit() or not 0 <= int(value) <= 23:
                        continue
                    hour = int(value)
                    if previous_hour is not None and hour < previous_hour:
                        offset += 24 * 60
                    hour_columns.append((column_index, hour, hour * 60 + offset))
                    previous_hour = hour
                continue
            if not hour_columns:
                continue
            name = _cell_value(raw_row[name_column] if name_column < len(raw_row) else {})
            if name:
                _append_feishu_slots(
                    schedule, day_key=day_key, row=raw_row, name=name,
                    hour_columns=hour_columns, marker=marker)

    return _validate_feishu_slots(schedule)


def normalize_feishu_schedule_source(source: Mapping[str, object] | object) -> dict:
    """返回可用的飞书主播排班来源；空配置代表关闭远端同步。"""
    if not isinstance(source, Mapping):
        return {}
    url = str(source.get("url") or "").strip()
    sheet_id = str(source.get("sheet_id") or "").strip()
    if not url or not sheet_id:
        return {}
    try:
        sync_seconds = max(1, int(source.get("sync_seconds") or 300))
    except (TypeError, ValueError):
        sync_seconds = 300
    return {
        "url": url,
        "sheet_id": sheet_id,
        "range": str(source.get("range") or "A1:Z1600").strip(),
        "live_marker": str(source.get("live_marker") or "旗舰店").strip(),
        "sync_seconds": sync_seconds,
    }


def _feishu_range_bounds(cell_range: str) -> tuple[str, int, str, int]:
    match = _FEISHU_RANGE_RE.fullmatch(str(cell_range or "").strip())
    if match is None:
        raise ValueError("飞书主播排班 range 必须是 A1:Z1600 形式")
    first_column, first_row, last_column, last_row = match.groups()
    start, end = int(first_row), int(last_row)
    if start < 1 or end < start:
        raise ValueError("飞书主播排班 range 行号无效")
    return first_column.upper(), start, last_column.upper(), end


def _lark_sheet_data(payload: object) -> Mapping[str, object]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        raise ValueError("飞书主播排班未返回表格内容")
    # 上游要求先读取 warning_message；有截断时绝不使用部分排班覆盖当前值。
    _warning = str(data.get("warning_message") or "")
    _ = _warning
    truncated = bool(data.get("truncated") or data.get("has_more"))
    if isinstance(payload, Mapping):
        truncated = truncated or bool(payload.get("truncated") or payload.get("has_more"))
    if truncated:
        raise ValueError("飞书主播排班读取被截断")
    return data


def _feishu_schedule_day_rows(
        annotated_csv: str, *, current: datetime) -> list[tuple[int, str]]:
    local = current.astimezone(SHANGHAI) if current.tzinfo else current.replace(tzinfo=SHANGHAI)
    business_day = local.date() - timedelta(days=1) if local.hour < 5 else local.date()
    wanted = {
        f"{(business_day + timedelta(days=offset)).month}/"
        f"{(business_day + timedelta(days=offset)).day}"
        for offset in range(2)
    }
    rows: list[tuple[int, str]] = []
    for row_number, row in _annotated_csv_rows(annotated_csv):
        first = str(row[0]).strip() if row else ""
        match = _FEISHU_DATE_RE.fullmatch(first)
        if match is None:
            continue
        day_key = f"{int(match.group(1))}/{int(match.group(2))}"
        rows.append((row_number, day_key))
    return [(row_number, day_key) for row_number, day_key in rows if day_key in wanted]


def load_feishu_anchor_schedule(
        source: Mapping[str, object], *, now: datetime | None = None,
) -> dict[str, list[dict]]:
    """读取当前和次日的飞书排班，并以颜色/时间标记转换为主播区间。"""
    normalized = normalize_feishu_schedule_source(source)
    if not normalized:
        raise ValueError("飞书主播排班缺少 url 或 sheet_id")
    url = normalized["url"]
    sheet_id = normalized["sheet_id"]
    first_column, first_row, last_column, last_row = _feishu_range_bounds(
        str(normalized["range"]))
    live_marker = normalized["live_marker"]
    index_payload = run_lark_cli(
        [
            "sheets", "+csv-get", "--url", url, "--sheet-id", sheet_id,
            "--range", f"{first_column}{first_row}:{first_column}{last_row}",
            "--max-chars", "100000",
        ],
        cwd=PROJECT_ROOT,
        timeout=15,
    )
    index_data = _lark_sheet_data(index_payload)
    annotated_csv = index_data.get("annotated_csv")
    if not isinstance(annotated_csv, str) or not annotated_csv.strip():
        raise ValueError("飞书主播排班未返回表格内容")
    all_date_rows = [
        (row_number, f"{int(match.group(1))}/{int(match.group(2))}")
        for row_number, row in _annotated_csv_rows(annotated_csv)
        if row
        for match in [_FEISHU_DATE_RE.fullmatch(str(row[0]).strip())]
        if match is not None
    ]
    selected_rows = _feishu_schedule_day_rows(
        annotated_csv, current=now or now_shanghai())
    if not selected_rows:
        raise ValueError("飞书主播排班未找到当前或次日日期")

    ranges: list[Mapping[str, object]] = []
    for row_number, _day_key in selected_rows:
        next_rows = [number for number, _key in all_date_rows if number > row_number]
        end_row = min(next_rows) - 1 if next_rows else last_row
        if end_row < row_number:
            continue
        payload = run_lark_cli(
            [
                "sheets", "+cells-get", "--url", url, "--sheet-id", sheet_id,
                "--range", f"{first_column}{row_number}:{last_column}{end_row}",
                "--include", "value,style", "--max-chars", "120000",
            ],
            cwd=PROJECT_ROOT,
            timeout=15,
        )
        data = _lark_sheet_data(payload)
        raw_ranges = data.get("ranges")
        if not isinstance(raw_ranges, list) or not all(
                isinstance(item, Mapping) for item in raw_ranges):
            raise ValueError("飞书主播排班未返回带样式单元格")
        ranges.extend(raw_ranges)
    return parse_feishu_anchor_schedule_cells(ranges, live_marker=live_marker)


def _business_day_minute(when: datetime) -> int:
    local = when.astimezone(SHANGHAI) if when.tzinfo else when.replace(tzinfo=SHANGHAI)
    minute = local.hour * 60 + local.minute
    return minute + (24 * 60 if local.hour < 5 else 0)


def schedule_slot_interval(slot: Mapping[str, object]) -> tuple[int, int] | None:
    """Return a slot's [start, end) in business-day minutes.

    The legacy JSON files contain only a whole-clock-hour ``hour``; Feishu
    supplies explicit minute boundaries for handovers inside a clock hour.
    """
    try:
        start = slot.get("start_minute")
        end = slot.get("end_minute")
        if start is not None or end is not None:
            if isinstance(start, bool) or isinstance(end, bool):
                return None
            start_minute, end_minute = int(start), int(end)
        else:
            hour = slot.get("hour")
            if isinstance(hour, bool):
                return None
            hour = int(hour)
            if not 0 <= hour <= 23:
                return None
            start_minute = hour * 60 + (24 * 60 if hour < 5 else 0)
            end_minute = start_minute + 60
    except (TypeError, ValueError):
        return None
    return (start_minute, end_minute) if end_minute > start_minute else None


def schedule_slot_is_active(slot: Mapping[str, object], when: datetime) -> bool:
    """兼容旧的整点 ``hour`` 排班，也支持飞书表的分钟级交接区间。"""
    interval = schedule_slot_interval(slot)
    if interval is None:
        return False
    return interval[0] <= _business_day_minute(when) < interval[1]


def scheduled_anchor_names_for_window(
        schedule: dict, window_start: datetime, window_end: datetime,
) -> tuple[str, ...]:
    """Return every scheduled anchor whose interval overlaps a recording window."""
    start = (window_start.astimezone(SHANGHAI) if window_start.tzinfo
             else window_start.replace(tzinfo=SHANGHAI))
    end = (window_end.astimezone(SHANGHAI) if window_end.tzinfo
           else window_end.replace(tzinfo=SHANGHAI))
    if end <= start:
        return ()
    business_day = start.date() - timedelta(days=1) if start.hour < 5 else start.date()
    start_minute = _business_day_minute(start)
    end_minute = start_minute + (end - start).total_seconds() / 60
    names: list[str] = []
    for slot in (schedule or {}).get(f"{business_day.month}/{business_day.day}") or []:
        if not isinstance(slot, Mapping):
            continue
        interval = schedule_slot_interval(slot)
        name = str(slot.get("anchor") or "").strip()
        if name and interval is not None and interval[0] < end_minute and interval[1] > start_minute:
            names.append(name)
    return tuple(sorted(set(names)))


def scheduled_anchor_names(schedule: dict, when: datetime) -> tuple[str, ...]:
    local = when.astimezone(SHANGHAI) if when.tzinfo else when.replace(tzinfo=SHANGHAI)
    day = local.date() - timedelta(days=1) if local.hour < 5 else local.date()
    names: list[str] = []
    for slot in (schedule or {}).get(f"{day.month}/{day.day}") or []:
        if not isinstance(slot, dict):
            continue
        name = str(slot.get("anchor") or "").strip()
        if name and schedule_slot_is_active(slot, local):
            names.append(name)
    return tuple(sorted(set(names)))


def resolve_unique_scheduled_anchor(schedule: dict, when: datetime) -> str | None:
    names = scheduled_anchor_names(schedule, when)
    return names[0] if len(names) == 1 else None
