"""BleCharger against a fake bleak client (no Bluetooth hardware needed)."""
import asyncio
import sys
import types

import pytest

from air8_backend import device, protocol as p


class FakeChar:
    def __init__(self, uuid, props):
        self.uuid, self.properties = uuid, props


class FakeServices:
    def __init__(self, chars):
        self._chars = {c.uuid: c for c in chars}

    def get_characteristic(self, uuid):
        return self._chars.get(uuid)

    def __iter__(self):
        return iter([])


def make_fake_client(props, respond):
    writes = []

    class FakeClient:
        def __init__(self, address, timeout=None, disconnected_callback=None):
            self.services = FakeServices([FakeChar(p.AF01_UUID, props), FakeChar(p.AF02_UUID, props)])
            self.is_connected = False
            self.name = "ISDT Air8"
            self._cb = None

        async def connect(self):
            self.is_connected = True

        async def disconnect(self):
            self.is_connected = False

        async def start_notify(self, uuid, cb):
            self._cb = cb

        async def write_gatt_char(self, uuid, data, response=None):
            writes.append((uuid, bytes(data), response))
            if respond and data[0] == 0x18:
                asyncio.get_running_loop().call_soon(self._cb, None, bytearray([0x19, 0x01, 0x00]))
            if respond and data[0] == 0xE0:
                asyncio.get_running_loop().call_soon(self._cb, None, bytearray([0xE1, 1, 0, 2, 3] + [0] * 8))

    return FakeClient, writes


@pytest.fixture
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(device, "BIND_TIMEOUT_S", 0.3)
    monkeypatch.setattr(device, "COMMAND_INTERVAL_S", 0.0)
    monkeypatch.setattr(device, "_config_dir", lambda: tmp_path)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(device.asyncio, "sleep", lambda s: real_sleep(0))


def run_connect(monkeypatch, props, respond):
    FakeClient, writes = make_fake_client(props, respond)
    monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakClient=FakeClient))
    bus = device.EventBus()
    q = bus.subscribe()
    ch = device.BleCharger(bus)

    async def go():
        await ch.connect("AA")
        ch.connected = False  # stop the poll loop
        await asyncio.sleep(0)
        logs = []
        while not q.empty():
            m = q.get_nowait()
            if m["type"] == "log":
                logs.append(m["data"])
        return logs

    return ch, writes, asyncio.run(go())


def test_write_with_response_when_only_write_supported(monkeypatch, fast):
    ch, writes, logs = run_connect(monkeypatch, ["read", "write", "notify"], respond=True)
    assert writes and all(resp is True for _, _, resp in writes)
    assert writes[0][1][0] == 0x18  # bind goes first
    assert ch.bound is True and ch.hw_info["fw_version"] == "2.3"
    assert not any("WARNING" in l for l in logs)


def test_write_without_response_when_supported(monkeypatch, fast):
    _, writes, _ = run_connect(monkeypatch, ["write-without-response", "write", "notify"], respond=True)
    assert all(resp is False for _, _, resp in writes)


def test_silent_charger_warns(monkeypatch, fast):
    ch, _, logs = run_connect(monkeypatch, ["write", "notify"], respond=False)
    assert ch.bound is None
    assert any("has not sent ANY data" in l for l in logs)


def test_auto_reconnect_after_drop(monkeypatch, fast):
    """A link that drops on its own is re-established; a user disconnect is not."""
    clients = []
    FakeClient, writes = make_fake_client(["write", "notify"], respond=True)

    class Tracking(FakeClient):
        def __init__(self, *a, disconnected_callback=None, **k):
            super().__init__(*a, **k)
            self._dc = disconnected_callback
            clients.append(self)

        async def disconnect(self):
            was = self.is_connected
            await super().disconnect()
            if was and self._dc:
                self._dc(self)

    monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakClient=Tracking))
    ch = device.BleCharger(device.EventBus())

    async def go():
        await ch.connect("AA")
        assert ch.connected and len(clients) == 1
        # charger drops the link
        clients[0].is_connected = False
        ch._on_disconnect(clients[0])
        for _ in range(200):
            await asyncio.sleep(0)
            if ch.connected and len(clients) == 2:
                break
        assert ch.connected and len(clients) == 2 and ch._client is clients[1]
        # user disconnect: no further reconnects
        await ch.disconnect()
        for _ in range(50):
            await asyncio.sleep(0)
        assert not ch.connected and len(clients) == 2

    asyncio.run(go())


def test_stall_watchdog_triggers_reconnect(monkeypatch, fast):
    monkeypatch.setattr(device, "STALL_TIMEOUT_S", -1)  # any silence counts as a stall
    monkeypatch.setattr(device, "POLL_PERIOD_S", 0)
    monkeypatch.setattr(device, "RESPONSE_TIMEOUT_S", 0.01)
    FakeClient, _ = make_fake_client(["write", "notify"], respond=True)
    made = []

    class Counting(FakeClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakClient=Counting))
    bus = device.EventBus()
    q = bus.subscribe()
    ch = device.BleCharger(bus)

    async def go():
        await ch.connect("AA")
        import time
        deadline = time.monotonic() + 3
        while len(made) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0)
        await ch.disconnect()
        logs = []
        while not q.empty():
            m = q.get_nowait()
            if m["type"] == "log":
                logs.append(m["data"])
        return logs

    logs = asyncio.run(go())
    assert len(made) >= 2
    assert any("Reconnecting" in l for l in logs)
