import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "reference"))

import golden_vectors as gv  # noqa: E402

from scud_relay import protocol as rp  # noqa: E402


def test_encode_stat_v1_matches_golden():
    assert rp.encode_stat_v1(device_id=1, relays=[1, 0, 0, 0], status=1,
                             signal=20, alarm=0) == gv.STAT_V1["bytes"]


def test_encode_stat_v2_norelay_matches_golden():
    assert rp.encode_stat_v2(device_id=1, door=1, power=1, battery=85, net_type=1,
                             relay_en=0) == gv.STAT_V2_NORELAY["bytes"]
    assert len(gv.STAT_V2_NORELAY["bytes"]) == 28


def test_encode_stat_v2_relay_matches_golden():
    assert rp.encode_stat_v2(device_id=1, door=2, power=0, battery=50, net_type=2,
                             relay_en=1, relays=[1, 1, 0, 0], status=1, signal=31) == gv.STAT_V2_RELAY["bytes"]
    assert len(gv.STAT_V2_RELAY["bytes"]) == 52


def test_encode_rep_matches_golden():
    assert rp.encode_rep(device_id=1, cmd_id=7, result=1) == gv.REP_SUCCESS["bytes"]


def test_encode_photo_matches_golden():
    v = gv.PHOTO_SINGLE
    assert rp.encode_photo(device_id=v["device_id"], event_id=v["event_id"],
                           photos_cnt=v["photos_cnt"], photo_id=v["photo_id"],
                           body=v["body"]) == v["bytes"]
    # 20-byte header (5×uint32 LE) + body — matches backend decode_photo contract.
    assert len(v["bytes"]) == 20 + len(v["body"])


def test_decode_com_matches_golden():
    com = rp.decode_com(gv.COM_LOCK_OPEN["bytes"])
    assert com.cmd_id == 7 and com.command == 5 and com.data == 0
