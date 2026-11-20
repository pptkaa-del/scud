import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "reference"))

import golden_vectors as gv  # noqa: E402

from scud_relay import protocol as rp  # noqa: E402


def test_emulator_stat_bytes_match_all_golden():
    assert rp.encode_stat_v1(1, [1, 0, 0, 0], 1, 20, 0) == gv.STAT_V1["bytes"]
    assert rp.encode_stat_v2(1, 1, 1, 85, 1, 0) == gv.STAT_V2_NORELAY["bytes"]
    assert rp.encode_stat_v2(1, 2, 0, 50, 2, 1, [1, 1, 0, 0], 1, 31) == gv.STAT_V2_RELAY["bytes"]


def test_emulator_decodes_backend_com_vectors():
    # bytes a backend MqttTransport would publish (golden) must decode correctly here
    com = rp.decode_com(gv.COM_LOCK_OPEN["bytes"])
    assert (com.cmd_id, com.command, com.data) == (7, 5, 0)
    com2 = rp.decode_com(gv.COM_RELAY_ON_2["bytes"])
    assert (com2.cmd_id, com2.command, com2.data) == (1, 1, 2)
