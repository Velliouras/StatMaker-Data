"""Read-only JSON shape/coverage report for existing UEFA support archive."""
from __future__ import annotations
from collections import Counter
import json
from pathlib import Path

def shape(data):
    if isinstance(data, dict):
        return {"type":"object","keys":list(data)[:30],
                "sampleFields":{k:shape(v) for k,v in list(data.items())[:12]}}
    if isinstance(data, list):
        return {"type":"list","length":len(data),
                "first":shape(data[0]) if data else None}
    if isinstance(data, str):
        return "str:"+data[:100]
    if isinstance(data,(int,float,bool)):
        return data
    return None

p=Path("data/statmaker/uefa_support_history.json")
data=json.loads(p.read_text(encoding="utf-8"))
print("PHASE1_UEFA_ARCHIVE_SHAPE")
info=shape(data)
print(json.dumps(info,ensure_ascii=False,indent=2)[:12500])
