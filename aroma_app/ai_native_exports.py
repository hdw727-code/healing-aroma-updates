"""Staging only: full reviewed AI-response Office exports, stdlib/template fill.

No model, credential, upload, app mutation or USB file is used by this module.
The caller must obtain response content from a completed, authorized saved job.
"""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import hashlib
import html
import json
import re
import textwrap
import xml.etree.ElementTree as ET

from native_exports import (
    NS, _read_template, _xml, _replace_shape, _note, _metadata, _pack,
    _word_paragraph, validate_native,
)

FIELDS = {'title','summary','sections','missing_information','cautions'}
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable'


def _string(value, label, limit=2000):
    if not isinstance(value,str) or not value.strip():
        raise ValueError(f'{label}을 글자로 작성해 주세요.')
    if len(value)>limit:
        raise ValueError(f'{label}이 너무 깁니다. 원문을 유지한 채 검토 미리보기에서 수정해 주세요.')
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]',value):
        raise ValueError(f'{label}에 문서에 넣을 수 없는 제어 문자가 있습니다.')
    return value


def _response(value):
    if not isinstance(value,dict) or set(value)!=FIELDS:
        raise ValueError('AI 결과의 제목·요약·섹션·확인사항·주의문구 전체 구조를 확인해 주세요. 알 수 없는 항목을 버리지 않았습니다.')
    _string(value['title'],'제목')
    _string(value['summary'],'요약')
    if not isinstance(value['sections'],list) or len(value['sections'])!=3:
        raise ValueError('실제 문서는 현재 3개 섹션의 검토한 결과를 지원합니다. 원문은 유지됩니다.')
    for i,section in enumerate(value['sections'],1):
        if not isinstance(section,dict) or set(section)!={'heading','bullets'}:
            raise ValueError(f'{i}번째 섹션의 제목과 문장 구조를 확인해 주세요.')
        _string(section['heading'],f'{i}번째 섹션 제목')
        if not isinstance(section['bullets'],list) or not 1<=len(section['bullets'])<=20:
            raise ValueError(f'{i}번째 섹션 문장 목록을 확인해 주세요.')
        for j,line in enumerate(section['bullets'],1):
            _string(line,f'{i}번째 섹션 {j}번째 문장')
    for name,label in [('missing_information','확인사항'),('cautions','주의문구')]:
        if not isinstance(value[name],list) or len(value[name])>20:
            raise ValueError(f'{label} 목록을 확인해 주세요.')
        for line in value[name]:
            _string(line,label)
    return deepcopy(value)


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def _wrap(value,width=26):
    lines=[]
    for raw in value.splitlines():
        lines.extend(textwrap.wrap(raw,width=width,break_long_words=True,break_on_hyphens=False))
    if re.sub(r'\s+','',value)!=re.sub(r'\s+','',''.join(lines)):
        raise ValueError('줄바꿈 중 원문 검사가 실패했습니다. 내용을 생략하지 않았습니다.')
    return lines


def prepare_ai_export(kind,response,*,original_response=None,source_job_id=None):
    """Return a complete review plan. Layout limits block instead of truncating."""
    if kind not in ('ppt','report'):
        raise ValueError('AI 문서 형식을 확인해 주세요.')
    if source_job_id is not None and (not isinstance(source_job_id,str) or not re.fullmatch(r'[0-9a-f]{32}',source_job_id)):
        raise ValueError('원본 작업 ID는 서버가 부여한 익명 ID로 확인해 주세요.')
    reviewed=_response(response)
    original=_response(original_response if original_response is not None else response)
    plan={'kind':kind,'status':'review_required','response':reviewed,
          'original_response':original,'original_response_sha256':_digest(original),
          'reviewed_response_sha256':_digest(reviewed),'manual_edits':reviewed!=original,
          'source_job_id':source_job_id,
          'ai_used':True,'content_origin':'ai_generated','blocking':[],
          'font_policy':{'ppt_title_pt':36,'ppt_body_pt':24.75,'docx_body_pt':12},
          'automatic_font_shrinking':False,'automatic_truncation':False}
    if kind=='ppt':
        flattened=[(s['heading'],b) for s in reviewed['sections'] for b in s['bullets']]
        if len(flattened)!=8:
            plan['blocking'].append('현재 전체 AI PPTX는 문장 8개를 각각 1장에 배치합니다. 문장 수를 검토 미리보기에서 맞춰 주세요. 원문은 보존됩니다.')
        slides=[]
        for i,(heading,bullet) in enumerate(flattened,1):
            lines=_wrap(bullet)
            if len(_wrap(heading,23))>1:
                plan['blocking'].append(f'{i}장 섹션 제목이 한 줄 범위를 넘습니다. 검토 미리보기에서 수정해 주세요.')
            if len(lines)>4:
                plan['blocking'].append(f'{i}장 AI 문장이 큰 글씨 4줄 범위를 넘습니다. 미리보기에서 문장을 수정해 주세요. 원문은 보존됩니다.')
            slides.append({'number':i,'title':heading,'bullet':bullet,'body_lines':lines,'extras':[],'extra_lines':[]})
        # All summary, missing-information and caution text is visible, in a
        # separate lower text area. Never move overflow only into hidden notes.
        extras=[('요약',reviewed['summary'])]
        extras += [('확인사항',line) for line in reviewed['missing_information']]
        extras += [('주의문구',line) for line in reviewed['cautions']]
        preferred=[0,2,6,3,7,1,4,5]
        for category,text in extras:
            lines=_wrap(category+': '+text)
            placed=False
            for index in preferred:
                if index<len(slides) and len(slides[index]['extra_lines'])+len(lines)<=3:
                    slides[index]['extras'].append({'category':category,'text':text})
                    slides[index]['extra_lines'].extend(lines)
                    placed=True
                    break
            if not placed:
                plan['blocking'].append(f'{category} 문구를 8장 안의 큰 글씨 영역에 모두 담을 수 없습니다. 검토 미리보기에서 수정해 주세요. 숨기거나 생략하지 않았습니다.')
        plan['slides']=slides
        plan['slide_count']=len(slides)
    else:
        if len(reviewed['title'])>40:
            plan['blocking'].append('Word 제목은 고정 큰 글씨에서 40자 이하로 검토해 주세요. 원문은 보존됩니다.')
        if any(len(s['heading'])>40 for s in reviewed['sections']):
            plan['blocking'].append('Word 섹션 제목은 고정 큰 글씨에서 40자 이하로 검토해 주세요. 원문은 보존됩니다.')
        pages=[]
        for i,section in enumerate(reviewed['sections'],1):
            paragraphs=[]
            if i==1:
                paragraphs += [('title',reviewed['title']),('subtitle','AI 구성안 · 검토용 초안'),('normal',reviewed['summary'])]
            paragraphs += [('heading',section['heading'])]
            paragraphs += [('normal',line) for line in section['bullets']]
            if i==3:
                for key,label in [('missing_information','확인사항'),('cautions','주의문구')]:
                    paragraphs.append(('heading',label))
                    paragraphs += [('normal',line) for line in reviewed[key]]
            # Conservative visual line budget at fixed 12pt: no text is cut.
            budget=sum(max(1,len(_wrap(text,38)))+(2 if style in ('title','heading','subtitle') else 1) for style,text in paragraphs)
            if budget>31:
                plan['blocking'].append(f'{i}쪽 AI 내용이 고정 12pt 3쪽 서식의 분량을 넘습니다. 검토 미리보기에서 수정해 주세요. 원문은 보존됩니다.')
            pages.append({'number':i,'paragraphs':[{'style':s,'text':t}for s,t in paragraphs],'visual_line_budget':budget})
        plan['pages']=pages
        plan['page_count']=3
    if plan['blocking']:
        plan['status']='revision_required'
    return plan


def render_ai_preview(plan):
    """Review HTML lists every item and each blocking issue; no font shrinking."""
    esc=html.escape
    content=[]
    if plan['kind']=='ppt':
        for slide in plan['slides']:
            extra=''.join(f'<p class="extra">{esc(e["category"])}: {esc(e["text"])}</p>' for e in slide['extras'])
            content.append(f'<article><h2>{slide["number"]}장 · {esc(slide["title"])}</h2><p>{esc(slide["bullet"])}</p>{extra}</article>')
    else:
        for page in plan['pages']:
            ps=''.join(f'<p class="{esc(p["style"])}">{esc(p["text"])}</p>' for p in page['paragraphs'])
            content.append(f'<article><h2>{page["number"]}쪽</h2>{ps}</article>')
    notices=''.join(f'<li>{esc(m)}</li>'for m in plan['blocking'])
    source=json.dumps(plan['original_response'],ensure_ascii=False,indent=2)
    current=json.dumps(plan['response'],ensure_ascii=False,indent=2)
    edit_label='사용자 검토 수정 있음 · AI 원문 별도 보존' if plan['manual_edits'] else 'AI 원문과 동일 · 자동 요약 또는 생략 없음'
    return '<!doctype html><html lang="ko"><meta charset="utf-8"><title>AI 전체 내용 검토</title><style>body{font-family:"Malgun Gothic",sans-serif;margin:32px;color:#183d31;background:#faf9f4}article{background:white;padding:28px;margin:24px 0;border:1px solid #c5d5ca}p{font-size:24px;line-height:1.5;white-space:pre-wrap}.extra{color:#655222}.heading,.title{font-weight:bold}pre{white-space:pre-wrap;word-break:break-word}li{color:#8b301d}</style><h1>AI 전체 내용 검토 · 저장 전 확인 필요</h1><p>'+esc(edit_label)+'</p><p>원문을 모두 보존합니다. 글씨를 작게 줄이거나 문장을 조용히 생략하지 않습니다. 실제 Office 배치는 이 HTML과 다릅니다.</p><ul>'+notices+'</ul>'+''.join(content)+'<details open><summary>문서에 넣을 전체 초안 · 분량 초과 내용도 표시</summary><pre>'+esc(current)+'</pre></details><details><summary>보존된 AI 원문</summary><pre>'+esc(source)+'</pre></details></html>'


def _geometry(shape,x,y,cx,cy):
    transform=shape.find('p:spPr/a:xfrm',NS)
    transform.find('a:off',NS).attrib.update({'x':str(x),'y':str(y)})
    transform.find('a:ext',NS).attrib.update({'cx':str(cx),'cy':str(cy)})
    props=shape.find('p:txBody/a:bodyPr',NS)
    for node in list(props):
        if node.tag.rsplit('}',1)[-1] in ('normAutofit','spAutoFit','noAutofit'):
            props.remove(node)
    ET.SubElement(props,'{'+NS['a']+'}noAutofit')


def export_ai_response(kind,response,*,reviewed=False,original_response=None,source_job_id=None,examples:Path|None=None):
    """Require review, then export every field; return (bytes, complete plan)."""
    if reviewed is not True:
        raise ValueError('AI 전체 내용과 확인사항·주의문구를 미리보기에서 검토한 뒤 저장해 주세요.')
    plan=prepare_ai_export(kind,response,original_response=original_response,source_job_id=source_job_id)
    if plan['blocking']:
        raise ValueError('\n'.join(plan['blocking']))
    provenance=('content_origin=ai_generated; ai_used=true; status=review_required; '
                f'original_sha256={plan["original_response_sha256"]}; '
                f'reviewed_sha256={plan["reviewed_response_sha256"]}; '
                f'source_job_id={plan["source_job_id"] or "not_supplied"}; '
                f'manual_edits={str(plan["manual_edits"]).lower()}')
    # Export only the reviewed text. The immutable original remains in the
    # local job store, never in hidden Office notes/properties/custom XML.
    reviewed_json=json.dumps(plan['response'],ensure_ascii=False,indent=2)
    if kind=='ppt':
        parts=_read_template('lecture_example.pptx',examples)
        sample=parts['ppt/slides/slide6.xml']
        for slide in plan['slides']:
            number=slide['number']
            root=_xml(sample)
            shapes=root.findall('.//p:sp',NS)
            title,main,extra,counter=shapes[0],shapes[1],shapes[2],shapes[-1]
            _replace_shape(title,slide['title'])
            _replace_shape(main,'\n'.join(slide['body_lines']))
            _replace_shape(extra,'\n'.join(slide['extra_lines']))
            _replace_shape(counter,str(number))
            _geometry(main,762000,1714500,10477500,2095500)
            _geometry(extra,762000,4191000,10477500,1600200)
            tree=root.find('p:cSld/p:spTree',NS)
            for shape in shapes[3:-1]:
                tree.remove(shape)
            parts[f'ppt/slides/slide{number}.xml']=ET.tostring(root,encoding='utf-8',xml_declaration=True)
            _note(parts,number,'\n'.join([slide['title'],slide['bullet'],*[e['text']for e in slide['extras']],provenance,'검토한 AI 내용:',reviewed_json]))
        _metadata(parts,provenance)
        data=_pack(parts)
        validate_native(data,'pptx')
    else:
        parts=_read_template('report_example.docx',examples)
        root=_xml(parts['word/document.xml'])
        for node in root.iter():
            node.attrib.pop(MC,None)
        body=root.find('w:body',NS)
        old=body.findall('w:p',NS)
        samples={'title':old[0],'subtitle':old[1],'normal':old[2],'heading':old[3]}
        section=deepcopy(body.find('w:sectPr',NS))
        for node in list(body):
            body.remove(node)
        for page in plan['pages']:
            for index,p in enumerate(page['paragraphs']):
                paragraph=_word_paragraph(samples[p['style']],p['text'],page_break=page['number']>1 and index==0)
                # Fix readable font size. Never scale down content to fit.
                for run in paragraph.findall('w:r',NS):
                    rpr=run.find('w:rPr',NS)
                    if rpr is None:
                        rpr=ET.Element('{'+NS['w']+'}rPr'); run.insert(0,rpr)
                    size=ET.SubElement(rpr,'{'+NS['w']+'}sz')
                    size.set('{'+NS['w']+'}val','24' if p['style']=='normal' else '28' if p['style']=='subtitle' else '36' if p['style']=='heading' else '44')
                body.append(paragraph)
        if section is not None:
            body.append(section)
        parts['word/document.xml']=ET.tostring(root,encoding='utf-8',xml_declaration=True)
        for name in list(parts):
            if re.fullmatch(r'word/(?:header|footer)\d+\.xml',name):
                side=_xml(parts[name])
                for node in side.iter():
                    node.attrib.pop(MC,None)
                texts=side.findall('.//w:t',NS)
                for i,node in enumerate(texts):
                    node.text=('AI 구성안 · 검토용 초안' if '/header' in name else '검토용 초안 ') if i==0 else ''
                parts[name]=ET.tostring(side,encoding='utf-8',xml_declaration=True)
        # Only reviewed structured text may be embedded. Removed original
        # private content must not reappear in hidden properties.
        custom_ns='http://schemas.openxmlformats.org/officeDocument/2006/custom-properties'
        value_ns='http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'
        ET.register_namespace('vt',value_ns)
        custom=ET.Element('{'+custom_ns+'}Properties')
        prop=ET.SubElement(custom,'{'+custom_ns+'}property',{'fmtid':'{D5CDD505-2E9C-101B-9397-08002B2CF9AE}','pid':'2','name':'AIReviewedResponse'})
        ET.SubElement(prop,'{'+value_ns+'}lpwstr').text=reviewed_json
        parts['docProps/custom.xml']=ET.tostring(custom,encoding='utf-8',xml_declaration=True)
        ctypes=_xml(parts['[Content_Types].xml'])
        ET.SubElement(ctypes,'{http://schemas.openxmlformats.org/package/2006/content-types}Override',{'PartName':'/docProps/custom.xml','ContentType':'application/vnd.openxmlformats-officedocument.custom-properties+xml'})
        parts['[Content_Types].xml']=ET.tostring(ctypes,encoding='utf-8',xml_declaration=True)
        rels=_xml(parts['_rels/.rels'])
        ET.SubElement(rels,'{http://schemas.openxmlformats.org/package/2006/relationships}Relationship',{'Id':'rIdAIReviewed','Type':'http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties','Target':'docProps/custom.xml'})
        parts['_rels/.rels']=ET.tostring(rels,encoding='utf-8',xml_declaration=True)
        _metadata(parts,provenance)
        data=_pack(parts)
        validate_native(data,'docx')
    plan['status']='reviewed_native_export_created'
    return data,plan
