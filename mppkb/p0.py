"""P0 safety rules (ticket 10).

Rules are deterministic rows evaluated by code. Seeds are deliberately narrow
(contraindicated drug classes by age) and are marked as awaiting clinical
confirmation; the model can only suggest new rules, never enable them.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .clock import now_iso
from .config import Config
from .db import ensure_databases

POPULATION_AGE_BOUNDS: dict[str, tuple[int, int]] = {
    "POP:NEONATE": (0, 1),
    "POP:INFANTS": (0, 3),
    "POP:UNDER_3": (0, 3),
    "POP:UNDER_8": (0, 8),
    "POP:CHILDREN": (0, 14),
    "POP:PRESCHOOL": (3, 6),
    "POP:SCHOOL_AGE": (6, 12),
    "POP:ADOLESCENT": (8, 18),
    "POP:ADULTS": (18, 120),
    "POP:ELDERLY": (65, 120),
}


def rule_applies(rule: dict[str, Any], population: str | None) -> bool:
    """A rule applies when the claim population's age range overlaps [0, age_max).

    Unknown populations never trigger an age-based rule; a rule without an age
    bound applies to every population.
    """
    age_max = rule.get("age_max_years")
    if age_max is None:
        return True
    bounds = POPULATION_AGE_BOUNDS.get(population or "")
    if bounds is None:
        return False
    low, _high = bounds
    return low < int(age_max)


SEED_RULES = [    (
        "P0:QUINOLONE_UNDER_18",
        "DRUG:LEVOFLOXACIN",
        18,
        "18 岁以下禁用喹诺酮类（软骨与肌腱损伤风险）",
    ),
    (
        "P0:QUINOLONE_UNDER_18_MOXI",
        "DRUG:MOXIFLOXACIN",
        18,
        "18 岁以下禁用喹诺酮类（软骨与肌腱损伤风险）",
    ),
    (
        "P0:QUINOLONE_UNDER_18_CIPRO",
        "DRUG:CIPROFLOXACIN",
        18,
        "18 岁以下禁用喹诺酮类（软骨与肌腱损伤风险）",
    ),
    (
        "P0:TETRACYCLINE_UNDER_8",
        "DRUG:TETRACYCLINE",
        8,
        "8 岁以下禁用四环素类（牙齿黄染与骨发育影响）",
    ),
    (
        "P0:TETRACYCLINE_UNDER_8_DOXY",
        "DRUG:DOXYCYCLINE",
        8,
        "8 岁以下禁用四环素类（牙齿黄染与骨发育影响）",
    ),
    (
        "P0:TETRACYCLINE_UNDER_8_MINO",
        "DRUG:MINOCYCLINE",
        8,
        "8 岁以下禁用四环素类（牙齿黄染与骨发育影响）",
    ),
]


def seed_rules(kb, now: str | None = None) -> int:
    now = now or now_iso()
    existing = kb.execute("SELECT COUNT(*) AS n FROM p0_rules").fetchone()["n"]
    if existing:
        return 0
    with kb:
        for rule_id, drug, age_max, template in SEED_RULES:
            kb.execute(
                "INSERT OR IGNORE INTO p0_rules(rule_id, drug, age_max_years, condition, "
                "template, enabled, confirmed_by, created_at) VALUES (?, ?, ?, NULL, ?, 1, "
                "'awaiting-clinical-confirmation', ?)",
                (rule_id, drug, age_max, template, now),
            )
    return len(SEED_RULES)


def active_rules(kb) -> dict[str, dict[str, Any]]:
    seed_rules(kb)
    rows = kb.execute(
        "SELECT rule_id, drug, age_max_years, condition, template FROM p0_rules "
        "WHERE enabled = 1 ORDER BY rule_id"
    ).fetchall()
    return {row["drug"]: dict(row) for row in rows}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(args: Any, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        _emit({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def run(args: Any, cfg: Config) -> int:
    command = getattr(args, "p0_command", "list")
    if command != "list":
        return _fail(args, f"unknown p0 command: {command}")
    kb, _corpus = ensure_databases(cfg)
    seeded = seed_rules(kb)
    rows = kb.execute(
        "SELECT rule_id, drug, age_max_years, condition, template, enabled, confirmed_by "
        "FROM p0_rules ORDER BY rule_id"
    ).fetchall()
    rules = [dict(row) for row in rows]
    if getattr(args, "json", False):
        _emit({"ok": True, "seeded": seeded, "rules": rules})
    else:
        print(f"rules: {len(rules)}")
        for rule in rules:
            print(
                f"{rule['rule_id']}  {rule['drug']}  age<={rule['age_max_years']}  "
                f"{rule['template']}"
            )
    return 0
