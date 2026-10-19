"""Chronological features; an outcome becomes usable 24h after kickoff."""
import math
import unicodedata
from collections import defaultdict, deque

LAG = 86400
NUMERIC = ["home", "elo_gap", "rest", "opponent_rest", "sample", "opponent_sample"]
for prefix in ("own", "opponent"):
    for window in (8, 20):
        for metric in ("shots_for", "shots_against", "sot_for", "sot_against", "goals_for", "goals_against", "xg_for", "xg_against"):
            NUMERIC.append(f"{prefix}_{window}_{metric}")
    NUMERIC += [f"{prefix}_venue_shots_for", f"{prefix}_venue_shots_against"]
FEATURES = NUMERIC + ["league"]


def normalized(value):
    value = str(value or "").casefold().translate(str.maketrans({"ø": "o", "ł": "l", "đ": "d", "ß": "ss"}))
    return "".join(c for c in unicodedata.normalize("NFKD", value) if c.isalnum())


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None


def counts(row):
    s = row.get("normalized_stats") or {}
    hs, ass, ht, at = [number(s.get(k)) for k in ("HS", "AS", "HST", "AST")]
    if any(v is None or not v.is_integer() for v in (hs, ass, ht, at)):
        return None
    if ht > hs or at > ass:
        return None
    return hs, ass, ht, at


class History:
    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: float(r["_date"]))
        self.index = 0
        self.clock = float("-inf")
        self.teams = defaultdict(lambda: deque(maxlen=60))
        self.elo = defaultdict(lambda: 1500.)

    def advance(self, cutoff):
        if cutoff < self.clock:
            raise ValueError("Feature clock cannot go backwards")
        self.clock = cutoff
        while self.index < len(self.rows) and float(self.rows[self.index]["_date"]) + LAG <= cutoff:
            r = self.rows[self.index]
            self.index += 1
            h, a = r["_home"], r["_away"]
            c, s = counts(r), r.get("normalized_stats") or {}
            gh, ga = number(r.get("home_goals")), number(r.get("away_goals"))
            if gh is not None and ga is not None:
                expectation = 1 / (1 + 10 ** ((self.elo[a] - self.elo[h] - 60) / 400))
                change = 20 * ((1 if gh > ga else .5 if gh == ga else 0) - expectation)
                self.elo[h] += change
                self.elo[a] -= change
            if c is None:
                continue
            hs, ass, ht, at = c
            xh, xa = number(s.get("HxG")), number(s.get("AxG"))
            for team, home, values in ((h, 1, (hs, ass, ht, at, gh, ga, xh, xa)),
                                       (a, 0, (ass, hs, at, ht, ga, gh, xa, xh))):
                self.teams[team].append(dict(time=float(r["_date"]), home=home, values=values))

    def feature(self, team, opponent, home, league, kickoff):
        own, other = list(self.teams[team]), list(self.teams[opponent])
        out = [float(home), self.elo[team] - self.elo[opponent] + (60 if home else -60),
               min(60., (kickoff - own[-1]["time"]) / LAG) if own else 60.,
               min(60., (kickoff - other[-1]["time"]) / LAG) if other else 60., len(own), len(other)]
        for rows, venue in ((own, home), (other, 1 - home)):
            for window in (8, 20):
                for i in range(8):
                    values = [r["values"][i] for r in rows[-window:] if r["values"][i] is not None]
                    out.append(sum(values) / len(values) if values else float("nan"))
            vr = [r for r in rows if r["home"] == venue][-10:]
            out += [sum(r["values"][i] for r in vr) / len(vr) if vr else float("nan") for i in (0, 1)]
        return out + [str(league or "unknown")]

    def pair(self, h, a, league, kickoff):
        return [self.feature(h, a, 1, league, kickoff), self.feature(a, h, 0, league, kickoff)]


def training_frame(rows):
    history = History(rows)
    x, shots, sot, times = [], [], [], []
    for r in history.rows:
        ko = float(r["_date"])
        history.advance(ko)
        c = counts(r)
        if c is None:
            continue
        pair = history.pair(r["_home"], r["_away"], r.get("_home_group"), ko)
        if min(pair[0][4:6]) < 8:
            continue
        hs, ass, ht, at = c
        for features, total, target in zip(pair, (hs, ass), (ht, at)):
            x.append(features); shots.append(total); sot.append(target); times.append(ko)
    return x, shots, sot, times
