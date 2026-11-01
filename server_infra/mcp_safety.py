"""Shared output redaction. Never interprets log content as instructions."""
import json
import re
from urllib.parse import quote

SENSITIVE = re.compile(r'authorization|cookie|password|secret|credential|(?:api|access|refresh)[_-]?key|(?:access|refresh|bearer)[_-]?token|private[_-]?key|publicKey|shortId|^salt$|^hash$|^token$|^key$|^uuid$',re.I)


class Redactor:
    def __init__(self, secrets=()):
        self.secrets=sorted({s for value in secrets if isinstance(value,str) and len(value)>=6
                             for s in (value,quote(value,safe=''))},key=len,reverse=True)

    def text(self,value):
        value=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',str(value))
        if value.lstrip().startswith(('{','[')):
            try:return self.serialized(json.loads(value))
            except (ValueError,RecursionError):pass
        for secret in self.secrets:value=value.replace(secret,'[REDACTED]')
        value=re.sub(r'sk-[A-Za-z0-9_.-]{8,}|AIza[A-Za-z0-9_-]{20,}','[REDACTED]',value)
        value=re.sub(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}','[REDACTED]',value)
        value=re.sub(r'(?i)(?:vless|vmess|trojan|ss|hysteria2?|tuic)://[^\s"<>]+','[REDACTED VPN URI]',value)
        value=re.sub(r'https?://[^\s/@]+@[^\s"<>]+','[REDACTED URL]',value)
        value=re.sub(r'(?i)https://connect\.alpha-network\.org/[^\s"<>]+','[REDACTED SUBSCRIPTION]',value)
        value=re.sub(r'(?i)\b(?:bearer\s+\S+|(?:authorization|x-api-key|x-goog-api-key|cookie|set-cookie)\s*[:=][^\r\n]+)','[REDACTED]',value)
        value=re.sub(r'(?i)([?&](?:api[_-]?key|key|token|secret|signature|x-amz-signature)=)[^\s&#"<>]+',r'\1[REDACTED]',value)
        value=re.sub(r'(?<![\w])(?:[A-Za-z0-9+/]{512,}={0,2})(?![\w])','[BINARY OMITTED]',value)
        return ''.join(c for c in value if ord(c)>=32 or c in '\n\t')

    def value(self,value):
        if isinstance(value,dict):return {self.text(k):('[REDACTED]' if SENSITIVE.search(k) else self.value(v)) for k,v in value.items()}
        if isinstance(value,list):return [self.value(v) for v in value]
        if isinstance(value,str):return self.text(value)
        return value

    def serialized(self,value):return json.dumps(self.value(value),ensure_ascii=False)
