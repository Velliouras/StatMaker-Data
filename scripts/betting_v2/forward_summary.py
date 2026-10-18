"""Summarise all immutable prospective cohorts; never select an arm by ROI."""
from collections import defaultdict
from datetime import datetime, timezone
import gzip, hashlib, json
from pathlib import Path

MODELS = ('control', 'coupled_market_conservative', 'coupled_market_point')


def read(path):
    return json.loads(path.read_text())


def aggregate(parts):
    bets = sum(p.get('bets', 0) for p in parts)
    profit = sum(p.get('profitUnits', 0.) for p in parts)
    result = dict(bets=bets, fixtures=sum(p.get('fixtures', 0) for p in parts),
        profitUnits=profit, roi=profit / bets if bets else None,
        wins=sum(p.get('wins', 0) for p in parts), pushes=sum(p.get('pushes', 0) for p in parts))
    if bets:
        result['meanPredicted'] = sum(p.get('meanPredicted', 0.) * p.get('bets', 0) for p in parts) / bets
        result['hitRate'] = result['wins'] / bets
    return result


def summarise(out, index):
    proof_path = out / 'publication_evidence.json'
    proofs = read(proof_path) if proof_path.exists() else {}
    cohorts = []; seen = set(); overlaps = set(); scores = defaultdict(list)
    pending = defaultdict(int); all_rows = defaultdict(list); rows_complete = defaultdict(lambda: True)
    for item in index.get('cohorts', []):
        raw = (out / item['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('Immutable forward snapshot changed')
        doc = json.loads(gzip.decompress(raw)); proof = proofs.get(item['path'])
        if proof and proof['sha256'] != item['sha256']: raise ValueError('Forward publication hash mismatch')
        public = datetime.fromisoformat(proof['publiclyVerifiedAtUTC']).timestamp() if proof else None
        score_path = out / (item['path'].replace('.json.gz', '_score.json'))
        score = read(score_path) if score_path.exists() else {}
        if score and score['frozenSha256'] != item['sha256']: raise ValueError('Forward score hash mismatch')
        verified = bool(proof and score.get('publicationVerified') and
            score.get('publicationEvidence', {}).get('publiclyVerifiedAtUTC') == proof['publiclyVerifiedAtUTC'])
        fixtures = {r['fixture'] for data in doc['models'].values() for r in data['forecasts']
                    if public is not None and r['kickoff'] > public}
        overlaps.update(seen & fixtures); seen.update(fixtures)
        cohort = dict(path=item['path'], frozenSha256=item['sha256'],
            publicationVerified=bool(proof), scoreUsesPublicationProof=verified,
            scoredAtUTC=score.get('scoredAtUTC'), models={})
        for name, data in doc['models'].items():
            eligible = [r for r in data['selected'] if public is not None and r['kickoff'] > public]
            model = score.get('models', {}).get(name, {}) if verified else {}
            result = model.get('selected', dict(bets=0, fixtures=0))
            remaining = len(eligible) - result.get('bets', 0)
            if remaining < 0: raise ValueError('More settled picks than frozen eligible picks')
            scores[name].append(result); pending[name] += remaining
            rows = model.get('selectedRows', [])
            if len(rows) != result.get('bets', 0): rows_complete[name] = False
            all_rows[name].extend(rows)
            cohort['models'][name] = dict(selected=result, eligibleSelected=len(eligible),
                pendingEligible=remaining, awaitingPublication=len(data['selected']) if not proof else 0,
                excludedLatePublication=len(data['selected']) - len(eligible) if proof else 0)
        cohorts.append(cohort)
    models = {}
    for name in MODELS:
        model = dict(settled=aggregate(scores[name]), pendingEligible=pending[name])
        if overlaps:
            model['settled'] = None
            model['aggregationBlocked'] = 'Duplicate canonical fixtures across cohorts'
        elif rows_complete[name] and all_rows[name]:
            from betting_v2.research_quotes import stats
            rows = all_rows[name]; model['settled'] = stats(rows)
            model['expectedProfitUnits'] = sum(r['p'] * r['odd'] + r['push'] - 1 for r in rows)
            model['byMetric'] = {metric: stats([r for r in rows if r['spec'][0] == metric])
                for metric in sorted({r['spec'][0] for r in rows})}
        models[name] = model
    return dict(schema=1, generatedAtUTC=datetime.now(timezone.utc).isoformat(),
        cohorts=cohorts, models=models, overlappingFixtures=sorted(overlaps),
        publicBeforeKickoffFixtures=len(seen), providerCalls=0, llmCalls=0,
        policyChanges=0, autoPromotion=False,
        limitations=['Incomplete cohorts include only settled picks; pending picks remain outside the return denominator',
            'All predeclared arms are reported; no arm or market is chosen from observed ROI',
            'Observable archived odds are not execution fills; small samples do not establish profitability'])


def publish_summary(out, index):
    summary = summarise(out, index)
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    labels = dict(control='Παλιός συντηρητικός πυρήνας', coupled_market_conservative='Νέος συντηρητικός πυρήνας',
                  coupled_market_point='Νέος πυρήνας, κεντρική πιθανότητα')
    lines = ['# Αποτελέσματα προβλέψεων καταγεγραμμένων πριν από τη σέντρα', '',
        'Περιλαμβάνονται όλα τα παγωμένα cohorts. Οι χρονικές σφραγίδες δημοσίευσης και τα hashes ελέγχονται. '
        'Οι αγώνες που δεν έχουν βαθμολογηθεί παραμένουν εκκρεμείς.', '',
        '| Πολιτική | Βαθμολογημένες επιλογές | Κέρδος σε μονάδες | ROI | Εκκρεμείς επιλέξιμες |', '|---|---:|---:|---:|---:|']
    for name in MODELS:
        model = summary['models'][name]; result = model['settled']
        if result is None:
            lines.append(f"| {labels[name]} | — | — | — | {model['pendingEligible']} |")
        else:
            roi = f"{result['roi']:+.2%}" if result.get('roi') is not None else '—'
            lines.append(f"| {labels[name]} | {result['bets']} | {result.get('profitUnits', 0):+.3f} | {roi} | {model['pendingEligible']} |")
    lines += ['', 'Τα συνολικά ποσοστά υπολογίζονται ως συνολικό κέρδος / συνολικές βαθμολογημένες επιλογές, '
        'με μία μονάδα ανά επιλογή. Οι μερικώς ολοκληρωμένοι κύκλοι δεν αποτελούν τελικό αποτέλεσμα της περιόδου. '
        'Το μικρό δείγμα και οι μη επαληθευμένες εκτελέσεις αποδόσεων περιορίζουν τα συμπεράσματα. '
        'Δεν προάγεται αυτόματα μοντέλο βάσει αυτών των αποδόσεων.', '',
        'Στο `summary.json` υπάρχουν χωριστά οι κύκλοι, οι χρόνοι ενημέρωσης, οι εξαιρέσεις καθυστερημένης δημοσίευσης '
        'και, όπου υπάρχουν πλήρη στοιχεία, διαστήματα αβεβαιότητας ανά αγώνα και ανάλυση ανά αγορά.']
    (out / 'RESULTS_GR.md').write_text('\n'.join(lines) + '\n')
    return summary
