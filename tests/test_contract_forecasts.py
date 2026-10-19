import unittest,sys,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.contract_forecasts import restore

class ContractForecastTests(unittest.TestCase):
    def fixture(self):
        from betting_v2.research_model_revision import read
        from betting_v2.research_probability_recalibration import ROOT,PRIOR
        return read(PRIOR/'history.json.gz'),ROOT
    def test_real_full_first_and_latest_writer_restore_with_saved_option_parity(self):
        history,root=self.fixture();truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};source=root/'reports/betting_v2/market_first_counts_20261018'
        from betting_v2.research_model_revision import read
        for packet in ('earliest','latest'):
            d=read(source/f'replay_checkpoint_{packet}.json.gz');rows,audit=restore(read(source/f'quotes_{packet}.json.gz'),d['proper'],truth,d['options'])
            self.assertEqual(len(rows),len(d['proper']));self.assertGreater(audit['savedEligibleProbabilitiesChecked'],500)
    def test_changed_order_identity_cannot_be_joined_silently(self):
        history,root=self.fixture();truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};source=root/'reports/betting_v2/market_first_counts_20261018'
        from betting_v2.research_model_revision import read
        d=read(source/'replay_checkpoint_earliest.json.gz');proper=copy.deepcopy(d['proper']);proper[0]['fixture']='changed'
        with self.assertRaises(ValueError):restore(read(source/'quotes_earliest.json.gz'),proper,truth,d['options'])

if __name__=='__main__':unittest.main()
