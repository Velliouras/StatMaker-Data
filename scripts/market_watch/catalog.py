"""The app's 37 non-handicap market types, with explicit probability contracts."""
import re

COUNT_NAMES={'CORNERS':'CORNERS','SHOTS':'SHOTS','SOT':'SHOTS_ON_TARGET','FOULS':'FOULS',
             'CARDS':'CARDS','YELLOW_CARDS':'YELLOW_CARDS'}
GOAL_NAMES={('GOALS_FT','RESULT'):'1X2',('GOALS_FT','DC'):'DOUBLE_CHANCE',('GOALS_FT','DNB'):'DRAW_NO_BET',
 ('GOALS_1H','RESULT'):'HALF_TIME_1X2',('GOALS_1H','DC'):'HALF_TIME_DOUBLE_CHANCE',('GOALS_FT','BTTS'):'BTTS',
 ('GOALS_FT','TOTAL'):'MATCH_GOALS',('GOALS_FT','TEAM'):'TEAM_GOALS',
 ('GOALS_1H','TOTAL'):'FIRST_HALF_GOALS',('GOALS_1H','TEAM'):'TEAM_FIRST_HALF_GOALS',
 ('GOALS_2H','TOTAL'):'SECOND_HALF_GOALS',('GOALS_2H','TEAM'):'TEAM_SECOND_HALF_GOALS',
 ('GOALS_FT','EXACT'):'CORRECT_SCORE',('GOALS_FT','MARGIN'):'WINNING_MARGIN',('GOALS_FT','HTFT'):'HALF_TIME_FULL_TIME',
 ('GOALS_FT','PARITY'):'ODD_EVEN_GOALS',('GOALS_FT','BTTS_TOTAL'):'BTTS_OVER_UNDER',('GOALS_FT','RESULT_TOTAL'):'RESULT_OVER_UNDER'}

def app_market(spec):
    metric,scope,side,team,line=spec
    if metric=='GOALS_FT' and scope=='BOTH_HALVES':return 'TEAM_SCORE_BOTH_HALVES' if team else 'GOAL_IN_BOTH_HALVES'
    if metric=='GOALS_FT' and scope=='RANGE':return 'GOAL_BANDS'
    if (metric,scope) in GOAL_NAMES:return GOAL_NAMES[metric,scope]
    if metric=='RED_CARDS' and scope=='TOTAL' and line==.5:return 'RED_CARD'
    if metric in COUNT_NAMES and scope in ('TOTAL','TEAM'):return ('MATCH_' if scope=='TOTAL' else 'TEAM_')+COUNT_NAMES[metric]
    if metric in ('CORNERS','SHOTS','SOT') and scope=='RESULT':return 'MOST_'+COUNT_NAMES[metric]
    return None

def mapped_spec(offer,event):
    from betting_v2.research_scope_expansion import expanded_spec
    from betting_v2.research_app_markets import parsed_range
    name=re.sub(r'\s+',' ',offer['providerMarketName'].strip().casefold());selection=str(offer['selection']).strip()
    # Never let the older generic classifier turn a most/combination/card prop into a total.
    if any(t in name for t in ('handicap','asian','spread','player','scorer')):return None
    result_sides={'home':'HOME','away':'AWAY','draw':'DRAW','1':'HOME','2':'AWAY','x':'DRAW'}
    if name in ('most corners','most shots','most shots on target'):
        if offer.get('line') not in (None,0,0.):return None
        metric={'most corners':'CORNERS','most shots':'SHOTS','most shots on target':'SOT'}[name]
        side=result_sides.get(selection.casefold())
        return (metric,'RESULT',side,'',None) if side else None
    if 'card' in name or 'booking' in name:
        if any(t in name for t in ('half',' ht',' 1h',' 2h','first','minute','points','player')):return None
        if name in ('red card','red card yes/no','red card yes no'):
            return ('RED_CARDS','TOTAL','OVER' if selection.upper()=='YES' else 'UNDER','',.5) if selection.upper() in ('YES','NO') else None
        metric='YELLOW_CARDS' if 'yellow' in name else 'CARDS'
        team='HOME' if name.endswith(' home') else 'AWAY' if name.endswith(' away') else ''
        if 'team' in name and not team:return None
        side=selection.upper();line=offer.get('line')
        if side not in ('OVER','UNDER') or line is None:return None
        return (metric,'TEAM' if team else 'TOTAL',side,team,float(line))
    if any(t in name for t in (' + ',' and ',' & ')):
        scope='BTTS_TOTAL' if 'both teams' in name or 'btts' in name else 'RESULT_TOTAL' if 'result' in name else None
        if scope is None:return None
        parts=re.fullmatch(r'(home|draw|away|1|x|2|yes|no)\s*(?:/|&|and|\+)\s*(over|under)(?:\s+(\d+(?:\.\d+)?))?',selection,re.I)
        if not parts:return None
        side=result_sides.get(parts[1].casefold(),parts[1].upper());direction=parts[2].upper()
        line=float(parts[3]) if parts[3] else offer.get('line')
        if line is None or float(line)%1!=.5:return None
        if scope=='BTTS_TOTAL' and side not in ('YES','NO') or scope=='RESULT_TOTAL' and side not in ('HOME','DRAW','AWAY'):return None
        return ('GOALS_FT',scope,side+'_'+direction,'',float(line))
    spec=expanded_spec(offer,event)
    return spec if spec is not None and app_market(spec) is not None else None

def settlement(row,spec):
    """Return only comparable labels. Generic booking units never use HY+HR."""
    from betting_v2.reliable_counts import metric_values
    from betting_v2.research_app_markets import outcome_extended,PERIOD_SCOPES
    if 'CARD' in spec[0]:return None
    values=metric_values(row,spec[0])
    if values is None:return None
    first=metric_values(row,'GOALS_1H') if spec[1] in PERIOD_SCOPES else None
    if spec[1] in PERIOD_SCOPES and first is None:return None
    return outcome_extended(*values,spec,first=first)

def families(spec):
    metric,scope=spec[:2]
    if scope=='RESULT_TOTAL':return {'RESULT','GOALS'}
    if metric.startswith('GOALS'):return {'RESULT'} if scope in ('RESULT','DC','DNB','EXACT','MARGIN','HTFT') else {'GOALS'}
    return {'CARDS'} if 'CARD' in metric else {metric}

def selection_text(spec):
    metric,scope,side,team,line=spec
    if metric=='RED_CARDS':return 'Yes' if side=='OVER' else 'No'
    if scope in ('TOTAL','TEAM'):return f'{side.title()} {line:g}'
    if scope=='RESULT':return {'HOME':'1','DRAW':'X','AWAY':'2'}[side]
    if scope=='DC':return {'HOME_OR_DRAW':'1X','AWAY_OR_DRAW':'X2','HOME_OR_AWAY':'12'}[side]
    if scope=='DNB':return '1' if side=='HOME' else '2'
    if scope=='EXACT':return str(line).replace(':','-')
    if scope=='HTFT':return '/'.join({'HOME':'1','DRAW':'X','AWAY':'2'}[s] for s in side.split('_'))
    if scope in ('BTTS_TOTAL','RESULT_TOTAL'):return side.replace('_',' ')+' '+str(line)
    if scope=='MARGIN':return side+' '+str(line or '')
    if scope=='RANGE':return str(line)
    return side.title()
