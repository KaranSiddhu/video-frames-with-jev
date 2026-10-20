"""Pure decision/measurement functions; no API calls or environment access."""

import math
from typing import Any

MODEL = "jev-1.14.0"
INPUT_PRICE_PER_MILLION = 0.042  # USD; docs checked 2026-10-19, not an invoice.
OPERATOR_CONTEXT = (
    "The operator has identified the object held by the man in this clip as a gun. "
    "Use that supplied object label. The task is to locate the visible gun holder "
    "in the current image, not independently establish the object's identity. "
    "This context does not establish that the target is visible in every frame."
)
PRESENCE_PROMPT = (
    "Using `operator_context`, is the described gun-holding man clearly visible and distinguishable "
    "in `frame` so his visible body can be localized? Do not reclassify the supplied object label. "
    "Base visibility on the current image; supplied context alone is not evidence that the target is visible."
)
SUBJECTS_PROMPT = (
    "Using `operator_context`, how many visible people can be clearly associated with the supplied gun in `frame`? "
    "Choose unclear when the hand/object association or target person cannot be seen clearly."
)
BOX_PROMPT = (
    "Locate the visible man holding the gun described in `operator_context` in `frame`. "
    "Return a tight rectangle around his full visible body: head, torso, arms and legs where visible. "
    "The target is the man, not the gun. Exclude surrounding background and other people."
)
CRITERIA = {
    "none": "The described gun holder is not visible in the current image.",
    "one": "Exactly one visible person clearly matches the described gun holder.",
    "several": "Multiple visible people match and no unique gun holder can be selected.",
    "unclear": "The described gun holder cannot be localized confidently in this image.",
}


def finite_number(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def usable_box(box: Any) -> bool:
    return (
        isinstance(box, dict)
        and all(finite_number(box.get(k)) and 0 <= box[k] <= 1 for k in ("top", "bottom", "left", "right"))
        and box["left"] < box["right"]
        and box["top"] < box["bottom"]
    )


def video_box(box: dict | None, region: dict) -> dict | None:
    """Map Jev's input-image fractions through an original-pixel crop into video fractions."""
    if not usable_box(box):
        return None
    return {
        "left": (region["x"] + box["left"] * region["width"]) / region["source_width"],
        "right": (region["x"] + box["right"] * region["width"]) / region["source_width"],
        "top": (region["y"] + box["top"] * region["height"]) / region["source_height"],
        "bottom": (region["y"] + box["bottom"] * region["height"]) / region["source_height"],
    }


def decide(response: dict, threshold: float, min_box_confidence: float | None) -> dict:
    """Presence/class answers gate consumption of Box; they do not establish accuracy."""
    answers = response.get("answers", {})
    presence = answers.get("visible", {}).get("noul")
    subject = answers.get("subjects", {}).get("choice")
    answer = answers.get("person", {})
    box, confidence = answer.get("box"), answer.get("confidence")
    status = "marked"
    if not finite_number(presence) or not 0 <= presence <= 1:
        status = "invalid_presence"
    elif subject not in CRITERIA:
        status = "invalid_subjects"
    elif subject != "one":
        status = {"none": "not_present", "several": "multiple_people", "unclear": "unclear"}[subject]
    elif presence < threshold:
        status = "low_presence"
    elif not usable_box(box):
        status = "invalid_box"
    elif confidence is not None and (not finite_number(confidence) or not 0 <= confidence <= 1):
        status = "invalid_confidence"
    elif min_box_confidence is not None:
        if confidence is None:
            status = "missing_box_confidence"
        elif confidence < min_box_confidence:
            status = "low_box_confidence"
    return {
        "status": status,
        "draw": status == "marked",
        "presence_probability": presence,
        "subjects": subject,
        "box_confidence": confidence,
        "box": box if usable_box(box) else None,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lo = math.floor(index)
    hi = math.ceil(index)
    return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 3)


def token_value(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def summarize(records: list[dict]) -> dict:
    live = [r for r in records if r["mode"] == "live"]
    inputs = [token_value(r.get("response", {}).get("usage", {}).get("input_tokens")) for r in live]
    outputs = [token_value(r.get("response", {}).get("usage", {}).get("output_tokens")) for r in live]
    sdk_times = [r["timing"]["sdk_call_ms"] for r in live if r["timing"].get("sdk_call_ms") is not None]
    client_times = [r["client_timing"]["end_to_end_ms"] for r in records if r.get("client_timing")]
    return {
        "frames": len(records),
        "successful_responses": sum(r["error"] is None for r in records),
        "errors": sum(r["error"] is not None for r in records),
        "marked": sum(r["decision"]["draw"] for r in records),
        "mode": records[0]["mode"] if records else None,
        "live_attempts": len(live),
        "known_input_tokens": sum(v for v in inputs if v is not None),
        "known_output_tokens": sum(v for v in outputs if v is not None),
        "missing_input_usage": sum(v is None for v in inputs),
        "missing_output_usage": sum(v is None for v in outputs),
        "estimated_known_input_cost_usd": sum(v for v in inputs if v is not None) * INPUT_PRICE_PER_MILLION / 1_000_000,
        "sdk_timing_samples": len(sdk_times),
        "sdk_p50_ms": percentile(sdk_times, 0.5),
        "sdk_p95_ms": percentile(sdk_times, 0.95),
        "client_timing_samples": len(client_times),
        "client_p50_ms": percentile(client_times, 0.5),
        "client_p95_ms": percentile(client_times, 0.95),
    }


def mock_response(index: int) -> dict:
    """Deterministic UI fixtures, NOT model observations or simulated billing."""
    case = index % 4
    subject = ("one", "none", "unclear", "several")[case]
    return {
        "model": "MOCK-NOT-JEV",
        "answers": {
            "visible": {"type": "noul", "noul": (0.96, 0.08, 0.51, 0.97)[case]},
            "subjects": {"type": "choice", "choice": subject, "confidence": None},
            "person": {"type": "box", "box": {"top": 0.18, "bottom": 0.9, "left": 0.32, "right": 0.62}, "confidence": None},
        },
        "usage": {"input_tokens": None, "output_tokens": None},
    }
