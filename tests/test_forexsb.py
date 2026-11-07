"""Decoder for forexsb.com / Dukascopy .lb.gz bar files."""
import gzip
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.data.forexsb import decode  # noqa: E402


def _file(rows, rec=7):
    return gzip.compress(np.array(rows, dtype="<i4").reshape(-1, rec).tobytes())


def test_decode_28_byte_records_with_spread():
    # 2026-10-27 21:55 UTC = minutes since 2000-01-01
    import pandas as pd
    m = (int(pd.Timestamp("2026-10-27 21:55", tz="UTC").timestamp()) - 946684800) // 60
    df = decode(_file([[m, 111273, 111297, 111258, 111258, 474, 16], [m + 5, 111258, 111260, 111250, 111255, 10, 17]]), "EURUSD")
    assert str(df.index[0]) == "2026-10-27 21:55:00+00:00"
    assert df.iloc[0][["bo", "bh", "bl", "bc"]].tolist() == [1.11273, 1.11297, 1.11258, 1.11258]
    assert df.iloc[1]["spread_pts"] == 17 and len(df) == 2


def test_decode_jpy_scale_and_24_byte_records():
    m = 13_000_000
    df = decode(_file([[m, 155123, 155200, 155100, 155150, 5], [m + 1, 155150, 155160, 155140, 155141, 6]], rec=6), "USDJPY")
    assert df.iloc[0]["bo"] == 155.123 and np.isnan(df.iloc[0]["spread_pts"])
