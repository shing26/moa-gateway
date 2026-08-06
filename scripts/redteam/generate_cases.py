from __future__ import annotations

import json
from pathlib import Path

PROMPTS_PATH = Path(__file__).resolve().parent / "prompts.json"
CASES_PATH = Path(__file__).resolve().parent / "cases.json"

VARIANTS = [
    {"id": "v0", "prefix": "", "suffix": ""},
    {"id": "v1", "prefix": "请告诉我: ", "suffix": ""},
    {"id": "v2", "prefix": "需要你帮忙: ", "suffix": ""},
    {"id": "v3", "prefix": "紧急: ", "suffix": " 马上回复"},
    {"id": "v4", "prefix": "根据公司规定, ", "suffix": " 请配合"},
]

CATEGORY_LABELS = {
    "jailbreak": "越狱诱导",
    "inducement": "诱导违规输出",
    "injection": "注入攻击",
    "benign": "正常对照",
}


def load_seeds(path: Path = PROMPTS_PATH) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["seeds"]


def expand(seeds: list[dict], variants: list[dict] | None = None) -> list[dict]:
    variant_list = VARIANTS if variants is None else variants
    cases: list[dict] = []
    for seed in seeds:
        seed_id = seed["id"]
        for variant in variant_list:
            cases.append(
                {
                    "id": f"{seed_id}__{variant['id']}",
                    "seed_id": seed_id,
                    "category": seed["category"],
                    "prompt": f"{variant['prefix']}{seed['prompt']}{variant['suffix']}",
                    "output": seed["output"],
                    "expect_blocked": seed["expect_blocked"],
                    "variant": variant["id"],
                }
            )
    return cases


def expansion_detail(cases: list[dict]) -> list[dict]:
    detail: list[dict] = []
    seed_ids = sorted({c["seed_id"] for c in cases})
    for seed_id in seed_ids:
        case_ids = [c["id"] for c in cases if c["seed_id"] == seed_id]
        detail.append({"seed_id": seed_id, "case_count": len(case_ids), "case_ids": case_ids})
    return detail


def build_payload(seeds: list[dict], cases: list[dict], variants: list[dict] | None = None) -> dict:
    variant_list = VARIANTS if variants is None else variants
    per_category: dict[str, dict[str, int]] = {}
    for seed in seeds:
        entry = per_category.setdefault(seed["category"], {"seed_count": 0, "blocked_expected": 0})
        entry["seed_count"] += 1
        if seed["expect_blocked"]:
            entry["blocked_expected"] += 1
    return {
        "version": 1,
        "seed_count": len(seeds),
        "variant_count": len(variant_list),
        "case_count": len(cases),
        "categories": per_category,
        "expanded_from": expansion_detail(cases),
        "cases": cases,
    }


def generate(
    prompts_path: Path = PROMPTS_PATH,
    cases_path: Path = CASES_PATH,
    variants: list[dict] | None = None,
) -> dict:
    seeds = load_seeds(prompts_path)
    cases = expand(seeds, variants)
    payload = build_payload(seeds, cases, variants)
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    cases_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    payload = generate()
    print(f"已生成 {payload['case_count']} 条用例 -> {CASES_PATH}")


if __name__ == "__main__":
    main()
