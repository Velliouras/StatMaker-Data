"""Download exactly one pinned public GGUF, verifying its published SHA256."""
import hashlib
import os
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ai_analyst.local_provider import MODEL_SHA256

REVISION = 'a06e946bb6b655725eafa393f4a9745d460374c9'
FILENAME = 'Qwen3-4B-Instruct-2507-Q4_K_M.gguf'
URL = 'https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/'+REVISION+'/'+FILENAME


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b''):h.update(chunk)
    return h.hexdigest()


if __name__ == '__main__':
    folder=Path('/tmp/statmaker-ai-model');folder.mkdir(exist_ok=True)
    target=folder/FILENAME
    if not target.exists() or sha(target)!=MODEL_SHA256:
        partial=target.with_suffix('.partial')
        with urllib.request.urlopen(URL,timeout=55) as response, partial.open('wb') as output:
            for block in iter(lambda:response.read(8*1024*1024),b''):output.write(block)
        if sha(partial)!=MODEL_SHA256:raise ValueError('Downloaded model does not match pinned SHA256')
        partial.replace(target)
    print('Verified local model bytes:',target.stat().st_size,'SHA256:',MODEL_SHA256,flush=True)
    if os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'],'a') as env:env.write('AI_LOCAL_MODEL_PATH='+str(target)+'\n')
