"""Pinned open-weight LLM inference on the runner, without credentials or HTTP."""
import hashlib
import json
import os
from pathlib import Path
from ai_analyst.provider import ProviderError

MODEL_SHA256 = '3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597'
MODEL_ID = 'unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M'
_model = None


def schema(analysis):
    refs = {'type': 'array', 'minItems': 1, 'maxItems': 4,
            'items': {'enum': [f['id'] for f in analysis['facts']]}}
    note = {'type': 'string', 'minLength': 1, 'maxLength': 400}
    item = {'type': 'object', 'additionalProperties': False, 'required':
            ['offerId','verdict','reasonCodes','factIds','note'], 'properties': {
        'offerId': {'enum': [r['offerId'] for r in analysis['offers']] or ['NO_OFFER']},
        'verdict': {'enum': ['SUPPORT','CAUTION','ABSTAIN']},
        'reasonCodes': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {'enum':
            ['COVERAGE_LIMITED','MODEL_MARKET_GAP','HISTORICAL_VARIABILITY',
             'COHERENT_WITH_HISTORY','QUOTE_STALENESS','PUSH_RISK','NO_ADDITIONAL_EDGE']}},
        'factIds': refs, 'note': note}}
    return {'type': 'object', 'additionalProperties': False,
            'required': ['schemaVersion','fixtureRef','dataAssessment','reviews'], 'properties': {
        'schemaVersion': {'const': 1}, 'fixtureRef': {'const': analysis['fixtureRef']},
        'dataAssessment': {'type': 'object', 'additionalProperties': False,
            'required': ['level','factIds','note'], 'properties': {
                'level': {'enum': ['LIMITED','ADEQUATE']}, 'factIds': refs, 'note': note}},
        'reviews': {'type': 'array', 'minItems': len(analysis['offers']),
                    'maxItems': len(analysis['offers']), 'items': item}}}


def load_model():
    global _model
    if _model is not None:
        return _model
    path = Path(os.environ.get('AI_LOCAL_MODEL_PATH', '/nonexistent/statmaker-model.gguf'))
    if not path.is_file():
        raise ProviderError('MISSING_PINNED_LOCAL_MODEL')
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8*1024*1024), b''):
            h.update(chunk)
    if h.hexdigest() != MODEL_SHA256:
        raise ProviderError('LOCAL_MODEL_HASH_MISMATCH')
    try:
        from llama_cpp import Llama
        _model = Llama(model_path=str(path), n_ctx=4096, n_batch=256,
                       n_threads=min(4, os.cpu_count() or 2), n_gpu_layers=0,
                       chat_format='chatml', seed=19102026, verbose=False)
    except ImportError:
        raise ProviderError('MISSING_LOCAL_INFERENCE_RUNTIME') from None
    return _model


def infer(body, unused_token='', engine=None):
    if body.get('model') != MODEL_ID:
        raise ProviderError('UNEXPECTED_LOCAL_MODEL')
    analysis = json.loads(body['messages'][1]['content'])
    model = engine if engine is not None else load_model()
    try:
        result = model.create_chat_completion(messages=body['messages'],
                response_format={'type':'json_object','schema':schema(analysis)},
                temperature=.1, top_p=.8, top_k=20, repeat_penalty=1.05,
                max_tokens=min(body['max_tokens'],900), seed=19102026)
        choice = result['choices'][0]
        if choice.get('finish_reason') != 'stop':
            raise ProviderError('LOCAL_LLM_INCOMPLETE_RESPONSE')
        review = json.loads(choice['message']['content'])
        usage = {k:v for k,v in result.get('usage',{}).items()
                 if k in ('prompt_tokens','completion_tokens','total_tokens') and type(v) is int}
        return review, dict(model=MODEL_ID, weightsSha256=MODEL_SHA256,
                            runtime='llama-cpp-python', usage=usage, inferenceAPIRequests=0)
    except (ValueError, TypeError, KeyError, IndexError):
        raise ProviderError('LOCAL_LLM_INVALID_RESPONSE') from None
