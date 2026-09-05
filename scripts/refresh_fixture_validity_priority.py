#!/usr/bin/env python3
"""Priority/rotation controller for quota-safe canonical fixture validity.

This module reuses refresh_fixture_validity's matching, provider lookup and persistence helpers,
but changes request scheduling so past unresolved canonical rows cannot starve behind a single
large league. It also spends bounded capacity on near-term no-id fixtures, allowing reschedules
to be detected before the App-Ready recommendation gate publishes them.

No extra workflow is created and the validity request budget remains bounded by --max-requests.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from typing import Dict, List, Sequence, Tuple

import api_football_daily_quota_guard as quota_guard
import api_football_fetch_fixture_stats as stats_fetch
import refresh_fixture_validity as base
import refresh_live_settlements as live


def _disposed_requirement_keys(root: dict) -> set[str]:
    keys: set[str] = set()
    for item in root.get("dispositions", []) if isinstance(root, dict) else []:
        if not isinstance(item, dict):
            continue
        keys.add("|".join(str(item.get(key) or "") for key in (
            "competitionId", "matchKey", "localDate", "leagueCode"
        )))
    return keys


def _candidate_rows(
    requirements: Sequence[live.SettlementRequirement],
    feed: dict,
    existing_validity: dict,
) -> tuple[List[live.SettlementRequirement], dt.date]:
    completed = base.completed_requirement_keys(requirements, feed)
    disposed = _disposed_requirement_keys(existing_validity)
    today = base.now_utc().astimezone(base.ATHENS).date()
    low = today - dt.timedelta(days=base.RETENTION_DAYS)
    high = today + dt.timedelta(days=base.LOOKAHEAD_DAYS)
    rows: List[live.SettlementRequirement] = []
    for req in requirements:
        try:
            day = dt.date.fromisoformat(req.local_date)
        except ValueError:
            continue
        key = base.requirement_key(req)
        if day < low or day > high or key in completed or key in disposed:
            continue
        rows.append(req)
    return rows, today


def _group_no_id(
    rows: Sequence[live.SettlementRequirement],
) -> List[Tuple[Tuple[int, str, str], List[live.SettlementRequirement]]]:
    scopes = base.registry_scope_by_code()
    grouped: Dict[Tuple[int, str, str], List[live.SettlementRequirement]] = {}
    for req in rows:
        if req.api_fixture_id is not None:
            continue
        scope = scopes.get(req.league_code)
        if scope is None or not scope[1]:
            continue
        grouped.setdefault((scope[0], scope[1], req.league_code), []).append(req)
    return sorted(
        grouped.items(),
        key=lambda item: (
            min(row.local_date for row in item[1]),
            -len(item[1]),
            item[0][2],
        ),
    )


def _rotating_groups(
    groups: Sequence[Tuple[Tuple[int, str, str], List[live.SettlementRequirement]]],
    limit: int,
) -> List[Tuple[Tuple[int, str, str], List[live.SettlementRequirement]]]:
    """Keep one oldest/highest-pressure anchor and rotate the rest without persisted state."""
    if limit <= 0 or not groups:
        return []
    if len(groups) <= limit:
        return list(groups)

    selected = [groups[0]]
    if limit == 1:
        return selected

    rest = list(groups[1:])
    slot = int(base.now_utc().timestamp() // 900) % len(rest)
    for offset in range(min(limit - 1, len(rest))):
        selected.append(rest[(slot + offset) % len(rest)])
    return selected


def _fetch_id_batches(
    api_key: str,
    rows: Sequence[live.SettlementRequirement],
    request_state: Dict[str, int],
    max_requests: int,
    *,
    rotate: bool,
) -> List[dict]:
    by_id: Dict[int, List[live.SettlementRequirement]] = {}
    for req in rows:
        if req.api_fixture_id is not None:
            by_id.setdefault(req.api_fixture_id, []).append(req)
    if not by_id or request_state["count"] >= max_requests:
        return []

    ids = sorted(
        by_id,
        key=lambda fixture_id: min(
            row.local_date for row in by_id[fixture_id]
        ),
    )
    batches = base.chunks(ids, base.MAX_IDS_PER_REQUEST)
    remaining = max_requests - request_state["count"]
    if rotate and len(batches) > remaining:
        slot = int(base.now_utc().timestamp() // 900) % len(batches)
        chosen = [
            batches[(slot + offset) % len(batches)]
            for offset in range(min(remaining, len(batches)))
        ]
    else:
        chosen = batches[:remaining]

    detected: List[dict] = []
    for id_chunk in chosen:
        try:
            fixtures = base.fetch_by_ids(
                api_key,
                id_chunk,
                request_state,
                max_requests,
            )
        except stats_fetch.RequestLimitReached:
            break
        except Exception as error:
            print(f"fixture-validity-priority ids fetch failed: {error}")
            continue
        fixture_by_id = {
            stats_fetch.fixture_identity(fixture): fixture
            for fixture in fixtures
            if stats_fetch.fixture_identity(fixture) is not None
        }
        for fixture_id in id_chunk:
            fixture = fixture_by_id.get(fixture_id)
            if fixture is None:
                continue
            for req in by_id.get(fixture_id, []):
                row = base.explicit_disposition(req, fixture)
                if row is not None:
                    detected.append(row)
    return detected


def _scan_no_id_groups(
    api_key: str,
    groups: Sequence[Tuple[Tuple[int, str, str], List[live.SettlementRequirement]]],
    request_state: Dict[str, int],
    max_requests: int,
    budget: int,
) -> tuple[List[dict], List[str]]:
    detected: List[dict] = []
    checked: List[str] = []
    for (provider_id, season, league_code), rows in _rotating_groups(groups, budget):
        if request_state["count"] >= max_requests:
            break
        checked.append(f"{league_code}:{len(rows)}")
        try:
            detected.extend(
                base.resolve_no_id_group(
                    api_key,
                    rows,
                    provider_id,
                    season,
                    request_state,
                    max_requests,
                )
            )
        except stats_fetch.RequestLimitReached:
            break
        except Exception as error:
            print(
                "fixture-validity-priority league lookup failed "
                f"league={league_code}: {error}"
            )
    return detected, checked


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh canonical fixture validity with past-first bounded scheduling"
    )
    parser.add_argument("--max-requests", type=int, default=4)
    args = parser.parse_args()
    max_requests = max(0, min(4, args.max_requests))

    requirements = live.canonical_requirements()
    feed = base.load_json(base.FEED_PATH, {})
    if not isinstance(feed, dict):
        feed = {}
    existing = base.load_json(base.VALIDITY_PATH, {})
    if not isinstance(existing, dict):
        existing = {}

    candidates, today = _candidate_rows(requirements, feed, existing)
    past = [row for row in candidates if row.local_date < today.isoformat()]
    near_term = [row for row in candidates if row.local_date >= today.isoformat()]

    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    request_state = {"count": 0}
    detected: List[dict] = []
    checked_groups: List[str] = []

    if max_requests > 0 and api_key:
        quota_guard.install(stats_fetch)

        # Past unresolved rows have settlement priority. One league-season call can resolve every
        # stale no-id recommendation in that competition.
        past_no_id_groups = _group_no_id(past)
        past_ids_exist = any(row.api_fixture_id is not None for row in past)
        no_id_budget = max_requests - (1 if past_ids_exist else 0)
        no_id_detected, checked = _scan_no_id_groups(
            api_key,
            past_no_id_groups,
            request_state,
            max_requests,
            max(0, no_id_budget),
        )
        detected.extend(no_id_detected)
        checked_groups.extend(checked)

        # Reserve at most one request for old known fixture ids; one request batches 20 ids.
        if past_ids_exist and request_state["count"] < max_requests:
            detected.extend(
                _fetch_id_batches(
                    api_key,
                    past,
                    request_state,
                    min(max_requests, request_state["count"] + 1),
                    rotate=False,
                )
            )

        # If historical debt did not consume the cap, validate today's/upcoming no-id leagues and
        # then batched known ids. This catches reschedules before App-Ready publication without
        # increasing the validity API cap.
        remaining = max_requests - request_state["count"]
        if remaining > 0:
            near_no_id_groups = _group_no_id(near_term)
            near_id_exists = any(row.api_fixture_id is not None for row in near_term)
            near_no_id_budget = remaining
            if near_id_exists and remaining > 1:
                near_no_id_budget = remaining - 1
            near_detected, checked = _scan_no_id_groups(
                api_key,
                near_no_id_groups,
                request_state,
                max_requests,
                near_no_id_budget,
            )
            detected.extend(near_detected)
            checked_groups.extend(checked)

        if request_state["count"] < max_requests:
            detected.extend(
                _fetch_id_batches(
                    api_key,
                    near_term,
                    request_state,
                    max_requests,
                    rotate=True,
                )
            )

    dispositions = base.merge_dispositions(existing, detected)
    validity_changed = base.write_validity(dispositions)
    feed_changed = base.ensure_feed_dispositions(dispositions)
    manifest_changed = base.ensure_main_manifest_validity_artifact()

    print(
        "fixture-validity-priority "
        f"canonical={len(requirements)} candidates={len(candidates)} "
        f"past={len(past)} nearTerm={len(near_term)} "
        f"checkedGroups={','.join(checked_groups) or '-'} "
        f"detected={len(detected)} dispositions={len(dispositions)} "
        f"requests={request_state['count']} "
        f"validityChanged={validity_changed} feedChanged={feed_changed} "
        f"manifestChanged={manifest_changed} "
        f"quota={json.dumps(quota_guard.status()) if api_key else '{}'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
