"""Preserve the latest stats manifest during a targeted V2 betting publication."""
import hashlib
import json
from pathlib import Path
import sys

def merged(source,latest):
    result = json.loads(json.dumps(latest))
    new = next(v for v in source['artifacts'] if v['id']=='app_ready_betting_bundle')
    old = next(v for v in result['artifacts'] if v['id']=='app_ready_betting_bundle')
    if old['sha256'] not in (source['metadata']['bettingV2DirectBaseSha256'],new['sha256']):
        raise ValueError('Betting base changed during publication; rebuild against current bundle')
    old.clear();old.update(new)
    result.setdefault('metadata',{}).update({k:v for k,v in source['metadata'].items() if k.startswith('bettingV2Direct')})
    result['generatedAt'] = max(latest['generatedAt'],source['generatedAt'])
    result['contentVersion'] = hashlib.sha256((latest['contentVersion']+'|V2|'+new['sha256']).encode()).hexdigest()
    return result

if __name__=='__main__':
    src,target = map(Path,sys.argv[1:])
    target.write_text(json.dumps(merged(json.loads(src.read_text()),json.loads(target.read_text())),ensure_ascii=False,indent=2)+'\n')
