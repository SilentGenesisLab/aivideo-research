"""Lossless repairs for observed delimiter defects, never semantic completion."""
import json
import re


def parse_object(text):
    repairs=[]
    for attempt in range(4):
        try:
            result=json.loads(text)
            if not isinstance(result,dict):
                raise ValueError('模型返回的 JSON 必须是对象。')
            if repairs:
                result['_parse_repair']=dict(steps=repairs,semantic_values_changed=False,raw_response_preserved=True)
            return result
        except json.JSONDecodeError as exc:
            if attempt==3:
                raise
            pos=exc.pos
            if exc.msg=='Extra data' and re.match(r',\s*"segments"\s*:',text[pos:]):
                prefix=json.loads(text[:pos])
                if set(prefix)!={'classification','summary'} or text[pos-1]!='}':
                    raise
                text=text[:pos-1]+text[pos:]
                repairs.append(dict(kind='remove_premature_root_closure',character_offset=pos-1))
                continue
            if exc.msg=="Expecting ',' delimiter" and text[:pos].rstrip().endswith(']') and re.match(r'\{\s*"start"\s*:',text[pos:]):
                text=text[:pos]+'},'+text[pos:]
                repairs.append(dict(kind='insert_segment_object_closure_and_separator',character_offset=pos))
                continue
            if exc.msg=="Expecting ',' delimiter" and text[pos:pos+1]==']':
                matches=list(re.finditer(r'"evidence"\s*:\s*(\{)',text[:pos]))
                if matches:
                    at=matches[-1].start(1)
                    value,end=json.JSONDecoder().raw_decode(text[at:])
                    if isinstance(value,dict) and set(value) in ({'modality','observation'},{'start','end','observation'}) and not text[at+end:pos].strip():
                        text=text[:at]+'['+text[at:]
                        repairs.append(dict(kind='insert_missing_evidence_array_opening',character_offset=at))
                        continue
            raise
