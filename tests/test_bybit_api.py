import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch

import requests

from api.bybit_api import (
    BybitAPI,
    BybitAPIError,
    BybitAmbiguousWriteError,
    InstrumentRules,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, json_error=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = {}
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class RecordingSession:
    def __init__(
        self,
        *,
        post_error=None,
        post_payload=None,
        get_payload=None,
        json_error=None,
    ):
        self.headers = {}
        self.post_error = post_error
        self.post_payload = (
            {"retCode": 0, "retMsg": "OK", "result": {}}
            if post_payload is None
            else post_payload
        )
        self.get_payload = get_payload
        self.json_error = json_error
        self.post_count = 0
        self.last_body = None

    def post(self, url, headers, data, timeout):
        del url, headers, timeout
        self.post_count += 1
        self.last_body = data
        if self.post_error:
            raise self.post_error
        return FakeResponse(self.post_payload, json_error=self.json_error)

    def get(self, *args, **kwargs):
        del args, kwargs
        if self.get_payload is None:
            raise AssertionError("Unexpected GET")
        return FakeResponse(self.get_payload, json_error=self.json_error)

    def close(self):
        pass


class BybitClientTests(unittest.TestCase):
    def test_signature_uses_official_field_order(self):
        client = BybitAPI("key", "secret", dry_run=True)
        timestamp = "1700000000000"
        payload = "category=linear&symbol=BTCUSDT"
        expected = hmac.new(
            b"secret",
            f"{timestamp}key{client.recv_window}{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(client._sign_v5(timestamp, payload), expected)

    def test_write_timeout_is_not_blindly_retried(self):
        session = RecordingSession(post_error=requests.Timeout("lost response"))
        client = BybitAPI(
            "key",
            "secret",
            dry_run=False,
            session=session,
        )
        client._last_time_sync = time.monotonic()
        with self.assertRaises(BybitAmbiguousWriteError) as raised:
            client.create_order(
                "BTCUSDT",
                "Buy",
                "Market",
                "0.01",
                order_link_id="stable-id",
            )
        self.assertEqual(session.post_count, 1)
        self.assertEqual(raised.exception.order_link_id, "stable-id")

    def test_trading_stop_sends_full_paired_mode(self):
        session = RecordingSession()
        client = BybitAPI("key", "secret", dry_run=False, session=session)
        client._last_time_sync = time.monotonic()
        client.set_trading_stop(
            "BTCUSDT",
            0,
            take_profit="110",
            stop_loss="95",
        )
        body = json.loads(session.last_body)
        self.assertEqual(body["tpslMode"], "Full")
        self.assertEqual(body["tpOrderType"], "Market")
        self.assertEqual(body["slOrderType"], "Market")
        self.assertIn("takeProfit", body)
        self.assertIn("stopLoss", body)

    def test_missing_ret_code_after_write_is_ambiguous(self):
        session = RecordingSession(post_payload={})
        client = BybitAPI("key", "secret", dry_run=False, session=session)
        client._last_time_sync = time.monotonic()
        with self.assertRaises(BybitAmbiguousWriteError) as raised:
            client.create_order(
                "BTCUSDT",
                "Buy",
                "Market",
                "0.01",
                order_link_id="stable-id",
            )
        self.assertEqual(raised.exception.order_link_id, "stable-id")
        self.assertEqual(session.post_count, 1)

    def test_non_json_write_response_is_ambiguous(self):
        session = RecordingSession(json_error=ValueError("not json"))
        client = BybitAPI("key", "secret", dry_run=False, session=session)
        client._last_time_sync = time.monotonic()
        with self.assertRaises(BybitAmbiguousWriteError):
            client.create_order(
                "BTCUSDT",
                "Buy",
                "Market",
                "0.01",
                order_link_id="stable-id",
            )

    def test_non_object_write_result_is_ambiguous(self):
        session = RecordingSession(
            post_payload={"retCode": 0, "retMsg": "OK", "result": []}
        )
        client = BybitAPI("key", "secret", dry_run=False, session=session)
        client._last_time_sync = time.monotonic()
        with self.assertRaises(BybitAmbiguousWriteError):
            client.create_order(
                "BTCUSDT",
                "Buy",
                "Market",
                "0.01",
                order_link_id="stable-id",
            )

    def test_non_object_successful_read_result_is_typed_error(self):
        session = RecordingSession(
            get_payload={"retCode": 0, "retMsg": "OK", "result": []}
        )
        client = BybitAPI("key", "secret", dry_run=False, session=session)
        client._last_time_sync = time.monotonic()
        with self.assertRaises(BybitAPIError):
            client._private_request(
                "GET",
                "/v5/order/realtime",
                params={"category": "linear", "symbol": "BTCUSDT"},
            )

    def test_non_object_successful_public_result_is_typed_error(self):
        session = RecordingSession(
            get_payload={"retCode": 0, "retMsg": "OK", "result": None}
        )
        client = BybitAPI("", "", dry_run=True, session=session)
        with self.assertRaises(BybitAPIError):
            client.get_tickers("BTCUSDT")

    def test_positions_require_explicit_list(self):
        client = BybitAPI("key", "secret", dry_run=False)
        with patch.object(
            client,
            "_private_request",
            return_value={"retCode": 0, "result": {}},
        ):
            with self.assertRaises(BybitAPIError):
                client.get_positions()
        client.close()

    def test_repeated_pagination_cursor_fails_closed(self):
        client = BybitAPI("key", "secret", dry_run=False)
        page = {
            "retCode": 0,
            "result": {
                "list": [],
                "nextPageCursor": "same-cursor",
            },
        }
        with patch.object(client, "_private_request", side_effect=[page, page]):
            with self.assertRaisesRegex(BybitAPIError, "pagination cursor"):
                client.get_open_orders()
        client.close()

    def test_ioc_limit_can_attach_full_protection(self):
        session = RecordingSession()
        client = BybitAPI("key", "secret", dry_run=False, session=session)
        client._last_time_sync = time.monotonic()
        client.create_order(
            "BTCUSDT",
            "Buy",
            "Limit",
            "0.01",
            price="100.3",
            time_in_force="IOC",
            take_profit="105",
            stop_loss="98",
            order_link_id="bounded-entry",
        )
        body = json.loads(session.last_body)
        self.assertEqual(body["timeInForce"], "IOC")
        self.assertEqual(body["tpslMode"], "Full")
        self.assertNotIn("slippageTolerance", body)

    def test_dry_write_is_blocked_without_credentials(self):
        client = BybitAPI("", "", dry_run=True)
        result = client.create_order(
            "BTCUSDT",
            "Buy",
            "Market",
            "0.01",
            order_link_id="dry-entry",
        )
        self.assertTrue(result["result"]["simulated"])

    def test_instrument_rules_round_quantity_down(self):
        rules = InstrumentRules.from_payload(
            {
                "symbol": "XRPUSDT",
                "status": "Trading",
                "priceFilter": {"tickSize": "0.0001"},
                "lotSizeFilter": {
                    "minOrderQty": "0.1",
                    "qtyStep": "0.1",
                    "minNotionalValue": "5",
                    "maxMktOrderQty": "100000",
                },
                "leverageFilter": {
                    "maxLeverage": "50",
                    "leverageStep": "0.01",
                },
            }
        )
        self.assertEqual(str(rules.quantity("1.29")), "1.2")


if __name__ == "__main__":
    unittest.main()
