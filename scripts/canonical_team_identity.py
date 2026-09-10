#!/usr/bin/env python3
"""Canonical football-team identity resolution shared by StatMaker ingestion/audit.

The resolver is intentionally deterministic and fail-closed:
- verified provider aliases win over configured aliases;
- exact configured aliases win over inferred/current-name matches;
- harmless punctuation/diacritic/club-suffix differences are normalized;
- a provider label may use unique league-local token containment only when it points
  to exactly one canonical club;
- a multi-word provider label may resolve to a unique single-word canonical club, but
  the unsafe reverse direction (for example Paris -> Paris Saint Germain) is forbidden;
- fuzzy edit-distance matching is diagnostic only and never enters production data.

No provider call is made here. Domestic canonical universes are built only from the
already checked-out API-Football fixture caches, current roster artifact and explicit
alias registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple

# These are organisational decorations, not football identity. Do NOT remove semantic
# club words such as Real, Atletico, Sporting, Racing, Deportivo, Sociedad, Royal, etc.
CLUB_DECORATION_TOKENS = {
    "fc", "fk", "cf", "sc", "ac", "afc", "pfc", "bk", "if", "sk", "nk"
}
GENERIC_PREFIX_TOKENS = {
    "club", "clube", "football", "fotbal", "fotboll", "association", "asociacion"
}
GENERIC_CONTAINMENT_TOKENS = CLUB_DECORATION_TOKENS | GENERIC_PREFIX_TOKENS | {
    "de", "da", "do", "dos", "das", "the"
}

# Alias sources are deliberately ordered. VERIFIED_TEAM_ALIASES is the compatibility
# layer already checked against the provider/current API-Football identity, so it must
# be able to disambiguate stale historical aliases without deleting those aliases.
BASE_IDENTITY_PRIORITY = 0
CONFIGURED_ALIAS_PRIORITY = 100
VERIFIED_ALIAS_PRIORITY = 200


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("ß", "ss").replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def identity_tokens(value: Any) -> Tuple[str, ...]:
    tokens = [token for token in normalize_text(value).split() if token]
    tokens = [token for token in tokens if token not in CLUB_DECORATION_TOKENS]
    while len(tokens) > 1 and tokens[0] in GENERIC_PREFIX_TOKENS:
        tokens.pop(0)
    while len(tokens) > 1 and len(tokens[-1]) == 4 and tokens[-1].isdigit():
        tokens.pop()
    return tuple(tokens)


def identity_key(value: Any) -> str:
    return " ".join(identity_tokens(value))


def exact_identity_key(value: Any) -> str:
    return normalize_text(value)


def equivalent(left: Any, right: Any) -> bool:
    a = identity_key(left)
    b = identity_key(right)
    return bool(a and b and a == b)


def _meaningful(tokens: Iterable[str]) -> Set[str]:
    return {
        token for token in tokens
        if token not in GENERIC_CONTAINMENT_TOKENS and (len(token) >= 3 or token.isdigit())
    }


@dataclass
class CanonicalIdentityIndex:
    scope: str
    canonical_names: Set[str] = field(default_factory=set)
    _owners: Dict[str, Set[str]] = field(default_factory=dict)
    _claim_priorities: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def _add_claim(self, key: str, canonical: str, priority: int) -> None:
        if not key:
            return
        self._owners.setdefault(key, set()).add(canonical)
        priorities = self._claim_priorities.setdefault(key, {})
        priorities[canonical] = max(int(priority), priorities.get(canonical, BASE_IDENTITY_PRIORITY))

    def add_canonical(self, canonical: Any) -> None:
        name = str(canonical or "").strip()
        if not name:
            return
        self.canonical_names.add(name)
        for key in {exact_identity_key(name), identity_key(name)}:
            self._add_claim(key, name, BASE_IDENTITY_PRIORITY)

    def add_alias(
        self,
        alias: Any,
        canonical: Any,
        *,
        priority: int = CONFIGURED_ALIAS_PRIORITY,
    ) -> None:
        canonical_name = str(canonical or "").strip()
        alias_name = str(alias or "").strip()
        if not canonical_name or not alias_name:
            return
        self.add_canonical(canonical_name)
        for key in {exact_identity_key(alias_name), identity_key(alias_name)}:
            self._add_claim(key, canonical_name, priority)

    def resolve(self, provider_name: Any) -> Tuple[Optional[str], str, Sequence[str]]:
        raw = str(provider_name or "").strip()
        if not raw:
            return None, "blank", []

        direct_claims: Dict[str, int] = {}
        for key in {exact_identity_key(raw), identity_key(raw)}:
            for owner in self._owners.get(key, set()):
                priority = self._claim_priorities.get(key, {}).get(owner, BASE_IDENTITY_PRIORITY)
                direct_claims[owner] = max(priority, direct_claims.get(owner, BASE_IDENTITY_PRIORITY))
        if direct_claims:
            best_priority = max(direct_claims.values())
            direct_owners = sorted(
                owner for owner, priority in direct_claims.items() if priority == best_priority
            )
            if len(direct_owners) == 1:
                method = (
                    "exact-verified"
                    if best_priority >= VERIFIED_ALIAS_PRIORITY
                    else "exact-configured"
                    if best_priority >= CONFIGURED_ALIAS_PRIORITY
                    else "exact"
                )
                return direct_owners[0], method, direct_owners
            return None, "ambiguous-exact", direct_owners

        provider_tokens = set(identity_tokens(raw))
        provider_meaningful = _meaningful(provider_tokens)
        if not provider_tokens or not provider_meaningful:
            return None, "unmatched", []

        candidates: list[Tuple[Tuple[int, int, int], str]] = []
        for canonical in sorted(self.canonical_names):
            canonical_tokens = set(identity_tokens(canonical))
            if not canonical_tokens:
                continue
            canonical_meaningful = _meaningful(canonical_tokens)
            shared = provider_meaningful.intersection(canonical_meaningful)
            if not shared:
                continue

            # Direction matters. A short provider label must never infer a longer club
            # identity ("Paris" -> "Paris Saint Germain"). The reverse is useful and
            # safe when league-local uniqueness holds ("Tottenham Hotspur" -> "Tottenham",
            # "Bolton Wanderers" -> "Bolton").
            if len(provider_meaningful) == 1 and len(canonical_meaningful) > 1:
                continue
            if not (provider_tokens.issubset(canonical_tokens) or canonical_tokens.issubset(provider_tokens)):
                continue

            # More shared meaningful identity and fewer extra tokens rank higher.
            union = provider_tokens | canonical_tokens
            rank = (len(shared), -len(union - (provider_tokens & canonical_tokens)), len(canonical_tokens))
            candidates.append((rank, canonical))

        if not candidates:
            return None, "unmatched", []
        candidates.sort(reverse=True)
        best_rank = candidates[0][0]
        best = sorted({canonical for rank, canonical in candidates if rank == best_rank})
        if len(best) == 1:
            return best[0], "unique-containment", best
        return None, "ambiguous-containment", best


def configured_index(
    scope: str,
    canonical_names: Sequence[Any],
    aliases: Mapping[Any, Any],
) -> CanonicalIdentityIndex:
    index = CanonicalIdentityIndex(scope=scope)
    for canonical in canonical_names:
        index.add_canonical(canonical)
    for alias, canonical in aliases.items():
        index.add_alias(alias, canonical, priority=CONFIGURED_ALIAS_PRIORITY)
    return index


def domestic_indexes(
    odds_module: Any,
    pipeline_module: Any,
    registry: Sequence[Dict[str, Any]],
) -> Dict[str, CanonicalIdentityIndex]:
    raw_alias_root = pipeline_module.load_json(getattr(odds_module, "ALIASES_PATH"), {})
    raw_aliases = raw_alias_root.get("aliases", {}) if isinstance(raw_alias_root, dict) else {}
    roster_path = getattr(pipeline_module, "ROSTER_PATH", None)
    roster_root = pipeline_module.load_json(roster_path, {}) if roster_path is not None else {}
    roster_rows = roster_root.get("leagues", []) if isinstance(roster_root, dict) else []
    roster_by_key = {
        (
            str(row.get("leagueCode") or "").strip().upper(),
            str(row.get("appSeason") or "").strip(),
        ): [str(name).strip() for name in row.get("teams", []) or [] if str(name).strip()]
        for row in roster_rows
        if isinstance(row, dict)
    }

    indexes: Dict[str, CanonicalIdentityIndex] = {}
    for league in registry:
        code = str(league.get("leagueCode") or "").strip().upper()
        if not code:
            continue
        index = indexes.setdefault(code, CanonicalIdentityIndex(scope=code))

        league_aliases = raw_aliases.get(code, {}) if isinstance(raw_aliases, dict) else {}
        if isinstance(league_aliases, dict):
            for canonical, aliases in league_aliases.items():
                index.add_canonical(canonical)
                for alias in aliases or []:
                    index.add_alias(alias, canonical, priority=CONFIGURED_ALIAS_PRIORITY)

        cache = pipeline_module.load_json(pipeline_module.stats_fetch.cache_path_for(league), {})
        for fixture in cache.get("fixtures", []) if isinstance(cache, dict) else []:
            if not isinstance(fixture, dict):
                continue
            index.add_canonical(fixture.get("home_team"))
            index.add_canonical(fixture.get("away_team"))

        target_season = str(
            league.get("targetAppSeason") or league.get("app_season") or ""
        ).strip()
        for canonical in roster_by_key.get((code, target_season), []):
            index.add_canonical(canonical)

    # Preserve already-verified provider aliases from the shared expansion. These are
    # intentionally stronger than the historical alias registry because they bridge
    # known provider/current API-Football naming differences (for example Greek clubs).
    try:
        import domestic_odds_expansion as expansion
        verified = getattr(expansion, "VERIFIED_TEAM_ALIASES", {})
    except Exception:
        verified = {}
    for code, teams in verified.items() if isinstance(verified, dict) else []:
        index = indexes.setdefault(str(code).upper(), CanonicalIdentityIndex(scope=str(code).upper()))
        for canonical, aliases in teams.items():
            index.add_canonical(canonical)
            for alias in aliases or []:
                index.add_alias(alias, canonical, priority=VERIFIED_ALIAS_PRIORITY)

    return indexes


def install_domestic(odds_module: Any, pipeline_module: Any) -> Dict[str, CanonicalIdentityIndex]:
    registry_root = pipeline_module.load_json(pipeline_module.REGISTRY_PATH, {})
    registry = registry_root.get("leagues", []) if isinstance(registry_root, dict) else []
    indexes = domestic_indexes(odds_module, pipeline_module, registry)

    def canonical_team_info(
        name: str,
        league_code: str,
        aliases: Dict[str, Dict[str, str]],  # retained for call-site compatibility
        debug: Dict[str, Any],
    ) -> Tuple[str, Optional[str]]:
        del aliases
        code = str(league_code or "").strip().upper()
        index = indexes.get(code)
        if index is None:
            odds_module.record_unmatched_team(debug, code, str(name or "").strip(), identity_key(name))
            return str(name or "").strip(), None
        canonical, method, candidates = index.resolve(name)
        if canonical is None:
            odds_module.record_unmatched_team(debug, code, str(name or "").strip(), identity_key(name))
            if method.startswith("ambiguous"):
                debug.setdefault("ambiguousTeamMappings", []).append({
                    "leagueCode": code,
                    "providerTeam": str(name or "").strip(),
                    "candidates": list(candidates),
                    "policy": method,
                })
            return str(name or "").strip(), None
        debug.setdefault("canonicalTeamMappings", []).append({
            "leagueCode": code,
            "providerTeam": str(name or "").strip(),
            "canonicalTeam": canonical,
            "policy": method,
        })
        return canonical, canonical

    odds_module.canonical_team_info = canonical_team_info
    odds_module._statmaker_canonical_team_identity_installed = True
    return indexes


def runtime_key_matches_payload(match_key: Any, payload: Mapping[str, Any]) -> bool:
    """Validate a runtime/UI date|home|away key against the same match payload.

    This deliberately does NOT compare the runtime key with prepared_matches.match_key;
    those are different identities by contract.
    """
    raw = str(match_key or "").strip()
    parts = raw.split("|", 2)
    if len(parts) != 3:
        return False
    key_date, key_home, key_away = parts
    payload_date = str(payload.get("date") or "").strip()[:10]
    return (
        key_date[:10] == payload_date
        and equivalent(key_home, payload.get("homeTeam"))
        and equivalent(key_away, payload.get("awayTeam"))
    )