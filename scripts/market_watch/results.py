"""Exact FT/period/count settlements for routed markets from existing caches."""
from pathlib import Path
from scripts.shots_uat.results import SOURCES, cached_rows, stamp
from .catalog import settlement


def exact_settlement(issue, spec, actual, root, now):
    from betting_v2.reliable_counts import metric_values
    from betting_v2.research_app_markets import PERIOD_SCOPES
    signature = []
    for relative in SOURCES:
        path = Path(root)/relative
        info = path.stat() if path.exists() else None
        signature.append((str(path), info.st_mtime_ns if info else 0, info.st_size if info else 0))
    rows = [r for r in cached_rows(tuple(signature)) if r['_evidenceAt'] <= now]
    rows += [dict(r, _evidenceAt=0.) for r in actual]
    usable = []
    for row in rows:
        if str(row.get('fixture_id')) != str(issue['apiFixtureId']) or abs(row.get('_date',0)-stamp(issue['kickoff'])) > 300:
            continue
        labels = metric_values(row, spec[0])
        half = metric_values(row, 'GOALS_1H') if spec[1] in PERIOD_SCOPES else ()
        if labels is None or half is None:
            continue
        signature_values = tuple(labels) + tuple(half)
        if any(v < 0 or not float(v).is_integer() for v in signature_values):
            continue
        result = settlement(row, spec)
        if result is not None:
            usable.append((row, signature_values, result))
    if not usable:
        return None
    clock = max(r[0]['_evidenceAt'] for r in usable)
    newest = [r for r in usable if r[0]['_evidenceAt'] == clock]
    if len({r[1] for r in newest}) != 1:
        return None
    row, labels, result = newest[0]
    return result, row.get('_settlementSource','EXACT_HISTORY_FIXTURE'), clock
