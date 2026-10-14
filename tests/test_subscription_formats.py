"""TXT/YAML subscription parsing. Behavior only; no network."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from convert import (  # noqa: E402
    decode_subscription_bytes,
    fetch_subscription,
    parse_nodes_from_text,
    to_clash,
)

VLESS = "vless://11111111-1111-1111-1111-111111111111@example.invalid:443#中文节点"
PARENT_YAML = (
    '"proxies":\n'
    " - {name: yaml-node, type: trojan, server: example.invalid, port: 443, password: secret, sni: tls.invalid}\n"
)
GB_YAML = (
    "proxies:\n"
    "  - name: 香港节点\n"
    "    type: ss\n"
    "    server: 1.2.3.4\n"
    "    port: 8388\n"
    "    cipher: aes-256-gcm\n"
    "    password: 密码\n"
    "    tls: true\n"
)
QUOTED_YAML = """
"proxies":
  - "name": "Quoted HK"
    "type": "ss"
    "server": "q.example"
    "port": 443
    "cipher": "aes-256-gcm"
    "password": "p@ss:word#1"
    "udp": true
    "tls": true
    "network": "ws"
    "ws-opts":
      "path": "/quoted"
      "headers":
        "Host": "q.example"
    "alpn":
      - "h2"
      - "http/1.1"
  - MATCH,DIRECT
  - name: only-a-name
  - name: PROXY
    type: select
    proxies:
      - "Quoted HK"
proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - "Quoted HK"
rules:
  - MATCH,PROXY
port: 7890
"""
FLOW_YAML = (
    '{proxies: [{name: "HK Flow", type: ss, server: 8.8.8.8, port: 8388, '
    'cipher: aes-256-gcm, password: "flow:pass", tls: true, udp: true, '
    'network: ws, ws-opts: {path: /flow, headers: {Host: flow.example}}}]}'
)
LIST_YAML = """
- MATCH,DIRECT
- name: meta
  value: 1
- name: 日本
  type: trojan
  server: jp.example
  port: 443
  password: "秘密"
  sni: jp.example
  skip-cert-verify: false
  udp: true
  network: grpc
  grpc-opts:
    grpc-service-name: GunService
"""
SINGLE_QUOTED_YAML = """
'proxies':
  - name: sq
    type: ss
    server: 3.3.3.3
    port: 1
    cipher: aes-256-gcm
    password: sq-pass
"""


def _without_share(node: dict) -> dict:
    return {key: value for key, value in node.items() if key != "share"}


def _wrap_base64(text: str) -> str:
    blob = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\n".join(blob[index:index + 60] for index in range(0, len(blob), 60))


class FakeContent:
    def __init__(self, body: bytes):
        self.body = body
        self.offset = 0

    async def read(self, limit: int) -> bytes:
        chunk = self.body[self.offset:self.offset + limit]
        self.offset += len(chunk)
        return chunk


class FakeResponse:
    def __init__(self, body: bytes, charset: str | None):
        self.status = 200
        self.headers: dict[str, str] = {}
        self.charset = charset
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _type, _value, _tb):
        return False


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response

    def get(self, _url: str, **_kwargs):
        return self.response


async def _fetch(body: bytes, charset: str | None):
    session = FakeSession(FakeResponse(body, charset))
    with patch("convert.get_session", new=AsyncMock(return_value=session)), patch(
        "convert._check_public_url", new=AsyncMock()
    ):
        return await fetch_subscription("https://public.example/sub")


class SubscriptionFormatTests(unittest.TestCase):
    def test_plain_uri_txt_keeps_node_fields_and_share(self):
        sip = base64.urlsafe_b64encode(b"aes-256-gcm:sip-secret").decode("ascii").rstrip("=")
        legacy = base64.b64encode(b"aes-256-gcm:legacy-secret@8.8.8.8:8388").decode("ascii")
        text = "\n".join([
            VLESS,
            f"ss://{sip}@8.8.4.4:8388#SIP",
            f"ss://{legacy}#Legacy",
            "hy2://secret@hy.example:443?sni=hy.example&insecure=1#HY",
            "trojan://pass@tj.example:8443?sni=tj.example&allowInsecure=0#TJ",
        ])
        nodes = parse_nodes_from_text(text)
        self.assertEqual([node["name"] for node in nodes], ["中文节点", "SIP", "Legacy", "HY", "TJ"])

        vless, sip_node, legacy_node, hy_node, trojan = nodes
        self.assertEqual(vless["type"], "vless")
        self.assertEqual(vless["server"], "example.invalid")
        self.assertEqual(vless["port"], 443)
        self.assertEqual(vless["uuid"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(vless["network"], "tcp")
        self.assertIs(vless["tls"], False)
        self.assertEqual(vless["client-fingerprint"], "chrome")
        self.assertEqual(vless["share"], VLESS)

        self.assertEqual(sip_node["type"], "ss")
        self.assertEqual(sip_node["server"], "8.8.4.4")
        self.assertEqual(sip_node["port"], 8388)
        self.assertEqual(sip_node["cipher"], "aes-256-gcm")
        self.assertEqual(sip_node["password"], "sip-secret")

        self.assertEqual(legacy_node["server"], "8.8.8.8")
        self.assertEqual(legacy_node["port"], 8388)
        self.assertEqual(legacy_node["cipher"], "aes-256-gcm")
        self.assertEqual(legacy_node["password"], "legacy-secret")

        self.assertEqual(hy_node["type"], "hysteria2")
        self.assertEqual(hy_node["server"], "hy.example")
        self.assertEqual(hy_node["port"], 443)
        self.assertEqual(hy_node["password"], "secret")
        self.assertEqual(hy_node["sni"], "hy.example")
        self.assertIs(hy_node["skip-cert-verify"], True)

        self.assertEqual(trojan["password"], "pass")
        self.assertEqual(trojan["sni"], "tj.example")
        self.assertIs(trojan["skip-cert-verify"], False)
        self.assertTrue(trojan["share"].startswith("trojan://"))

    def test_malformed_uri_is_skipped_and_neighbors_survive(self):
        good_vless = (
            "vless://11111111-1111-1111-1111-111111111111@10.0.0.8:443"
            "?encryption=none&security=tls&sni=ex.com&type=ws#HK"
        )
        bad_legacy = "ss://" + base64.b64encode(b"aes-256-gcm:secret@1.2.3.4:notaport").decode("ascii") + "#bad"
        bad_vmess = "vmess://" + base64.b64encode(
            json.dumps({"add": "9.9.9.9", "port": "nope", "id": "u", "ps": "bad"}).encode("utf-8")
        ).decode("ascii")
        good_vmess_payload = {
            "add": "vmess.example",
            "port": 443,
            "id": "11111111-1111-1111-1111-111111111111",
            "aid": 2,
            "scy": "auto",
            "net": "ws",
            "tls": "tls",
            "sni": "vmess.example",
            "host": "vmess.example",
            "path": "/vm",
            "ps": "VMess节点",
        }
        good_vmess = "vmess://" + base64.b64encode(
            json.dumps(good_vmess_payload).encode("utf-8")
        ).decode("ascii")
        text = "\n".join([
            good_vless,
            "vmess://%%%%",
            bad_legacy,
            "ss://%%%not-a-node#nope",
            "this is not a node",
            "https://example.invalid/sub",
            bad_vmess,
            good_vmess,
            "ss://aes-256-gcm:s3cret@9.9.9.9:8388#SSNode",
        ])
        nodes = parse_nodes_from_text(text)
        self.assertEqual([node["name"] for node in nodes], ["HK", "VMess节点", "SSNode"])
        self.assertTrue(all(node.get("type") != "unknown" for node in nodes))
        self.assertNotIn("1.2.3.4", [node.get("server") for node in nodes])

        vless, vmess, ss_node = nodes
        self.assertEqual(vless["server"], "10.0.0.8")
        self.assertEqual(vless["port"], 443)
        self.assertEqual(vless["uuid"], "11111111-1111-1111-1111-111111111111")
        self.assertIs(vless["tls"], True)
        self.assertEqual(vless["network"], "ws")
        self.assertEqual(vless["servername"], "ex.com")
        self.assertEqual(vmess["server"], "vmess.example")
        self.assertEqual(vmess["port"], 443)
        self.assertEqual(vmess["uuid"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(vmess["alterId"], 2)
        self.assertIs(vmess["tls"], True)
        self.assertEqual(vmess["network"], "ws")
        self.assertEqual(vmess["servername"], "vmess.example")
        self.assertEqual(vmess["ws-opts"], {"path": "/vm", "headers": {"Host": "vmess.example"}})
        self.assertEqual(ss_node["server"], "9.9.9.9")
        self.assertEqual(ss_node["port"], 8388)
        self.assertEqual(ss_node["cipher"], "aes-256-gcm")
        self.assertEqual(ss_node["password"], "s3cret")

    def test_wrapped_and_bom_base64_txt_match_plain_nodes(self):
        plain = VLESS + "\nss://aes-256-gcm:s3cret@9.9.9.9:8388#SSNode\n"
        wrapped = _wrap_base64(plain)
        bom_blob = base64.b64encode(b"\xef\xbb\xbf" + plain.encode("utf-8")).decode("ascii")
        gb_blob = base64.b64encode(VLESS.encode("gb18030")).decode("ascii")

        plain_nodes = parse_nodes_from_text(plain)
        self.assertEqual(parse_nodes_from_text(wrapped), plain_nodes)
        self.assertEqual(parse_nodes_from_text(bom_blob), plain_nodes)
        self.assertEqual(parse_nodes_from_text("\ufeff" + plain)[0]["name"], "中文节点")
        gb_nodes = parse_nodes_from_text(gb_blob)
        self.assertEqual(gb_nodes[0]["name"], "中文节点")
        self.assertEqual(gb_nodes[0]["server"], "example.invalid")
        self.assertEqual(gb_nodes[0]["uuid"], "11111111-1111-1111-1111-111111111111")

    def test_bom_utf16_and_gb18030_keep_multibyte_names(self):
        cases = {
            "utf-8-bom": GB_YAML.encode("utf-8-sig"),
            "utf-16-le": b"\xff\xfe" + GB_YAML.encode("utf-16-le"),
            "utf-16-be": b"\xfe\xff" + GB_YAML.encode("utf-16-be"),
            "gb18030": GB_YAML.encode("gb18030"),
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                text = decode_subscription_bytes(raw)
                self.assertFalse(text.startswith("\ufeff"))
                node = parse_nodes_from_text(text)[0]
                self.assertEqual(node["name"], "香港节点")
                self.assertEqual(node["password"], "密码")
                self.assertEqual(node["server"], "1.2.3.4")
                self.assertEqual(node["port"], 8388)
                self.assertEqual(node["cipher"], "aes-256-gcm")
                self.assertIs(node["tls"], True)
                self.assertEqual(node["share"], "")

        uri_cases = {
            "uri-utf-8-bom": VLESS.encode("utf-8-sig"),
            "uri-utf-16": VLESS.encode("utf-16"),
            "uri-gb18030": VLESS.encode("gb18030"),
            "uri-base64": base64.b64encode(VLESS.encode("utf-8")),
        }
        for label, raw in uri_cases.items():
            with self.subTest(label=label):
                node = parse_nodes_from_text(decode_subscription_bytes(raw))[0]
                self.assertEqual(node["name"], "中文节点")
                self.assertEqual(node["server"], "example.invalid")
                self.assertEqual(node["port"], 443)

    def test_declared_charset_and_fallbacks(self):
        gb_raw = GB_YAML.encode("gb18030")
        utf_raw = GB_YAML.encode("utf-8-sig")
        for charset in (None, "gb18030", " GB18030 ", "utf-8", "x-not-a-charset"):
            with self.subTest(charset=charset):
                node = parse_nodes_from_text(decode_subscription_bytes(gb_raw, charset))[0]
                self.assertEqual(node["name"], "香港节点")
                self.assertEqual(node["password"], "密码")
        quoted = decode_subscription_bytes(utf_raw, '"UTF-8"')
        self.assertFalse(quoted.startswith("\ufeff"))
        self.assertEqual(parse_nodes_from_text(quoted)[0]["password"], "密码")
        wrong_declared = decode_subscription_bytes(b"\xff\xfe" + GB_YAML.encode("utf-16-le"), "utf-8")
        self.assertEqual(parse_nodes_from_text(wrong_declared)[0]["name"], "香港节点")

    def test_quoted_flow_and_list_yaml_preserve_proxy_fields(self):
        quoted = parse_nodes_from_text(QUOTED_YAML)
        self.assertEqual([node["name"] for node in quoted], ["Quoted HK"])
        node = quoted[0]
        self.assertEqual(node["type"], "ss")
        self.assertEqual(node["server"], "q.example")
        self.assertEqual(node["port"], 443)
        self.assertEqual(node["cipher"], "aes-256-gcm")
        self.assertEqual(node["password"], "p@ss:word#1")
        self.assertIs(node["udp"], True)
        self.assertIs(node["tls"], True)
        self.assertEqual(node["network"], "ws")
        self.assertEqual(node["ws-opts"], {"path": "/quoted", "headers": {"Host": "q.example"}})
        self.assertEqual(node["alpn"], ["h2", "http/1.1"])
        self.assertEqual(node["share"], "")
        self.assertNotIn("PROXY", [item["name"] for item in quoted])

        parent = parse_nodes_from_text(PARENT_YAML)
        self.assertEqual(len(parent), 1)
        self.assertEqual(parent[0]["name"], "yaml-node")
        self.assertEqual(parent[0]["type"], "trojan")
        self.assertEqual(parent[0]["server"], "example.invalid")
        self.assertEqual(parent[0]["port"], 443)
        self.assertEqual(parent[0]["password"], "secret")
        self.assertEqual(parent[0]["sni"], "tls.invalid")
        self.assertEqual(parent[0]["share"], "")

        single = parse_nodes_from_text(SINGLE_QUOTED_YAML)
        self.assertEqual(single[0]["name"], "sq")
        self.assertEqual(single[0]["server"], "3.3.3.3")
        self.assertEqual(single[0]["port"], 1)
        self.assertEqual(single[0]["password"], "sq-pass")
        self.assertEqual(single[0]["cipher"], "aes-256-gcm")

        flow = parse_nodes_from_text(FLOW_YAML)[0]
        self.assertEqual(flow["name"], "HK Flow")
        self.assertEqual(flow["server"], "8.8.8.8")
        self.assertEqual(flow["port"], 8388)
        self.assertEqual(flow["password"], "flow:pass")
        self.assertEqual(flow["cipher"], "aes-256-gcm")
        self.assertIs(flow["tls"], True)
        self.assertIs(flow["udp"], True)
        self.assertEqual(flow["network"], "ws")
        self.assertEqual(flow["ws-opts"], {"path": "/flow", "headers": {"Host": "flow.example"}})

        listed = parse_nodes_from_text(LIST_YAML)
        self.assertEqual([item["name"] for item in listed], ["日本"])
        japan = listed[0]
        self.assertEqual(japan["type"], "trojan")
        self.assertEqual(japan["server"], "jp.example")
        self.assertEqual(japan["port"], 443)
        self.assertEqual(japan["password"], "秘密")
        self.assertEqual(japan["sni"], "jp.example")
        self.assertIs(japan["skip-cert-verify"], False)
        self.assertIs(japan["udp"], True)
        self.assertEqual(japan["network"], "grpc")
        self.assertEqual(japan["grpc-opts"], {"grpc-service-name": "GunService"})

    def test_metadata_rules_and_broken_yaml_do_not_invent_nodes(self):
        self.assertEqual(parse_nodes_from_text(""), [])
        self.assertEqual(parse_nodes_from_text("   \n"), [])
        self.assertEqual(
            parse_nodes_from_text(
                "port: 7890\nmode: rule\nrules:\n  - MATCH,DIRECT\n"
                "proxy-groups:\n  - {name: PROXY, type: select, proxies: [DIRECT]}\n"
            ),
            [],
        )
        self.assertEqual(parse_nodes_from_text('{"port": 7890, "rules": ["MATCH,DIRECT"]}'), [])
        self.assertEqual(parse_nodes_from_text("proxies:\n  name: not-a-list\n"), [])
        self.assertEqual(parse_nodes_from_text("proxies: [unterminated"), [])
        self.assertEqual(parse_nodes_from_text("not a subscription\nrandom: value\n"), [])
        survivor = parse_nodes_from_text(VLESS + "\nproxies: [\n")
        self.assertEqual([node["name"] for node in survivor], ["中文节点"])
        self.assertEqual(survivor[0]["server"], "example.invalid")

    def test_singbox_outbounds_keep_tls_transport_and_skip_non_nodes(self):
        document = {
            "outbounds": [
                {"type": "selector", "tag": "proxy", "outbounds": ["香港", "ss1"]},
                {"type": "urltest", "tag": "auto", "outbounds": ["香港"]},
                {"type": "direct", "tag": "direct"},
                {"type": "block", "tag": "block"},
                {"type": "dns", "tag": "dns"},
                {
                    "type": "vmess",
                    "tag": "香港",
                    "server": "a.example",
                    "server_port": 443,
                    "uuid": "u-1",
                    "security": "auto",
                    "alter_id": 0,
                    "tls": {"enabled": True, "server_name": "a.example", "insecure": False},
                    "transport": {"type": "ws", "path": "/ws", "headers": {"Host": "a.example"}},
                },
                {
                    "type": "shadowsocks",
                    "tag": "ss1",
                    "server": "s.example",
                    "server_port": 8388,
                    "method": "aes-256-gcm",
                    "password": "secret",
                },
            ]
        }
        nodes = parse_nodes_from_text(json.dumps(document, ensure_ascii=False))
        self.assertEqual([node["name"] for node in nodes], ["香港", "ss1"])
        vmess, ss_node = nodes
        self.assertEqual(
            vmess,
            {
                "name": "香港",
                "type": "vmess",
                "share": "",
                "server": "a.example",
                "server_port": 443,
                "uuid": "u-1",
                "security": "auto",
                "alter_id": 0,
                "tls": {"enabled": True, "server_name": "a.example", "insecure": False},
                "transport": {"type": "ws", "path": "/ws", "headers": {"Host": "a.example"}},
            },
        )
        self.assertNotIn("tag", vmess)
        self.assertEqual(
            ss_node,
            {
                "name": "ss1",
                "type": "shadowsocks",
                "share": "",
                "server": "s.example",
                "server_port": 8388,
                "method": "aes-256-gcm",
                "password": "secret",
            },
        )
        filtered_only = {
            "outbounds": [
                {"type": "direct", "tag": "direct"},
                {"type": "block", "tag": "block"},
                {"type": "selector", "tag": "proxy", "outbounds": ["direct"]},
                {"type": "urltest", "tag": "auto", "outbounds": ["direct"]},
                {"type": "dns", "tag": "dns"},
            ]
        }
        self.assertEqual(parse_nodes_from_text(json.dumps(filtered_only)), [])

    def test_parsed_nodes_round_trip_through_clash_export(self):
        for source in (QUOTED_YAML, FLOW_YAML, LIST_YAML, PARENT_YAML, GB_YAML):
            with self.subTest(source=source.splitlines()[0][:24]):
                nodes = parse_nodes_from_text(source)
                self.assertTrue(nodes)
                exported = to_clash(nodes)
                again = parse_nodes_from_text(exported)
                self.assertEqual([_without_share(node) for node in again], [_without_share(node) for node in nodes])
                self.assertEqual([node["name"] for node in again], [node["name"] for node in nodes])
                self.assertNotIn("PROXY", [node["name"] for node in again])

    def test_fetch_matches_decoded_upload_parse(self):
        bodies = {
            "utf-8-bom-yaml": (GB_YAML.encode("utf-8-sig"), None),
            "utf-16-declared-utf-8": (b"\xff\xfe" + GB_YAML.encode("utf-16-le"), "utf-8"),
            "gb18030": (GB_YAML.encode("gb18030"), None),
            "gb18030-declared-utf-8": (GB_YAML.encode("gb18030"), "utf-8"),
            "gb18030-declared": (GB_YAML.encode("gb18030"), "gb18030"),
            "quoted-yaml": (QUOTED_YAML.encode("utf-8"), "utf-8"),
            "base64-txt": (base64.b64encode(VLESS.encode("utf-8")), "utf-8"),
            "plain-txt": (VLESS.encode("gb18030"), None),
        }
        for label, (body, charset) in bodies.items():
            with self.subTest(label=label):
                nodes, _meta, error = asyncio.run(_fetch(body, charset))
                direct = parse_nodes_from_text(decode_subscription_bytes(body, charset))
                self.assertIsNone(error)
                self.assertEqual(nodes, direct)
                self.assertEqual(nodes[0]["server"], direct[0]["server"])
                if label == "base64-txt" or label == "plain-txt":
                    self.assertEqual(nodes[0]["name"], "中文节点")
                    self.assertEqual(nodes[0]["server"], "example.invalid")
                elif label == "quoted-yaml":
                    self.assertEqual(nodes[0]["password"], "p@ss:word#1")
                    self.assertEqual(nodes[0]["ws-opts"]["path"], "/quoted")
                else:
                    self.assertEqual(nodes[0]["name"], "香港节点")
                    self.assertEqual(nodes[0]["password"], "密码")


if __name__ == "__main__":
    unittest.main()
