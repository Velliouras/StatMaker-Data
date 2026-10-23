"""Daily follows current Singles, then freezes the last eligible pre-kickoff set."""
import bisect
import json
from pathlib import Path

from .repricing import iso, timestamp


def live_rows(payload):
    return (payload.get("candidates", []) + payload.get("marketCandidates", [])
            + payload.get("priceUpdates", []))


def eligible_at(row, instant):
    observed = timestamp(row.get("observedAt") or row.get("latestQuoteObservedAt") or row.get("quoteUpdatedAt"))
    return observed > 0 and instant - 86400 <= observed <= instant


def archive_states(directory, issued, now):
    """Bootstrap from published snapshots before kickoff, never from match outcomes."""
    snapshots = []
    for path in Path(directory).glob("candidates-*.json"):
        document = json.loads(path.read_text())
        generated = timestamp(document.get("generatedAt"))
        if generated <= 0 or generated > now:
            continue
        snapshots.append((generated, {r["selectionId"]: r for r in live_rows(document)}))
    snapshots.sort(key=lambda item: item[0])
    clocks = [item[0] for item in snapshots]
    states = {}
    for row in issued:
        kickoff = timestamp(row["kickoff"])
        if kickoff > now:
            continue
        index = bisect.bisect_left(clocks, kickoff) - 1
        if index < 0:
            continue
        generated, active = snapshots[index]
        current = active.get(row["selectionId"])
        states[row["selectionId"]] = dict(kickoff=row["kickoff"],
            included=current is not None and eligible_at(current, kickoff),
            observedAt=iso(generated), frozen=True)
    return states


def attach_daily(payload, directory, state_path):
    now = timestamp(payload["generatedAt"])
    issued = payload.get("issued", []) + payload.get("marketIssued", [])
    previous = json.loads(Path(state_path).read_text()) if Path(state_path).exists() else {}
    states = previous.get("selections", {})
    pending_past = [r for r in issued if timestamp(r["kickoff"]) <= now
                    and not states.get(r["selectionId"], {}).get("frozen")]
    if pending_past:
        states.update(archive_states(directory, pending_past, now))
    active = {r["selectionId"]: r for r in live_rows(payload)}
    selected = []
    for row in issued:
        key = row["selectionId"]
        kickoff = timestamp(row["kickoff"])
        if kickoff > now:
            current = active.get(key)
            state = dict(kickoff=row["kickoff"], included=current is not None and eligible_at(current, now),
                         observedAt=payload["generatedAt"], frozen=False)
        else:
            state = states.get(key)
            if state is None:
                # No archived pre-match publication is not evidence of an eligible wager.
                state = dict(kickoff=row["kickoff"], included=False, observedAt=None, frozen=True)
            elif not state.get("frozen"):
                state = dict(state, included=False, frozen=True)
        if state["kickoff"] != row["kickoff"]:
            raise ValueError("Daily selection kickoff changed")
        states[key] = state
        if state["included"]:
            selected.append(key)
    payload.update(dailySelectionIds=selected, dailySelectionCount=len(selected))
    validate_daily(payload)
    state_document = dict(schemaVersion=1, generatedAt=payload["generatedAt"], selections=states)
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(json.dumps(state_document, sort_keys=True, indent=2) + "\n")
    return payload


def validate_daily(payload):
    if "dailySelectionIds" not in payload:
        return
    ids = payload["dailySelectionIds"]
    issued = {r["selectionId"]: r for r in payload.get("issued", []) + payload.get("marketIssued", [])}
    if (not isinstance(ids, list) or len(ids) != payload.get("dailySelectionCount")
            or len(ids) != len(set(ids)) or any(key not in issued for key in ids)):
        raise ValueError("Invalid Daily selection identities/count")
    now = timestamp(payload["generatedAt"])
    current = {r["selectionId"] for r in live_rows(payload)}
    future = {key for key, row in issued.items() if timestamp(row["kickoff"]) > now}
    if set(ids) & future != current & future:
        raise ValueError("Daily and Singles future membership differs")
