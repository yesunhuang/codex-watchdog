import asyncio
from dataclasses import replace
import json
import time

import pytest
from websockets.asyncio.server import serve

from codex_watchdog._vendor.napcat_sdk.connection import Connection
from codex_watchdog.onebot_transport import OneBotApi, OneBotConfig, OneBotConnection, OneBotError, check_configuration, connect_once
from codex_watchdog.onebot_setup import pair_onebot


def config(url):
    return OneBotConfig(url, "wire-fixture-token", "12345", "group", "67890", ("54321",))


async def response(ws, request, data):
    await ws.send(json.dumps(dict(status="ok", retcode=0, data=data, echo=request["echo"])))


def test_read_only_backend_check_validates_identity_and_never_sends():
    async def exercise():
        calls = []
        async def backend(ws):
            async for raw in ws:
                request = json.loads(raw)
                calls.append(request["action"])
                await response(ws, request, {"user_id": 12345})
        async with serve(backend, "127.0.0.1", 0) as server:
            cfg = config("ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1]))
            offline = check_configuration(cfg)
            assert offline["error"] is None and not offline["backend_verified"] and not calls
            live = await asyncio.to_thread(check_configuration, cfg, connect=True)
            assert live["backend_verified"] and live["error"] is None
            failed = await asyncio.to_thread(check_configuration, replace(cfg, self_id="99999"), connect=True)
            assert failed["error"] == "onebot_backend_identity_mismatch" and not failed["backend_verified"]
            assert calls == ["get_login_info", "get_login_info"]
            assert cfg.ws_url not in json.dumps(live) and cfg.access_token not in json.dumps(live)
    asyncio.run(exercise())


def test_actual_websocket_header_auth_identity_and_send_helpers():
    async def exercise():
        calls = []
        async def backend(ws):
            assert ws.request.headers["Authorization"] == "Bearer wire-fixture-token"
            async for raw in ws:
                request = json.loads(raw)
                calls.append(request)
                await response(ws, request, {"user_id": 12345} if request["action"] == "get_login_info" else {"message_id": -200})
        async with serve(backend, "127.0.0.1", 0) as server:
            url = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            result = await asyncio.to_thread(OneBotApi(config(url)).send, "literal [CQ:at,qq=all] text", "operation", reply_to="-100")
            assert result == {"chat_id": "group:67890", "message_id": "-200"}
            assert [call["action"] for call in calls] == ["get_login_info", "send_msg"]
            assert calls[1]["params"] == {"message_type": "group", "group_id": 67890, "message": [
                {"type": "reply", "data": {"id": "-100"}},
                {"type": "text", "data": {"text": "literal [CQ:at,qq=all] text"}}]}
            calls.clear()
            private = replace(config(url), chat_type="private", chat_id="54321")
            await asyncio.to_thread(OneBotApi(private).send, "private text", "other-operation")
            assert calls[1]["params"]["user_id"] == 54321 and "group_id" not in calls[1]["params"]
    asyncio.run(exercise())


def test_upstream_echo_correlation_out_of_order_and_bad_input():
    async def exercise():
        async def backend(ws):
            login = json.loads(await ws.recv())
            await response(ws, login, {"user_id": 12345})
            first, second = [json.loads(await ws.recv()) for _ in range(2)]
            await ws.send("not-json")
            await ws.send(json.dumps({"echo": "unknown", "data": {"ignored": True}}))
            await response(ws, second, {"name": second["action"]})
            await response(ws, first, {"name": first["action"]})
            await ws.wait_closed()
        async with serve(backend, "127.0.0.1", 0) as server:
            url = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            async with connect_once(url, "fixture", "12345") as (connection, _):
                first, second = await asyncio.gather(connection.send({"action": "first"}), connection.send({"action": "second"}))
                assert first["data"] == {"name": "first"}
                assert second["data"] == {"name": "second"}
                assert not connection._futures
    asyncio.run(exercise())


def test_backend_identity_mismatch_prevents_send():
    async def exercise():
        calls = []
        async def backend(ws):
            async for raw in ws:
                request = json.loads(raw)
                calls.append(request["action"])
                await response(ws, request, {"user_id": 99999})
        async with serve(backend, "127.0.0.1", 0) as server:
            cfg = config("ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1]))
            with pytest.raises(OneBotError, match="identity_mismatch"):
                await asyncio.to_thread(OneBotApi(cfg).send, "must not send", "operation")
            assert calls == ["get_login_info"]
    asyncio.run(exercise())


def test_send_timeout_is_not_retried_by_transport():
    async def exercise():
        calls = []
        async def backend(ws):
            async for raw in ws:
                request = json.loads(raw)
                calls.append(request["action"])
                if request["action"] == "get_login_info":
                    await response(ws, request, {"user_id": 12345})
                # Deliberately lose the send response after the backend got it.
        async with serve(backend, "127.0.0.1", 0) as server:
            cfg = config("ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1]))
            with pytest.raises(OneBotError, match="outcome_uncertain"):
                await asyncio.to_thread(OneBotApi(cfg, timeout=0.05).send, "one attempt", "operation")
            assert calls == ["get_login_info", "send_msg"]
    asyncio.run(exercise())


def test_observer_reconnects_and_revalidates_identity(tmp_path):
    async def exercise():
        connects, received = [], []
        async def backend(ws):
            index = len(connects)
            login = json.loads(await ws.recv())
            connects.append(login["action"])
            await response(ws, login, {"user_id": 12345})
            await asyncio.sleep(0.05)
            await ws.send(json.dumps(dict(post_type="message", message_id=index + 1)))
            if index == 0:
                await ws.close()
            else:
                await ws.wait_closed()
        async with serve(backend, "127.0.0.1", 0) as server:
            cfg = config("ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1]))
            listener = OneBotConnection(cfg, received.append, timeout=1, runtime=tmp_path)
            listener.start()
            try:
                for _ in range(150):
                    if len(received) >= 2:
                        break
                    await asyncio.sleep(0.02)
                assert [value["message_id"] for value in received] == [1, 2]
                assert connects == ["get_login_info", "get_login_info"]
                health = json.loads((tmp_path / "onebot" / cfg.scope / "health.json").read_text())
                assert health["status"] == "connected"
            finally:
                await asyncio.to_thread(listener.close)
            assert json.loads((tmp_path / "onebot" / cfg.scope / "health.json").read_text())["status"] == "stopped"
    asyncio.run(exercise())


def test_connection_cleanup_fails_pending_action_without_replaying():
    async def exercise():
        async def backend(ws):
            login = json.loads(await ws.recv())
            await response(ws, login, {"user_id": 12345})
            await ws.recv()
            await ws.close()
        async with serve(backend, "127.0.0.1", 0) as server:
            cfg = config("ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1]))
            with pytest.raises(OneBotError, match="outcome_uncertain"):
                await asyncio.to_thread(OneBotApi(cfg).send, "uncertain", "operation")
    asyncio.run(exercise())


def test_pairing_learns_exact_human_and_conversation_from_fresh_nonce():
    async def exercise():
        confirmation = asyncio.Queue()
        loop = asyncio.get_running_loop()
        def output(text):
            if text.startswith("PAIR_CODEX_ONEBOT_"):
                loop.call_soon_threadsafe(confirmation.put_nowait, text)
        async def backend(ws):
            login = json.loads(await ws.recv())
            await response(ws, login, {"user_id": 12345})
            code = await confirmation.get()
            def event(**overrides):
                base = dict(post_type="message", message_type="group", self_id=12345, user_id=54321,
                            group_id=67890, message_id=201, time=int(time.time()), sender={"user_id": 54321},
                            message=[{"type": "text", "data": {"text": code}}])
                base.update(overrides)
                return base
            for value in (event(time=1), event(self_id=99999), event()):
                await ws.send(json.dumps(value))
            await ws.wait_closed()
        async with serve(backend, "127.0.0.1", 0) as server:
            url = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            result = await asyncio.to_thread(pair_onebot, url, "fixture", output=output, timeout=2)
            assert result == dict(schema_version=1, protocol_version=11, ws_url=url, self_id="12345",
                                  chat_type="group", chat_id="67890", allowed_user_ids=["54321"])
    asyncio.run(exercise())


def test_upstream_event_queue_stays_bounded_and_echo_survives_pressure():
    async def exercise():
        async def backend(ws):
            login = json.loads(await ws.recv())
            await response(ws, login, {"user_id": 12345})
            request = json.loads(await ws.recv())
            for index in range(300):
                await ws.send(json.dumps({"post_type": "message", "message_id": index}))
            await response(ws, request, {"alive": True})
            await ws.wait_closed()
        async with serve(backend, "127.0.0.1", 0) as server:
            url = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            async with connect_once(url, "fixture", "12345") as (connection, _):
                iterator = connection.events()
                pending = asyncio.create_task(iterator.__anext__())
                await asyncio.sleep(0)
                result = await connection.send({"action": "get_status"})
                assert result["data"] == {"alive": True}
                assert all(queue.qsize() <= 32 for queue in connection._event_queues)
                await pending
                await iterator.aclose()
    asyncio.run(exercise())
