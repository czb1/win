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


def merge_material_plan(previous, proposed, news, clues, shop):
    """Validate structure and quote provenance; interpretation belongs to LLM."""
    if proposed is None:
        return previous, "unchanged"
    if not isinstance(proposed, dict):
        return previous, "invalid_plan"
    status = proposed.get("status")
    if status not in ("unknown", "confirmed", "conflict"):
        return previous, "invalid_status"
    # An explicit uncertainty/conflict pauses spending. Missing output does not.
    if status != "confirmed":
        return {"status": status, "items": []}, status
    items = proposed.get("items")
    if not isinstance(items, list) or not items:
        return previous, "invalid_items"
    sources = [(n.get("day"), n.get("folkLegends", "")) for n in news]
    sources += [(c.get("day"), c.get("quote", "")) for c in clues]
    sources += [(i.get("day"), i.get("quote", "")) for i in (previous or {}).get("items", [])]
    checked, names = [], set()
    for item in items:
        if not isinstance(item, dict):
            return previous, "invalid_item"
        name, quantity = item.get("name"), item.get("quantity")
        day, quote = item.get("day"), item.get("quote")
        if (not isinstance(name, str) or name not in shop or name in names
                or type(quantity) is not int or quantity <= 0
                or type(day) is not int or not isinstance(quote, str) or not quote.strip()
                or not any(day == d and quote.strip() in text for d, text in sources)):
            return previous, "invalid_item_or_evidence"
        checked.append(dict(name=name, quantity=quantity, day=day, quote=quote.strip()))
        names.add(name)
    return {"status": status, "items": checked}, "confirmed"


def treasure_active(turn, mem):
    """Protect the pioneer while an evidence-backed treasure objective exists."""
    if mem.treasure_done or mem.treasure_attempted:
        return False
    if mem.treasure_materials and mem.treasure_materials.get("status") != "confirmed":
        return False
    if mem.treasure:
        return turn.round <= mem.treasure["endRound"]
    return bool(mem.treasure_materials and mem.treasure_materials.get("status") == "confirmed")


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
