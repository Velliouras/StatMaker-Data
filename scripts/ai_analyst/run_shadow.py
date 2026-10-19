"""Isolated AI reviews of immutable, pre-published baseline forecasts."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_analyst.contracts import (VERSION, build_packets, canonical, digest, finite,
                                offer_key, timestamp, utc, validate_review, within)
from ai_analyst.provider import SYSTEM, request_body, ProviderError
from ai_analyst.local_provider import infer, MODEL_ID, MODEL_SHA256

OUTPUT = 'reports/ai_analyst/v2_local'
POLICY = 'research/ai_analyst/v2_local/policy.json'


def write(root, relative, doc, immutable=False):
    if not relative.startswith(OUTPUT + '/'):
        raise ValueError('AI writer may only touch its own output namespace')
    path = within(root, relative)
    if not path.is_relative_to((root/OUTPUT).resolve()):
        raise ValueError('AI writer path escapes its own namespace')
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical(doc) + b'\n'
    if immutable and path.exists():
        if path.read_bytes() != raw:
            raise ValueError('Refusing to change an immutable AI record')
        return
    if immutable:
        with path.open('xb') as f:
            f.write(raw)
    else:
        staged = path.with_suffix('.tmp')
        staged.write_bytes(raw)
        staged.replace(path)


def load(path):
    return json.loads(path.read_bytes())


def validate_policy(config):
    if (config['version'] != VERSION or config['mode'] != 'SHADOW_ONLY' or
        config['maximumRequestsPerUTCDay'] != 2 or config['maximumRequestsPerRun'] != 2 or
        config['automaticPromotion'] is not False or config['numericProbabilityChanges'] is not False or
        type(config.get('inferenceEnabled', True)) is not bool or
        config['model'] != MODEL_ID or config['modelWeightsSha256'] != MODEL_SHA256):
        raise ValueError('Unexpected AI policy; new policy needs a new experiment')


def guard(root, bindings):
    if any(digest(within(root, p).read_bytes()) != h for p, h in bindings.items()):
        raise ValueError('Read-only upstream input changed during AI run')


def run(root, now, live=False, token='', inference=infer, clock=time.time):
    config = load(root / POLICY)
    validate_policy(config)
    policy_hash = digest((root / POLICY).read_bytes())
    packets, bindings = build_packets(root, config, now)
    bindings[POLICY] = policy_hash
    out = root / OUTPUT
    records = [load(p) for p in (out / 'attempts').glob('*.json')]
    today = utc(now)[:10]
    remaining = config['maximumRequestsPerUTCDay'] - sum(r['budgetDayUTC'] == today for r in records)
    attempted = {r['fixtureRef'] for r in records}
    status = dict(version=VERSION, generatedAtUTC=utc(now), mode='SHADOW_ONLY',
                  realLLMRequested=live, llmCallsThisRun=0, upstreamProviderCalls=0,
                  readyFixtures=len(packets), prepared=[], errors=[],
                  policySha256=policy_hash, inputBindings=bindings, engineWrites=0,
                  autoPromotion=False, remainingDailyRequests=max(0, remaining))
    if live and not config.get('inferenceEnabled', True):
        status['status'] = 'PAUSED_FOR_QUALITY'
        status['pauseReason'] = config.get('pauseReason', '')
        guard(root, bindings)
        write(root, OUTPUT+'/status.json', status)
        score(root, now)
        return status
    for packet in packets:
        if len(status['prepared']) >= config['maximumRequestsPerRun']:
            break
        fixture_ref = packet['analysis']['fixtureRef']
        if fixture_ref in attempted:
            continue
        identity = digest(canonical([VERSION, policy_hash, fixture_ref,
                                     packet['binding']['sourceSha256'], digest(SYSTEM.encode())]))[:32]
        input_path = OUTPUT + '/inputs/' + identity + '.json'
        # A preview is never stored as a real model response or paid attempt.
        if not live:
            write(root, OUTPUT+'/previews/'+identity+'.json', packet)
            status['prepared'].append(identity)
            continue
        if remaining <= 0:
            break
        body = request_body(packet['analysis'], config['model'])
        if len(canonical(body)) > 24000:
            status['errors'].append('INPUT_EXCEEDS_FIXED_BUDGET')
            continue
        if clock() >= packet['binding']['kickoff'] - 120:
            continue
        write(root, input_path, packet, immutable=True)
        write(root, OUTPUT+'/attempts/'+identity+'.json',
              dict(version=VERSION, budgetDayUTC=today, fixtureRef=fixture_ref,
                   startedAtUTC=utc(clock()), inputPath=input_path,
                   inputSha256=digest((root/input_path).read_bytes()),
                   policySha256=policy_hash, promptSha256=digest(SYSTEM.encode()),
                   requestSha256=digest(canonical(body)), model=config['model']), immutable=True)
        remaining -= 1
        status['llmCallsThisRun'] += 1
        status['prepared'].append(identity)
        attempted.add(fixture_ref)
        try:
            review, metadata = inference(body, token)
            validate_review(review, packet)
            completed = clock()
            if completed >= packet['binding']['kickoff'] - 60:
                raise ValueError('AI response arrived too late')
            doc = dict(version=VERSION, completedAtUTC=utc(completed), review=review,
                       inputPath=input_path, inputSha256=digest((root/input_path).read_bytes()),
                       provider='Local llama.cpp', realLLM=True, metadata=metadata,
                       researchOnly=True, reliabilityScore=None,
                       baselineProbabilitiesUnchanged=True, automaticPromotion=False)
            write(root, OUTPUT+'/reviews/'+identity+'.json', doc, immutable=True)
        except (ProviderError, ValueError, KeyError, TypeError) as error:
            # No template, classifier, cached-other-game or fabricated-AI fallback.
            reason = str(error) if isinstance(error, (ProviderError, ValueError)) else 'INVALID_RESPONSE_CONTRACT'
            status['errors'].append(reason)
            write(root, OUTPUT+'/failures/'+identity+'.json',
                  dict(failedAtUTC=utc(clock()), reason=reason, fixtureRef=fixture_ref,
                       inputSha256=digest((root/input_path).read_bytes())), immutable=True)
            if isinstance(error, ProviderError):
                break  # An unavailable provider is not retried for every fixture.
    guard(root, bindings)
    status['remainingDailyRequests'] = max(0, remaining)
    status['status'] = 'BLOCKED' if status['errors'] else 'COMPLETE' if live else 'PREVIEW_ONLY'
    write(root, OUTPUT+'/status.json', status)
    score(root, now)
    return status


def record_publication(root, now):
    commit = os.environ.get('AI_PUBLISHED_COMMIT', '')
    if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
        raise ValueError('Successful publication commit required')
    directory = root / OUTPUT
    target = directory/'publication_evidence.json'
    proofs = load(target) if target.exists() else {}
    for path in sorted((directory/'reviews').glob('*.json')):
        relative = path.relative_to(root).as_posix()
        if relative in proofs:
            if proofs[relative]['sha256'] != digest(path.read_bytes()):
                raise ValueError('Published AI review changed')
            continue
        raw = subprocess.check_output(['git', 'show', commit+':'+relative], cwd=root)
        if raw != path.read_bytes():
            raise ValueError('AI review absent from successful publication commit')
        if timestamp(load(path)['completedAtUTC']) > now:
            raise ValueError('AI publication precedes response')
        proofs[relative] = dict(commit=commit, sha256=digest(raw), publiclyVerifiedAtUTC=utc(now),
                                method='Successful GitHub push followed by exact committed-blob comparison')
    write(root, OUTPUT+'/publication_evidence.json', proofs)
    score(root, now)


def score(root, now):
    """Reuse exact baseline labels after publication; never pass labels to the LLM."""
    directory = root/OUTPUT
    config = load(root/POLICY)
    pp = directory/'publication_evidence.json'
    proofs = load(pp) if pp.exists() else {}
    counts = {k: dict(bets=0, wins=0, pushes=0, profitUnits=0., pending=0)
              for k in ('BASELINE_SAME_REVIEWED_FIXTURES', 'AI_SUPPORT', 'AI_CAUTION', 'AI_ABSTAIN')}
    reviews, awaiting, late = [], 0, 0
    for path in sorted((directory/'reviews').glob('*.json')):
        doc = load(path)
        packet_raw = within(root, doc['inputPath']).read_bytes()
        if digest(packet_raw) != doc['inputSha256']:
            raise ValueError('AI input binding changed')
        packet = json.loads(packet_raw)
        validate_review(doc['review'], packet)
        binding = packet['binding']
        proof = proofs.get(path.relative_to(root).as_posix())
        if not proof:
            awaiting += 1
            continue
        if proof['sha256'] != digest(path.read_bytes()):
            raise ValueError('AI publication hash mismatch')
        verified = timestamp(proof['publiclyVerifiedAtUTC'])
        if not timestamp(doc['completedAtUTC']) <= verified < binding['kickoff']:
            late += 1
            continue
        source_file = root/binding['sourceDirectory']/binding['sourceCohort'].replace('.json.gz', '_score.json')
        actual = {}
        if source_file.exists():
            src = load(source_file)
            if src['frozenSha256'] != binding['sourceSha256']:
                raise ValueError('Source grading refers to different predictions')
            if src['publicationVerified']:
                actual = {offer_key(r): r for r in src['models'][binding['sourceModel']]['selectedRows']}
        verdicts = {r['offerId']: r['verdict'] for r in doc['review']['reviews']}
        for row in binding['baselineRows']:
            identity = digest(canonical(list(offer_key(row))))[:24]
            groups = ['BASELINE_SAME_REVIEWED_FIXTURES', 'AI_'+verdicts[identity]]
            result = actual.get(offer_key(row))
            for group in groups:
                target = counts[group]
                if result is None:
                    target['pending'] += 1
                    continue
                if now < binding['kickoff'] or any(result[k] != row[k] for k in ('p', 'push', 'ev', 'odd')):
                    raise ValueError('Source result crosses clock or changed original forecast')
                win, push = finite(result['y'], 0, 1), finite(result['actualPush'], 0, 1)
                if win not in (0., 1.) or push not in (0., 1.) or win+push > 1:
                    raise ValueError('Invalid settled outcome')
                target['bets'] += 1
                target['wins'] += int(win)
                target['pushes'] += int(push)
                target['profitUnits'] += win*row['odd']+push-1
        reviews.append(dict(fixture=binding['fixture'], home=binding['home'], away=binding['away'],
                            sourceModel=binding['sourceModel'], reviewedOffers=len(binding['baselineRows']),
                            verdicts=list(verdicts.values()), dataAssessment=doc['review']['dataAssessment'],
                            offerReviews=doc['review']['reviews'], baselineRows=binding['baselineRows']))
    for value in counts.values():
        value['roi'] = value['profitUnits']/value['bets'] if value['bets'] else None
    result = dict(version=VERSION, generatedAtUTC=utc(now), groups=counts, reviews=reviews,
                  awaitingPublication=awaiting, excludedLatePublication=late,
                  primary='AI_SUPPORT', researchOnly=True, autoPromotion=False,
                  probabilityChanges=0, engineWrites=0, realBetsPlaced=0)
    result['inferenceEnabled'] = config.get('inferenceEnabled', True)
    result['analysisQualityAccepted'] = config.get('analysisQualityAccepted')
    result['pauseReason'] = config.get('pauseReason')
    write(root, OUTPUT+'/summary.json', result)
    lines = ['# Ανεξάρτητος AI αναλυτής — δοκιμή', '',
             'Οι αρχικές πιθανότητες και επιλογές παραμένουν αμετάβλητες. Το AI σχολιάζει μόνο τα δοσμένα στοιχεία.',
             'Κύρια σύγκριση: AI_SUPPORT έναντι των ίδιων αρχικών επιλογών στους ίδιους αξιολογημένους αγώνες.', '',
             '| Ομάδα | Ολοκληρωμένες | Κέρδος μονάδων | ROI | Εκκρεμείς |',
             '|---|---:|---:|---:|---:|']
    for key, value in counts.items():
        roi = '—' if value['roi'] is None else f"{100*value['roi']:.2f}%"
        lines.append(f"| {key} | {value['bets']} | {value['profitUnits']:+.3f} | {roi} | {value['pending']} |")
    if not result['inferenceEnabled']:
        lines += ['', 'Νέες αναλύσεις AI σε παύση: η πρώτη δοκιμή δεν πέρασε τον έλεγχο ποιότητας κειμένου.',
                  'Τα παρακάτω πρωτότυπα κείμενα διατηρούνται ως τεχνικό ιστορικό και δεν αποτελούν αποδεκτή ανάλυση χρήστη.']
    lines += ['', 'Η γνώμη του AI δεν είναι βαθμολογία HIGH/CONFIRMED, νέα πιθανότητα ή απόδειξη κερδοφορίας.',
              'Οι ελλείπουσες ενδεκάδες, απουσίες και καιρικές συνθήκες παραμένουν άγνωστες.',
              'Η βαθμολόγηση απαιτεί δημοσίευση της AI ανάλυσης πριν από τη σέντρα. Καμία αυτόματη προαγωγή στο app.']
    for review in reviews:
        lines += ['', '## '+review['home']+' — '+review['away'], '', review['dataAssessment']['note']]
        for row, opinion in zip(review['baselineRows'], review['offerReviews']):
            identity = digest(canonical(list(offer_key(row))))[:24]
            opinion = next(r for r in review['offerReviews'] if r['offerId'] == identity)
            lines += ['', f"- {row['spec']} · απόδοση {row['odd']} · αρχική πιθανότητα {100*row['p']:.2f}%",
                      '  AI: '+opinion['verdict']+' — '+opinion['note']]
        if not review['baselineRows']:
            lines += ['', 'Δεν υπήρχε αρχική επιλεγμένη πρόταση για αξιολόγηση. Το AI εξέτασε μόνο τα διαθέσιμα στοιχεία.']
    # Keep every write under the same guarded JSON/text output prefix.
    path = within(root, OUTPUT+'/RESULTS_GR.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository-root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--record-publication', action='store_true')
    parser.add_argument('--score-only', action='store_true')
    parser.add_argument('--check-work', action='store_true')
    args = parser.parse_args()
    root = args.repository_root.resolve()
    lock = Path(tempfile.gettempdir())/('statmaker-ai-'+digest(str(root).encode())[:16]+'.lock')
    with lock.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = time.time()
        if args.check_work:
            config=load(root/POLICY);validate_policy(config)
            packets,_=build_packets(root,config,now)
            attempts=[load(p) for p in (root/OUTPUT/'attempts').glob('*.json')]
            seen={r['fixtureRef'] for r in attempts}
            available=(config.get('inferenceEnabled', True)
                       and sum(r['budgetDayUTC']==utc(now)[:10] for r in attempts)<2
                       and any(p['analysis']['fixtureRef'] not in seen for p in packets))
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'],'a') as output:
                    output.write('needs_ai='+str(available).lower()+'\n')
            print('New local inference needed:',available)
        elif args.record_publication:
            record_publication(root, now)
        elif args.score_only:
            score(root, now)
        else:
            status = run(root, now, args.run_live, os.environ.get('AI_GITHUB_TOKEN', ''))
            print(json.dumps({k: status[k] for k in ('status', 'readyFixtures', 'llmCallsThisRun',
                              'remainingDailyRequests', 'errors')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
