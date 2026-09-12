from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
import unicodedata

from app.config import PROJECT_ROOT
from app.lark_cli import run_lark_cli

from .models import WordEntry, WordlistSnapshot
from .store import ComplianceStore


TRUE_VALUES = {"是", "true", "yes", "1", "启用", "checked"}
WORDLIST_RANGE_ROW_LIMIT = 5000
MACHINE_TERM_PREFIX_RE = re.compile(r"^(?:违禁词|敏感词)\s*[:：]")
MACHINE_TERM_LABEL_RE = re.compile(
    r"(?:相关词语|禁用词|敏感词|禁忌|的表述|迷信用语)$"
)
MULTI_TERM_SEPARATOR_RE = re.compile(r"[\r\n,，、;；/／()（）\[\]【】]")
CELLS_GET_RANGE_METADATA_ADVISORY = (
    "处理 ranges[n].cells 之前，必须先查看顶层 has_more，以及每个 range 的 "
    "actual_range / row_indices / col_indices。定位真实行号时用 row_indices[i]，"
    "定位真实列字母时用 col_indices[j]，不要按二维数组下标自己数行列；"
    "skip_hidden=true 或结果被截断时，这样会错位。"
)


class WordlistValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedWordlist:
    source_hash: str
    entries: tuple[WordEntry, ...]
    warnings: tuple[str, ...]


def validate_complete_container(
    container: dict[object, object], *, allow_range_metadata_advisory: bool = False,
) -> None:
    if any(bool(container.get(name)) for name in ("has_more", "truncated")):
        raise WordlistValidationError("wordlist response is truncated")
    warning = str(container.get("warning_message") or "").strip()
    if warning and not (
        allow_range_metadata_advisory
        and warning == CELLS_GET_RANGE_METADATA_ADVISORY
    ):
        raise WordlistValidationError("wordlist response has warning")


def _unwrap_range_cells(cells: list[list[object]]) -> list[list[object]]:
    """Normalize the current cell-object response without weakening old shapes."""
    matrix: list[list[object]] = []
    for row in cells:
        decoded: list[object] = []
        for cell in row:
            if not isinstance(cell, dict):
                decoded.append(cell)
                continue
            if not cell:
                decoded.append("")
                continue
            if "value" not in cell:
                raise WordlistValidationError("wordlist response cell has no value")
            value = cell["value"]
            if isinstance(value, (dict, list, tuple, set)):
                raise WordlistValidationError("wordlist response cell value is invalid")
            decoded.append(value)
        matrix.append(decoded)
    return matrix


def extract_value_matrix(payload: object) -> list[list[object]]:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise WordlistValidationError("wordlist response was not successful")
    if not isinstance(payload.get("data"), dict):
        raise WordlistValidationError("wordlist response has no data object")
    data = payload["data"]
    validate_complete_container(
        data,
        allow_range_metadata_advisory=isinstance(data.get("ranges"), list),
    )
    candidates: list[list[list[object]]] = []
    ranges = data.get("ranges")
    if ranges is not None:
        if not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(ranges[0], dict):
            raise WordlistValidationError("wordlist response range count is invalid")
        range_data = ranges[0]
        validate_complete_container(range_data)
        cells = range_data.get("cells")
        if not isinstance(cells, list) or not all(isinstance(row, list) for row in cells):
            raise WordlistValidationError("wordlist response cells are invalid")
        candidates.append(_unwrap_range_cells(cells))
    values = data.get("values")
    value_range = data.get("valueRange")
    if value_range is not None and not isinstance(value_range, dict):
        raise WordlistValidationError("wordlist response valueRange is invalid")
    if isinstance(value_range, dict):
        validate_complete_container(value_range)
    range_values = value_range.get("values") if isinstance(value_range, dict) else None
    for matrix in (values, range_values):
        if matrix is None:
            continue
        if not isinstance(matrix, list) or not all(isinstance(row, list) for row in matrix):
            raise WordlistValidationError("wordlist response matrix is invalid")
        candidates.append(matrix)
    if not candidates:
        raise WordlistValidationError("wordlist response has no matrix")
    if any(candidate != candidates[0] for candidate in candidates[1:]):
        raise WordlistValidationError("wordlist response matrices are ambiguous")
    return candidates[0]


def validate_wordlist_range_rows(
    rows: list[list[object]], *, row_limit: int = WORDLIST_RANGE_ROW_LIMIT,
) -> list[list[object]]:
    if len(rows) > row_limit or (len(rows) == row_limit and any(
        str(value or "").strip() for value in rows[-1]
    )):
        raise WordlistValidationError("configured wordlist range may be truncated")
    return rows


class FeishuSheetWordlistProvider:
    def __init__(
        self, spreadsheet_token: str, sheet_name: str, cell_range: str,
        *, runner=run_lark_cli,
    ) -> None:
        self._token = spreadsheet_token
        self._sheet_name = sheet_name
        self._range = cell_range
        self._runner = runner

    def fetch(self) -> list[list[object]]:
        payload = self._runner([
            "sheets", "+cells-get", "--as", "user",
            "--spreadsheet-token", self._token,
            "--sheet-name", self._sheet_name,
            "--range", self._range,
            "--include", "value", "--skip-hidden=false",
            "--format", "json",
        ], cwd=PROJECT_ROOT, timeout=60)
        return validate_wordlist_range_rows(extract_value_matrix(payload))


def normalize_term(text: str) -> str:
    output: list[str] = []
    for char in unicodedata.normalize("NFKC", str(text or "")).casefold():
        if char == "%":
            output.append(char)
        elif char.isspace() or unicodedata.category(char)[0] in {"P", "Z"}:
            continue
        else:
            output.append(char)
    return "".join(output)


def _enabled(value: object) -> bool:
    if value is True or value == 1:
        return True
    return str(value or "").strip().casefold() in TRUE_VALUES


def _validate_machine_term(raw_term: str) -> None:
    label = raw_term.rstrip(":：").strip()
    if (
        raw_term in {"违禁词", "敏感词"}
        or label == "严禁使用"
        or MACHINE_TERM_PREFIX_RE.search(raw_term)
        or MACHINE_TERM_LABEL_RE.search(label)
        or MULTI_TERM_SEPARATOR_RE.search(raw_term)
    ):
        raise WordlistValidationError(
            "enabled term must be one concrete phrase, not a label or list"
        )


def parse_wordlist_matrix(rows: list[list[object]]) -> ParsedWordlist:
    header_at = next(
        (index for index, row in enumerate(rows)
         if any(str(value or "").strip() for value in row)),
        None,
    )
    if header_at is None:
        raise WordlistValidationError("wordlist is empty")
    headers = [str(value or "").strip() for value in rows[header_at]]
    required = ("极限词", "启用")
    supported = (*required, "替换建议", "备注")
    if any(headers.count(name) > 1 for name in supported):
        raise WordlistValidationError("supported headers must not be duplicated")
    if any(headers.count(name) != 1 for name in required):
        raise WordlistValidationError("required headers are missing or duplicated")
    indexes = {name: headers.index(name) for name in (
        "极限词", "启用", "替换建议", "备注") if name in headers}
    candidates: list[WordEntry] = []
    for raw_row in rows[header_at + 1:]:
        row = list(raw_row)

        def value(name: str) -> object:
            if name in indexes and indexes[name] < len(row):
                return row[indexes[name]]
            return ""

        if not _enabled(value("启用")):
            continue
        raw_term = str(value("极限词") or "").strip()
        if not 1 <= len(raw_term) <= 50:
            raise WordlistValidationError("enabled term length must be 1 to 50")
        _validate_machine_term(raw_term)
        normalized = normalize_term(raw_term)
        if not normalized:
            raise WordlistValidationError("enabled term normalizes to empty")
        candidates.append(WordEntry(
            raw=raw_term,
            normalized=normalized,
            replacement=str(value("替换建议") or "").strip(),
            note=str(value("备注") or "").strip(),
        ))
    if not candidates:
        raise WordlistValidationError("enabled wordlist is empty")
    ordered = sorted(candidates, key=lambda item: (
        item.normalized, item.raw, item.replacement, item.note))
    entries: list[WordEntry] = []
    warnings: set[str] = set()
    seen: set[str] = set()
    for entry in ordered:
        if entry.normalized in seen:
            warnings.add("duplicate_normalized_term")
            continue
        seen.add(entry.normalized)
        entries.append(entry)
    canonical = [dataclasses.asdict(entry) for entry in entries]
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ParsedWordlist(
        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        tuple(entries),
        tuple(sorted(warnings)),
    )


class WordlistService:
    def __init__(
        self, store: ComplianceStore, provider: object, *, sync_seconds: int = 300,
    ) -> None:
        if sync_seconds != 300:
            raise ValueError("wordlist sync interval must be 300 seconds")
        self.store = store
        self.provider = provider
        self.sync_seconds = sync_seconds

    def sync_if_due(self, now: float) -> WordlistSnapshot | None:
        next_sync = now + self.sync_seconds
        claim_token = self.store.claim_wordlist_sync(now, next_sync)
        if claim_token is None:
            return self.store.active_wordlist()
        current = self.store.active_wordlist()
        try:
            rows = self.provider.fetch()  # type: ignore[attr-defined]
            parsed = parse_wordlist_matrix(rows)
        except WordlistValidationError:
            return self.store.mark_wordlist_failure(
                "wordlist_validation_failed", checked_at=now,
                next_sync_at=next_sync, claim_token=claim_token,
            )
        except Exception:
            return self.store.mark_wordlist_failure(
                "wordlist_fetch_failed", checked_at=now, next_sync_at=next_sync,
                claim_token=claim_token,
            )
        if current is not None and current.source_hash == parsed.source_hash:
            return self.store.mark_wordlist_checked(
                checked_at=now, next_sync_at=next_sync, claim_token=claim_token)
        return self.store.activate_wordlist(
            parsed.source_hash, parsed.entries, parsed.warnings,
            checked_at=now, next_sync_at=next_sync, claim_token=claim_token,
        )  # type: ignore[return-value]
