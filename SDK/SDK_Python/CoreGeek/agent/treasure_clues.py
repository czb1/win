"""Source-checked hints and conservative preparation, never summon plans."""

# Official task book, task supplies table. Keep the protocol spelling AcientTablet.
ITEM_DESCRIPTIONS = {
    "AcientTablet": "古符石板：灰白色石板，刻有远古文字，触摸时微弱嗡鸣",
    "StarSand": "星辰之沙：银白色细沙，暗处闪烁冷光，触感冰凉",
    "FlameBreath": "烈焰之息：透明晶石瓶，内封橙红色雾气，晃动时发光发热",
    "FrostPotion": "寒霜药剂：深蓝色粘稠液体，瓶口薄霜，刺骨寒意",
    "ThornAmulet": "荆棘护符：活藤蔓编织的圆形护符，细密尖刺，草木气味",
    "IronWhistle": "回音铁哨：生铁哨子，锈迹，内部金属片碰撞脆响",
}
KINDS = ("items", "location", "time")


def preparation_items(news, shop):
    """Conservative appearance matching for one speculative set, never a summon.

    Match independent visual features in the same source record. Model prose
    cannot authorize purchases. These rules deliberately prefer missed matches
    to guessing from a generic word such as 'light'.
    """
    texts = [n.get("folkLegends", "") for n in news]
    if not any("三钥" in t or "三道封印" in t for t in texts):
        return []
    features = {
        "AcientTablet": (("灰白",), ("石板",), ("刻", "铭文")),
        "StarSand": (("银白",), ("粉末", "细沙"), ("发光", "冷光")),
        "FlameBreath": (("水晶瓶", "晶石瓶"), ("橙红",), ("雾",)),
        "FrostPotion": (("深蓝",), ("液体",), ("薄霜", "寒意")),
        "ThornAmulet": (("藤蔓",), ("护符",), ("尖刺",)),
        "IronWhistle": (("铁哨",), ("锈",), ("金属片",)),
    }
    found = [name for name, groups in features.items()
             if any(all(any(word in t for word in group) for group in groups) for t in texts)]
    # Ambiguous sets and absent shop entries must not become partial purchases.
    return found if len(found) == 3 and all(name in shop for name in found) else []


def merge_clues(previous, proposed, news):
    """Validate quote provenance, not the truth of the model's interpretation.

    Unknown/null fields never erase earlier evidence. At most six hints per
    dimension are retained. Conflicting quotes remain for the model to compare.
    """
    merged = list(previous)
    rejected = 0
    if proposed is None:
        return merged, rejected
    if not isinstance(proposed, list):
        return merged, 1
    for clue in proposed[:18]:
        if not isinstance(clue, dict):
            rejected += 1
            continue
        kind, day, quote, meaning = (clue.get(k) for k in ("kind", "day", "quote", "meaning"))
        if (kind not in KINDS or type(day) is not int
                or not isinstance(quote, str) or not 4 <= len(quote.strip()) <= 500
                or not isinstance(meaning, str) or not 1 <= len(meaning.strip()) <= 300):
            rejected += 1
            continue
        quote, meaning = quote.strip(), meaning.strip()
        in_news = any(n.get("day") == day and quote in n.get("folkLegends", "") for n in news)
        in_saved = any(c["day"] == day and c["quote"] == quote for c in previous)
        if not (in_news or in_saved):
            rejected += 1
            continue
        entry = {"kind": kind, "day": day, "quote": quote, "meaning": meaning}
        key = (kind, day, quote)
        merged = [c for c in merged if (c["kind"], c["day"], c["quote"]) != key]
        merged.append(entry)
    # Preserve all three dimensions even when a response contains many item hints.
    merged = [c for kind in KINDS for c in [x for x in merged if x["kind"] == kind][-6:]]
    return merged, rejected + max(0, len(proposed) - 18)


def clue_status(clues):
    # A location hint like "west" is not an exact position. Never call these complete.
    return {kind: [c for c in clues if c["kind"] == kind] for kind in KINDS}
