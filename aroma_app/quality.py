"""Offline, evidence-conscious document studio. No model or network calls.

The generated files are local drafts. Editable Office exports use our own
reviewed templates, without model calls or additional runtime packages.
This module uses only the Python standard library so the portable app can run
without an additional package installation.
"""
from __future__ import annotations

import html
import json
import re
import textwrap
from typing import Any

KINDS = {"report": "사업보고서·계획서", "ppt": "강의 PPT 준비", "promo": "홍보물 시안"}
FIELDS = {"title": 100, "goal": 700, "audience": 400, "length": 80,
          "evidence": 8000, "photos": 1000, "format": 100, "date": 100,
          "time": 100, "place": 200, "program": 200, "duration": 80,
          "contact": 200, "price": 200, "materials": 1000, "steps": 2000,
          "source": 1000}
FIELD_NAMES = {"title": "제목", "goal": "목적", "audience": "대상", "length": "분량", "evidence": "근거", "photos": "사진 메모", "format": "파일 형식", "date": "날짜", "time": "시간", "place": "장소", "program": "프로그램", "duration": "수업 길이", "contact": "문의처", "price": "비용", "materials": "준비물", "steps": "활동 순서", "source": "근거 출처"}
RISK = re.compile(r"치료|치유|완치|예방|항암|항염|무독성|부작용\s*없|태아|면역력|호르몬|혈압|질환|질병|아토피|불면증|우울증|통증\s*(완화|개선)|천연.{0,12}안전", re.IGNORECASE)


def _clean_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("입력 내용을 확인해 주세요.")
    result = {}
    for key, limit in FIELDS.items():
        value = payload.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError("입력 칸에는 글자나 숫자를 넣어 주세요.")
        value = str(value).strip()
        # Prevent invalid XML characters and confusing invisible control input.
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
        if len(value) > limit:
            raise ValueError(f"{FIELD_NAMES[key]} 입력이 너무 깁니다. {limit:,}자 안으로 줄여 주세요.")
        result[key] = value
    return result


def _lines(value: str, max_items: int = 20) -> list[str]:
    values = [re.sub(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)", "", part).strip()
              for part in value.splitlines() if part.strip()]
    return values[:max_items]


def _safe(value: str, fallback: str) -> str:
    return "[건강 효능 표현 검토 필요]" if RISK.search(value) else value or fallback


def _duration_minutes(time_value: str) -> int | None:
    pairs = re.findall(r"(\d{1,2}):(\d{2})", time_value)
    if len(pairs) != 2:
        return None
    numbers = [(int(h), int(m)) for h, m in pairs]
    if any(h > 23 or m > 59 for h, m in numbers):
        return None
    start, end = (h * 60 + m for h, m in numbers)
    return end - start if end > start else None


def _requested_minutes(value: str) -> int | None:
    hour = re.search(r"(\d+(?:\.\d+)?)\s*시간", value)
    minute = re.search(r"(\d+)\s*분", value)
    if hour or minute:
        return round(float(hour.group(1)) * 60) + (int(minute.group(1)) if minute else 0) if hour else int(minute.group(1))
    return None


def _ppt_count(value: str) -> int:
    match = re.search(r"\d+", value)
    return max(4, min(12, int(match.group()))) if match else 8


def _qa(kind: str, p: dict[str, str], outline: list[dict[str, Any]]) -> list[dict[str, str]]:
    qa = []
    add = lambda level, message: qa.append({"level": level, "message": message})
    for key, name in [("title", "제목"), ("goal", "만드는 목적"), ("audience", "보는 사람")]:
        if not p[key]:
            add("warning", f"{name}이 비어 있어 기본 안내로 채웠습니다. 확인해 주세요.")
    if not p["evidence"]:
        add("warning", "근거가 비어 있습니다. 실제 실적·인원·후기·금액을 만들지 않았습니다.")
    elif not p["source"]:
        add("warning", "입력한 근거의 출처가 비어 있습니다. 원본 파일명이나 확인한 사람을 적어 주세요.")
    if any(RISK.search(value) for value in p.values()):
        add("block", "건강·의료 효능으로 읽힐 수 있는 표현을 시안에서 제외했습니다. 전문가와 근거를 확인해 주세요.")
    if re.search(r"\d", p["evidence"]):
        add("warning", "근거에 수치가 있습니다. 증빙과 단위를 확인해 주세요. 이 기능은 금액을 계산하지 않습니다.")
    if len([line for line in p["evidence"].splitlines() if line.strip()]) > 20:
        add("warning", "근거가 20줄을 넘었습니다. 초안에는 앞의 20줄을 넣었습니다. 원문은 요청과 JSON 파일에 보관합니다.")
    if re.search(r"추천|후기|만족|자격|전문\s*강사|공인|인증", p["evidence"]):
        add("warning", "후기·자격·인증 문구는 증빙과 사용 동의를 확인해 주세요.")
    if kind in ("ppt", "promo"):
        for key, name in [("date", "날짜"), ("time", "시간"), ("place", "장소")]:
            if not p[key]:
                add("warning", f"{name}은 아직 확인하지 않았습니다. 이전 자료에서 자동으로 가져오지 않았습니다.")
    actual = _duration_minutes(p["time"])
    claimed = _requested_minutes(p["duration"])
    if p["time"] and len(re.findall(r"\d{1,2}:\d{2}", p["time"])) >= 2 and actual is None:
        add("warning", "입력한 시간대를 계산할 수 없습니다. 예: 14:00~15:30처럼 시작·종료 시간을 확인해 주세요.")
    if actual is not None and claimed is not None and actual != claimed:
        add("block", f"시간대는 {actual}분인데 수업 길이는 {claimed}분입니다. 하나의 실제 시간으로 맞춰 주세요.")
    if kind == "ppt":
        add("info", "HTML은 4~12장, 실제 편집 가능한 PPTX는 현재 8장으로 만들 수 있습니다. 사진은 확인 자리로 남깁니다.")
        if p["length"] and str(_ppt_count(p["length"])) not in p["length"]:
            add("warning", "첫 버전은 4~12장으로 만들 수 있습니다. 장 수를 이 범위에 맞췄습니다.")
        if any(len(s["title"]) > 30 for s in outline):
            add("warning", "긴 슬라이드 제목이 있습니다. 미리보기에서 줄바꿈을 확인해 주세요.")
    if kind == "promo":
        if not p["contact"]:
            add("warning", "문의처는 아직 확인하지 않았습니다. 과거 홍보물의 연락처를 복사하지 않았습니다.")
        add("info", "홍보물은 글자와 배치로 만든 SVG 시안입니다. 실제 제품 사진 생성·자동 삽입은 하지 않습니다.")
        add("info", "사진 사용 동의·저작권과 인쇄소의 최종 규격을 확인해 주세요. SVG는 RGB 색상이며 CMYK 변환은 하지 않습니다.")
    if p["photos"]:
        add("info", "사진 메모만 보관했습니다. 사진은 외부로 보내거나 생성물에 자동으로 넣지 않았습니다.")
    add("info", "현재 AI 제공자에 연결하지 않았습니다. 이 초안은 기기에 있는 서식으로 만들었습니다.")
    add("info", "실제 날짜·금액·자격·실적·배합 수치는 사용자가 확인한 뒤 사용해 주세요.")
    return qa


def _report(p: dict[str, str]) -> list[dict[str, Any]]:
    facts = [_safe(line, "[근거 확인 필요]") for line in _lines(p["evidence"])]
    return [
        {"title": "작성 목적", "body": [_safe(p["goal"], "[이 문서로 무엇을 설명할지 적어 주세요]")]},
        {"title": "대상과 프로그램", "body": ["대상: " + _safe(p["audience"], "[대상 확인 필요]"), "프로그램: " + _safe(p["program"], "[프로그램 확인 필요]")]},
        {"title": "운영 계획", "body": ["날짜: " + _safe(p["date"], "[날짜 확인 필요]"), "시간: " + _safe(p["time"], "[시간 확인 필요]"), "장소: " + _safe(p["place"], "[장소 확인 필요]"), "진행 내용: " + _safe(p["steps"], "[진행 순서 확인 필요]")]},
        {"title": "준비물과 준비 업무", "body": [_safe(p["materials"], "[실제로 필요한 재료·도구·장소 준비를 적어 주세요]")]},
        {"title": "입력한 근거", "body": facts or ["[실적·참여 인원·사진 등 확인된 근거를 넣어 주세요]"]},
        {"title": "비용과 정산", "body": ["금액은 견적 만들기의 검증된 계산 결과를 첨부해 주세요.", "임의 단가·매출·세금은 이 보고서에서 생성하지 않았습니다."]},
        {"title": "확인할 항목", "body": ["일정·인원·단가·증빙 확인", "인물 사진 사용 동의와 건강 효능 표현 검토", "운영 완료와 계획 상태 구분"]},
        {"title": "근거 출처", "body": [_safe(p["source"], "[원본 파일명·쪽수·확인한 사람을 적어 주세요]")]},
    ]


def _slides(p: dict[str, str]) -> list[dict[str, Any]]:
    count = _ppt_count(p["length"])
    title = _safe(p["title"], "아로마 체험 수업")
    content = [
        {"title": title, "body": [_safe(p["audience"], "[참여 대상 확인 필요]"), _safe(p["program"], "[프로그램 확인 필요]")]},
        {"title": "오늘의 활동", "body": [_safe(p["goal"], "[오늘 활동의 목적을 적어 주세요]")]},
        {"title": "수업 안내", "body": ["날짜: " + _safe(p["date"], "[확인 필요]"), "시간: " + _safe(p["time"], "[확인 필요]"), "장소: " + _safe(p["place"], "[확인 필요]")]},
        {"title": "준비물", "body": [_safe(s, "[준비물 확인 필요]") for s in _lines(p["materials"], 4)] or ["[승인된 재료와 도구 목록을 적어 주세요]"]},
        {"title": "활동 순서", "body": [_safe(s, "[순서 확인 필요]") for s in _lines(p["steps"], 4)] or ["[강사가 확인한 순서를 넣어 주세요]", "배합·농도·사용량은 임의로 만들지 않았습니다."]},
        {"title": "진행 중 확인", "body": ["강사가 확인한 사용 안내를 따릅니다.", "불편하면 멈추고 강사에게 알립니다.", "대상별 주의사항과 사용법을 확인합니다."]},
        {"title": "활동 마무리", "body": ["도구와 자리를 정리합니다.", "사용·보관 안내는 승인된 자료로 확인합니다."]},
        {"title": "질문과 확인", "body": ["궁금한 점을 함께 확인합니다.", "근거: " + _safe(p["source"], "[원본 자료 확인 필요]")]},
    ]
    if count < 8:
        indexes = {4: [0, 1, 4, 7], 5: [0, 1, 3, 4, 7], 6: [0, 1, 3, 4, 5, 7], 7: [0, 1, 2, 3, 4, 5, 7]}[count]
        content = [content[i] for i in indexes]
    else:
        while len(content) < count:
            content.insert(-1, {"title": f"활동 설명 {len(content) - 6}", "body": ["[이 장에서 설명할 한 가지 내용을 적어 주세요]"]})
    # Split long body input into display lines, never reduce type to fit more text.
    for slide in content:
        lines = []
        for value in slide["body"]:
            lines.extend(textwrap.wrap(value, width=26, break_long_words=True, break_on_hyphens=False) or [value])
        slide["body"] = lines
    return content


def _promo(p: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"title": _safe(p["title"], "나만의 아로마 체험"), "body": [_safe(p["goal"], "향을 알아보고 직접 만드는 시간")]},
        {"title": "함께하는 활동", "body": [_safe(p["program"], "[체험 프로그램 확인 필요]"), "대상: " + _safe(p["audience"], "[참여 대상 확인 필요]")]},
        {"title": "참여 안내", "body": ["날짜: " + _safe(p["date"], "[확인 필요]"), "시간: " + _safe(p["time"], "[확인 필요]"), "장소: " + _safe(p["place"], "[확인 필요]"), "비용: " + _safe(p["price"], "[확인 필요]"), "문의: " + _safe(p["contact"], "[확인 필요]")]},
        {"title": "사진", "body": ["[사용 동의를 받은 실제 제품 사진을 선택해 주세요]"]},
    ]


def _document_html(kind: str, p: dict[str, str], outline: list[dict[str, Any]], qa: list[dict[str, str]]) -> str:
    esc = html.escape
    items = []
    for i, section in enumerate(outline, 1):
        body = "".join(f"<p>{esc(line)}</p>" for line in section["body"])
        if kind == "ppt" and i == 1:
            items.append(f'<section class="sheet cover"><div class="page-number">{i:02d}</div><div class="cover-copy"><span class="cover-kicker">수업 안내</span><h2>{esc(section["title"])}</h2>{body}</div><div class="cover-photo"><span>직접 찍은 작품 사진을<br>넣는 자리</span><small>사용 동의를 확인한 사진</small></div></section>')
        else:
            items.append(f'<section class="sheet"><div class="page-number">{i:02d}</div><h2>{esc(section["title"])}</h2>{body}</section>')
    css = """
    *{box-sizing:border-box}body{margin:0;background:#eeeee8;color:#203d34;font:20px/1.65 'Malgun Gothic',sans-serif}
    .header{padding:36px max(24px,calc((100% - 1000px)/2));background:#203d34;color:white}.header h1{font-size:34px;margin:0 0 12px}
    .header p{margin:0}.draft{padding:18px 24px;background:#fff2ce;color:#493a13}.content{max-width:1000px;margin:26px auto;padding:0 24px}
    .sheet{background:#fff;padding:32px 40px;margin:24px 0;border-top:5px solid #789482;break-inside:avoid;overflow-wrap:anywhere}
    h2{font-size:28px;line-height:1.35;margin:0 0 22px}p{white-space:pre-wrap;margin:12px 0}.page-number{font-size:18px;color:#64776e}
    .checks{padding:28px 32px;background:#fff}.checks li{margin:12px 0}.warning,.block{color:#7b3c12}.info{color:#3a5447}
    @media print{body{background:white;font-size:12pt}.header{padding:18px;background:white;color:#203d34}.header h1{font-size:22pt}.draft{font-size:10pt}.content{margin:0;max-width:none;padding:0}.sheet{margin:14px 0;padding:18px 24px;box-shadow:none}.checks{break-before:page}}
    """
    if kind == "ppt":
        css += """
        .sheet{aspect-ratio:16/9;min-height:510px;padding:48px 58px;display:flex;flex-direction:column;justify-content:center;border:0;margin:36px 0;position:relative}
        .sheet h2{font-size:42px;line-height:1.3;margin-bottom:28px}.sheet p{font-size:28px;line-height:1.5;margin:6px 0}.page-number{position:absolute;right:30px;bottom:20px}
        .sheet.cover{background:#e6eee6;border-top:12px solid #203d34;display:grid;grid-template-columns:1.6fr 1fr;align-items:center;gap:28px;justify-content:initial}
        .cover h2{font-size:48px;line-height:1.3}.cover-kicker{display:block;font-size:20px;color:#607467;margin-bottom:20px}.cover-photo{height:240px;border:2px dashed #819585;display:flex;flex-direction:column;justify-content:center;text-align:center;color:#5b7160;font-size:22px;line-height:1.6}.cover-photo small{font-size:16px;margin-top:18px}
        @media print{@page{size:landscape;margin:0}.header,.draft,.checks{display:none}.sheet{aspect-ratio:auto;width:100vw;height:100vh;margin:0;page-break-after:always;padding:7vw}.sheet h2{font-size:32pt}.sheet p{font-size:22pt}}
        """
    notes = "".join(f'<li class="{esc(item["level"])}">{esc(item["message"])}</li>' for item in qa)
    label = "HTML 슬라이드 초안 · PPTX 파일 아님" if kind == "ppt" else "로컬 서식 초안 · 최종 확인 필요"
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(_safe(p['title'],KINDS[kind]))}</title><style>{css}</style></head><body><header class="header"><h1>{esc(_safe(p['title'],KINDS[kind]))}</h1><p>{label}</p></header><div class="draft">확인용 초안입니다. AI 연결 없이 만들었습니다. 미확인 값은 [확인 필요]로 남겼습니다.</div><main class="content">{''.join(items)}<aside class="checks"><h2>사용 전 확인</h2><ul>{notes}</ul></aside></main></body></html>'''


def _promo_svg(p: dict[str, str]) -> str:
    esc = html.escape
    def text_lines(value: str, x: int, y: int, size: int, width: int = 23, color: str = "#23483B", max_lines: int = 4):
        lines = textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1][:-1] + "…"
        return "".join(f'<text x="{x}" y="{y+i*int(size*1.5)}" font-size="{size}" fill="{color}">{esc(line)}</text>' for i, line in enumerate(lines))
    title = _safe(p["title"], "나만의 아로마 체험")
    detail = [("날짜", p["date"]), ("시간", p["time"]), ("장소", p["place"]), ("비용", p["price"]), ("문의", p["contact"])]
    details = "".join(text_lines(f"{key}: {_safe(value, '[확인 필요]')}", 90, 940 + i*76, 28, 32, max_lines=2) for i, (key, value) in enumerate(detail))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 1000 1414" role="img" aria-label="홍보물 확인용 시안"><title>{esc(title)}</title><desc>AI 생성 이미지가 아닌 글자와 배치로 만든 SVG 시안입니다. 실제 사진과 일정은 확인 필요합니다.</desc><style>text{{font-family:'Malgun Gothic',sans-serif}} .photo{{font-size:25px;fill:#597064}}</style><rect width="1000" height="1414" fill="#F7F4E9"/><rect width="1000" height="20" fill="#23483B"/><text x="90" y="87" font-size="23" fill="#62715E">참여 안내 · 확인용 초안</text>{text_lines(title,90,176,52,17,max_lines=2)}{text_lines(_safe(p['goal'],'향을 알아보고 직접 만드는 시간'),90,355,30,29,max_lines=3)}<rect x="90" y="520" width="820" height="235" fill="#E5EBDD" stroke="#9AAE98" stroke-dasharray="10 8"/><text x="500" y="625" text-anchor="middle" class="photo">사용 동의를 받은 실제 제품 사진 자리</text><text x="500" y="675" text-anchor="middle" class="photo">사진을 만들거나 바꾸지 않았습니다</text>{text_lines(_safe(p['program'],'[체험 프로그램 확인 필요]'),90,820,32,31,max_lines=2)}{details}<text x="90" y="1385" font-size="20" fill="#6B685E">외부 전달 전 날짜·비용·문의처와 사진을 확인해 주세요</text></svg>'''


def _prompt(kind: str, p: dict[str, str], outline: list[dict[str, Any]]) -> str:
    values = "\n".join(f"{key}: {value or '[확인 필요]'}" for key, value in p.items() if value or key in ("title", "goal", "audience", "evidence", "source"))
    return (f"다음 입력과 확인된 근거로 {KINDS[kind]}를 만들어 주세요.\n"
            "먼저 목적·대상·근거·누락을 확인하고, 구조화한 초안과 미리보기를 제시해 주세요.\n"
            "입력은 사실이 확정됐다는 뜻이 아닙니다. 근거가 없는 실적·수치·후기·자격·의료 효능을 만들지 마세요.\n"
            "예전 날짜·연락처·타인 자료를 승인된 원본처럼 쓰지 마세요. 사진은 사용자가 직접 선택하고 사용 동의를 확인하세요.\n"
            "금액은 견적 기능의 계산 결과와 원본을 대조하세요. 배합·농도·대상별 사용량을 추측하지 마세요.\n"
            "보고서는 수치·증빙·출처와 계획/완료를 구분하세요. PPT는 한 장에 한 주제, 큰 글씨, 짧은 문장으로 만들고 렌더 후 확인하세요.\n"
            "홍보 이미지는 실제 제품 모양을 보존하고 한글 가독성·사진 권리·인쇄 규격을 확인하세요.\n"
            "필요한 플러그인·모델·계정이 없으면 설치나 업로드를 먼저 실행하지 말고 필요한 연결과 비용을 설명하세요.\n\n"
            f"입력 내용:\n{values}\n\n초안 구조:\n" + json.dumps(outline, ensure_ascii=False, indent=2))


def create_brief(kind: str, payload: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("보고서, 강의 PPT 준비, 홍보물 중에서 골라 주세요.")
    p = _clean_payload(payload)
    outline = {"report": _report, "ppt": _slides, "promo": _promo}[kind](p)
    qa = _qa(kind, p, outline)
    if kind == "ppt" and any(len(s["body"]) > 5 for s in outline):
        qa.insert(0, {"level": "block", "message": "슬라이드에 글자가 많습니다. 내용을 줄여 5줄 이하로 맞춰 주세요. 글씨를 작게 줄이지 않았습니다."})
    if kind == "promo" and (len(p["title"]) > 34 or len(p["goal"]) > 87 or any(len(p[k]) > 45 for k in ("date", "time", "place", "price", "contact"))):
        qa.insert(0, {"level": "block", "message": "홍보물에 긴 문구가 있습니다. SVG에서는 줄임표가 보일 수 있습니다. 문구를 줄인 뒤 다시 만들어 주세요."})
    formats = ["html", "txt", "json"] + (["svg"] if kind == "promo" else ["docx"] if kind == "report" else ["pptx"] if len(outline) == 8 else [])
    return {"kind": kind, "label": KINDS[kind], "mode": "local_template",
            "status": "draft", "ai_connected": False, "ai_used": False, "content_origin": "local_template", "payload": p,
            "outline": outline, "qa": qa, "prompt": _prompt(kind, p, outline),
            "preview_html": _document_html(kind, p, outline, qa),
            "preview_svg": _promo_svg(p) if kind == "promo" else None,
            "export_formats": formats, "blocking_count": sum(x["level"] == "block" for x in qa),
            "metrics": {"section_count": len(outline), "body_character_count": sum(len(line) for s in outline for line in s["body"]), "evidence_count": len(_lines(p["evidence"])), "slide_title_px": 42 if kind == "ppt" else None, "slide_body_px": 28 if kind == "ppt" else None}}


def export_brief(kind: str, payload: Any, format: str = "html") -> tuple[bytes, str, str]:
    """Return (file bytes, MIME type, suffix). The caller chooses a new file path."""
    brief = create_brief(kind, payload)
    if format not in brief["export_formats"]:
        raise ValueError("이 파일 형식은 아직 지원하지 않습니다. 지원되는 형식을 골라 주세요.")
    if format in ("pptx", "docx"):
        from native_exports import MIME, export_pptx, export_docx
        data = export_pptx(brief) if format == "pptx" else export_docx(brief)
        return data, MIME[format], "." + format
    if format == "html":
        return brief["preview_html"].encode("utf-8"), "text/html; charset=utf-8", ".html"
    if format == "svg":
        return brief["preview_svg"].encode("utf-8"), "image/svg+xml; charset=utf-8", ".svg"
    if format == "json":
        return json.dumps(brief, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", ".json"
    sections = "\n\n".join(s["title"] + "\n" + "\n".join(s["body"]) for s in brief["outline"])
    checks = "\n".join("- " + q["message"] for q in brief["qa"])
    value = f"{brief['label']}\n로컬 서식 초안 · AI 미연결 · 최종 확인 필요\n\n{sections}\n\n사용 전 확인\n{checks}\n\nCodex에 전달할 요청\n{brief['prompt']}\n"
    return value.encode("utf-8-sig"), "text/plain; charset=utf-8", ".txt"
