"""Portable editable Office exports using our own reviewed templates.

Only Python's standard library is used. No original USB document, font, Office
binary, Codex dependency, model, credential or network connection is required.
This is application template filling, not a general presentation authoring SDK.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import json
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
for prefix, uri in {**NS, "r":"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                    "dc":"http://purl.org/dc/elements/1.1/", "dcterms":"http://purl.org/dc/terms/",
                    "cp":"http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
                    "xsi":"http://www.w3.org/2001/XMLSchema-instance"}.items():
    ET.register_namespace(prefix, uri)
EXAMPLES = Path(__file__).resolve().parent / "examples"
RISK = re.compile(r"치료|치유|완치|예방|항암|항염|무독성|부작용\s*없|태아|면역력|호르몬|혈압|질환|질병|아토피|불면증|우울증|통증\s*(완화|개선)|천연.{0,12}안전", re.I)
MIME = {"pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def _xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("외부 XML 참조는 지원하지 않습니다.")
    return ET.fromstring(data)


def _read_template(name: str, examples: Path | None = None) -> dict[str, bytes]:
    source = (examples or EXAMPLES) / name
    if not source.is_file():
        raise ValueError("문서 템플릿을 찾을 수 없습니다. 앱의 examples 폴더를 확인해 주세요.")
    with zipfile.ZipFile(source) as archive:
        if archive.testzip() or sum(i.file_size for i in archive.infolist()) > 8_000_000:
            raise ValueError("문서 템플릿이 손상되었거나 너무 큽니다.")
        parts = {i.filename: archive.read(i) for i in archive.infolist()}
    _safe_parts(parts)
    return parts


def _safe_parts(parts: dict[str, bytes]) -> None:
    for name, data in parts.items():
        if name.startswith("/") or ".." in name.split("/") or "\\" in name:
            raise ValueError("안전하지 않은 문서 파일 경로입니다.")
        if "vbaproject" in name.lower() or "/embeddings/" in name.lower():
            raise ValueError("매크로와 삽입 실행 파일은 지원하지 않습니다.")
        if name == "[Content_Types].xml" and re.search(rb"macroEnabled|vbaProject",data,re.I):
            raise ValueError("매크로 문서 형식은 지원하지 않습니다.")
        if name.endswith(".rels"):
            for rel in _xml(data):
                if rel.get("TargetMode") == "External":
                    raise ValueError("외부 문서 링크는 지원하지 않습니다.")
        if name.endswith(".xml"):
            _xml(data)


def _text(value: object, max_chars: int, label: str, *, fallback: str = "[확인 필요]") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}은 글자로 입력해 주세요.")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
    if len(value) > max_chars:
        raise ValueError(f"{label}이 깁니다. 실제 문서에서는 {max_chars}자 이하로 줄여 주세요. 원문은 작업 내용에 보관됩니다.")
    if RISK.search(value):
        raise ValueError("건강·의료 효능 문구를 확인한 뒤 실제 문서를 만들어 주세요.")
    return value or fallback


def _provenance(brief: dict) -> str:
    ai = brief.get("ai_used", False)
    origin = brief.get("content_origin", "local_template")
    if ai is not True and ai is not False:
        raise ValueError("생성 출처를 확인해 주세요.")
    if (ai and origin != "ai_generated") or (not ai and origin != "local_template"):
        raise ValueError("AI 사용 여부와 생성 출처가 다릅니다.")
    return f"content_origin={origin}; ai_used={str(ai).lower()}; status=review_required"


def _ensure_brief(brief: dict) -> None:
    if not isinstance(brief, dict) or not isinstance(brief.get("outline"), list):
        raise ValueError("문서 초안 구조를 확인해 주세요.")
    if any(q.get("level") == "block" for q in brief.get("qa", [])):
        raise ValueError("사용 전 확인에서 막힌 항목을 해결한 뒤 실제 문서를 만들어 주세요.")
    _provenance(brief)


def _metadata(parts: dict[str, bytes], provenance: str) -> None:
    core = parts.get("docProps/core.xml")
    if not core:
        return
    root = _xml(core)
    for node in root:
        local = node.tag.rsplit("}", 1)[-1]
        if local in ("creator", "lastModifiedBy"):
            node.text = "아로마 업무 도우미"
        elif local in ("title", "subject", "description", "keywords"):
            node.text = provenance if local in ("description", "keywords") else "검토용 업무 문서"
        elif local in ("created", "modified"):
            node.text = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    # Core metadata can omit description/keywords in the clean source template.
    if not any(node.tag.endswith("}description") for node in root):
        ET.SubElement(root, "{http://purl.org/dc/elements/1.1/}description").text = provenance
    parts["docProps/core.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _pack(parts: dict[str, bytes]) -> bytes:
    _safe_parts(parts)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return output.getvalue()


def _replace_shape(shape: ET.Element, text: str) -> None:
    body = shape.find("p:txBody", NS)
    if body is None:
        raise ValueError("편집 가능한 텍스트 템플릿이 아닙니다.")
    paragraphs = body.findall("a:p", NS)
    sample = deepcopy(paragraphs[0])
    properties = sample.find("a:pPr", NS)
    run = sample.find("a:r", NS)
    if run is None:
        raise ValueError("템플릿의 글자 스타일을 찾을 수 없습니다.")
    run_properties = deepcopy(run.find("a:rPr", NS))
    for para in paragraphs:
        body.remove(para)
    for line in text.split("\n"):
        paragraph = ET.SubElement(body, "{" + NS["a"] + "}p")
        if properties is not None:
            paragraph.append(deepcopy(properties))
        native_run = ET.SubElement(paragraph, "{" + NS["a"] + "}r")
        if run_properties is not None:
            native_run.append(deepcopy(run_properties))
        ET.SubElement(native_run, "{" + NS["a"] + "}t").text = line


def _note(parts: dict[str, bytes], number: int, value: str) -> None:
    name = f"ppt/notesSlides/notesSlide{number}.xml"
    root = _xml(parts[name])
    shapes = root.findall(".//p:sp", NS)
    for shape in shapes:
        texts = shape.findall(".//a:t", NS)
        if shape.find("p:nvSpPr/p:nvPr/p:ph[@type='body']", NS) is not None or (texts and any(len(t.text or "") > 1 for t in texts)):
            _replace_shape(shape, value)
    parts[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)


def export_pptx(brief: dict, *, examples: Path | None = None) -> bytes:
    """Fill the editable eight-slide local template; reject text that cannot fit."""
    _ensure_brief(brief)
    sections = brief["outline"]
    for key,label in [("steps","활동 순서"),("materials","준비물")]:
        raw = brief.get("payload",{}).get(key,"")
        if not isinstance(raw,str) or len([line for line in raw.splitlines() if line.strip()]) > 4:
            raise ValueError(f"실제 PPTX의 {label}은 4줄 이하로 줄여 주세요. 원문은 작업 내용에 보관됩니다.")
    if len(sections) != 8:
        raise ValueError("실제 PPTX는 현재 8장으로 만들 수 있습니다. 장 수를 8장으로 바꿔 주세요. HTML은 다른 장 수도 지원합니다.")
    parts = _read_template("lecture_example.pptx", examples)
    # Slide four originally has three body rows; reuse our four-row text layout.
    parts["ppt/slides/slide4.xml"] = parts["ppt/slides/slide6.xml"]
    for number, section in enumerate(sections, 1):
        title = _text(section.get("title", ""), 28 if number == 1 else 23, f"{number}장 제목")
        lines = section.get("body")
        if not isinstance(lines, list) or len(lines) > (2 if number == 1 else 4):
            raise ValueError(f"{number}장 본문은 {'2' if number == 1 else '4'}줄 이하로 줄여 주세요. 원문은 작업 내용에 보관됩니다.")
        limit = 26
        lines = [_text(line, limit, f"{number}장 본문") for line in lines]
        root = _xml(parts[f"ppt/slides/slide{number}.xml"])
        shapes = root.findall(".//p:sp", NS)
        if number > 1:
            _replace_shape(shapes[-1], str(number))
        # The checked template deliberately orders its title before all body rows.
        _replace_shape(shapes[0], title if number != 1 else "\n".join([title[:14], title[14:]]).strip())
        if number == 1:
            _replace_shape(shapes[1], "\n".join(lines) or "[대상·프로그램 확인 필요]")
            extent = shapes[1].find("p:spPr/a:xfrm/a:ext", NS)
            if extent is not None:
                extent.set("cy", str(1257300))  # 132px, enough for two 32px rows.
            _replace_shape(shapes[2], "로컬 서식 초안 · 사진 미삽입")
        else:
            body_shapes = shapes[1:-1]
            if number in (3, 5):
                _replace_shape(body_shapes[-1], "")
                body_shapes = body_shapes[:-1]  # Blank unused photo row; no photo is copied.
            for i, shape in enumerate(body_shapes):
                extent = shape.find("p:spPr/a:xfrm/a:ext", NS)
                if extent is not None:
                    extent.set("cx", "10477500")  # 1100px, plain text without photos.
                _replace_shape(shape, lines[i] if i < len(lines) else "")
            if number == 2:
                # A wrapped goal remains one paragraph, with adjacent lines.
                _replace_shape(body_shapes[0], "\n".join(lines))
                body_shapes[0].find("p:spPr/a:xfrm/a:ext",NS).set("cy","2286000")
                for shape in body_shapes[1:]:
                    _replace_shape(shape, "")
        parts[f"ppt/slides/slide{number}.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        source = _text(brief.get("payload", {}).get("source", ""), 250, "근거 출처")
        _note(parts, number, "\n".join([title, *lines, "근거: " + source, "실습 방법·양·효능·사진·일정은 확인한 내용만 사용합니다.", _provenance(brief)]))
    _metadata(parts, _provenance(brief))
    data = _pack(parts)
    validate_native(data, "pptx")
    return data


def _word_paragraph(sample: ET.Element, text: str, *, page_break: bool = False) -> ET.Element:
    para = deepcopy(sample)
    for child in list(para):
        if child.tag != "{" + NS["w"] + "}pPr":
            para.remove(child)
    if page_break:
        props = para.find("w:pPr", NS)
        if props is None:
            props = ET.SubElement(para, "{" + NS["w"] + "}pPr")
        for old in props.findall("w:pageBreakBefore", NS):
            props.remove(old)
        ET.SubElement(props, "{" + NS["w"] + "}pageBreakBefore")
    run = ET.SubElement(para, "{" + NS["w"] + "}r")
    for i, line in enumerate(text.split("\n")):
        if i:
            ET.SubElement(run, "{" + NS["w"] + "}br")
        node = ET.SubElement(run, "{" + NS["w"] + "}t")
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        node.text = line
    return para


def export_docx(brief: dict, *, examples: Path | None = None) -> bytes:
    """A bounded three-page draft with native paragraphs and copied style policy."""
    _ensure_brief(brief)
    p = brief.get("payload", {})
    if brief.get("ai_used") is True:
        p = brief.get("generated_payload")
        if not isinstance(p,dict):
            raise ValueError("AI 보고서의 검토한 항목별 내용을 확인해 주세요. AI 구성안만으로 Word 본문을 생성했다고 표시하지 않습니다.")
    title = _text(p.get("title", ""), 30, "문서 제목", fallback="아로마 체험 운영 계획서")
    values = {k: _text(p.get(k, ""), n, label) for k, n, label in [
        ("goal", 180, "작성 목적"), ("audience", 80, "참여 대상"), ("program", 80, "프로그램"),
        ("date", 60, "날짜"), ("time", 60, "시간"), ("place", 80, "장소"), ("source", 180, "근거 출처")]}
    groups = {}
    for key, count, label in [("steps",4,"진행 순서"),("materials",5,"준비물"),("evidence",4,"입력한 근거")]:
        raw = p.get(key, "")
        if not isinstance(raw,str):
            raise ValueError(f"{label}은 글자로 입력해 주세요.")
        lines = [s.strip() for s in raw.splitlines() if s.strip()]
        if len(lines) > count:
            raise ValueError(f"실제 DOCX의 {label}은 {count}줄 이하로 줄여 주세요. 원문은 작업 내용에 보관됩니다.")
        groups[key] = [_text(line, 90, label) for line in lines] or ["[확인 필요]"]
    parts = _read_template("report_example.docx", examples)
    root = _xml(parts["word/document.xml"])
    # ElementTree removes unused namespace declarations. Remove the corresponding
    # compatibility prefix list rather than leaving undeclared prefixes in Word.
    for node in root.iter():
        node.attrib.pop("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable", None)
    body = root.find("w:body", NS)
    old_paragraphs = body.findall("w:p", NS)
    title_sample, subtitle_sample, normal_sample = old_paragraphs[:3]
    heading_sample = old_paragraphs[3]
    section = deepcopy(body.find("w:sectPr", NS))
    for node in list(body):
        body.remove(node)
    def paragraph(text, kind="normal", page_break=False):
        sample = {"normal":normal_sample,"title":title_sample,"subtitle":subtitle_sample,"heading":heading_sample}[kind]
        body.append(_word_paragraph(sample, text, page_break=page_break))
    paragraph(title,"title")
    paragraph("운영 계획 검토용 초안","subtitle")
    paragraph(values["goal"])
    paragraph("대상과 프로그램","heading")
    paragraph("대상: " + values["audience"])
    paragraph("프로그램: " + values["program"])
    paragraph("운영 일정","heading")
    paragraph("날짜: " + values["date"])
    paragraph("시간: " + values["time"])
    paragraph("장소: " + values["place"])
    paragraph("실제 일정과 활동을 확인한 뒤 제출합니다.")
    paragraph("진행과 준비","heading",True)
    paragraph("진행 순서","heading")
    for line in groups["steps"]:
        paragraph(line)
    paragraph("준비물","heading")
    for line in groups["materials"]:
        paragraph(line)
    paragraph("재료·수량·방법은 강사가 확인한 안내로 준비합니다. 시간 배분과 준비·정리 범위도 확인합니다.")
    paragraph("근거와 최종 확인","heading",True)
    paragraph("입력한 근거","heading")
    for line in groups["evidence"]:
        paragraph(line)
    paragraph("근거 출처","heading")
    paragraph(values["source"])
    paragraph("비용과 정산","heading")
    paragraph("별도로 확인한 견적서를 첨부합니다. 이 문서는 임의 단가·금액·세금을 만들지 않습니다.")
    paragraph("사용 전 확인","heading")
    paragraph("일정·활동·대상과 근거를 확인합니다. 제품 안내와 사진 사용 동의도 별도로 확인합니다.")
    if section is not None:
        body.append(section)
    parts["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    for name in list(parts):
        if re.fullmatch(r"word/(?:header|footer)\d+\.xml",name):
            side = _xml(parts[name])
            for node in side.iter():
                node.attrib.pop("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable",None)
            texts = side.findall(".//w:t",NS)
            if texts:
                texts[0].text = "아로마 업무 도우미" if "/header" in name else "검토용 초안 "
                for text in texts[1:]:
                    text.text = ""
            parts[name] = ET.tostring(side,encoding="utf-8",xml_declaration=True)
    _metadata(parts, _provenance(brief))
    data = _pack(parts)
    validate_native(data,"docx")
    return data


def validate_native(data: bytes, kind: str) -> dict:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        if archive.testzip():
            raise ValueError("만든 문서의 파일 검사가 실패했습니다.")
        parts = {name:archive.read(name) for name in archive.namelist()}
    _safe_parts(parts)
    if kind == "pptx":
        slide_names = [n for n in parts if re.fullmatch(r"ppt/slides/slide\d+\.xml",n)]
        if len(slide_names) != 8:
            raise ValueError("슬라이드 수 검사가 실패했습니다.")
        count = 0
        for name in slide_names:
            root = _xml(parts[name])
            count += len(root.findall(".//p:sp",NS))
            for transform in root.findall(".//a:xfrm",NS):
                off, ext = transform.find("a:off",NS),transform.find("a:ext",NS)
                if off is not None and ext is not None:
                    x,y,cx,cy = map(int,[off.get("x"),off.get("y"),ext.get("cx"),ext.get("cy")])
                    if min(x,y,cx,cy)<0 or x+cx>12192000 or y+cy>6858000:
                        raise ValueError("슬라이드 배치 범위 검사가 실패했습니다.")
        return {"kind":kind,"slide_count":8,"editable_shapes":count,"external_links":0,"macros":0,"zip_crc":"passed"}
    if kind == "docx":
        root = _xml(parts["word/document.xml"])
        return {"kind":kind,"paragraph_count":len(root.findall("w:body/w:p",NS)),"explicit_page_breaks":len([n for n in root.findall(".//w:pageBreakBefore",NS) if n.get("{"+NS["w"]+"}val") != "0"]),"external_links":0,"macros":0,"zip_crc":"passed"}
    raise ValueError("문서 형식을 확인해 주세요.")


def write_new(path: Path, data: bytes) -> None:
    """Opt-in helper for controlled export folders. Existing files are preserved."""
    with Path(path).open("xb") as handle:
        handle.write(data)
