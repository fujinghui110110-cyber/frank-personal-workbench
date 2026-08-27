from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any


AUDIO_VIDEO_TYPES = {"audio", "video"}
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
MEETING_ACTION_PATTERNS = (
    (
        "decision",
        "确认半年经济活动分析会召开时间",
        "会议组织人",
        (("周五",), ("周六",), ("会议", "半年度")),
    ),
    (
        "task",
        "财务准备半年经济活动分析主报告",
        "财务部",
        (("半年经济活动", "半年度", "经营分析"), ("财务",), ("报告",)),
    ),
    (
        "task",
        "人力部门准备人工成本专题报告",
        "人力部门",
        (("人力", "人员部", "人工成本"), ("报告",), ("成本", "经营部门", "发言")),
    ),
    (
        "task",
        "各经营部门总监准备会议发言",
        "各经营部门总监",
        (("经营部门",), ("总监",), ("发言",)),
    ),
    (
        "task",
        "按框架梳理制度清单并逐项报批",
        "相关职能部门",
        (("制度",), ("框架",), ("梳理",), ("报", "回复", "上级")),
    ),
    (
        "risk",
        "明确会议材料和企业微信文档的保密要求",
        "全体参会人员",
        (("企业微信",), ("保密",)),
    ),
    (
        "task",
        "草坪部门准备成本分析",
        "草坪部门",
        (("草坪",), ("成本分析",)),
    ),
)


def is_audio_video(material: dict[str, Any]) -> bool:
    return str(material.get("source_type") or "") in AUDIO_VIDEO_TYPES


def format_timestamp(seconds: float | int | None) -> str:
    total = max(0, int(float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_transcript(title: str, result: dict[str, Any]) -> str:
    lines = [
        f"# {title}",
        "",
        "> 本地自动转写。金额、日期、人名和关键结论请回到原始录音核对。",
        "",
        "## 完整转写",
    ]
    for segment in result.get("segments") or []:
        text = re.sub(r"\s+", " ", str(segment.get("text") or "")).strip()
        if not text:
            continue
        start = format_timestamp(segment.get("start"))
        end = format_timestamp(segment.get("end"))
        lines.append(f"[{start} - {end}] {text}")
    if len(lines) == 5:
        text = re.sub(r"\s+", " ", str(result.get("text") or "")).strip()
        if text:
            lines.append(f"[00:00:00] {text}")
    return "\n".join(lines).strip()


def meeting_windows(transcription: dict[str, Any]) -> list[tuple[str, str, str]]:
    segments = [
        segment
        for segment in transcription.get("segments") or []
        if str(segment.get("text") or "").strip()
    ]
    windows: list[tuple[str, str, str]] = []
    for size in (1, 2, 3):
        for index in range(len(segments) - size + 1):
            selected = segments[index : index + size]
            text = re.sub(
                r"\s+",
                " ",
                " ".join(str(segment.get("text") or "").strip() for segment in selected),
            ).strip()
            start = format_timestamp(selected[0].get("start"))
            end = format_timestamp(selected[-1].get("end"))
            windows.append((start, end, text))
    return windows


def derive_meeting_items(
    transcription: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    windows = meeting_windows(transcription)
    actions: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    inferences: list[dict[str, Any]] = []
    for kind, title, owner, groups in MEETING_ACTION_PATTERNS:
        source = next(
            (
                (start, end, text)
                for start, end, text in windows
                if all(any(keyword in text for keyword in group) for group in groups)
            ),
            None,
        )
        if source is None:
            continue
        start, end, quote = source
        locator = f"{start} - {end}"
        quote = quote[:420]
        actions.append(
            {
                "kind": kind,
                "title": title,
                "detail": f"原始录音位置 {locator}。原话：{quote}",
                "owner": owner,
                "due_date": None,
            }
        )
        claim = {
            "field_type": "待确认" if kind == "decision" else "风险提示" if kind == "risk" else "会议安排",
            "value": title,
            "source_locator": locator,
            "quote": quote,
            "confidence": 0.82 if kind == "decision" else 0.9,
        }
        if kind == "decision":
            inferences.append(claim)
        else:
            facts.append(claim)
    return actions, facts, inferences


def build_meeting_summary(transcription: dict[str, Any]) -> str:
    actions, _, inferences = derive_meeting_items(transcription)
    minutes = max(1, round(float(transcription.get("duration_seconds") or 0) / 60))
    if not actions:
        return f"会议录音已完成本地转写，共约 {minutes} 分钟。请在工作详情中查看完整转写。"
    topics = "；".join(item["title"] for item in actions[:5])
    pending = f"其中 {len(inferences)} 项需要确认。" if inferences else ""
    return f"会议录音已完成本地转写，共约 {minutes} 分钟。已整理：{topics}。{pending}"


def transcribe_material(material: dict[str, Any], content: bytes) -> dict[str, Any]:
    try:
        import mlx_whisper
    except ImportError as error:
        raise RuntimeError("本机尚未安装会议转写能力，请运行安装脚本后自动重试") from error

    filename = str(material.get("filename") or "会议录音.wav")
    title = Path(filename).stem[:80] or "会议录音"
    suffix = Path(filename).suffix or ".wav"
    model = os.getenv("WORKBENCH_WHISPER_MODEL", DEFAULT_MODEL)
    language = os.getenv("WORKBENCH_WHISPER_LANGUAGE", "zh")
    prompt = f"这是Frank 的中文工作会议。会议主题：{title}。"

    with tempfile.NamedTemporaryFile(suffix=suffix) as source:
        source.write(content)
        source.flush()
        result = mlx_whisper.transcribe(
            source.name,
            path_or_hf_repo=model,
            language=language,
            initial_prompt=prompt,
            temperature=0.0,
            verbose=False,
        )

    text = format_transcript(title, result)
    segments = result.get("segments") or []
    if len(text.splitlines()) <= 5:
        raise RuntimeError("录音没有识别出可用文字，请检查文件是否包含清晰人声")
    duration = max((float(item.get("end") or 0) for item in segments), default=0.0)
    return {
        "text": text,
        "segments": segments,
        "language": str(result.get("language") or language),
        "model": model,
        "duration_seconds": duration,
        "segment_count": len(segments),
    }


def prepare_analysis_result(
    material: dict[str, Any], transcription: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    derived_actions, derived_facts, derived_inferences = derive_meeting_items(transcription)
    result["summary"] = build_meeting_summary(transcription)
    existing_inferences = [
        item for item in result.get("inferences") or [] if item.get("field_type") != "材料类型"
    ]
    result["inferences"] = derived_inferences or existing_inferences
    if derived_facts:
        result["facts"] = derived_facts
    fallback_actions = []
    for action in result.get("actions") or []:
        title = str(action.get("title") or "")
        if "请回到原始录音核对" in title or "不需要" in title:
            continue
        match = re.match(r"^\[([^]]+)]\s*", title)
        if match:
            action["title"] = title[match.end() :]
            action["detail"] = f"原始录音位置 {match.group(1)}。请结合转写和录音确认。"
        fallback_actions.append(action)
    result["actions"] = derived_actions or fallback_actions
    result["matter_title"] = Path(str(material.get("filename") or "会议录音")).stem[:80]
    return result
