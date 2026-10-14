"""Shared competitive club graph and opponent-adjusted count likelihoods.

Real UEFA results connect domestic competitions. Prices never enter the fit.
Observed xG and form are lagged; first/second-half goals split the same FT basis.
"""
from collections import defaultdict
from .monotone_count import fit_monotone_count
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import unicodedata

def normalized(value):
    text = str(value or '').casefold().translate(str.maketrans({'ø':'o','ł':'l','đ':'d','ß':'ss'}))
    return ''.join(c for c in unicodedata.normalize('NFKD',text) if c.isalnum())

def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):
        return 0.0

class Identities:
    """Explicit country-scoped aliases; ambiguous global names are unresolved."""
    def __init__(self):
        self.parent = {}
        self.candidates = defaultdict(set)
        self.scope_country = {}
    def find(self,key):
        self.parent.setdefault(key,key)
        if self.parent[key] != key:
            self.parent[key] = self.find(self.parent[key])
        return self.parent[key]
    def add(self,country,canonical,synonyms=()):
        country,canonical = normalized(country),normalized(canonical)
        if not country or not canonical:
            return
        root = self.find(country+':'+canonical)
        for alias in {canonical,*(normalized(v) for v in synonyms)}:
            if not alias or len(alias)<4 and alias != canonical:
                continue
            key = country+':'+alias
            other = self.find(key)
            if other != root:
                self.parent[other] = root
            self.candidates[alias].add(key)
    def resolve(self,value,country=None):
        key = normalized(value)
        if not key:
            return None
        if country:
            return self.find(normalized(country)+':'+key)
        choices = {self.find(v) for v in self.candidates.get(key,())}
        return next(iter(choices)) if len(choices)==1 else None

def load_competitive_history(root,cutoff):
    root = Path(root)
    ids = Identities()
    index = json.loads((root/'data/statmaker/domestic_enriched/index.json').read_text())
    domestic = []
    for league in index.get('leagues',[]):
        path = root/str(league.get('output_path') or '')
        country,code = league.get('country'),str(league.get('league_code') or '').upper()
        if not path.is_file() or not country or not code:
            continue
        ids.scope_country[code] = country
        for raw in json.loads(path.read_text()).get('matches',[]):
            if raw.get('status')=='FT' and 0<timestamp(raw.get('date_utc'))<cutoff:
                domestic.append((country,code,raw))
                ids.add(country,raw.get('home_team'));ids.add(country,raw.get('away_team'))
    path = root/'mappings/domestic_team_aliases.json'
    if path.is_file():
        for code,teams in json.loads(path.read_text()).get('aliases',{}).items():
            if code in ids.scope_country:
                for canonical,aliases in teams.items():
                    ids.add(ids.scope_country[code],canonical,aliases)
    participants = []
    for filename in ('uefa_support_history.json','uefa_team_support_history.json'):
        path = root/'data/statmaker'/filename
        if path.is_file():
            participants.extend(json.loads(path.read_text()).get('participants',[]))
    for p in participants:
        ids.add(p.get('country'),p.get('canonicalTeam') or p.get('providerTeam'),
                [p.get('providerTeam'),*p.get('aliases',[])])
    path = root/'data/statmaker/uefa_current_stats.json'
    current = json.loads(path.read_text()) if path.is_file() else {}
    configs = []
    for filename in ('uefa_club_competitions.json','uefa_simulation_2026_27.json'):
        path = root/'config'/filename
        if path.is_file():
            configs.extend(json.loads(path.read_text()).get('competitions',[]))
    for config in configs:
        for alias,canonical in config.get('aliases',{}).items():
            target = ids.resolve(canonical)
            if target:
                ids.add(target.split(':',1)[0],canonical,[alias])
    path = root/'mappings/uefa_betting_team_aliases.json'
    if path.is_file():
        for aliases in json.loads(path.read_text()).get('aliases',{}).values():
            for alias,canonical in aliases.items():
                target = ids.resolve(canonical)
                if target:
                    ids.add(target.split(':',1)[0],canonical,[alias])
    for comp in current.get('competitions',[]):
        for f in comp.get('fixtures',[]):
            for side in ('home','away'):
                target = ids.resolve(f.get(side+'Team'))
                if target:
                    ids.add(target.split(':',1)[0],f.get(side+'Team'),[f.get('provider'+side.title()+'Team')])
    official,api_names,api_known = [],defaultdict(set),defaultdict(set)
    for path in sorted((root/'data/api_football/league_tables').glob('*_fixtures.json')):
        if not re.fullmatch(r'(CL|EL|UECL)_\d+_fixtures.json',path.name):
            continue
        for raw in json.loads(path.read_text()).get('response',[]):
            for side in ('home','away'):
                team = raw.get('teams',{}).get(side,{})
                if team.get('id') and team.get('name'):
                    api_names[normalized(team['name'])].add(str(team['id']))
                    known = ids.resolve(team['name'])
                    if known:
                        api_known[str(team['id'])].add(known)
            if (raw.get('fixture',{}).get('status',{}).get('short') in ('FT','AET','PEN')
                    and 0<timestamp(raw.get('fixture',{}).get('date'))<cutoff):
                official.append(raw)
    def resolved(value,country=None):
        known = ids.resolve(value,country)
        if known:
            return known
        choices = api_names.get(normalized(value),set())
        if len(choices)==1:
            provider = next(iter(choices))
            known = {ids.find(v) for v in api_known.get(provider,set())}
            return next(iter(known)) if len(known)==1 else 'api:'+provider
        return None
    merged = {}
    fields = dict(HS='homeShots',AS='awayShots',HST='homeShotsOnTarget',AST='awayShotsOnTarget',
        HC='homeCorners',AC='awayCorners',HY='homeYellowCards',AY='awayYellowCards',
        HR='homeRedCards',AR='awayRedCards',HF='homeFouls',AF='awayFouls',HxG='homeXg',AxG='awayXg')
    def add(row,priority):
        try:
            hg,ag = float(row['home_goals']),float(row['away_goals'])
            if not all(math.isfinite(v) and v>=0 and v.is_integer() for v in (hg,ag)):
                return
        except (KeyError,TypeError,ValueError):
            return
        if (not row.get('_home') or not row.get('_away') or row['_home']==row['_away']
                or not 0<row.get('_date',0)<cutoff):
            return
        key = (datetime.fromtimestamp(row['_date'],timezone.utc).date().isoformat(),row['_home'],row['_away'])
        previous = merged.get(key)
        if previous is None or priority>previous[0]:
            if previous and (previous[1]['home_goals'],previous[1]['away_goals'])==(hg,ag):
                row['normalized_stats'] = {**previous[1].get('normalized_stats',{}),**row.get('normalized_stats',{})}
            merged[key] = (priority,row)
    for country,code,raw in domestic:
        add(dict(raw,_home=resolved(raw['home_team'],country),_away=resolved(raw['away_team'],country),
            _date=timestamp(raw['date_utc']),_home_group=code,_away_group=code),4)
    def support(raw,country=None,code=None,date_key='date'):
        competition = normalized(raw.get('competition'))
        if any(v in competition for v in ('friendl','women','youth','u19','u21','u23','virtual')):
            return
        international = 'uefa' in competition
        context_country = None if international else country
        stats = dict(raw.get('normalizedStats') or {})
        for key,field in fields.items():
            if key not in stats and raw.get(field) is not None:
                stats[key] = raw[field]
        group = code if code and code!='UEFA_TEAM_SUPPORT' else normalized(country or '')+':'+competition
        add(dict(_home=resolved(raw.get('homeTeam'),context_country),_away=resolved(raw.get('awayTeam'),context_country),
            _date=timestamp(raw.get(date_key)),home_goals=raw.get('homeGoals'),away_goals=raw.get('awayGoals'),
            normalized_stats=stats,hthg=raw.get('homeHalfGoals',raw.get('homeHalfTimeGoals')),
            htag=raw.get('awayHalfGoals',raw.get('awayHalfTimeGoals')),
            _home_group=None if international else group,_away_group=None if international else group),1)
    for p in participants:
        for raw in p.get('matches',[]):
            support(raw,p.get('country'),p.get('leagueCode'))
    for comp in current.get('competitions',[]):
        for f in comp.get('fixtures',[]):
            if f.get('status')=='FT':
                support(dict(f,competition='UEFA '+str(comp.get('leagueCode'))),date_key='kickoff')
    for raw in official:
        def official_id(side):
            provider = str(raw['teams'][side]['id'])
            known = {ids.find(v) for v in api_known.get(provider,set())}
            return next(iter(known)) if len(known)==1 else 'api:'+provider
        full = raw.get('score',{}).get('fulltime') or {}
        if raw['fixture']['status']['short']=='FT' and any(full.get(s) is None for s in ('home','away')):
            full = raw.get('goals') or {}
        half = raw.get('score',{}).get('halftime') or {}
        add(dict(_home=official_id('home'),_away=official_id('away'),_date=timestamp(raw['fixture']['date']),
            home_goals=full.get('home'),away_goals=full.get('away'),hthg=half.get('home'),htag=half.get('away'),
            normalized_stats={},_home_group=None,_away_group=None),3)
    rows = sorted((v[1] for v in merged.values()),key=lambda v:(v['_date'],v['_home'],v['_away']))
    groups = {}
    for row in rows:
        for side in ('home','away'):
            team,group = row['_'+side],row.get('_'+side+'_group')
            if group:
                groups[team] = group
            else:
                row['_'+side+'_group'] = groups.get(team,'unmapped:'+team)
    bindings = {}
    for code,country in ids.scope_country.items():
        bindings[code] = {k:ids.resolve(k,country) for k in ids.candidates if normalized(country)+':'+k in ids.parent}
    global_bindings = {k:resolved(k) for k in set(ids.candidates)|set(api_names)}
    for code in ('CL','EL','UECL'):
        bindings[code] = {k:v for k,v in global_bindings.items() if v}
    return rows,bindings

def mean(values):
    return sum(values)/len(values) if values else 0.0

class OpponentMatchup:
    """Symmetric count fit with club/league attack and opponent defence."""
    def __init__(self,matches,cutoff,metric_values):
        import numpy as np
        from scipy.sparse import csr_matrix
        from sklearn.linear_model import PoissonRegressor,LogisticRegression
        self.history = defaultdict(lambda:defaultdict(list))
        self.elo = defaultdict(lambda:1500.0)
        self.xg = defaultdict(list)
        self.groups,self.models,self.columns,self.feature_scales = {},{},{},{}
        self.train = defaultdict(list)
        self.period_train,self.period_model,self.matches = [],None,0
        metrics = ('GOALS_FT','GOALS_1H','GOALS_2H','CORNERS','SHOTS','SOT','YELLOW_CARDS','CARDS','FOULS')
        for row in matches:
            if row['_date']>=cutoff:
                break
            h,a = row['_home'],row['_away']
            self.groups[h],self.groups[a] = row['_home_group'],row['_away_group']
            targets = {m:metric_values(row,m) for m in metrics}
            for metric,target in targets.items():
                if target is not None:
                    for team,opp,venue,value in ((h,a,True,target[0]),(a,h,False,target[1])):
                        self.train[metric].append((self.features(team,opp,metric,venue),value))
            full,half = targets['GOALS_FT'],targets['GOALS_1H']
            if full is not None and half is not None:
                for team,opp,venue,total,first in ((h,a,True,full[0],half[0]),(a,h,False,full[1],half[1])):
                    if total>0 and 0<=first<=total:
                        self.period_train.append((self.features(team,opp,'GOALS_FT',venue),first,total-first))
            if full is not None:
                expected = 1/(1+10**(-(self.elo[h]-self.elo[a]+65)/400))
                actual = 1 if full[0]>full[1] else 0 if full[0]<full[1] else 0.5
                change = 20*(actual-expected)
                self.elo[h]+=change;self.elo[a]-=change
            for metric,target in targets.items():
                if target is not None:
                    self.history[h][metric].append((target[0],target[1],True))
                    self.history[a][metric].append((target[1],target[0],False))
            stats = row.get('normalized_stats') or {}
            try:
                hx,ax = float(stats['HxG']),float(stats['AxG'])
                if all(math.isfinite(v) and v>=0 for v in (hx,ax)):
                    self.xg[h].append((hx,ax));self.xg[a].append((ax,hx))
            except (KeyError,TypeError,ValueError):
                pass
            self.matches+=1
        def matrix(features):
            keys = sorted({key for values in features for key in values})
            columns = {key:i for i,key in enumerate(keys)}
            rr,cc,vv = [],[],[]
            for i,values in enumerate(features):
                for key,value in values.items():
                    if value:
                        rr.append(i);cc.append(columns[key]);vv.append(value)
            x = csr_matrix((vv,(rr,cc)),shape=(len(features),len(keys)))
            average = np.asarray(x.mean(axis=0)).ravel()
            variance = np.asarray(x.power(2).mean(axis=0)).ravel()-average**2
            scales = np.asarray([max(math.sqrt(max(0,variance[i])),1e-4) if ':' not in key else 1.0 for i,key in enumerate(keys)])
            return x.multiply(1/scales).tocsr(),columns,scales
        for metric in ['GOALS_FT']+[m for m in self.train if m!='GOALS_FT']:
            if metric in ('GOALS_1H','GOALS_2H'):
                continue
            records = self.train.get(metric,[])
            if not records:
                continue
            if metric not in ('GOALS_FT','GOALS_1H','GOALS_2H'):
                records = [(self.count_strength_features(f),v) for f,v in records]
            x,columns,scales = matrix([f for f,_ in records])
            self.columns[metric],self.feature_scales[metric] = columns,scales
            self.models[metric] = fit_monotone_count(x,np.asarray([v for _,v in records]),columns,metric,2.0/len(records))
        if self.period_train:
            records = []
            for f,first,second in self.period_train:
                values = self.count_strength_features(f)
                if first>0:
                    records.append((values,1,first))
                if second>0:
                    records.append((values,0,second))
            if len({label for _,label,_ in records})==2:
                x,self.period_columns,self.period_scales = matrix([f for f,_,_ in records])
                self.period_model = LogisticRegression(C=0.5,solver='newton-cholesky',max_iter=80,tol=1e-6).fit(
                    x,[label for _,label,_ in records],sample_weight=[w for _,_,w in records])
    def features(self,team,opponent,metric,home):
        t,o = self.history[team][metric],self.history[opponent][metric]
        tx,ox = self.xg[team],self.xg[opponent]
        values = {'attack:'+team:1.0,'defence:'+opponent:1.0,
            'league_attack:'+self.groups.get(team,'unmapped:'+team):1.0,
            'league_defence:'+self.groups.get(opponent,'unmapped:'+opponent):1.0,
            'home':float(home),'elo_gap':(self.elo[team]-self.elo[opponent])/400,
            'recent_for':mean([v[0] for v in t[-8:]]),'recent_opponent_against':mean([v[1] for v in o[-8:]]),
            'recent_for_observed':float(bool(t)),'recent_opponent_observed':float(bool(o)),
            'xg_for':mean([v[0] for v in tx[-8:]]),'opponent_xg_against':mean([v[1] for v in ox[-8:]]),
            'xg_for_observed':float(bool(tx)),'opponent_xg_observed':float(bool(ox))}
        tv,ov = [v for v in t if v[2]==home][-8:],[v for v in o if v[2]!=home][-8:]
        values.update(venue_for=mean([v[0] for v in tv]),venue_opponent_against=mean([v[1] for v in ov]),
            venue_for_observed=float(bool(tv)),venue_opponent_observed=float(bool(ov)))
        return values
    def count_strength_features(self,features):
        """Joint pre-cutoff goal strength transfers opponent quality to other counts."""
        values = dict(features)
        fit,columns = self.models.get('GOALS_FT'),self.columns.get('GOALS_FT',{})
        def effect(key):
            return float(fit.coef_[columns[key]]) if fit is not None and key in columns else 0.0
        team = next(k.removeprefix('attack:') for k in values if k.startswith('attack:'))
        opp = next(k.removeprefix('defence:') for k in values if k.startswith('defence:'))
        gt = next(k.removeprefix('league_attack:') for k in values if k.startswith('league_attack:'))
        go = next(k.removeprefix('league_defence:') for k in values if k.startswith('league_defence:'))
        values['goal_strength_gap'] = (effect('attack:'+team)-effect('defence:'+team)
            +effect('league_attack:'+gt)-effect('league_defence:'+gt)
            -effect('attack:'+opp)+effect('defence:'+opp)-effect('league_attack:'+go)+effect('league_defence:'+go))
        values.pop('league_attack:'+gt);values.pop('league_defence:'+go)
        # League corner/shot tempo is shared; it cannot act as league strength.
        values['league_tempo:'+gt] = 1.0
        values['league_tempo:'+go] = values.get('league_tempo:'+go,0.0)+1.0
        return values
    def predict(self,home,away,metric):
        import numpy as np
        from scipy.sparse import csr_matrix
        if metric in ('GOALS_1H','GOALS_2H'):
            if self.period_model is None or not self.history[home][metric] or not self.history[away][metric]:
                return None
            full = self.predict(home,away,'GOALS_FT')
            if full is None:
                return None
            values = []
            for team,opp,venue in ((home,away,True),(away,home,False)):
                f = self.count_strength_features(self.features(team,opp,'GOALS_FT',venue))
                row = np.zeros(len(self.period_columns))
                for k,v in f.items():
                    if k in self.period_columns:
                        row[self.period_columns[k]]=v
                values.append(row/self.period_scales)
            shares = self.period_model.predict_proba(csr_matrix(values))[:,1]
            if metric=='GOALS_2H':
                shares = 1-shares
            return float(full[0]*shares[0]),float(full[1]*shares[1]),len(self.history[home][metric]),len(self.history[away][metric]),len(self.period_train)//2
        fit = self.models.get(metric)
        if fit is None or not self.history[home][metric] or not self.history[away][metric]:
            return None
        columns,values = self.columns[metric],[]
        for team,opp,venue in ((home,away,True),(away,home,False)):
            f = self.features(team,opp,metric,venue)
            if metric not in ('GOALS_FT','GOALS_1H','GOALS_2H'):
                f = self.count_strength_features(f)
            if 'attack:'+team not in columns or 'defence:'+opp not in columns:
                return None
            row = np.zeros(len(columns))
            for k,v in f.items():
                if k in columns:
                    row[columns[k]]=v
            values.append(row/self.feature_scales[metric])
        hr,ar = fit.predict(csr_matrix(values))
        ceiling = {'GOALS_FT':12,'GOALS_1H':8,'GOALS_2H':8,'CORNERS':32,'SHOTS':65,'SOT':28,
            'YELLOW_CARDS':16,'CARDS':18,'FOULS':55}[metric]
        if any(not math.isfinite(v) or not 0<v<=ceiling for v in (hr,ar)):
            return None
        return float(hr),float(ar),len(self.history[home][metric]),len(self.history[away][metric]),len(self.train[metric])//2
    def context(self,home,away,metric):
        fit,columns = self.models.get(metric),self.columns.get(metric,{})
        def effect(key):
            return float(fit.coef_[columns[key]]) if fit is not None and key in columns else None
        return dict(homeIdentity=home,awayIdentity=away,homeElo=self.elo[home],awayElo=self.elo[away],
            homeGroup=self.groups.get(home),awayGroup=self.groups.get(away),
            homeLeagueAttack=effect('league_attack:'+self.groups.get(home,'')),
            awayLeagueAttack=effect('league_attack:'+self.groups.get(away,'')),
            homeLeagueDefence=effect('league_defence:'+self.groups.get(home,'')),
            awayLeagueDefence=effect('league_defence:'+self.groups.get(away,'')),
            homeFeatures=self.features(home,away,metric,True),awayFeatures=self.features(away,home,metric,False))
