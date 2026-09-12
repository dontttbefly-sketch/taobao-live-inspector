#!/usr/bin/env python3
"""Safely provision and verify the dedicated machine compliance wordlist."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.compliance.wordlist import (  # noqa: E402
    WordlistValidationError,
    extract_value_matrix,
    parse_wordlist_matrix,
    validate_complete_container,
    validate_wordlist_range_rows,
)
from app.config import PROJECT_ROOT, load_config  # noqa: E402
from app.lark_cli import run_lark_cli  # noqa: E402


SHEET_NAME = "机器识别词库"
HEADER = ("极限词", "启用", "替换建议", "备注")
RANGE_PREFIX = "A1:D"
Runner = Callable[..., dict]
ConfigLoader = Callable[[], dict]


class SafetyRefusal(RuntimeError):
    """A write was refused before the command could change a workbook."""


@dataclass(frozen=True)
class WordlistLocator:
    token: str
    sheet_name: str
    cell_range: str


def _report(action: str, rows: list[list[object]]) -> None:
    try:
        parsed = parse_wordlist_matrix(rows)
    except WordlistValidationError:
        raise SafetyRefusal("sheet content is invalid") from None
    print(json.dumps({
        "action": action,
        "enabled_count": len(parsed.entries),
        "duplicate_warning_count": len(parsed.warnings),
        "source_hash": parsed.source_hash,
    }, ensure_ascii=True, separators=(",", ":")))


def _require_success(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SafetyRefusal("invalid lark response")
    return payload


def _call(runner: Runner, args: list[str]) -> dict:
    try:
        return _require_success(runner(args, cwd=PROJECT_ROOT, timeout=60))
    except SafetyRefusal:
        raise
    except Exception:
        raise SafetyRefusal("lark command failed") from None


def _workbook_sheets(payload: dict) -> dict[str, str]:
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("sheets"), list):
        raise SafetyRefusal("workbook structure is invalid")
    try:
        validate_complete_container(data)
    except WordlistValidationError:
        raise SafetyRefusal("workbook structure is incomplete") from None
    sheets: dict[str, str] = {}
    for item in data["sheets"]:
        if not isinstance(item, dict):
            raise SafetyRefusal("workbook structure is invalid")
        title = item.get("title")
        if not isinstance(title, str) or not title:
            title = item.get("sheet_name")
        sheet_id = item.get("sheet_id")
        if not isinstance(title, str) or not title or not isinstance(sheet_id, str) or not sheet_id:
            raise SafetyRefusal("workbook structure is invalid")
        if title in sheets:
            raise SafetyRefusal("workbook structure is ambiguous")
        sheets[title] = sheet_id
    return sheets


def _workbook_info(runner: Runner, locator: WordlistLocator) -> dict[str, str]:
    return _workbook_sheets(_call(runner, [
        "sheets", "+workbook-info", "--as", "user",
        "--spreadsheet-token", locator.token, "--format", "json",
    ]))


def _read_sheet(
    runner: Runner, locator: WordlistLocator, sheet_id: str,
) -> list[list[object]]:
    payload = _call(runner, [
        "sheets", "+cells-get", "--as", "user",
        "--spreadsheet-token", locator.token, "--sheet-id", sheet_id,
        "--range", locator.cell_range, "--include", "value", "--skip-hidden=false",
        "--format", "json",
    ])
    try:
        return validate_wordlist_range_rows(extract_value_matrix(payload))
    except WordlistValidationError:
        raise SafetyRefusal("sheet values are invalid") from None


def _nonempty(rows: list[list[object]]) -> bool:
    return any(str(value or "").strip() for row in rows for value in row)


def _parse_seed(path: Path) -> list[list[object]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows: list[list[object]] = [list(row) for row in csv.reader(handle)]
    except (OSError, UnicodeError, csv.Error):
        raise SafetyRefusal("seed is unavailable") from None
    if not rows or tuple(rows[0]) != HEADER:
        raise SafetyRefusal("seed header is invalid")
    if any(len(row) != len(HEADER) for row in rows):
        raise SafetyRefusal("seed row width is invalid")
    try:
        validate_wordlist_range_rows(rows)
        parse_wordlist_matrix(rows)
    except WordlistValidationError:
        raise SafetyRefusal("seed content is invalid") from None
    return rows


def _same_content(left: list[list[object]], right: list[list[object]]) -> bool:
    try:
        return parse_wordlist_matrix(left).source_hash == parse_wordlist_matrix(right).source_hash
    except WordlistValidationError:
        return False


def _create_sheet(runner: Runner, locator: WordlistLocator, row_count: int) -> None:
    _call(runner, [
        "sheets", "+sheet-create", "--as", "user",
        "--spreadsheet-token", locator.token, "--title", locator.sheet_name,
        "--row-count", str(row_count), "--col-count", "4", "--format", "json",
    ])


def _write_sheet(
    runner: Runner, locator: WordlistLocator, sheet_id: str, rows: list[list[object]],
) -> None:
    if any(len(row) != 4 for row in rows):
        raise SafetyRefusal("seed row width is invalid")
    cells = [[{"value": str(value)} for value in row] for row in rows]
    _call(runner, [
        "sheets", "+cells-set", "--as", "user",
        "--spreadsheet-token", locator.token, "--sheet-id", sheet_id,
        "--range", f"{RANGE_PREFIX}{len(rows)}", "--cells",
        json.dumps(cells, ensure_ascii=False, separators=(",", ":")),
        "--allow-overwrite=false", "--format", "json",
    ])


def _locator_for(
    *, spreadsheet_token: str | None, config_loader: ConfigLoader,
) -> WordlistLocator:
    if spreadsheet_token:
        return WordlistLocator(spreadsheet_token, SHEET_NAME, "A1:D5000")
    try:
        config = config_loader()
        wordlist = config.get("compliance", {}).get("wordlist", {})
        token = str(wordlist.get("spreadsheet_token") or "").strip()
        sheet_name = str(wordlist.get("sheet_name") or "").strip()
        cell_range = str(wordlist.get("range") or "").strip()
    except Exception:
        raise SafetyRefusal("spreadsheet locator is unavailable") from None
    if not token or sheet_name != SHEET_NAME or cell_range != "A1:D5000":
        raise SafetyRefusal("spreadsheet locator is unavailable")
    return WordlistLocator(token, sheet_name, cell_range)


def _bootstrap(
    seed: Path, *, apply: bool, runner: Runner, locator: WordlistLocator,
) -> int:
    rows = _parse_seed(seed)
    sheets = _workbook_info(runner, locator)
    sheet_id = sheets.get(locator.sheet_name)
    if sheet_id is not None:
        existing = _read_sheet(runner, locator, sheet_id)
        if _nonempty(existing):
            if not _same_content(rows, existing):
                raise SafetyRefusal("refusing non-empty, non-identical sheet")
            _report("bootstrap_idempotent", rows)
            return 0
    if not apply:
        _report("bootstrap_preview", rows)
        return 0
    if sheet_id is None:
        _create_sheet(runner, locator, len(rows))
        sheets = _workbook_info(runner, locator)
        sheet_id = sheets.get(locator.sheet_name)
        if sheet_id is None:
            raise SafetyRefusal("created sheet was not found")
    _write_sheet(runner, locator, sheet_id, rows)
    read_back = _read_sheet(runner, locator, sheet_id)
    if len(read_back) != len(rows) or not _same_content(rows, read_back):
        raise SafetyRefusal("read-back verification failed")
    _report("bootstrap_applied", rows)
    return 0


def _verify(*, runner: Runner, locator: WordlistLocator) -> int:
    sheets = _workbook_info(runner, locator)
    sheet_id = sheets.get(locator.sheet_name)
    if sheet_id is None:
        raise SafetyRefusal("machine wordlist sheet is absent")
    _report("verify", _read_sheet(runner, locator, sheet_id))
    return 0


def main(
    argv: list[str] | None = None, *, runner: Runner = run_lark_cli,
    spreadsheet_token: str | None = None, config_loader: ConfigLoader = load_config,
) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    subcommands = parser.add_subparsers(dest="command", required=True)
    bootstrap = subcommands.add_parser("bootstrap", add_help=False)
    bootstrap.add_argument("--seed", required=True)
    bootstrap.add_argument("--apply", action="store_true")
    subcommands.add_parser("verify", add_help=False)
    args = parser.parse_args(argv)
    locator = _locator_for(
        spreadsheet_token=spreadsheet_token, config_loader=config_loader)
    if args.command == "bootstrap":
        return _bootstrap(Path(args.seed), apply=args.apply, runner=runner, locator=locator)
    return _verify(runner=runner, locator=locator)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyRefusal:
        print("wordlist_command_failed", file=sys.stderr)
        raise SystemExit(2)
