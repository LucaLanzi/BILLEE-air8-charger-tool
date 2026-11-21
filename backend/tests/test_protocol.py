import struct

import pytest
from fastapi.testclient import TestClient

from air8_backend import limits, protocol as p
from air8_backend.server import create_app


def test_work_task_packet_layout():
    t = p.WorkTask(task_type=0, battery_type=p.BATTERY_TYPES["LiPo"], current_ma=5000,
                   cells=6, cutoff_mv=4200, capacity_limit_mah=0)
    b = p.work_task_req(t)
    assert len(b) == 17
    assert b[:6] == bytes([0x13, 0xEA, 0x00, 0x00, 0x01, 0x00])
    assert struct.unpack_from("<I", b, 6)[0] == 5000
    assert b[10] == 6
    assert struct.unpack_from("<H", b, 11)[0] == 4200
    assert struct.unpack_from("<I", b, 13)[0] == 0


def test_bind_req():
    cid = bytes(range(16))
    b = p.bind_req(cid)
    assert b[0] == 0x18 and b[1:17] == cid and b[17:] == b"\x00\x00"
    with pytest.raises(ValueError):
        p.bind_req(b"short")


def test_parse_hw_info():
    b = bytes([0xE1, 1, 2, 3, 4]) + struct.pack("<Q", 0x1122334455667788)
    hw = p.parse_hw_info(b)
    assert hw.hw_version == "1.2" and hw.fw_version == "3.4" and hw.device_id == 0x1122334455667788


def test_parse_electric_long_format():
    cells = [4100, 4105, 4098, 4102, 4101, 4099] + [0] * 10
    b = bytes([0x31, 0xE5, 0x00]) + struct.pack("<IIII", 24000, 3100, 24600, 5000) + struct.pack("<16H", *cells)
    e = p.parse_electric(b)
    assert e.input_mv == 24000 and e.input_ma == 3100 and e.output_mv == 24600 and e.charge_ma == 5000
    assert e.cell_mv == cells[:6]


def test_parse_electric_short_format():
    b = bytes([0x31, 0xE5, 0x00]) + struct.pack("<HIHI", 12000, 900, 8400, 2000) + struct.pack("<2H", 4200, 4199)
    e = p.parse_electric(b)
    assert (e.input_mv, e.input_ma, e.output_mv, e.charge_ma) == (12000, 900, 8400, 2000)
    assert e.cell_mv == [4200, 4199]


def test_parse_work_state_full_and_truncated():
    body = struct.pack("<BBIII", 3, 42, 1234, 20000, 60000)  # state, pct, mAh, mWh, ms
    body += bytes([1, 6, 0]) + struct.pack("<HI", 4200, 5000) + b"\x00" * 4
    body += struct.pack("<HIH", 11000, 500000, 0)
    b = bytes([0x31, 0xE7, 0x00]) + body
    ws = p.parse_work_state(b)
    assert ws.state == 3 and ws.state_label == "working" and ws.progress_pct == 42
    assert ws.capacity_mah == 1234 and ws.battery_name == "LiPo" and ws.cells == 6
    assert ws.cutoff_mv == 4200 and ws.current_ma == 5000 and ws.error_code == 0

    short = p.parse_work_state(b[:8])
    assert short.state == 3 and short.capacity_mah is None


def test_parse_ir_and_ack_and_dispatch():
    ir = p.parse_ir(bytes([0x31, 0xFB, 0]) + struct.pack("<3H", 25, 0, 10001))
    assert ir["cell_mohm"] == [2.5]
    assert p.parse_task_ack(bytes([0x31, 0xEB, 0, 0])).ok
    assert not p.parse_task_ack(bytes([0x31, 0xEB, 0, 0xFF])).ok
    assert p.parse_any(bytes([0x19, 0x01, 0x00])) == ("bind", {"ok": True})
    assert p.parse_any(b"\x00\x01") is None


def test_air8_advertisement():
    assert p.is_air8_advertisement({0xABBA: bytes([0xAF, 0xFA, 1, 3, 0, 0, 9])})
    assert not p.is_air8_advertisement({0xABBA: bytes([0xAF, 0xFA, 1, 0xF, 0, 0xA8])})


# ---- limits ---------------------------------------------------------------
def job(**kw):
    base = dict(task="charge", chemistry="LiPo", cells=6, current_ma=5000)
    base.update(kw)
    return limits.JobRequest(**base)


def test_defaults_applied():
    t, _ = limits.validate(job())
    assert t.cutoff_mv == 4200 and t.battery_type == 1
    t, _ = limits.validate(job(task="storage"))
    assert t.cutoff_mv == 3850 and t.task_type == 2


@pytest.mark.parametrize("kw", [
    dict(cutoff_mv=4300),                 # over LiPo max
    dict(chemistry="LiFe", cutoff_mv=4200),
    dict(cells=9),
    dict(current_ma=25000),
    dict(current_ma=10),
    dict(chemistry="NiMH", task="storage"),
    dict(task="destroy"),                 # needs confirmation
    dict(chemistry="Unobtainium"),
])
def test_rejects_unsafe(kw):
    with pytest.raises(limits.JobError):
        limits.validate(job(**kw))


def test_power_warning():
    _, warnings = limits.validate(job(cells=8, current_ma=20000))
    assert any("500 W" in w for w in warnings)


def test_nimh_delta_peak():
    t, _ = limits.validate(job(chemistry="NiMH", cells=12, cutoff_mv=5, current_ma=2000))
    assert t.cutoff_mv == 5 and t.battery_type == 5


# ---- API with the simulator --------------------------------------------------
def test_api_sim_flow():
    with TestClient(create_app(simulate=True)) as c:
        assert c.get("/api/health").json()["simulated"]
        devs = c.get("/api/scan").json()
        assert devs[0]["is_air8"]
        assert c.post("/api/job/start", json=dict(task="charge", chemistry="LiPo", cells=6,
                                                  current_ma=5000)).status_code == 409
        assert c.post("/api/connect", json={"address": devs[0]["address"]}).json()["connected"]
        r = c.post("/api/job/validate", json=dict(task="charge", chemistry="LiPo", cells=6, current_ma=5000))
        assert r.json()["packet"].startswith("13 ea 00 00 01 00")
        bad = c.post("/api/job/start", json=dict(task="charge", chemistry="LiPo", cells=6,
                                                 current_ma=5000, cutoff_mv=4500))
        assert bad.status_code == 422
        ok = c.post("/api/job/start", json=dict(task="charge", chemistry="LiPo", cells=6, current_ma=5000))
        assert ok.json()["ack"]["ok"]
        assert c.post("/api/raw", json={"hex": "12 e4 00"}).status_code == 403
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "status"
