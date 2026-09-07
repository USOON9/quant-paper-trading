"""Regression tests for pre-open generation and feature/model provenance."""

import json
from pathlib import Path
import tempfile
import unittest

import joblib
import numpy as np
import pandas as pd

from quantpaper.ml.features import FEATURE_COLUMNS, FEATURE_CONTRACT_VERSION
from quantpaper.ml.regime import REGIME_FEATURE_COLUMNS, CONTEXT_SYMBOLS
from quantpaper.shadow import generate_shadow_signals, file_sha256
from quantpaper.sessions import completed_bars


class FrozenPredictor:
    def predict_proba(self, frame):
        assert len(frame.columns) == 19
        return np.array([[0.4,0.6]] * len(frame))


class ShadowInferenceTests(unittest.TestCase):
    def fixture(self, folder):
        path=Path(folder)/"model.joblib"
        artifact={"model":FrozenPredictor(), "features":FEATURE_COLUMNS+REGIME_FEATURE_COLUMNS,
                  "feature_contract_version":FEATURE_CONTRACT_VERSION,
                  "symbols":["SPY","BTC-USD"], "trained_through":"2026-09-03",
                  "created_at":"2026-09-04T20:35:00Z"}
        joblib.dump(artifact,path)
        metadata={"feature_contract_version":FEATURE_CONTRACT_VERSION,"model_sha256":file_sha256(path),
                  "evaluation":{"approved_for_paper_signals":False}}
        dates=pd.date_range("2026-05-01","2026-09-04",freq="B",tz="UTC")
        close=100+np.arange(len(dates))*.1+np.sin(np.arange(len(dates)))
        frame=pd.DataFrame({"open":close-.1,"high":close+.5,"low":close-.5,"close":close,
                            "volume":1000+np.arange(len(dates))*10},index=dates)
        return path,metadata,frame,{symbol:frame.copy() for symbol in CONTEXT_SYMBOLS}

    def test_postclose_uses_today_and_declares_after_holiday_target(self):
        with tempfile.TemporaryDirectory() as folder:
            path,meta,frame,context=self.fixture(folder)
            result=generate_shadow_signals(path,meta,{"SPY":frame},context,5,
                                          pd.Timestamp("2026-09-04T21:15:00Z"))
            self.assertEqual(result[0].feature_as_of,"2026-09-04")
            self.assertEqual(result[0].target_session,"2026-09-08")
            self.assertEqual(len(json.loads(result[0].feature_payload_json)),19)
            self.assertFalse(result[0].model_approved)

    def test_later_bar_perturbation_cannot_change_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path,meta,frame,context=self.fixture(folder)
            now=pd.Timestamp("2026-09-04T21:15:00Z")
            a=generate_shadow_signals(path,meta,{"SPY":frame},context,5,now)[0]
            future=frame.iloc[[-1]].copy()*10
            future.index=pd.to_datetime(["2026-09-08"],utc=True)
            b=generate_shadow_signals(path,meta,{"SPY":pd.concat([frame,future])},
                                     {s:pd.concat([f,future]) for s,f in context.items()},5,now)[0]
            self.assertEqual(a.feature_hash,b.feature_hash)

    def test_unknown_contract_and_metadata_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path,meta,frame,context=self.fixture(folder)
            with self.assertRaisesRegex(RuntimeError,"SHA-256 mismatch"):
                generate_shadow_signals(path,{**meta,"model_sha256":"wrong"},{"SPY":frame},context,5)
            with self.assertRaisesRegex(RuntimeError,"obsolete"):
                generate_shadow_signals(path,{}, {"SPY":frame},context,5)

    def test_crypto_exact_midnight_target_is_not_claimed_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            path,meta,frame,context=self.fixture(folder)
            with self.assertRaisesRegex(ValueError,"delayed-entry"):
                generate_shadow_signals(path,meta,{"BTC-USD":frame},context,5,
                                        pd.Timestamp("2026-09-04T21:15:00Z"))

    def test_complete_bar_filter_honors_early_close_buffer(self):
        frame=pd.DataFrame({"close":[100]},index=pd.to_datetime(["2026-11-27"],utc=True))
        self.assertTrue(completed_bars(frame,"SPY","2026-11-27T18:29:00Z").empty)
        self.assertEqual(len(completed_bars(frame,"SPY","2026-11-27T18:31:00Z")),1)

    def test_stale_features_do_not_generate_retargeted_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            path,meta,frame,context=self.fixture(folder)
            with self.assertRaisesRegex(ValueError,"already opened"):
                generate_shadow_signals(path,meta,{"SPY":frame},context,5,
                                        pd.Timestamp("2026-09-08T15:00:00Z"))


if __name__=="__main__":
    unittest.main()
