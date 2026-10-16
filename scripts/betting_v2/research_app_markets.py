"""Research-only exact contracts for existing app markets on one joint PMF.

No independent probability rescaling, new model, invented odds, player props,
handicaps, or cards. The original adapter can consume these prepared wrappers.
"""
import re
import numpy as np
from scipy.stats import binom
from betting_v2.joint_count import JointCount,outcome as legacy_outcome
from betting_v2.exact_forecast_adapter import prepare as legacy_prepare

LEGACY={'RESULT','DC','DNB','BTTS','TOTAL','TEAM'}
PERIOD_SCOPES={'HTFT','BOTH_HALVES'}

def side_mask(h,a,side):
    if side=='HOME':return h>a
    if side=='AWAY':return a>h
    if side=='DRAW':return h==a
    raise ValueError('Unknown result side')

def range_mask(value,text):
    low,high=str(text).split(':')
    low=int(low);high=float('inf') if high=='INF' else int(high)
    if low<0 or high<low:raise ValueError('Invalid count range')
    return (value>=low)&(value<=high)

class ResearchJoint:
    def __init__(self,joint,shares=None):
        self.joint=joint;self.shares=shares;self.cache={};self.period_cache={}
    @property
    def means(self):return self.joint.means
    @property
    def omitted_mass(self):return self.joint.omitted_mass
    def evaluate(self,spec):
        if spec[1] in LEGACY:return self.joint.evaluate(spec)
        key=tuple(spec)
        if key in self.cache:return self.cache[key]
        _,scope,side,team,line=spec;h,a=np.indices(self.joint.pmf.shape);mass=self.joint.pmf
        if scope=='EXACT':
            home,away=map(int,str(line).split(':'));mask=(h==home)&(a==away)
        elif scope=='RANGE':mask=range_mask(h if team=='HOME' else a if team=='AWAY' else h+a,line)
        elif scope=='MARGIN':
            if side=='NO_GOAL':mask=(h==0)&(a==0)
            elif side=='SCORE_DRAW':mask=(h==a)&(h>0)
            else:mask=side_mask(h,a,side)&range_mask(abs(h-a),line)
        elif scope=='PARITY':
            if side not in ('ODD','EVEN'):raise ValueError('Invalid parity')
            mask=(h+a)%2==(1 if side=='ODD' else 0)
        elif scope in ('BTTS_TOTAL','RESULT_TOTAL'):
            base,direction=side.rsplit('_',1)
            if direction not in ('OVER','UNDER') or not np.isfinite(float(line)):raise ValueError('Invalid combined total')
            # Integer combined totals need explicit bookmaker void rules.
            if float(line)%1!=.5:raise ValueError('Combined markets require a half-goal line')
            target=((h>0)&(a>0)) if scope=='BTTS_TOTAL' else side_mask(h,a,base)
            if scope=='BTTS_TOTAL':
                if base=='NO':target=~target
                elif base!='YES':raise ValueError('Invalid combined BTTS side')
            mask=target&((h+a>float(line)) if direction=='OVER' else (h+a<float(line)))
        elif scope=='HTFT':
            if self.shares is None:raise ValueError('Half allocation missing')
            first,full=side.split('_')
            if full not in self.period_cache:
                sh,sa=self.shares;nh,na=mass.shape
                bh=binom.pmf(np.arange(nh)[None,:],np.arange(nh)[:,None],sh)
                ba=binom.pmf(np.arange(na)[None,:],np.arange(na)[:,None],sa)
                self.period_cache[full]=bh.T@(mass*side_mask(h,a,full))@ba
            first_mass=self.period_cache[full];hh,aa=np.indices(first_mass.shape)
            value=float(first_mass[side_mask(hh,aa,first)].sum());self.cache[key]=(value,0.);return self.cache[key]
        elif scope=='BOTH_HALVES':
            if self.shares is None:raise ValueError('Half allocation missing')
            sh,sa=self.shares
            if team=='HOME':zero_first=(1-sh)**h;zero_second=sh**h;zero=(h==0)
            elif team=='AWAY':zero_first=(1-sa)**a;zero_second=sa**a;zero=(a==0)
            elif not team:zero_first=(1-sh)**h*(1-sa)**a;zero_second=sh**h*sa**a;zero=(h+a==0)
            else:raise ValueError('Invalid team')
            conditional=np.clip(1-zero_first-zero_second+zero,0,1)
            if side=='NO':conditional=1-conditional
            elif side!='YES':raise ValueError('Invalid both-halves side')
            value=float((mass*conditional).sum());self.cache[key]=(value,0.);return self.cache[key]
        else:raise ValueError('Unimplemented research market')
        value=float(mass[mask].sum());self.cache[key]=(value,0.);return self.cache[key]

def prepare_extended(row,profile):
    joint,conditions=legacy_prepare(row,profile)
    shares=row.get('periodShares')
    return ResearchJoint(joint,shares),{side:(ResearchJoint(c,shares) if c is not None else None,mass)
        for side,(c,mass) in conditions.items()}

def outcome_extended(home,away,spec,first=None):
    if spec[1] in LEGACY:return legacy_outcome(home,away,spec)
    if min(home,away)<0 or int(home)!=home or int(away)!=away:raise ValueError('Invalid count labels')
    if spec[1] in PERIOD_SCOPES:
        if first is None:raise ValueError('Half labels missing')
        fh,fa=first
        if min(fh,fa)<0 or fh>home or fa>away or int(fh)!=fh or int(fa)!=fa:raise ValueError('Invalid half labels')
        if spec[1]=='HTFT':
            h1,h2=spec[2].split('_');return float(side_mask(fh,fa,h1)&side_mask(home,away,h2)),0.
        team=spec[3];one=fh if team=='HOME' else fa if team=='AWAY' else fh+fa
        two=home-fh if team=='HOME' else away-fa if team=='AWAY' else home+away-fh-fa
        return float((one>0 and two>0)==(spec[2]=='YES')),0.
    mass=np.zeros((int(home)+1,int(away)+1));mass[int(home),int(away)]=1
    return ResearchJoint(JointCount(mass)).evaluate(spec)

def parsed_range(text):
    text=text.casefold().strip()
    match=re.fullmatch(r'(\d+)\s*(?:-|or)\s*(\d+)(?:\s*goals?)?',text)
    if match:return f'{int(match[1])}:{int(match[2])}'
    match=re.fullmatch(r'(over|under)\s+(\d+)(?:\s*goals?)?',text)
    if match:
        if match[1]=='under' and int(match[2])==0:return None
        return f'{int(match[2])+1}:INF' if match[1]=='over' else f'0:{int(match[2])-1}'
    match=re.search(r'(\d+)(\+)?\s*(?:goals?)?$',text)
    if match:return f'{int(match[1])}:'+('INF' if match[2] else str(int(match[1])))
    return None

def range_spec(metric,team,line):
    low,high=line.split(':');low=int(low)
    if low==0 and high!='INF':return (metric,'TEAM' if team else 'TOTAL','UNDER',team,int(high)+.5)
    if high=='INF' and low>0:return (metric,'TEAM' if team else 'TOTAL','OVER',team,low-.5)
    return (metric,'RANGE','IN',team,line)

def mapped_app_spec(offer,event,fallback):
    raw=re.sub(r'\s+',' ',offer['providerMarketName'].casefold()).strip();selection=str(offer['selection']).strip()
    # These are scorer props even when the outcome exposes an OVER field.
    if re.fullmatch(r'to score \d+\+ goals?',raw):return None
    period='GOALS_1H' if raw.endswith(' ht') or '1st half' in raw else 'GOALS_2H' if raw.endswith(' 2h') or '2nd half' in raw else 'GOALS_FT'
    if raw=='ml ht':
        side={'home':'HOME','draw':'DRAW','away':'AWAY','1':'HOME','x':'DRAW','2':'AWAY'}.get(selection.casefold())
        return (period,'RESULT',side,'',None) if side else None
    if raw in ('both teams to score ht','both teams to score 2h','draw no bet ht','draw no bet 2h'):
        scope='BTTS' if raw.startswith('both teams') else 'DNB'
        sides={'yes':'YES','no':'NO'} if scope=='BTTS' else {'home':'HOME','away':'AWAY','1':'HOME','2':'AWAY'}
        side=sides.get(selection.casefold())
        return (period,scope,side,'',None) if side else None
    if raw in ('clean sheet home','clean sheet away'):
        side={'yes':'UNDER','no':'OVER'}.get(selection.casefold())
        team='AWAY' if raw.endswith(' home') else 'HOME'
        return ('GOALS_FT','TEAM',side,team,.5) if side else None
    if raw in ('correct score','correct score ht'):
        match=re.fullmatch(r'(\d+)\s*[-:]\s*(\d+)',selection)
        return (period,'EXACT','SCORE','',f'{int(match[1])}:{int(match[2])}') if match else None
    if raw=='winning margin':
        if selection.casefold() in ('no goal','no goals'):return ('GOALS_FT','MARGIN','NO_GOAL','',None)
        if selection.casefold()=='score draw':return ('GOALS_FT','MARGIN','SCORE_DRAW','',None)
        match=re.fullmatch(r'(\d+)(\+)?\s*\(([12])\)',selection)
        if match:return ('GOALS_FT','MARGIN','HOME' if match[3]=='1' else 'AWAY','',f'{int(match[1])}:'+('INF' if match[2] else str(int(match[1]))))
        return None
    if raw in ('half time / full time','half time/full time'):
        sides={'home':'HOME','draw':'DRAW','away':'AWAY','1':'HOME','x':'DRAW','2':'AWAY'}
        parts=[sides.get(v.strip().casefold()) for v in selection.split('/')]
        return ('GOALS_FT','HTFT','_'.join(parts),'',None) if len(parts)==2 and all(parts) else None
    if raw in ('exact total goals','exact 1st half goals','exact 2nd half goals','number of goals in match','home team exact goals','away team exact goals','total corners'):
        line=parsed_range(selection)
        team='HOME' if raw.startswith('home team') else 'AWAY' if raw.startswith('away team') else ''
        metric='CORNERS' if raw=='total corners' else period
        return range_spec(metric,team,line) if line is not None else None
    if raw in ('odd / even goals','odd/even goals','odd even goals'):
        side=selection.upper();return ('GOALS_FT','PARITY',side,'',None) if side in ('ODD','EVEN') else None
    if raw in ('goal in both halves','team to score in both halves home','team to score in both halves away'):
        side=selection.upper();team='HOME' if raw.endswith(' home') else 'AWAY' if raw.endswith(' away') else ''
        return ('GOALS_FT','BOTH_HALVES',side,team,None) if side in ('YES','NO') else None
    return fallback(offer,event)
