from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "operators.json"

CLASSES = {
    "CASTER": "术师",
    "MEDIC": "医疗",
    "PIONEER": "先锋",
    "SNIPER": "狙击",
    "SPECIAL": "特种",
    "SUPPORT": "辅助",
    "TANK": "重装",
    "WARRIOR": "近卫",
}

SPECIAL_ALIASES = {
    "阿米娅近卫": {"近卫阿米娅", "剑兔", "阿米娅近卫形态"},
    "阿米娅医疗": {"医疗阿米娅", "奶兔", "阿米娅医疗形态"},
    "维什戴尔": {"W", "w", "W异格", "异格W", "威什戴尔"},
    "缄默德克萨斯": {"异德", "德克萨斯异格"},
    "假日威龙陈": {"水陈", "异格陈"},
    "浊心斯卡蒂": {"红蒂", "异格斯卡蒂"},
    "耀骑士临光": {"耀光", "异格临光"},
    "麒麟R夜刀": {"麒麟夜刀", "异格夜刀"},
    "百炼嘉维尔": {"异格嘉维尔"},
    "寒芒克洛丝": {"异格克洛丝"},
}


def load_json(name: str) -> Any:
    return json.loads((RAW / name).read_text(encoding="utf-8"))


def normalize_text(text: Any) -> str:
    return str(text or "").strip()


def power_name(team_table: dict[str, Any], power_id: str | None) -> str:
    if not power_id:
        return "未知"
    return team_table.get(power_id, {}).get("powerName") or "未知"


def story_text(handbook_info: dict[str, Any], char_id: str) -> str:
    item = handbook_info.get(char_id)
    if not item:
        return ""
    for story in item.get("storyTextAudio", []):
        if story.get("storyTitle") == "基础档案":
            stories = story.get("stories") or []
            if stories:
                return stories[0].get("storyText") or ""
    return ""


def extract_profile(handbook_info: dict[str, Any], char_id: str, fallback_id: str | None = None) -> tuple[str, str]:
    text = story_text(handbook_info, char_id) or (story_text(handbook_info, fallback_id) if fallback_id else "")
    sex = "未知"
    race = "未知"
    if text:
        sex_match = re.search(r"【性别】\s*([^\n|\s]+)", text)
        race_match = re.search(r"【种族】\s*([^\n|\s]+)", text)
        if sex_match:
            sex = sex_match.group(1).strip()
        if race_match:
            race = race_match.group(1).strip()
    return sex, race


def build_skin_map(skin_table: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    skin_map: dict[str, list[dict[str, Any]]] = {}
    for skin in skin_table.values():
        char_id = skin.get("charId")
        skin_id = skin.get("skinId", "")
        if "char_1001_amiya2" in skin_id:
            char_id = "char_1001_amiya2"
        if "char_1037_amiya3" in skin_id:
            char_id = "char_1037_amiya3"
        if char_id:
            skin_map.setdefault(char_id, []).append(skin)
    for items in skin_map.values():
        items.sort(key=lambda item: item.get("displaySkin", {}).get("getTime") or 0)
    return skin_map


def drawer_name(skin_map: dict[str, list[dict[str, Any]]], char_id: str, fallback_id: str | None = None) -> str:
    skins = skin_map.get(char_id) or (skin_map.get(fallback_id) if fallback_id else None) or []
    for skin in skins:
        drawers = skin.get("displaySkin", {}).get("drawerList") or []
        if drawers:
            return drawers[-1]
    return "未知"


def rarity_value(value: Any) -> int:
    if isinstance(value, int):
        return value + 1
    value = str(value)
    if "_" in value:
        return int(value.split("_")[-1])
    return int(value)


def display_name(char_id: str, data: dict[str, Any]) -> str:
    if char_id == "char_1001_amiya2":
        return "阿米娅近卫"
    if char_id == "char_1037_amiya3":
        return "阿米娅医疗"
    return normalize_text(data.get("name"))


def aliases_for(char_id: str, data: dict[str, Any], name: str) -> list[str]:
    aliases: set[str] = set()
    origin_name = normalize_text(data.get("name"))
    english_name = normalize_text(data.get("appellation"))
    if origin_name and origin_name != name:
        aliases.add(origin_name)
    if english_name:
        aliases.add(english_name)
    compact_name = re.sub(r"[\s\-·・.。_]+", "", name)
    if compact_name and compact_name != name:
        aliases.add(compact_name)
    aliases.update(SPECIAL_ALIASES.get(name, set()))
    return sorted(item for item in aliases if item and item != name)


def build() -> list[dict[str, Any]]:
    character_table = load_json("character_table.json")
    char_patch_table = load_json("char_patch_table.json").get("patchChars", {})
    handbook_info = load_json("handbook_info_table.json").get("handbookDict", {})
    team_table = load_json("handbook_team_table.json")
    uniequip_table = load_json("uniequip_table.json").get("subProfDict", {})
    skin_table = load_json("skin_table.json").get("charSkins", {})

    characters = {**character_table, **char_patch_table}
    skin_map = build_skin_map(skin_table)
    operators = []

    for char_id, data in characters.items():
        profession = data.get("profession")
        if profession not in CLASSES:
            continue
        raw_name = normalize_text(data.get("name"))
        if not raw_name:
            continue

        fallback_id = "char_002_amiya" if char_id in {"char_1001_amiya2", "char_1037_amiya3"} else None
        name = display_name(char_id, data)
        sex, race = extract_profile(handbook_info, char_id, fallback_id)
        sub_id = data.get("subProfessionId")
        operators.append(
            {
                "id": char_id,
                "name": name,
                "rarity": rarity_value(data.get("rarity")),
                "classes": CLASSES[profession],
                "classes_sub": uniequip_table.get(sub_id, {}).get("subProfessionName") or "未知",
                "race": race,
                "nation": power_name(team_table, data.get("nationId")),
                "sex": sex,
                "team": power_name(team_table, data.get("teamId")),
                "group": power_name(team_table, data.get("groupId")),
                "drawer": drawer_name(skin_map, char_id, fallback_id),
                "aliases": aliases_for(char_id, data, name),
            }
        )

    operators.sort(key=lambda item: (-item["rarity"], item["name"], item["id"]))
    return operators


if __name__ == "__main__":
    operators = build()
    OUT.write_text(json.dumps(operators, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(operators)} operators to {OUT}")
