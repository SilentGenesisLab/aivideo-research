from hashlib import sha256
import json
from threading import local
import requests
from .store import read_json, write_json, now
from .parse_output import parse_object


class UnknownRequest(RuntimeError):
    pass


class Provider:
    """One HTTP submission per fingerprint. Unknown calls are never replayed."""
    def __init__(self, config, session=None):
        self.config=config
        self._session = session
        self._worker = local()

    @property
    def session(self):
        if self._session is not None:
            return self._session
        if not hasattr(self._worker, 'session'):
            self._worker.session = requests.Session()
        return self._worker.session

    def call(self, path, messages, *, retry_failed=False, response_schema=None):
        previous=read_json(path)
        if previous:
            state=previous['status']
            if state=='received':
                return self.parse(previous['response'])
            if state in ('submitting','unknown'):
                raise UnknownRequest('此前请求结果未知，禁止自动重新提交；请先核对供应商请求与费用。')
            if not retry_failed:
                raise RuntimeError('此前请求失败；明确重试失败任务后才会再发送。')
        key=self.config.api_key()
        body=dict(model=self.config.model,messages=messages,temperature=0.1,max_tokens=12000,response_format={'type':'json_object'},thinking={'type':'disabled'})
        if response_schema:
            body['response_format']=dict(type='json_schema',json_schema=dict(name='general_video_analysis',strict=True,schema=response_schema))
        encoded=json.dumps(body,ensure_ascii=False).encode('utf-8')
        if len(encoded)>=self.config.max_request_bytes:
            raise ValueError('实际请求体超出配置上限，未调用模型。')
        record=dict(status='submitting',model=self.config.model,started_at=now(),attempt=(previous or {}).get('attempt',0)+1,request_bytes=len(encoded),output_format=body['response_format']['type'])
        record['request_sha256'] = sha256(encoded).hexdigest()
        write_json(path,record)
        try:
            response=self.session.post(self.config.api_base.rstrip('/')+'/chat/completions',data=encoded,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},timeout=(20,self.config.request_timeout))
        except requests.RequestException as exc:
            record.update(status='unknown',finished_at=now(),error_type=type(exc).__name__)
            write_json(path,record)
            raise UnknownRequest('模型请求连接中断或超时，状态已记为 unknown；未自动重试。') from None
        record['http_status']=response.status_code
        record['request_id']=response.headers.get('x-request-id')
        if not response.ok:
            record.update(status='unknown' if response.status_code>=500 else 'failed',finished_at=now())
            # Persist a bounded provider error, redact known credential if echoed.
            record['error']=response.text[:1500].replace(key,'[REDACTED]')
            write_json(path,record)
            if record['status']=='unknown':
                raise UnknownRequest(f'模型 HTTP {response.status_code}，是否受理未知，未自动重试。')
            raise RuntimeError(f'模型 HTTP {response.status_code}；错误详情已保存在调用记录。')
        try:
            data=response.json()
        except ValueError:
            record.update(status='unknown',finished_at=now(),error='HTTP success but non-JSON response')
            write_json(path,record)
            raise UnknownRequest('模型返回非 JSON 协议响应，已保存 unknown，未重试。') from None
        record.update(status='received',finished_at=now(),response=data,usage=data.get('usage',{}),response_id=data.get('id'))
        write_json(path,record)
        return self.parse(data)

    @staticmethod
    def parse(data):
        choices=data.get('choices') or []
        if not choices or choices[0].get('finish_reason')!='stop':
            raise ValueError('模型输出未正常结束；原始响应已保存，不能视为分析完成。')
        text=choices[0].get('message',{}).get('content')
        if not isinstance(text,str):
            raise ValueError('模型未返回文本内容。')
        return parse_object(text)
