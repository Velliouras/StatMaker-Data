"""Distinguish available count-training labels from actual priced evaluation support."""
import argparse,sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT,PRIOR,OCTOBER
from betting_v2.reliable_counts import metric_values
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.cross_market_inputs import valid_quote
from betting_v2.research_context_market import partitions
from betting_v2.research_git_archive import absolute_time

METRICS=('GOALS_FT','GOALS_1H','GOALS_2H','CORNERS','SHOTS','SOT','FOULS','YELLOW_CARDS','CARDS','RED_CARDS')

def main(out):
    history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    files=[PRIOR/'history.json.gz',Path(__file__),ROOT/'scripts/materialize_betting_v2_direct.py',ROOT/'scripts/betting_v2/reliable_counts.py',ROOT/'scripts/betting_v2/cross_market_inputs.py',ROOT/'scripts/betting_v2/research_context_market.py']
    result=dict(historyFixtures=len(history),metrics={},providerCalls=0,llmCalls=0,modelFits=0,
        scope='Only the frozen normalized history and canonical prior/October quote packets; not all raw app caches',
        interpretation='Quoted fixtures are coverage counts, not qualifying picks or independent evidence. Earliest/latest overlap.')
    for m in METRICS:
        actuals={f for f,r in truth.items() if metric_values(r,m) is not None}
        result['metrics'][m]=dict(validHistoryLabels=len(actuals),priorLabels={d:sum(r['_date']+86400<=absolute_time(d+'T00:00:00Z') and metric_values(r,m) is not None for r in history) for d in ('2026-08-01','2026-09-01','2026-10-01')},packets={})
    for packet in ('earliest','latest'):
        paths=[source/f'quotes_{packet}.json.gz' for source in (PRIOR,OCTOBER)];files+=paths;quotes=sum((read(p) for p in paths),[]);valid=[q for q in quotes if valid_quote(q) is None];parts,_=partitions(valid)
        for m in METRICS:
            raw={q['fixture'] for q in quotes if q['spec'][0]==m};strict={q['fixture'] for q in valid if q['spec'][0]==m}
            complete={g['over']['fixture'] for g in parts if g['over']['spec'][0]==m};labelled={f for f in complete if f in truth and metric_values(truth[f],m) is not None}
            result['metrics'][m]['packets'][packet]=dict(quotedFixtures=len(raw),validClockQuotedFixtures=len(strict),completeCountTotalTeamFixtures=len(complete) if m in ('CORNERS','SHOTS','SOT','FOULS') else None,completeCountFixturesWithActual=len(labelled) if m in ('CORNERS','SHOTS','SOT','FOULS') else None)
    result['cardCaveat']='Canonical packets omit card grading because raw HY+HR is not verified bookmaker units. Zero canonical card quotes does not mean no raw card offers exist.'
    result['inputsSha256']={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in files};write(out/'coverage.json',result)
    lines=['# Διαθέσιμα δεδομένα εκπαίδευσης και πραγματικών τιμών — 18/10/2026','','Οι αγώνες με στατιστικά και οι αγώνες με πραγματικές αποδόσεις είναι διαφορετικά σύνολα. Το μεγαλύτερο πρώτο σύνολο χρησιμοποιείται για εκπαίδευση, όπου το επιτρέπει κάθε υπόθεση. Το ROI χρειάζεται σωστό αποτέλεσμα, ακριβή αγορά και πραγματική προγενέστερη απόδοση.','','| Μέγεθος | Έγκυρα ιστορικά αποτελέσματα | Αγώνες με τιμές: πρώτες / τελευταίες | Πλήρεις TOTAL/TEAM count αγορές και αποτέλεσμα: πρώτες / τελευταίες |','|---|---:|---:|---:|']
    for m,v in result['metrics'].items():
        q=[v['packets'][p]['quotedFixtures'] for p in ('earliest','latest')];c=[v['packets'][p]['completeCountFixturesWithActual'] for p in ('earliest','latest')]
        lines.append(f"| {m} | {v['validHistoryLabels']} | {q[0]} / {q[1]} | "+(' / '.join(str(x) for x in c) if c[0] is not None else '—')+' |')
    lines+=['','Τα πακέτα πρώτων και τελευταίων τιμών επικαλύπτονται. Οι παραπάνω αγώνες δεν είναι αριθμοί προτάσεων μετά τα όρια. Ο πίνακας αφορά τη συγκεκριμένη παγωμένη ιστορία και τα κανονικοποιημένα πακέτα, όχι όλα τα raw δεδομένα του app. Τα CARDS είναι HY+HR, όχι επαληθευμένη μονάδα bookmaker.','','## Κάρτες: ακριβές εμπόδιο και λύση','','Υπάρχουν ήδη raw card offers, όπως καταγράφει το `../all_market_revision_20261016/card_contract_audit.json`: 546 Number of Cards In Match και άλλα TOTAL/TEAM συμβόλαια, με επαναλήψεις σε παραλαβές. Το μηδέν του κανονικοποιημένου πίνακα δεν σημαίνει απουσία τους από την εφαρμογή. Ο παλαιός έλεγχος κατέγραψε 90 event files και μηδενική τότε κάλυψη lineups στα ίδια UEFA fixtures· δεν έγινε εδώ νέος έλεγχος ολόκληρου live cache.','','Η επίσημη [Bet365 Number of Cards](https://help.bet365.com/s/en/sportsrules/soccer/card-markets), επαληθευμένη 18/10/2026, δίνει βάρος 1 στην κίτρινη και 2 στην κόκκινη, εξαιρεί δεύτερη κίτρινη και κάρτες μετά τη λήξη, και περιορίζει ποιοι παίκτες μετρούν. Το άθροισμα HY+HR του υφιστάμενου target δεν τεκμηριώνει αυτό το συμβόλαιο. Δεν αρκεί μηχανική αλλαγή σε HY+2HR: παραμένουν δεύτερες κίτρινες, επιλεξιμότητα και χρονικό όριο.','','Επόμενη απαιτούμενη εργασία για card ROI: ακριβής ένωση odds fixture με πλήρη events και player appearance/period στοιχεία, ξεχωριστό target στις επιβεβαιωμένες μονάδες κάθε bookmaker, νέο χρονικό fit και replay. Δεν χρησιμοποιούνται αποτελέσματα διαφορετικών μονάδων ως απόδειξη κέρδους.','','[Πλήρεις μετρήσεις και hashes](coverage.json)']
    (out/'RESULTS_GR.md').write_text('\n'.join(lines)+'\n')
    print({m:v['validHistoryLabels'] for m,v in result['metrics'].items()},flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True);main(a.output)
