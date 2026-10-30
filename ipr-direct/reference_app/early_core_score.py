import json, math
from pathlib import Path

ARTIFACT = json.loads((Path(__file__).parent/"frozen_early_core_artifact.json").read_text(encoding="utf-8"))

def score_feature_values(values_by_anchor):
    num = 0.0
    den = 0.0
    components = []
    for d in ARTIFACT["definitions"]:
        anchor = str(d["anchor_minutes"])
        v = (values_by_anchor.get(anchor) or {}).get(d["feature"])
        if not isinstance(v, (int,float)) or not math.isfinite(v):
            continue
        c = d["direction"] * (float(v)-d["hard_negative_center"]) / d["pooled_symbol_sd"]
        c = max(-3.0, min(3.0, c))
        num += d["weight"] * c
        den += d["weight"]
        components.append((d["feature"], d["anchor_minutes"], c, d["weight"]))
    if den < 0.5:
        return None, den, components
    return num/den, den, components

def frozen_pass_from_score(score):
    """Literal historical comparator: raw Python float >= raw frozen threshold."""
    return score is not None and score >= ARTIFACT["threshold"]

def passes_early_core(values_by_anchor):
    score, observed_weight, components = score_feature_values(values_by_anchor)
    return frozen_pass_from_score(score), score, observed_weight, components
