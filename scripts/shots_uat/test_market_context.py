"""Research checks for leakage and actual numerical market influence."""
import unittest
import numpy as np
from .market_context import CONTEXT_FEATURES, forecast, select, validate_training


class ZeroResidual:
    def predict(self, pool, prediction_type=None):
        return np.zeros(pool.num_row())


def row(market=.5):
    return dict(fixture="fixture",metric="SHOTS",scope="TOTAL",team="",line=20.5,group=0,
                kickoff=1000000.,featureCutoff=800000.,observed=900000.,baseBefore=700000.,
                count_mean=.65,count_replicas=[.60,.65,.70],market=market,push=0.,label=1,
                odds={"OVER":1.9,"UNDER":1.9},guards={"OVER":True,"UNDER":True},
                bookmaker="book",sourceReceipt="receipt",x=[0.]*(len(CONTEXT_FEATURES)-4)+["SHOTS","TOTAL","TOTAL","league"])


class MarketContextContract(unittest.TestCase):
    def test_market_is_a_numerical_input_not_annotation(self):
        rs=[row(.2),row(.8)]
        predictions,_=forecast(rs,dict(parameters=[0.,0.,0.,0.,0.]),[ZeroResidual()]*3)
        self.assertTrue(np.allclose(predictions["context_mean"],[.2,.8]))
        self.assertTrue(np.allclose(predictions["linear"],[.2,.8]))
        self.assertEqual(predictions["count_mean"].tolist(),[.65,.65])

    def test_training_rejects_result_not_available_yet(self):
        r=row()
        validate_training([r],r["kickoff"]+86401)
        with self.assertRaises(ValueError):validate_training([r],r["kickoff"]+86399)
        r["baseBefore"]=r["featureCutoff"]+1
        with self.assertRaises(ValueError):validate_training([r],r["kickoff"]+86401)

    def test_lower_under_probability_uses_complement_of_highest_replica(self):
        r=row();r["count_replicas"]=[.3,.4,.5];r["count_mean"]=.4
        # OVER lower=.3; UNDER lower=.5, so neither meets the unchanged .55.
        self.assertEqual(select([r],"count_lower",np.array([.3]),np.array([[.3],[.4],[.5]])),[])

    def test_existing_favourite_guard_remains_binding(self):
        r=row();r["guards"]["OVER"]=False
        self.assertEqual(select([r],"linear",np.array([.8]),np.array([[.8]]*3)),[])


if __name__=="__main__":unittest.main()
