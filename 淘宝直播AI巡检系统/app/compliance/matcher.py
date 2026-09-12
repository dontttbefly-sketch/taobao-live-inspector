from __future__ import annotations

from dataclasses import dataclass

from .models import Match, RecognizedSentence, RecognizedUnit, WordEntry
from .wordlist import normalize_term


@dataclass(frozen=True)
class NormalizedUnits:
    text: str
    unit_indexes: tuple[int, ...]


def normalize_units(units: tuple[RecognizedUnit, ...]) -> NormalizedUnits:
    chars: list[str] = []
    indexes: list[int] = []
    for unit_index, unit in enumerate(units):
        normalized_unit = normalize_term(unit.text)
        chars.extend(normalized_unit)
        indexes.extend([unit_index] * len(normalized_unit))
    return NormalizedUnits("".join(chars), tuple(indexes))


def find_matches(
    sentence: RecognizedSentence, entries: tuple[WordEntry, ...]
) -> tuple[Match, ...]:
    normalized = normalize_units(sentence.units)
    matches: list[Match] = []
    for entry in entries:
        if not entry.normalized:
            continue
        cursor = 0
        occurrence = 0
        while True:
            start = normalized.text.find(entry.normalized, cursor)
            if start < 0:
                break
            occurrence += 1
            end = start + len(entry.normalized)
            first_unit = sentence.units[normalized.unit_indexes[start]]
            last_unit = sentence.units[normalized.unit_indexes[end - 1]]
            matches.append(Match(
                raw_term=entry.raw,
                normalized_term=entry.normalized,
                sentence_text=sentence.text,
                occurrence_index=occurrence,
                start_ms=first_unit.start_ms,
                end_ms=last_unit.end_ms,
            ))
            cursor = start + 1
    return tuple(sorted(
        matches,
        key=lambda item: (
            item.start_ms, item.end_ms, item.normalized_term, item.raw_term),
    ))
