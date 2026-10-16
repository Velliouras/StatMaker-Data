"""Bind original research predictions/profiles for an archive-price replay.

Run only from the recovered, verified research checkpoint. Current historical
files supply identity/league metadata only; no feature or prediction is rebuilt.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_csv_prices import validate_input, write, digest, sources
from betting_v2.research_regularization import profile_mode
from betting_v2.validated_walk_forward import FOLDS


def export(args):
    hashes = {}
    def read(path):
        path = path.resolve()
        raw = path.read_bytes()
        hashes[str(path.relative_to(direct.ROOT))] = digest(raw)
        return json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)
    inventory = read(args.baseline/'inventory.json')
    if inventory['dataSha256'] != '1653f6779eece3b7fef7dd5265db1978ffe566ec61e312f5866374739e14e9c0':
        raise ValueError('Unexpected original research cohort')
    choices = read(args.research/'goal_factorized_choices.json')
    if choices['testLabelsUsedForSelection'] or choices['untouchedHoldout']:
        raise ValueError('Unexpected selection protocol')
    history = direct.load_actual_results(direct.timestamp('2026-10-15'))['GLOBAL']
    metadata_sha = digest(json.dumps(history, sort_keys=True, separators=(',', ':')).encode())
    metadata = {f"{r['_date']}:{r['_home']}:{r['_away']}": r for r in history}
    codes = {s['code'] for s in sources()}
    def code_alias(code):
        return 'ROU' if code == 'ROM' else code
    aliases = {code_alias(code): dict(values) for code, values in direct.UEFA_ALIASES.items() if code_alias(code) in codes}
    folds = []
    exclusions = Counter()
    for fitday, testday, endday in FOLDS:
        choice = next(f for f in choices['folds'] if f['testFrom'] == testday)
        if choice['maxSelectionTimestamp'] >= direct.timestamp(testday):
            raise ValueError('Selection crosses test boundary')
        old = {r['fixture']: r for r in read(args.baseline/('full_'+testday+'_predictions.json.gz')) if r['metric'] == 'GOALS_FT'}
        new = {r['fixture']: r for r in read(args.research/('goal_factorized_separate_ft_selected_period_'+testday+'_predictions.json.gz')) if r['metric'] == 'GOALS_FT'}
        if set(old) != set(new):
            raise ValueError('Prediction cohorts differ')
        candidate = choice['experimentalFT']['candidate']
        new_profile = read(args.research/(candidate+'_'+testday+'_parameters.json'))
        new_profile = profile_mode({k:v for k,v in new_profile.items() if k.startswith('GOALS_FT::')}, choice['experimentalFT']['mode'])
        old_profile = read(args.baseline/('full_'+testday+'_parameters.json'))
        fixtures = []
        for key, baseline in old.items():
            experimental = new[key]
            if baseline['date'] != experimental['date'] or baseline['actual'] != experimental['actual']:
                raise ValueError('Paired fixture labels changed')
            m = metadata.get(key)
            if m is None:
                exclusions['noCurrentIdentityMetadata'] += 1
                continue
            league = code_alias(m.get('_home_group'))
            if league != code_alias(m.get('_away_group')) or league not in codes:
                exclusions['outsideCsvCompetitionScope'] += 1
                continue
            if list(direct.metric_values(m, 'GOALS_FT')) != baseline['actual']:
                raise ValueError('CSV-scope identity metadata disagrees with immutable FT label')
            predictions = {name:{k:r[k] for k in ('metric','regime','means','samples')} for name,r in
                           [('full',baseline),('separate_ft_selected_period',experimental)]}
            fixtures.append(dict(fixture=key, date=baseline['date'],
                utcDate=datetime.fromtimestamp(baseline['date'], timezone.utc).date().isoformat(),
                featureCutoff=math.floor(baseline['date']/86400)*86400,
                league=league, home=m['_home'], away=m['_away'], actual=baseline['actual'], predictions=predictions))
        folds.append(dict(fitBefore=fitday, profileBefore=testday, testFrom=testday, testBefore=endday,
            ftChoice=choice['experimentalFT'], profiles={'full':old_profile,'separate_ft_selected_period':new_profile}, fixtures=fixtures))
    doc = dict(schema=1, trainingDataSha256=inventory['dataSha256'], testFrom=FOLDS[0][1], testBefore=FOLDS[-1][2],
        sourceArtifactsSha256=hashes, identityMetadataSha256=metadata_sha,
        identityMetadataUse='Existing aliases and league/date/pair metadata only; no recomputation of model features or labels',
        testLabelsUsedForSelection=False, untouchedHoldout=False, aliases=aliases, folds=folds,
        excludedPredictions=dict(exclusions), newFits=0, frozenPredictionsReused=True)
    validate_input(doc)
    write(args.output, doc, zipped=True)
    manifest = dict(schema=1, inputSha256=digest(args.output.read_bytes()),
        fixtures=sum(len(f['fixtures']) for f in folds), byFold={f['testFrom']:len(f['fixtures']) for f in folds},
        sourceArtifactsSha256=hashes, identityMetadataSha256=metadata_sha,
        originalResearchDataSha256=inventory['dataSha256'], excludedPredictions=dict(exclusions),
        decision='Bet365 primary, Pinnacle secondary; closing prices only; all three existing policies; no refits or tuning',
        sourceDocumentation=['https://football-data.co.uk/notes.txt','https://football-data.co.uk/all_new_data.php'],
        appContract='CSV remains inactive archive only; research does not change active sources or publish app data')
    write(args.manifest, manifest)
    print(json.dumps({k:v for k,v in manifest.items() if k!='sourceArtifactsSha256'}, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--research', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    export(p.parse_args())
