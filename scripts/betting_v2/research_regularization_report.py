"""Readable evidence for the offline regularization study."""
import argparse,hashlib,json,sys
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.recovery_walk_forward import write

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();out=args.output
    result=json.loads((out/'regularization_comparison.json').read_text())
    choices=json.loads((out/'regularization_choices.json').read_text())
    lines=['# Prior-window model selection and stronger regularization',
        '', 'Research only. No UAT or PROD change. No provider requests.', '',
        'Data: the same 18,764 matches and SHA-256 as the corrected baseline. '
        'Three chronological test windows: August, September, and October 1–14, 2026. '
        'These periods were inspected in earlier development; they are not an untouched holdout.', '',
        '## Protocol', '',
        '- Compare full, no-xG, no-goal-stack, chronological goal-stack, and chronological stack with 10× or 50× ridge.',
        '- Hold the full-time goal coefficients identical for the ridge variants; scale the count ridge penalty and half-split logistic penalty.',
        '- Fit validation calibration on the first two thirds of the prior calibration month; use the remaining third to choose a model and joint, half-shrunk, or identity calibration.',
        '- Choose by common-cohort Brier score, equally weighting the metrics in the goal group; keep full-time and both half-time models coupled.',
        '- Freeze choices before the evaluation stage reads test rows, then use the chosen model’s profile refitted on the full prior calibration month.',
        '- Evaluate the identical event cohort across all six models. Prices and historical betting returns do not enter selection.', '',
        '## Paired development results', '',
        'ΔBrier = candidate minus the existing full calibrated baseline; negative is better. '
        'Intervals resample whole fixtures. They are marginal descriptive intervals, with no multiplicity correction '
        'or allowance for serial team/league dependence.', '',
        '| Family | Baseline Brier | Selected Brier | ΔBrier | 95% interval |',
        '|---|---:|---:|---:|---:|']
    for metric,s in result['selectedVsFull'].items():
        lo,hi=s['pairedBrierDelta95'];b=s['baseline']['brier'];c=s['candidate']['brier']
        lines.append(f'| {metric} | {b:.6f} | {c:.6f} | {c-b:+.6f} | [{lo:+.6f}, {hi:+.6f}] |')
    lines+=['',f"Common event rows: {result['commonEvents']:,}. These are correlated market events, not distinct matches.",
        '', '## Fixed ridge alternatives with full prior-month calibration', '',
        '| Family | 10× ridge ΔBrier | 50× ridge ΔBrier |','|---|---:|---:|']
    delta=lambda s:s['candidate']['brier']-s['baseline']['brier']
    for metric in result['selectedVsFull']:
        lines.append(f"| {metric} | {delta(result['ridgeVsFull']['oof_ridge_10'][metric]):+.6f} | {delta(result['ridgeVsFull']['oof_ridge_50'][metric]):+.6f} |")
    lines+=['','## Frozen prior-window choices','','| Test window | Group | Model | Calibration |',
        '|---|---|---|---|']
    for fold in choices['folds']:
        for group,choice in fold['choices'].items():
            lines.append(f"| {fold['testFrom']} | {group} | {choice['candidate']} | {choice['mode']} |")
    lines+=['','## High-probability diagnostic grid','','These bins cover unpriced events. They do not certify HIGH/CONFIRMED priced picks.',
        '', '| Family | 80–100% events | Fixtures | Predicted | Observed |', '|---|---:|---:|---:|---:|']
    for metric,bands in result['probabilityBands'].items():
        if '80-100' not in bands:continue
        b=bands['80-100'];lines.append(f"| {metric} | {b['events']} | {b['fixtures']} | {b['predicted']:.2%} | {b['observed']:.2%} |")
    lines+=['','## Interpretation and limits','',
        'The selected rule lowers Brier in eight of nine families. Full-time goals worsen slightly; this negative '
        'result is retained. The full-time goal regression weights were held fixed: the larger FT difference '
        'comes from the selected half-strength calibration in the August test, while fixed-ridge joint calibration '
        'has only the previously documented numerical refit difference. No claim of universal improvement is made.', '',
        'The candidate menu was designed after prior development inspection. Chronological selection prevents each '
        'test window’s labels from selecting its own model, but does not make the overall research an independent final test.', '',
        'No new ROI estimate is produced here. The earlier 59-bet replay, including HIGH 0/2 and CONFIRMED 8/19, '
        'is retained unchanged. Card markets remain count diagnostics without a verified bookmaker settlement contract. '
        'Historical statistic publication timestamps remain unverified. No application tier thresholds were changed.', '']
    prospective=out/'regularization_forward_inventory.json'
    if prospective.exists():
        p=json.loads(prospective.read_text());lines+=['## Separate prospective batch','',
            f"Frozen at {p['frozenAtUTC']}, SHA-256 `{p['sha256']}`. The first prospective file remains unchanged.", '',
            '| Model | Forecasts | Fixtures | Research selections |','|---|---:|---:|---:|']
        for name,data in p['overview'].items():
            lines.append(f"| {name} | {data['forecasts']} | {data['fixtures']} | {data['selected']} |")
        lines+=['','This batch overlaps the first prospective batch; it is not an additional independent sample.',
            '', 'All kickoffs follow this new freeze. Inputs are the same archived quote packets, with at least '
            '24 hours of feature lag before the quote. Quote age is recorded; current execution and slippage are unverified. '
            'No future results were read and no automatic future scoring job was created.','']
    (out/'REGULARIZATION.md').write_text('\n'.join(lines))
    manifest={p.name:dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(out.iterdir()) if p.is_file() and p.name not in ('artifact_manifest.json','running.lock') and p.suffix!='.tmp'}
    write(out/'artifact_manifest.json',dict(files=manifest,status='Complete chronological stack, ridge and prior-window selection research',
        providerCalls=0,untouchedHoldout=False,productionEnabled=False))
    print('\n'.join(lines[:29]),flush=True)

if __name__=='__main__':main()
