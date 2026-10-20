"""Fixed operational UAT rules; positive ROI is not certification of an edge."""
from collections import Counter
from datetime import datetime, timezone
import math


def time(value):
    if isinstance(value,(int,float)):return float(value)
    return datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()


def statistics(records):
    rows=sorted([r for r in records if r.get("outcome") in ("WON","LOST","VOID")],key=lambda r:(r["kickoff"],r["id"]))
    n=len(rows);profit=sum(r["returned"]-1 for r in rows);balance=0.;peak=0.;drawdown=0.;curve=[]
    for r in rows:
        balance+=r["returned"]-1;peak=max(peak,balance);drawdown=max(drawdown,peak-balance)
        curve.append(dict(date=datetime.fromtimestamp(r["kickoff"],timezone.utc).isoformat(),profit=round(balance,6)))
    by=Counter(r["fixture"] for r in rows)
    unique=len(by);days=(max(r["kickoff"] for r in rows)-min(r["kickoff"] for r in rows))/86400 if rows else 0.
    roi95=None
    if unique>=2:
        # Descriptive fixture-cluster sandwich interval; not an anytime-valid
        # discovery test or a guarantee after adaptive market selection.
        totals={f:sum(r["returned"]-1 for r in rows if r["fixture"]==f) for f in by}
        mean=profit/n;variance=unique/(unique-1)*sum((totals[f]-mean*by[f])**2 for f in by)/n**2
        radius=1.96*math.sqrt(variance);roi95=[mean-radius,mean+radius]
    nonvoid=[r for r in rows if r["outcome"]!="VOID"]
    return dict(settled=n,fixtures=unique,calendarDays=days,wins=sum(r["outcome"]=="WON" for r in rows),
                losses=sum(r["outcome"]=="LOST" for r in rows),pushes=n-len(nonvoid),profitUnits=profit,
                roi=profit/n if n else None,roi95Descriptive=roi95,maxDrawdownUnits=drawdown,
                currentDrawdownUnits=peak-balance,hitRate=sum(r["outcome"]=="WON" for r in rows)/len(nonvoid) if nonvoid else None,
                meanProbability=sum(r["p"]/(1-r["push"]) for r in nonvoid)/len(nonvoid) if nonvoid else None,
                meanOdds=sum(r["odd"] for r in rows)/n if n else None,curve=curve)


def decision(records,previous,config,market,now):
    settings=config["markets"][market]
    rows=sorted([r for r in records if r.get("outcome") in ("WON","LOST","VOID")],key=lambda r:(r["kickoff"],r["id"]))
    total=statistics(rows); recent=statistics(rows[-config["recentWindow"]:]);last50=statistics(rows[-50:])
    prev=previous or {};old=prev.get("status", "ACTIVE" if settings["bootstrapActive"] else "SHADOW")
    status=old;reason="Αρχική ενεργή αγορά UAT· δεν αποτελεί πιστοποίηση κερδοφορίας" if settings["bootstrapActive"] else "Συλλογή προδημοσιευμένων επιλογών"
    baseline=bool(prev.get("baselineAdmission",settings["bootstrapActive"]))
    pause_at=prev.get("pausedAt");pause_count=prev.get("settledAtPause",0)
    if not settings["publisherConnected"]:
        status="BLOCKED_CONTRACT" if settings.get("blockedReason") else "UNSUPPORTED";reason=settings.get("blockedReason","Δεν υπάρχει συνδεδεμένο μοντέλο και συμβόλαιο δημοσίευσης")
    elif old=="ACTIVE":
        hard=total["currentDrawdownUnits"]>=config["pauseDrawdownUnits"]
        weak=recent["settled"]>=config["recentWindow"] and recent["roi"]<=config["pauseRecentROI"]
        if hard or weak:
            status="PAUSED";reason="Παύση: drawdown >=10u" if hard else "Παύση: ROI τελευταίων20 <=-10%"
            pause_at=now;pause_count=len(rows)
        elif total["settled"]:
            reason="Ενεργό· οι κανόνες παύσης δεν έχουν ενεργοποιηθεί"
    elif old=="PAUSED":
        # The pause watermark never moves as new paper selections arrive.
        new=[r for r in rows if pause_at is not None and r["published"]>=time(pause_at)];new_stats=statistics(new)
        enough=new_stats["fixtures"]>=config["resumeNewSettledFixtures"]
        cooled=pause_at is not None and now-time(pause_at)>=config["resumeCooldownDays"]*86400
        recovering=enough and new_stats["roi"]>=config["resumeMinimumROI"] and recent["roi"]>0
        if cooled and recovering and total["roi"]>0 and total["currentDrawdownUnits"]<config["pauseDrawdownUnits"]:
            status="ACTIVE";reason="Επανενεργοποίηση μετά από νέα κερδοφόρα δοκιμή και cooldown"
        else:reason="Παραμένει σε παύση· συνεχίζεται η καταγραφή χωρίς εμφάνιση στο app"
    else:
        enough=total["fixtures"]>=config["activationSettledFixtures"] and total["calendarDays"]>=config["activationCalendarDays"]
        profitable=total["roi"] is not None and total["roi"]>=config["activationMinimumROI"] and recent["roi"] is not None and recent["roi"]>0
        if enough and profitable:
            status="ACTIVE";baseline=False;reason="Ενεργοποίηση: 50+ αγώνες /14+ ημέρες /ROI>=3% /θετικό πρόσφατο ROI"
        else:status="SHADOW";reason="Αναμονή: 50 αγώνες,14 ημέρες,ROI>=3% και θετικό ROI τελευταίων20"
    return dict(status=status,reason=reason,baselineAdmission=baseline,pausedAt=pause_at,settledAtPause=pause_count,
                total=total,recent20=recent,last50=last50,pending=sum(r.get("outcome")=="PENDING" for r in records),
                transition=status!=old,previousStatus=old)
