from __future__ import annotations

import copy
import difflib
import json
import random
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

try:
    from pypinyin import lazy_pinyin
except Exception:
    lazy_pinyin = None


HOMOPHONE_FALLBACK_TRANSLATION = str.maketrans({"亚": "娅"})


def parse_rarity(value: Any) -> int | str:
    if isinstance(value, int):
        return value
    text = str(value or "未知").strip()
    return int(text) if text.isdigit() else text


UNKNOWN_VALUES = {"未知", "未公开", ""}
NAME_SEPARATORS = re.compile(r"[\s\-_.·・。．,，/／\\|【】\[\]（）()《》<>〈〉'\"“”‘’!！?？:：;；]+")


@dataclass(frozen=True)
class Operator:
    id: str
    name: str
    rarity: int | str
    classes: str
    classes_sub: str
    race: str = "未知"
    nation: str = "未知"
    sex: str = "未知"
    team: str = "未知"
    group: str = "未知"
    drawer: str = "未知"
    aliases: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Operator":
        aliases = data.get("aliases") or []
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            rarity=parse_rarity(data["rarity"]),
            classes=str(data["classes"]),
            classes_sub=str(data["classes_sub"]),
            race=str(data.get("race") or "未知"),
            nation=str(data.get("nation") or "未知"),
            sex=str(data.get("sex") or "未知"),
            team=str(data.get("team") or "未知"),
            group=str(data.get("group") or "未知"),
            drawer=str(data.get("drawer") or "未知"),
            aliases=tuple(str(item) for item in aliases),
        )


@dataclass(frozen=True)
class NameEntry:
    key: str
    phonetic: str
    operator: Operator
    primary: bool


def normalize_name(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    return NAME_SEPARATORS.sub("", normalized)


def phonetic_name(text: str) -> str:
    normalized = normalize_name(text)
    if not normalized:
        return ""
    if lazy_pinyin is None:
        return normalized.translate(HOMOPHONE_FALLBACK_TRANSLATION)
    return "".join(lazy_pinyin(normalized, errors=lambda value: list(value))).lower()


class OperatorRepository:
    def __init__(self, operators: list[Operator]):
        if not operators:
            raise ValueError("operator repository cannot be empty")

        self.operators = operators
        self._index: dict[str, Operator] = {}
        self._index_primary: dict[str, bool] = {}
        self._entries: list[NameEntry] = []
        self._phonetic_index: dict[str, list[NameEntry]] = {}
        for operator in operators:
            self._add_name(operator.name, operator, primary=True)
            for alias in operator.aliases:
                self._add_name(alias, operator, primary=False)

    @classmethod
    def load(cls, path: Path, aliases_path: Path | None = None) -> "OperatorRepository":
        with path.open("r", encoding="utf-8") as file:
            raw_data = json.load(file)

        if isinstance(raw_data, dict):
            raw_data = raw_data.get("operators", [])

        operators = [Operator.from_mapping(item) for item in raw_data]
        operators = cls._apply_extra_aliases(operators, aliases_path or path.with_name("operator_aliases.json"))
        return cls(operators)

    @staticmethod
    def _apply_extra_aliases(operators: list[Operator], path: Path) -> list[Operator]:
        if not path.is_file():
            return operators

        with path.open("r", encoding="utf-8") as file:
            raw_aliases = json.load(file)

        if not isinstance(raw_aliases, dict):
            raise ValueError(f"operator alias file must be a JSON object: {path}")

        by_key: dict[str, str] = {}
        for operator in operators:
            for name in (operator.id, operator.name, *operator.aliases):
                key = normalize_name(name)
                if key and key not in by_key:
                    by_key[key] = operator.id

        extra_by_id: dict[str, list[str]] = {}
        for name, aliases in raw_aliases.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"operator alias key must be a non-empty string: {name!r}")
            alias_items = OperatorRepository._normalize_extra_alias_items(name, aliases)
            operator_id = by_key.get(normalize_name(name))
            reverse_alias = False
            if operator_id is None:
                for alias in alias_items:
                    operator_id = by_key.get(normalize_name(alias))
                    if operator_id is not None:
                        reverse_alias = True
                        break
            if operator_id is None:
                continue
            extra_by_id.setdefault(operator_id, []).extend(alias_items)
            if reverse_alias:
                extra_by_id[operator_id].append(str(name))

        merged: list[Operator] = []
        for operator in operators:
            aliases = list(operator.aliases)
            seen = {normalize_name(operator.name), *(normalize_name(alias) for alias in aliases)}
            for alias in extra_by_id.get(operator.id, []):
                alias = alias.strip()
                key = normalize_name(alias)
                if alias and key and key not in seen:
                    aliases.append(alias)
                    seen.add(key)
            merged.append(replace(operator, aliases=tuple(aliases)))
        return merged

    @staticmethod
    def _normalize_extra_alias_items(name: str, aliases: Any) -> list[str]:
        if isinstance(aliases, str):
            alias_items = [aliases]
        elif isinstance(aliases, list):
            alias_items = aliases
        else:
            raise ValueError(f"operator alias value for {name!r} must be a string or list of strings")

        normalized: list[str] = []
        for alias in alias_items:
            if not isinstance(alias, str):
                raise ValueError(f"operator alias item for {name!r} must be a string: {alias!r}")
            alias = alias.strip()
            if not alias:
                raise ValueError(f"operator alias item for {name!r} cannot be empty")
            normalized.append(alias)
        return normalized

    def find(self, text: str, target: Operator | None = None) -> Operator | None:
        query = normalize_name(text)
        if not query:
            return None

        query_phonetic = phonetic_name(query)
        if target and self._operator_matches_query(query, query_phonetic, target):
            return target

        exact = self._index.get(query)
        if exact:
            return exact

        candidates: list[tuple[float, NameEntry]] = []
        if query_phonetic:
            for entry in self._phonetic_index.get(query_phonetic, []):
                candidates.append((94.0 + (1.0 if entry.primary else 0.0), entry))

        if len(query) >= 2:
            candidates.extend(self._fuzzy_candidates(query, query_phonetic))

        return self._select_candidate(candidates, target)

    def _add_name(self, name: str, operator: Operator, primary: bool) -> None:
        key = normalize_name(name)
        if not key:
            return

        entry = NameEntry(key=key, phonetic=phonetic_name(key), operator=operator, primary=primary)
        self._entries.append(entry)
        if key not in self._index or (primary and not self._index_primary.get(key, False)):
            self._index[key] = operator
            self._index_primary[key] = primary
        if entry.phonetic:
            self._phonetic_index.setdefault(entry.phonetic, []).append(entry)

    def _fuzzy_candidates(self, query: str, query_phonetic: str) -> list[tuple[float, NameEntry]]:
        candidates: list[tuple[float, NameEntry]] = []
        for entry in self._entries:
            score = 0.0
            if entry.key.startswith(query):
                score = 86.0 - min(len(entry.key) - len(query), 8) * 1.5
            elif query.startswith(entry.key) and len(entry.key) >= 2:
                score = 80.0 - min(len(query) - len(entry.key), 8) * 1.5
            elif query in entry.key:
                score = 76.0 - min(len(entry.key) - len(query), 8) * 1.5
            elif entry.key in query and len(entry.key) >= 2:
                score = 74.0 - min(len(query) - len(entry.key), 8) * 1.5

            if query_phonetic and len(query_phonetic) >= 2 and entry.phonetic:
                if entry.phonetic.startswith(query_phonetic):
                    score = max(score, 72.0 - min(len(entry.phonetic) - len(query_phonetic), 10))
                elif query_phonetic.startswith(entry.phonetic) and len(entry.phonetic) >= 2:
                    score = max(score, 68.0 - min(len(query_phonetic) - len(entry.phonetic), 10))

            ratio = difflib.SequenceMatcher(None, query, entry.key).ratio()
            threshold = 0.74 if len(query) <= 3 else 0.68
            if ratio >= threshold:
                score = max(score, 60.0 * ratio)

            if score > 0:
                if entry.primary:
                    score += 1.0
                candidates.append((score, entry))
        return candidates

    @staticmethod
    def _operator_matches_query(query: str, query_phonetic: str, operator: Operator) -> bool:
        if len(query) < 2:
            return query == normalize_name(operator.name) or any(query == normalize_name(alias) for alias in operator.aliases)

        for name in (operator.name, *operator.aliases):
            key = normalize_name(name)
            if not key:
                continue
            if query == key or key.startswith(query) or query in key:
                return True
            key_phonetic = phonetic_name(key)
            if query_phonetic and key_phonetic:
                if query_phonetic == key_phonetic or key_phonetic.startswith(query_phonetic):
                    return True
            ratio = difflib.SequenceMatcher(None, query, key).ratio()
            threshold = 0.74 if len(query) <= 3 else 0.68
            if ratio >= threshold:
                return True
        return False

    @staticmethod
    def _select_candidate(candidates: list[tuple[float, NameEntry]], target: Operator | None) -> Operator | None:
        if not candidates:
            return None

        best_by_operator: dict[str, tuple[float, NameEntry]] = {}
        for score, entry in candidates:
            current = best_by_operator.get(entry.operator.id)
            candidate_key = (score, entry.primary, -len(entry.key))
            current_key = (current[0], current[1].primary, -len(current[1].key)) if current else None
            if current is None or candidate_key > current_key:
                best_by_operator[entry.operator.id] = (score, entry)

        if target and target.id in best_by_operator and best_by_operator[target.id][0] >= 55:
            return target

        _, best_entry = max(
            best_by_operator.values(),
            key=lambda item: (item[0], item[1].primary, -len(item[1].key), item[1].operator.rarity),
        )
        return best_entry.operator

    def fresh_pool(self) -> "OperatorPool":
        return OperatorPool(self.operators)


class OperatorPool:
    def __init__(self, operators: list[Operator]):
        self.operators = copy.deepcopy([item for item in operators if "预备干员" not in item.name])

    @property
    def is_empty(self) -> bool:
        return not self.operators

    def pick_one(self) -> Operator:
        if not self.operators:
            raise ValueError("operator pool is empty")
        index = random.randrange(len(self.operators))
        return self.operators.pop(index)


@dataclass
class TagElement:
    title: str
    value: Any
    show_title: bool = True
    show_value: bool = False

    def show(self) -> None:
        self.show_title = True
        self.show_value = True

    @property
    def display_value(self) -> str:
        if self.title == "稀有度":
            return f"{self.value}星" if isinstance(self.value, int) else str(self.value)
        return str(self.value)


class GuessProcess:
    def __init__(self, operator: Operator, previous: Operator | None, hardcode: bool = False, max_count: int = 10):
        self.wrongs: dict[str, dict[str, Any]] = {}
        self.max_count = max_count
        self.operator = operator
        self.hardcode = hardcode
        self.bingo = False
        self.display = True
        self.tips_lock = False

        self.tags = self._build_hardcode() if hardcode else self._build_normal()
        for tag in self.tags.values():
            if str(tag.value) in UNKNOWN_VALUES:
                tag.show()

        if not hardcode and previous:
            self.guess(previous)

    @property
    def count(self) -> int:
        return len(self.wrongs)

    @property
    def closed_tags(self) -> list[TagElement]:
        return [item for item in self.tags.values() if item.show_value is False]

    @property
    def view_data(self) -> dict[str, Any]:
        tags = []
        for field, item in self.tags.items():
            tag_data = asdict(item)
            tag_data["field"] = field
            tag_data["value"] = item.display_value
            tags.append(tag_data)

        wrongs = []
        for item in self.wrongs.values():
            wrongs.append(
                {
                    "name": item["name"],
                    "statuses": [item["fields"].get(tag["field"], "") for tag in tags],
                }
            )

        return {
            "answer": self.operator.name,
            "tags": tags,
            "wrongs": wrongs,
            "hardcode": self.hardcode,
            "count": self.count,
            "max_count": self.max_count,
        }

    def get_tips(self) -> TagElement | bool | None:
        if not self.closed_tags:
            return False
        if self.tips_lock:
            return None

        disclosed = random.choice(self.closed_tags)
        disclosed.show()
        return disclosed

    def reveal(self) -> None:
        for tag in self.tags.values():
            tag.show()

    def guess(self, answer: Operator) -> tuple[int, int]:
        self.tips_lock = False

        if answer.id == self.operator.id:
            self.bingo = True
            return -1, 0

        unlock = 0
        wrong: dict[str, str] = {}
        for field, tag in self.tags.items():
            answer_value = getattr(answer, field)
            if answer_value == tag.value:
                if tag.show_value is False:
                    unlock += 1
                tag.show()
                wrong[field] = "ok"
            elif field == "rarity":
                if not isinstance(answer_value, int) or not isinstance(tag.value, int):
                    wrong[field] = "unknown"
                    continue
                if answer_value > tag.value:
                    wrong[field] = "down"
                elif answer_value < tag.value:
                    wrong[field] = "up"
                else:
                    wrong[field] = "ng"
            elif str(answer_value) in UNKNOWN_VALUES:
                wrong[field] = "unknown"
            else:
                wrong[field] = "ng"

        self.wrongs[answer.id] = {"name": answer.name, "fields": wrong}
        return len([item for item in wrong.values() if item == "ok"]), unlock

    def render_text_board(self) -> str:
        lines = [f"线索：{self.count}/{self.max_count}"]
        if self.hardcode:
            lines[0] += " HARDCODE"

        tag_parts = []
        for tag in self.tags.values():
            title = tag.title if tag.show_title else "???"
            value = tag.display_value if tag.show_value else "???"
            tag_parts.append(f"{title}: {value}")
        lines.append(" | ".join(tag_parts))

        if self.wrongs:
            icon = {"ok": "✓", "ng": "×", "up": "↑", "down": "↓", "unknown": "?", "": "-"}
            for item in self.view_data["wrongs"]:
                statuses = " ".join(icon.get(status, "-") for status in item["statuses"])
                lines.append(f"{item['name']}: {statuses}")

        return "\n".join(lines)

    def _build_normal(self, show_title: bool = True) -> dict[str, TagElement]:
        race = self.operator.race if self.operator.race != "未公开" else "未知"
        return {
            "rarity": TagElement("稀有度", self.operator.rarity, show_title),
            "classes": TagElement("职业", self.operator.classes, show_title),
            "classes_sub": TagElement("子职业", self.operator.classes_sub, show_title),
            "race": TagElement("种族", race, show_title),
            "nation": TagElement("势力", self.operator.nation, show_title),
            "sex": TagElement("性别", self.operator.sex, show_title),
        }

    def _build_hardcode(self) -> dict[str, TagElement]:
        tags = {
            **self._build_normal(False),
            "team": TagElement("队伍", self.operator.team, False),
            "group": TagElement("阵营", self.operator.group, False),
            "drawer": TagElement("画师", self.operator.drawer, False),
        }
        result: dict[str, TagElement] = {}

        while len(result) < 6 and tags:
            field = random.choice(list(tags.keys()))
            item = tags.pop(field)
            if str(item.value) in UNKNOWN_VALUES:
                continue
            result[field] = item

        return result
