from scripts.transcription import (
    _resolve_whisper_model,
    format_transcript,
    prepare_analysis_result,
)


def test_cached_whisper_model_is_used_without_network(monkeypatch) -> None:
    monkeypatch.setattr(
        "huggingface_hub.snapshot_download",
        lambda model, local_files_only: f"/cached/{model}" if local_files_only else "",
    )

    assert _resolve_whisper_model("owner/model") == "/cached/owner/model"


def sample_transcription():
    return {
        "duration_seconds": 125,
        "segments": [
            {"start": 1.2, "end": 4.8, "text": " 周五召开半年经济活动分析会。 "},
            {"start": 65, "end": 72, "text": "财务部需要准备经营分析报告。"},
        ],
    }


def test_transcript_keeps_clickable_time_context_and_plain_chinese():
    text = format_transcript("半年经济活动分析会", sample_transcription())

    assert text.startswith("# 半年经济活动分析会")
    assert "[00:01:05 - 00:01:12] 财务部需要准备经营分析报告。" in text
    assert "金额、日期、人名和关键结论请回到原始录音核对" in text


def test_meeting_result_removes_technical_placeholder_and_preserves_locator():
    result = {
        "summary": "旧摘要",
        "inferences": [
            {"field_type": "材料类型", "value": "会议录音"},
            {"field_type": "待确认", "value": "会议时间"},
        ],
        "actions": [
            {
                "kind": "task",
                "title": "[00:01:05 - 00:01:12] 财务部需要准备经营分析报告",
                "detail": "旧说明",
            }
        ],
    }

    prepared = prepare_analysis_result(
        {"filename": "半年经济活动分析会.wav"}, sample_transcription(), result
    )

    assert prepared["matter_title"] == "半年经济活动分析会"
    assert prepared["summary"].startswith("会议录音已完成本地转写，共约 2 分钟")
    assert prepared["inferences"] == [{"field_type": "待确认", "value": "会议时间"}]
    assert prepared["actions"][0]["title"] == "财务准备半年经济活动分析主报告"
    assert "00:01:05 - 00:01:12" in prepared["actions"][0]["detail"]


def test_meeting_result_drops_header_warning_and_negative_phrase():
    transcription = {
        "duration_seconds": 10,
        "segments": [{"start": 0, "end": 10, "text": "大家先讨论一下其他事项。"}],
    }
    result = {
        "actions": [
            {"kind": "task", "title": "金额、日期、人名和关键结论请回到原始录音核对"},
            {"kind": "task", "title": "不需要汇报"},
        ]
    }

    prepared = prepare_analysis_result({"filename": "测试.wav"}, transcription, result)

    assert prepared["actions"] == []
