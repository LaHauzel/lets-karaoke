"""Shared, validated alignment settings and bounded automatic postprocessing.

Automatic mode preserves acoustic anchors. Legacy repairs remain opt-in for
comparison; confidence is a model score, not an accuracy guarantee.
"""
from __future__ import annotations

import math

AUTO_OVERRIDES = {
    "reflow_gapped": False, "fix_over_long": False,
    "relocate_lowconf": False, "zeroconf_collapse": False,
    "drop_beyond_vocals": False, "enforce_timing": False,
    "char_dur_max": 0.0, "tx_crosscheck": False,
    "rel_drop_db": 0.0,
}

BALANCED_OVERRIDES = {
    "char_dur_max": 0.0, "char_rate_min": 0.09, "tail_gain": 6.0,
    "rel_drop_db": 6.0, "tx_crosscheck": False,
}

LABELS = {
    "reflow_gapped": "断档整行重排", "fix_over_long": "超长整行重排",
    "start_snap": "起点吸附", "tail_clamp": "全局尾部收回",
    "tail_extend": "拖音补偿", "relocate_lowconf": "低置信整行搬移",
    "clamp_tails_per_line": "逐行尾部收回", "enforce_timing": "最短时长拉伸",
    "gap_max": "断档阈值（秒）", "tail_gain": "拖音延长上限（秒）",
    "extend_min_p": "拖音最低置信分数", "char_dur_max": "单字上限（秒，0=不限）",
    "vocal_thr": "活跃人声阈值（峰值比例）", "rel_drop_db": "弱能量剔除（dB，0=关闭）",
    "drop_beyond_vocals": "删除人声区间外歌词", "tx_crosscheck": "转写交叉验证并剪除区间",
    "tx_min_match": "转写文本匹配阈值", "tx_prune_min": "转写剪除最小交叠（秒）",
    "tx_max_seg": "允许剪除的最长段（秒）", "tx_cap_frac": "剪除总量上限比例",
    "zeroconf_collapse": "零置信行压缩", "zeroconf_p": "零置信分数阈值",
    "min_line_dur": "最短行时长（秒）", "char_rate_min": "每字最短时长（秒）",
    "conf_high": "高置信分数", "conf_low": "低置信分数",
    "ev_prob": "证据词最低分数", "ev_dur": "证据词最短时长（秒）",
    "boundary_limit": "自动边界最大调整（秒）",
}


def schema(defaults):
    result = {}
    for key, value in defaults.items():
        item = {"label": LABELS.get(key, key), "default": value,
                "type": "boolean" if isinstance(value, bool) else "number"}
        if item["type"] == "number":
            probability = key in {"extend_min_p", "vocal_thr", "tx_min_match",
                                  "tx_cap_frac", "zeroconf_p", "conf_high",
                                  "conf_low", "ev_prob"}
        # The defaults include values such as char_rate_min=0.09.  A coarse
        # HTML step (0.05) makes a valid default fail checkValidity() before
        # the request even reaches the backend.  Keep a hundredth-second
        # editing precision for all numeric controls; the backend still owns
        # the authoritative range validation.
            item.update(min=0, max=1 if probability else 60, step=0.01)
        result[key] = item
    return result


def resolve_rules(defaults, overrides=None, profile="automatic"):
    if profile not in ("automatic", "balanced", "legacy"):
        raise ValueError("未知对齐模式")
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("对齐参数必须是对象")
    result = dict(defaults)
    if profile == "automatic":
        result.update(AUTO_OVERRIDES)
    elif profile == "balanced":
        result.update(BALANCED_OVERRIDES)
    specs = schema(defaults)
    for key, value in (overrides or {}).items():
        if key not in specs:
            raise ValueError(f"未知对齐参数: {key}")
        spec = specs[key]
        if spec["type"] == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{spec['label']}必须是开关")
        elif (isinstance(value, bool) or not isinstance(value, (float, int))
              or not math.isfinite(value) or not spec["min"] <= value <= spec["max"]):
            raise ValueError(f"{spec['label']}应介于 {spec['min']} 和 {spec['max']}")
        result[key] = value
    if result["conf_low"] > result["conf_high"]:
        raise ValueError("低置信阈值不能高于高置信阈值")
    return result


def bounded_refine(lines, intervals, rules):
    """One boundary pass: never rescale entire lines or infer lyric absence.

Only nearby vocal boundaries can change the first/last token. Long notes are
allowed; the next acoustic line anchor is a hard limit. Ambiguity is reported.
"""
    limit = rules.get("boundary_limit", 0.6)
    report = {"mode": "automatic", "changed": [], "uncertain": [], "overlaps": []}
    for i, line in enumerate(lines):
        if not line.segments:
            report["uncertain"].append(i)
            continue
        original = (line.start, line.end)
        next_start = lines[i + 1].start if i + 1 < len(lines) else float("inf")
        if line.prob < rules["conf_low"]:
            report["uncertain"].append(i)
        # Low confidence is insufficient evidence for moving or deleting text.
        if line.prob >= rules["conf_low"] and intervals:
            first, last = line.segments[0], line.segments[-1]
            starts = [a for a, b in intervals if abs(a - first.start) <= limit
                      and a < first.end and b > first.start]
            if rules["start_snap"] and starts:
                first.start = max(0.0, min(starts, key=lambda a: abs(a - first.start)))
            ends = [b for a, b in intervals if a < last.end and b > last.start
                    and abs(b - last.end) <= limit]
            if ends:
                target = min(ends, key=lambda b: abs(b - last.end))
                if target < last.end and rules["clamp_tails_per_line"]:
                    last.end = target
                elif (target > last.end and rules["tail_extend"]
                      and line.prob >= rules["extend_min_p"]):
                    last.end = min(target, last.end + rules["tail_gain"])
        # Clip overlaps at existing anchors; never push following lines forward.
        for k, segment in enumerate(line.segments):
            bound = line.segments[k + 1].start if k + 1 < len(line.segments) else next_start
            if segment.end > bound:
                if bound > segment.start:
                    segment.end = bound
                else:
                    report["overlaps"].append(i)
            if segment.end <= segment.start:
                report["overlaps"].append(i)
        line.start = line.segments[0].start
        line.end = max(s.end for s in line.segments)
        if original != (line.start, line.end):
            report["changed"].append(i)
    report["overlaps"] = sorted(set(report["overlaps"]))
    report["projection"] = project_timeline(lines)
    report["uncertain"] = sorted(set(report["uncertain"] + report["overlaps"]
                                     + report["projection"]["changed_rows"]))
    return report


def project_timeline(lines, tick=0.01):
    """Weighted isotonic projection: the least squared change satisfying order.

    Unlike forward shifting, later reliable anchors also constrain earlier
    guesses. One centisecond per character is the ASS format's time resolution,
    not an assumed singing rate. Large corrections remain explicitly uncertain.
    """
    segments = [(i, s) for i, line in enumerate(lines) for s in line.segments]
    blocks = []
    old = [(s.start, s.end) for _, s in segments]
    for k, (_, segment) in enumerate(segments):
        weight = max(0.01, min(1.0, segment.prob))
        for j, value in enumerate((segment.start, segment.end)):
            if not math.isfinite(value):
                raise ValueError("模型返回非有限时间戳")
            index = 2 * k + j
            offset = (k + j) * tick
            blocks.append([index, index, weight, weight * (value - offset)])
            while len(blocks) >= 2 and blocks[-2][3] / blocks[-2][2] > blocks[-1][3] / blocks[-1][2]:
                right = blocks.pop()
                left = blocks.pop()
                blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    projected = [0.0] * (len(segments) * 2)
    for first, last, weight, total in blocks:
        value = max(0.0, total / weight)
        for index in range(first, last + 1):
            projected[index] = value + ((index + 1) // 2) * tick
    changed, maximum = set(), 0.0
    for k, (row, segment) in enumerate(segments):
        segment.start, segment.end = projected[2 * k:2 * k + 2]
        delta = max(abs(segment.start - old[k][0]), abs(segment.end - old[k][1]))
        if delta > 1e-6:
            changed.add(row)
            maximum = max(maximum, delta)
    for line in lines:
        if line.segments:
            line.start, line.end = line.segments[0].start, line.segments[-1].end
    return {"changed_rows": sorted(changed), "max_adjustment_s": round(maximum, 4)}
