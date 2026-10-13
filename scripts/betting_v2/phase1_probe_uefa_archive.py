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


print("PHASE1_BROAD_SUPPORT_MARKET_COVERAGE")
from collections import defaultdict
records=defaultdict(lambda:{"rows":0,"uniqueMatches":set(),"corners":0,"shots":0,"yellow":0,"goals":0})
for participant in data.get("participants") or []:
    code=str(participant.get("leagueCode") or "")
    for m in participant.get("matches") or []:
        group=records[code]
        key=(str(m.get("season")),str(m.get("date")),str(m.get("homeTeam")),str(m.get("awayTeam")))
        group["rows"]+=1
        group["uniqueMatches"].add(key)
        if m.get("homeCorners") is not None and m.get("awayCorners") is not None:
            group["corners"]+=1
        if m.get("homeShots") is not None and m.get("awayShots") is not None:
            group["shots"]+=1
        if m.get("homeYellowCards") is not None and m.get("awayYellowCards") is not None:
            group["yellow"]+=1
        if m.get("homeGoals") is not None and m.get("awayGoals") is not None:
            group["goals"]+=1
for code in ["NOR2","SC1","SC2","SC3","BRA2","SVK","MAR","RSA","EGY","E1","I2","SAU","ROM"]:
    v=records.get(code)
    if v: print("PHASE1_SUPPORT_LEAGUE",code,"unique",len(v["uniqueMatches"]),
      "rows",v["rows"],"corners",v["corners"],"shots",v["shots"],
      "yellow",v["yellow"],"goals",v["goals"])
