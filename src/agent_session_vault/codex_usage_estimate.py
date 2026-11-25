"""Estimate missing usage for the local receipt from the current projected rollouts."""

from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import re
from statistics import mean, median_low
from typing import NamedTuple

from .views import TokscaleView


_MISSING_USAGE = re.compile(
    r"Warning: ([\d,]+) of ([\d,]+) inspected rollout files contain Codex activity "
    r"but no supported usage records"
)
_COUNTERS = {
    "input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens",
    "reasoning_output_tokens",
}
_FALLBACK_COUNTERS = _COUNTERS | {"prompt_tokens", "input", "completion_tokens", "output", "cached_tokens"}
_RESPONSE_ACTIVITY = {
    "agent_message", "reasoning", "function_call", "function_call_output", "local_shell_call",
    "custom_tool_call", "custom_tool_call_output", "tool_search_call", "tool_search_output",
    "web_search_call", "image_generation_call",
}
_RELEVANT = (
    b'"usage"', b'token_count', b'token_usage_record', b'response_item', b'user_message',
    b'agent_message', b'agent_reasoning', b'turn_context', b'session_meta', b'world_state',
)


class Rollout(NamedTuple):
    provider: str
    model: str
    month: str
    turns: int
    activity: bool
    model_activity: bool
    metered: bool
    calibration_tokens: int


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _has_counters(usage: dict, fields: set[str]) -> bool:
    return any(type(usage.get(key)) is int for key in fields)


def _tokens(usage: dict) -> int:
    return sum(max(usage[key], 0) for key in ("input_tokens", "output_tokens") if type(usage.get(key)) is int)


def _summarize(path: Path) -> Rollout:
    provider = model = month = ""
    turns = native_tokens = response_tokens = 0
    metered = native_metered = activity = model_activity = False
    with path.open("rb") as handle:
        for line in handle:
            if not any(marker in line for marker in _RELEVANT):
                continue
            try:
                row = _object(json.loads(line))
            except (UnicodeError, ValueError):
                continue
            payload = _object(row.get("payload"))
            kind, event = row.get("type"), payload.get("type")
            if kind == "session_meta" and not month:
                month = _string(row.get("timestamp"))[:7]
                provider = _string(payload.get("model_provider"))
            elif kind == "turn_context":
                model = (
                    _string(payload.get("model")) or _string(payload.get("model_name"))
                    or _string(_object(payload.get("model_info")).get("slug")) or model
                )
            elif kind == "world_state" and not model:
                model = _string(_object(payload.get("state")).get("model"))
            elif kind == "response_item" and isinstance(event, str):
                model_activity |= event in _RESPONSE_ACTIVITY
                activity |= event == "message"
            elif kind == "event_msg":
                if event == "user_message" and payload.get("message") == "user":
                    turns += 1
                if event == "agent_message" or event == "agent_reasoning":
                    model_activity = True
                if event == "token_count":
                    info = _object(payload.get("info"))
                    last = _object(info.get("last_token_usage"))
                    total = _object(info.get("total_token_usage"))
                    native_metered |= _has_counters(last, _COUNTERS) or _has_counters(total, _COUNTERS)
                    native_tokens += _tokens(last)
            elif kind == "token_usage_record":
                usage = _object(payload.get("usage"))
                metered |= _has_counters(usage, _COUNTERS)
                response_tokens += _tokens(usage)
            for container in (row, row.get("data"), row.get("result"), row.get("response")):
                usage = _object(_object(container).get("usage"))
                metered |= _has_counters(usage, _FALLBACK_COUNTERS)
    return Rollout(
        provider, model, month or path.name[8:15], min(max(turns, 1), 4),
        bool(activity or model_activity or turns), model_activity, metered or native_metered,
        native_tokens if native_metered else response_tokens,
    )


def _signature(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _scan(paths: set[Path], cache_path: Path) -> dict[str, list[Rollout]]:
    try:
        cache = _object(json.loads(cache_path.read_bytes()))
    except (OSError, ValueError):
        cache = {}
    previous = _object(cache.get("files")) if cache.get("version") == 1 else {}
    current = {}
    copies = defaultdict(list)
    for path in sorted(paths):
        signature = _signature(path)
        saved = previous.get(str(path))
        if (
            isinstance(saved, list) and len(saved) == 2 and saved[0] == signature
            and isinstance(saved[1], list) and len(saved[1]) == len(Rollout._fields)
        ):
            summary = Rollout(*saved[1])
        else:
            summary = _summarize(path)
            if _signature(path) != signature:
                raise OSError("a projected rollout changed during the estimate")
        current[str(path)] = (signature, summary)
        copies[path.name].append(summary)
    temporary = cache_path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps({"version": 1, "files": current}, separators=(",", ":")), encoding="utf-8")
        temporary.replace(cache_path)
    finally:
        temporary.unlink(missing_ok=True)
    return copies


def _cohort_keys(rollout: Rollout) -> tuple[tuple, ...]:
    provider, model, month, turns, _, model_activity, _, _ = rollout
    shape = (model_activity, turns)
    return ((provider, model, month, *shape), (provider, month, *shape), (month, *shape), shape, ())


def _quantile(ordered: list[int], fraction: float) -> int:
    return ordered[int((len(ordered) - 1) * fraction)]


def _estimate(copies: dict[str, list[Rollout]], missing_files: int) -> dict[str, object]:
    cohorts = defaultdict(list)
    candidates = []
    for rollouts in copies.values():
        metered = [rollout for rollout in rollouts if rollout.metered]
        if metered:
            sample = max(metered, key=lambda rollout: rollout.calibration_tokens)
            if sample.activity and sample.calibration_tokens:
                for key in _cohort_keys(sample):
                    cohorts[key].append(sample.calibration_tokens)
        else:
            active = [rollout for rollout in rollouts if rollout.activity]
            if active:
                candidates.append(max(active, key=lambda rollout: (rollout.model_activity, rollout.turns, rollout)))

    minimum_samples = 25
    if len(cohorts[()]) < minimum_samples or not candidates:
        return {"status": "unavailable", "reason": "insufficient_calibration"}
    if len(candidates) > missing_files:
        return {"status": "unavailable", "reason": "candidate_count_exceeds_report"}

    for sample in cohorts.values():
        sample.sort()
    points = []
    lower = upper = 0
    for rollout in candidates:
        sample = next(cohorts[key] for key in _cohort_keys(rollout) if len(cohorts[key]) >= minimum_samples)
        points.append(median_low(sample) if rollout.model_activity else 0)
        lower += _quantile(sample, 0.25) if rollout.model_activity else 0
        upper += _quantile(sample, 0.75)

    points.sort()
    unlocated = missing_files - len(candidates)
    positive = [point for point in points if point > 0]
    cap = _quantile(positive, 0.90) if positive else 0
    fallback = round(mean(min(point, cap) for point in points))
    return {
        "status": "estimated",
        "method": "rollout_cohort_median_v1",
        "located_candidates": len(candidates),
        "unlocated_files": unlocated,
        "calibration_rollouts": len(cohorts[()]),
        "estimated_missing_tokens": sum(points) + unlocated * fallback,
        "lower_scenario_tokens": lower,
        "upper_scenario_tokens": upper + unlocated * max(fallback, _quantile(points, 0.75)),
    }


def local_usage_estimate(
    view: TokscaleView,
    output: str,
    recorded_tokens: int,
    cache_path: Path,
) -> dict[str, object]:
    warning = _MISSING_USAGE.search(output)
    if not warning:
        return {"status": "no_missing_usage_reported"}
    missing, inspected = (int(value.replace(",", "")) for value in warning.groups())
    paths = {
        path for client, root in view.extra_dirs if client == "codex"
        for directory in ("sessions", "archived_sessions")
        for path in (root / directory).rglob("*.jsonl") if path.is_file()
    }
    report: dict[str, object] = {"missing_usage_files": missing, "inspected_files": inspected}
    if len(paths) != inspected or not 0 < missing <= inspected:
        return {**report, "status": "unavailable", "reason": "rollout_scope_mismatch"}
    estimate = _estimate(_scan(paths, cache_path), missing)
    report.update(estimate)
    if estimate["status"] == "estimated":
        report["recorded_tokens"] = recorded_tokens
        report["combined_token_estimate"] = recorded_tokens + int(estimate["estimated_missing_tokens"])
        report["combined_lower_scenario"] = recorded_tokens + int(estimate["lower_scenario_tokens"])
        report["combined_upper_scenario"] = recorded_tokens + int(estimate["upper_scenario_tokens"])
    return report
