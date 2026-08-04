import datetime as dt, importlib.util, unittest
from pathlib import Path
P=Path(__file__).resolve().parents[1]/'scripts/refresh_greece_super_league_2.py'
spec=importlib.util.spec_from_file_location('g2',P); g2=importlib.util.module_from_spec(spec); spec.loader.exec_module(g2)
def catalog(name='Super League 2',country='Greece',ident=999):
 return {'league':{'id':ident,'name':name},'country':{'name':country},'seasons':[{'year':2025,'start':'2025-09-01','end':'2026-05-31','current':False},{'year':2026,'start':'2026-09-01','end':'2027-05-31','current':True}]}
def fixture(i,date,status='NS',home='Kalamata',away='Iraklis'):
 return {'fixture':{'id':i,'date':date,'status':{'short':status}},'teams':{'home':{'id':10,'name':home,'logo':'h.png'},'away':{'id':20,'name':away,'logo':'a.png'}},'goals':{'home':2,'away':1},'score':{'halftime':{'home':1,'away':0}}}
class TestG2(unittest.TestCase):
 def test_identity_is_resolved_not_hardcoded(self): self.assertEqual(999,(g2.resolve([catalog()])['league']['id']))
 def test_wrong_provider_identity_is_rejected(self):
  with self.assertRaises(RuntimeError): g2.resolve([catalog('2. Deild','Iceland',494)])
 def test_seasons(self):
  h,t=g2.seasons(catalog()); self.assertEqual((2025,2026),(h['year'],t['year']))
 def test_normalized_stats(self):
  raw=[{'team':{'id':10},'statistics':[{'type':'Corner Kicks','value':7},{'type':'Shots on Goal','value':5}]},{'team':{'id':20},'statistics':[{'type':'Corner Kicks','value':4},{'type':'Shots on Goal','value':3}]}]
  x=g2.normalized(raw,10,20); self.assertEqual((7.0,4.0,5.0,3.0),(x['HC'],x['AC'],x['HST'],x['AST']))
 def test_future_filter(self):
  rows=[fixture('1','2026-08-10T18:00:00+00:00'),fixture('2','2026-08-11T18:00:00+00:00','FT'),fixture('3','2027-01-10T18:00:00+00:00')]
  self.assertEqual(['1'],[g2.fid(x) for x in g2.future(rows,dt.date(2026,8,4),120)])
 def test_schedule_rows_have_no_fake_odds(self):
  x=g2.schedule_row(fixture('1','2026-08-10T18:00:00+00:00'),{'kalamata'}); self.assertEqual([],x['markets']); self.assertTrue(x['scheduleOnly']); self.assertTrue(x['usableForStats'])
if __name__=='__main__':unittest.main()
