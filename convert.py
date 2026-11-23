from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

MAX_SUBSCRIPTION_REDIRECTS = 5
MAX_SUBSCRIPTION_BYTES = 10 * 1024 * 1024


import aiohttp
import yaml

PROTO_RE = re.compile(
    r"^(?:ss|ssr|vmess|vless|trojan|hysteria2?|hy2|tuic|snell|wireguard|anytls|socks5(?:-tls)?)://\S+",
    re.I,
)

# ── 共享 HTTP 会话 ────────────────────────────────────────────────────────────
# 旧实现每抓一条订阅就新建一个 ClientSession，等于每条都重做 TLS 握手、
# 建连接池然后立刻扔掉。同一机场的多条订阅同域名，keep-alive 完全吃不到。
# 改成进程级共享：连接复用，握手往返省掉，和批量并发叠加收益最明显。
_SESSION: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    """取共享会话。惰性创建（ClientSession 必须在事件循环内构造）。

    并发安全：判空到赋值之间没有 await（ClientSession/TCPConnector 构造都是
    同步的），单线程事件循环下是原子的，不会重复建会话。
    """
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        connector = aiohttp.TCPConnector(
            limit=32,             # 总连接上限
            limit_per_host=8,     # 单域名上限，防止把小机场打崩
            ttl_dns_cache=300,    # DNS 缓存 5 分钟，省掉重复解析
            enable_cleanup_closed=True,
            resolver=PublicResolver(),
        )
        _SESSION = aiohttp.ClientSession(connector=connector)
    return _SESSION


async def close_session() -> None:
    """关闭共享会话，供进程退出时收尾。"""
    global _SESSION
    session, _SESSION = _SESSION, None
    if session is not None and not session.closed:
        await session.close()


def _b64decode(data: str) -> bytes | None:
    s = data.strip().replace("-", "+").replace("_", "/")
    pad = "=" * ((4 - len(s) % 4) % 4)
    try:
        return base64.b64decode(s + pad)
    except Exception:
        return None


def _strip_leading_bom(text: str) -> str:
    return text.lstrip("\ufeff") if text else ""


def _normalize_charset(charset: str | None) -> str | None:
    if charset is None:
        return None
    name = str(charset).strip().strip("'\"").lower()
    if not name:
        return None
    name = name.split(";", 1)[0].strip()
    if not name:
        return None
    return {
        "utf8": "utf-8-sig",
        "utf-8": "utf-8-sig",
        "utf_8": "utf-8-sig",
        "utf-8-sig": "utf-8-sig",
        "utf16": "utf-16",
        "utf-16le": "utf-16-le",
        "utf-16be": "utf-16-be",
    }.get(name, name)


def decode_subscription_bytes(raw: bytes, charset: str | None = None) -> str:
    """把订阅正文解码成文本。

    优先认 UTF-8 / UTF-16 BOM，其次用响应声明的 charset，再严格回退
    UTF-8 与 GB18030。不用 errors=ignore，避免多字节节点名被丢掉。
    """
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    if not isinstance(raw, bytes):
        raise TypeError("subscription body must be bytes")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) and not raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        try:
            return _strip_leading_bom(raw.decode("utf-16"))
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    encodings: list[str] = []
    declared = _normalize_charset(charset)
    if declared:
        encodings.append(declared)
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        if enc not in encodings:
            encodings.append(enc)
    for enc in encodings:
        try:
            return _strip_leading_bom(raw.decode(enc))
        except (UnicodeDecodeError, LookupError):
            continue
    return _strip_leading_bom(raw.decode("utf-8", errors="replace"))


def _decode_payload_bytes(data: bytes, *, allow_gb18030: bool = True) -> str | None:
    """解码 base64 内层字节。失败返回 None，不用替换字符凑节点。

    折行的单行片段不走 GB18030：它几乎能解码任意字节，会把片段误认成节点。
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) and not data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
    encodings = ("utf-8", "gb18030") if allow_gb18030 else ("utf-8",)
    for enc in encodings:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _decode_base64_document(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 16 or len(compact) % 4 == 1:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/_=-]+", compact):
        return None
    raw = _b64decode(compact)
    if not raw:
        return None
    payload = _decode_payload_bytes(raw)
    if not payload:
        return None
    payload = _strip_leading_bom(payload).strip()
    return payload or None


def decode_maybe_base64(text: str) -> str:
    stripped = _strip_leading_bom(text or "").strip()
    if not stripped:
        return ""
    payload = _decode_base64_document(stripped)
    if not payload or payload == stripped:
        return stripped
    if _structured_nodes(payload) or extract_share_links(payload):
        return payload
    return stripped


def extract_share_links(text: str) -> list[str]:
    links: list[str] = []
    for line in _strip_leading_bom(text).splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        if PROTO_RE.match(line):
            links.append(line)
            continue
        raw = _b64decode(line)
        if not raw:
            continue
        decoded = _decode_payload_bytes(raw, allow_gb18030=False)
        if not decoded:
            continue
        for sub in _strip_leading_bom(decoded).splitlines():
            sub = sub.strip().lstrip("\ufeff")
            if PROTO_RE.match(sub):
                links.append(sub)
    return links


def node_name_from_url(url: str) -> str:
    if "#" in url:
        return unquote(url.split("#", 1)[1]) or "未命名"
    try:
        return urlparse(url).scheme.upper() + " 节点"
    except Exception:
        return "未命名"


def parse_vmess(url: str) -> dict[str, Any] | None:
    try:
        raw = url.split("://", 1)[1]
        decoded = _b64decode(raw)
        if not decoded:
            return None
        data = json.loads(decoded)
        if not isinstance(data, dict):
            return None
        server = data.get("add")
        if not isinstance(server, str) or not server:
            return None
        name = data.get("ps") or "vmess"
        node: dict[str, Any] = {
            "name": str(name),
            "type": "vmess",
            "server": server,
            "port": int(data.get("port") or 0),
            "uuid": data.get("id"),
            "alterId": int(data.get("aid") or 0),
            "cipher": data.get("scy") or "auto",
            "network": data.get("net") or "tcp",
            "tls": True if data.get("tls") in ("tls", True, "1") else False,
            "share": url,
        }
        if data.get("sni") or data.get("host"):
            node["servername"] = data.get("sni") or data.get("host")
        if data.get("path"):
            node["ws-opts"] = {"path": data.get("path"), "headers": {"Host": data.get("host") or ""}}
        return node
    except Exception:
        return None


def parse_share_link(url: str) -> dict[str, Any] | None:
    try:
        return _parse_share_link(url)
    except Exception:
        return None


def _parse_share_link(url: str) -> dict[str, Any] | None:
    lower = url.lower()
    if lower.startswith("vmess://"):
        return parse_vmess(url)
    name = node_name_from_url(url)
    u = urlparse(url.split("#", 1)[0])
    scheme = (u.scheme or "").lower()
    if not scheme:
        return None
    if scheme == "hy2":
        scheme = "hysteria2"
    host = u.hostname or ""
    port = u.port or 0
    username = unquote(u.username or "")
    password = unquote(u.password or "")
    qs = {k: v[0] for k, v in parse_qs(u.query).items()}
    node: dict[str, Any] = {"name": name, "type": scheme, "server": host, "port": port, "share": url}
    if scheme == "vless":
        node.update({
            "uuid": username,
            "tls": qs.get("security") in ("tls", "reality"),
            "flow": qs.get("flow") or "",
            "network": qs.get("type") or "tcp",
            "client-fingerprint": qs.get("fp") or "chrome",
            "servername": qs.get("sni") or "",
        })
        if qs.get("security") == "reality":
            node["reality-opts"] = {"public-key": qs.get("pbk") or "", "short-id": qs.get("sid") or ""}
    elif scheme == "trojan":
        node.update({
            "password": username,
            "sni": qs.get("sni") or host,
            "skip-cert-verify": qs.get("allowInsecure") in ("1", "true", "True"),
        })
    elif scheme == "ss":
        # ss://base64(method:pass)@host:port、ss://method:pass@host:port
        # 或整段 base64(method:pass@host:port)。解不出服务器就丢弃，不填默认密码。
        if not _fill_ss_fields(url, node, username, password):
            return None
    elif scheme == "hysteria2":
        node.update({
            "password": username or password or qs.get("auth") or "",
            "sni": qs.get("sni") or "",
            "skip-cert-verify": qs.get("insecure") in ("1", "true", "True"),
        })
    elif scheme == "tuic":
        node.update({
            "uuid": username,
            "password": password or username,
            "sni": qs.get("sni") or "",
            "udp-relay-mode": qs.get("udp_relay_mode") or "native",
            "congestion-controller": qs.get("congestion_control") or "bbr",
            "skip-cert-verify": qs.get("allow_insecure") in ("1", "true", "True"),
        })
    else:
        node["raw"] = url
    return node


def _fill_ss_fields(url: str, node: dict[str, Any], username: str, password: str) -> bool:
    method = ""
    pwd = ""
    parsed = False
    if username and ":" not in username and not password:
        decoded = _b64decode(username)
        if decoded and b":" in decoded:
            method, pwd = decoded.decode("utf-8", "ignore").split(":", 1)
            parsed = bool(node.get("server"))
    elif username and password:
        method, pwd = username, password
        parsed = bool(node.get("server"))
    elif "@" not in url:
        body = url.split("://", 1)[1].split("#", 1)[0]
        decoded = _b64decode(body)
        if decoded and b"@" in decoded:
            method_pass, hostport = decoded.decode("utf-8", "ignore").split("@", 1)
            method, pwd = method_pass.split(":", 1)
            if ":" in hostport:
                host, port_s = hostport.rsplit(":", 1)
                node["server"], node["port"] = host, int(port_s)
                parsed = bool(host)
    if not parsed:
        return False
    node.update({"cipher": method or "aes-256-gcm", "password": pwd})
    return True


_SINGBOX_SKIP_TYPES = frozenset({"direct", "block", "dns", "selector", "urltest"})


def _is_proxy_mapping(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    name = item.get("name")
    typ = item.get("type")
    server = item.get("server")
    return (
        isinstance(name, str)
        and bool(name.strip())
        and isinstance(typ, str)
        and bool(typ.strip())
        and isinstance(server, str)
        and bool(server.strip())
    )


def _nodes_from_proxy_list(items: list[Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for item in items:
        if not _is_proxy_mapping(item):
            continue
        node = dict(item)
        node.setdefault("share", "")
        nodes.append(node)
    return nodes


def _nodes_from_singbox_outbounds(outbounds: list[Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for ob in outbounds:
        if not isinstance(ob, dict):
            continue
        if ob.get("type") in _SINGBOX_SKIP_TYPES:
            continue
        name = ob.get("tag") or ob.get("type") or "node"
        node: dict[str, Any] = {"name": name, "type": ob.get("type"), "share": ""}
        node.update({k: v for k, v in ob.items() if k not in ("tag",)})
        nodes.append(node)
    return nodes


def _nodes_from_loaded(data: Any) -> list[dict[str, Any]]:
    """只取 Clash proxies、独立节点列表或 sing-box outbounds。

    rules、proxy-groups 和其它元数据映射不会被当成节点。
    """
    if isinstance(data, list):
        return _nodes_from_proxy_list(data)
    if not isinstance(data, dict):
        return []
    proxies = data.get("proxies")
    if isinstance(proxies, list):
        return _nodes_from_proxy_list(proxies)
    outbounds = data.get("outbounds")
    if isinstance(outbounds, list):
        return _nodes_from_singbox_outbounds(outbounds)
    return []


def _structured_nodes(text: str) -> list[dict[str, Any]]:
    text = _strip_leading_bom(text).strip()
    if not text:
        return []
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            loaded = json.loads(stripped)
        except Exception:
            loaded = None
        else:
            nodes = _nodes_from_loaded(loaded)
            if nodes:
                return nodes
    try:
        loaded = yaml.safe_load(text)
    except Exception:
        return []
    return _nodes_from_loaded(loaded)


def _nodes_from_shares(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for link in extract_share_links(text):
        try:
            node = parse_share_link(link)
        except Exception:
            continue
        if node:
            nodes.append(node)
    return nodes


def _nodes_from_plain(text: str) -> list[dict[str, Any]]:
    nodes = _structured_nodes(text)
    if nodes:
        return nodes
    return _nodes_from_shares(text)


def parse_nodes_from_text(text: str) -> list[dict[str, Any]]:
    text = _strip_leading_bom(text or "").strip()
    if not text:
        return []
    # 整段都是 Base64 时先解码。折行后逐行解会截断 URI，不能抢在完整文档前面。
    decoded = decode_maybe_base64(text)
    if decoded and decoded != text:
        nodes = _nodes_from_plain(decoded)
        if nodes:
            return nodes
    return _nodes_from_plain(text)


def _decode_profile_title(raw: str) -> str:
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    try:
        if raw.lower().startswith("base64:"):
            decoded = _b64decode(raw.split(":", 1)[1])
            if decoded:
                return decoded.decode("utf-8", "ignore").strip()
        if re.fullmatch(r"[A-Za-z0-9+/=_-]+", raw) and len(raw) >= 8:
            decoded = _b64decode(raw)
            if decoded:
                text = decoded.decode("utf-8", "ignore").strip()
                if text and (not text.isascii() or any("\u4e00" <= ch <= "\u9fff" for ch in text)):
                    return text
    except Exception:
        pass
    return unquote(raw)


def _filename_from_disposition(value: str) -> str:
    if not value:
        return ""
    m = re.search(r"filename\*=(?:UTF-8''|utf-8'')([^;]+)", value, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r"filename=([^;]+)", value, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"').strip("'"))
    return ""


def parse_subscription_headers(headers: aiohttp.typedefs.LooseHeaders | dict[str, str]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "traffic_used": None,
        "traffic_total": None,
        "expire_at": None,
        "profile_name": None,
    }
    h = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    userinfo = h.get("subscription-userinfo") or ""
    kv = {}
    for part in userinfo.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            kv[k.strip()] = v.strip()
    try:
        upload = float(kv.get("upload") or 0)
        download = float(kv.get("download") or 0)
        total = float(kv.get("total") or 0)
        if total > 0:
            info["traffic_used"] = (upload + download) / (1024 ** 3)
            info["traffic_total"] = total / (1024 ** 3)
    except Exception:
        pass
    try:
        if kv.get("expire"):
            info["expire_at"] = int(float(kv["expire"]))
    except Exception:
        pass
    name = (
        _decode_profile_title(h.get("profile-title") or "")
        or _filename_from_disposition(h.get("content-disposition") or "")
        or _decode_profile_title(h.get("content-disposition") or "")
    )
    name = name.replace(".yaml", "").replace(".yml", "").replace(".txt", "").strip()
    if name:
        info["profile_name"] = name[:64]
    return info


def _is_public_ip(raw_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public_ip(str(ip.ipv4_mapped))
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


class PublicResolver:
    """aiohttp resolver that rejects private addresses at connection time."""

    def __init__(self) -> None:
        self._resolver = aiohttp.resolver.ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_UNSPEC
    ) -> list[dict[str, Any]]:
        resolved = await self._resolver.resolve(host, port, family)
        blocked = next(
            (
                item.get("host")
                for item in resolved
                if not _is_public_ip(str(item.get("host") or ""))
            ),
            None,
        )
        if blocked is not None:
            raise OSError(f"连接目标为内网/保留地址: {blocked}")
        return resolved

    async def close(self) -> None:
        await self._resolver.close()


async def _check_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("仅支持 http/https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL 不允许携带用户名或密码")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL 缺少主机名")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise ValueError(f"主机名解析失败: {exc}") from exc
    addresses = {str(info[4][0]) for info in infos}
    blocked = next((ip for ip in addresses if not _is_public_ip(ip)), None)
    if blocked is not None:
        raise ValueError(f"URL 指向内网/保留地址: {host} -> {blocked}")






async def fetch_subscription(url: str, timeout: int = 25) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    headers = {
        "User-Agent": "clash-meta/1.18.0",
        "Accept": "*/*",
    }
    try:
        session = await get_session()
        current = url
        for redirect_count in range(MAX_SUBSCRIPTION_REDIRECTS + 1):
            await _check_public_url(current)
            async with session.get(
                current,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as resp:
                meta = parse_subscription_headers(resp.headers)
                if 300 <= resp.status < 400:
                    location = resp.headers.get("Location")
                    if not location:
                        return [], meta, "订阅重定向缺少 Location"
                    if redirect_count >= MAX_SUBSCRIPTION_REDIRECTS:
                        return [], meta, "订阅重定向次数过多"
                    current = urljoin(current, location)
                    continue
                if resp.status >= 400:
                    return [], meta, f"HTTP {resp.status}"
                body = bytearray()
                while len(body) <= MAX_SUBSCRIPTION_BYTES:
                    chunk = await resp.content.read(
                        min(64 * 1024, MAX_SUBSCRIPTION_BYTES + 1 - len(body))
                    )
                    if not chunk:
                        break
                    body.extend(chunk)
                if len(body) > MAX_SUBSCRIPTION_BYTES:
                    return [], meta, "订阅响应过大"
                text = decode_subscription_bytes(body, resp.charset)
                nodes = parse_nodes_from_text(text)
                return nodes, meta, None
        return [], {"traffic_used": None, "traffic_total": None, "expire_at": None, "profile_name": None}, "订阅重定向次数过多"
    except Exception as e:
        return [], {"traffic_used": None, "traffic_total": None, "expire_at": None, "profile_name": None}, str(e)


def apply_path_maps(nodes: list[dict[str, Any]], maps: dict[str, str]) -> list[dict[str, Any]]:
    """Apply path rules by case-insensitive substring matching.

    The longest keyword wins, so a specific rule such as ``hk-premium`` is
    evaluated before a broader rule such as ``hk``.  Besides the displayed node
    name, the server and share URL are searched because many subscriptions put
    the provider/path only in one of those fields.
    """
    rules = sorted(
        [
            (str(keyword).strip().casefold(), str(remark).strip())
            for keyword, remark in maps.items()
            if str(keyword).strip() and str(remark).strip()
        ],
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for node in nodes:
        item = dict(node)
        name = str(item.get("name") or "")
        haystack = " ".join(
            str(item.get(field) or "")
            for field in ("name", "server", "share")
        ).casefold()
        for keyword, remark in rules:
            if keyword in haystack:
                item["name"] = f"{name} → {remark}"
                break
        out.append(item)
    return out


def _resolve_temp_node(node: dict[str, Any]) -> dict[str, Any]:
    """将只有 share 链接的临时节点尽力解析为结构化节点，保留自定义名称。"""
    if node.get("type") != "temp" or node.get("server"):
        return node
    share = str(node.get("share") or "").strip()
    if not share:
        return node
    parsed = parse_share_link(share)
    if not parsed:
        return node
    merged = dict(parsed)
    if node.get("name"):
        merged["name"] = str(node["name"])
    merged["share"] = share
    return merged


def node_to_share_link(node: dict[str, Any]) -> str:
    """将节点转换为分享链接；若已有原始 share 则优先返回，否则从 YAML/JSON 结构化字段逆向生成。"""
    share = str(node.get("share") or "").strip()
    if share:
        return share
    n = _resolve_temp_node(node)
    share = str(n.get("share") or "").strip()
    if share:
        return share

    typ = str(n.get("type") or "").lower()
    if typ == "shadowsocks":
        typ = "ss"
    elif typ == "hy2":
        typ = "hysteria2"

    server = str(n.get("server") or "").strip()
    port = int(n.get("port") or n.get("server_port") or 0)
    if not server or port <= 0:
        return ""
    name = str(n.get("name") or f"{typ.upper()} 节点").strip()
    frag = quote(name, safe="")
    host_part = f"[{server}]" if ":" in server and not server.startswith("[") else server

    tls_raw = n.get("tls")
    tls_dict = tls_raw if isinstance(tls_raw, dict) else {}
    tls_enabled = bool(tls_dict.get("enabled")) if tls_dict else bool(tls_raw)
    sni = str(
        n.get("sni")
        or n.get("servername")
        or tls_dict.get("server_name")
        or ""
    ).strip()
    insecure = bool(
        n.get("skip-cert-verify")
        or tls_dict.get("insecure")
    )

    transport = n.get("transport") if isinstance(n.get("transport"), dict) else {}
    network = str(n.get("network") or transport.get("type") or "tcp").lower()
    ws_opts = n.get("ws-opts") if isinstance(n.get("ws-opts"), dict) else {}
    grpc_opts = n.get("grpc-opts") if isinstance(n.get("grpc-opts"), dict) else {}
    ws_path = str(ws_opts.get("path") or transport.get("path") or "").strip()
    ws_headers = ws_opts.get("headers") if isinstance(ws_opts.get("headers"), dict) else (
        transport.get("headers") if isinstance(transport.get("headers"), dict) else {}
    )
    ws_host = str(ws_headers.get("Host") or ws_headers.get("host") or "").strip()
    grpc_service = str(
        grpc_opts.get("grpc-service-name") or transport.get("service_name") or ""
    ).strip()

    if typ == "ss":
        cipher = str(n.get("cipher") or n.get("method") or "aes-256-gcm").strip()
        password = str(n.get("password") or "")
        userinfo = base64.urlsafe_b64encode(f"{cipher}:{password}".encode("utf-8")).decode("ascii").rstrip("=")
        return f"ss://{userinfo}@{host_part}:{port}#{frag}"

    if typ == "vmess":
        uuid = str(n.get("uuid") or "").strip()
        if not uuid:
            return ""
        payload = {
            "v": "2",
            "ps": name,
            "add": server,
            "port": port,
            "id": uuid,
            "aid": int(n.get("alterId") or n.get("alter_id") or 0),
            "scy": str(n.get("cipher") or n.get("security") or "auto"),
            "net": network or "tcp",
            "type": "none",
            "host": ws_host or sni,
            "path": ws_path or grpc_service,
            "tls": "tls" if tls_enabled else "",
            "sni": sni,
        }
        b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        return f"vmess://{b64}"

    if typ == "vless":
        uuid = str(n.get("uuid") or "").strip()
        if not uuid:
            return ""
        reality = n.get("reality-opts") if isinstance(n.get("reality-opts"), dict) else (
            tls_dict.get("reality") if isinstance(tls_dict.get("reality"), dict) else {}
        )
        pbk = str(reality.get("public-key") or reality.get("public_key") or "").strip()
        sid = str(reality.get("short-id") or reality.get("short_id") or "").strip()
        params: dict[str, str] = {"encryption": "none"}
        if pbk:
            params["security"] = "reality"
            params["pbk"] = pbk
            if sid:
                params["sid"] = sid
        elif tls_enabled:
            params["security"] = "tls"
        if sni:
            params["sni"] = sni
        if network and network != "tcp":
            params["type"] = network
        if ws_path:
            params["path"] = ws_path
        if ws_host:
            params["host"] = ws_host
        if grpc_service:
            params["serviceName"] = grpc_service
        flow = str(n.get("flow") or "").strip()
        if flow:
            params["flow"] = flow
        fp = str(
            n.get("client-fingerprint")
            or (tls_dict.get("utls") or {}).get("fingerprint", "")
            or ""
        ).strip()
        if fp:
            params["fp"] = fp
        if insecure:
            params["allowInsecure"] = "1"
        return f"vless://{quote(uuid, safe='')}@{host_part}:{port}?{urlencode(params)}#{frag}"

    if typ == "trojan":
        password = str(n.get("password") or "")
        params = {}
        if sni:
            params["sni"] = sni
        if insecure:
            params["allowInsecure"] = "1"
        if network and network != "tcp":
            params["type"] = network
        if ws_path:
            params["path"] = ws_path
        if ws_host:
            params["host"] = ws_host
        if grpc_service:
            params["serviceName"] = grpc_service
        qs = f"?{urlencode(params)}" if params else ""
        return f"trojan://{quote(password, safe='')}@{host_part}:{port}{qs}#{frag}"

    if typ == "hysteria2":
        password = str(n.get("password") or "")
        params = {}
        if sni:
            params["sni"] = sni
        if insecure:
            params["insecure"] = "1"
        obfs = str(n.get("obfs") or "").strip()
        if obfs:
            params["obfs"] = obfs
            if n.get("obfs-password"):
                params["obfs-password"] = str(n["obfs-password"])
        qs = f"?{urlencode(params)}" if params else ""
        return f"hysteria2://{quote(password, safe='')}@{host_part}:{port}{qs}#{frag}"

    if typ == "tuic":
        uuid = str(n.get("uuid") or "").strip()
        password = str(n.get("password") or uuid)
        params = {}
        if sni:
            params["sni"] = sni
        if insecure:
            params["allow_insecure"] = "1"
        udp_mode = str(n.get("udp-relay-mode") or n.get("udp_relay_mode") or "").strip()
        if udp_mode:
            params["udp_relay_mode"] = udp_mode
        cc = str(n.get("congestion-controller") or n.get("congestion_control") or "").strip()
        if cc:
            params["congestion_control"] = cc
        qs = f"?{urlencode(params)}" if params else ""
        return f"tuic://{quote(uuid, safe='')}:{quote(password, safe='')}@{host_part}:{port}{qs}#{frag}"

    return ""


def deduplicate_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按协议+服务器+端口+凭据+传输参数去重节点，保持原始顺序。"""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for raw in nodes:
        n = _resolve_temp_node(raw)
        typ = str(n.get("type") or "").lower()
        if typ == "shadowsocks":
            typ = "ss"
        elif typ == "hy2":
            typ = "hysteria2"
        server = str(n.get("server") or "").strip().lower()
        port = int(n.get("port") or n.get("server_port") or 0)
        cred = str(n.get("uuid") or n.get("password") or "")
        ws_opts = n.get("ws-opts") if isinstance(n.get("ws-opts"), dict) else {}
        transport = n.get("transport") if isinstance(n.get("transport"), dict) else {}
        path = str(ws_opts.get("path") or transport.get("path") or "")
        if server and port > 0:
            key: tuple[Any, ...] = (typ, server, port, cred, path)
        else:
            share = str(n.get("share") or n.get("name") or id(n))
            key = (typ, share)
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def format_subscription_userinfo(subs: list[dict[str, Any]] | dict[str, Any]) -> str | None:
    """构造标准 subscription-userinfo 响应头，供 Clash / Shadowrocket / Sing-box 显示流量与到期时间。"""
    items = [subs] if isinstance(subs, dict) else list(subs)
    total_gb = 0.0
    used_gb = 0.0
    has_traffic = False
    expires: list[int] = []

    for sub in items:
        t_total = sub.get("traffic_total")
        t_used = sub.get("traffic_used")
        if t_total is not None and float(t_total) > 0:
            has_traffic = True
            total_gb += float(t_total)
            used_gb += max(0.0, float(t_used or 0.0))
        exp = sub.get("expire_at")
        if exp is not None and int(exp) > 0:
            expires.append(int(exp))

    if not has_traffic and not expires:
        return None

    parts: list[str] = []
    if has_traffic:
        used_bytes = int(round(used_gb * (1024 ** 3)))
        total_bytes = int(round(total_gb * (1024 ** 3)))
        parts.extend([f"upload=0", f"download={used_bytes}", f"total={total_bytes}"])
    if expires:
        parts.append(f"expire={min(expires)}")
    return "; ".join(parts)


def to_share_links(nodes: list[dict[str, Any]]) -> list[str]:
    links = []
    for n in nodes:
        link = node_to_share_link(n)
        if link:
            links.append(link)
    return links


def to_base64_sub(nodes: list[dict[str, Any]]) -> str:
    links = to_share_links(nodes)
    raw = "\n".join(links).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _unique_node_name(base: str, used: set[str]) -> str:
    name = base or "node"
    if name not in used:
        used.add(name)
        return name
    idx = 2
    while f"{name} #{idx}" in used:
        idx += 1
    candidate = f"{name} #{idx}"
    used.add(candidate)
    return candidate


def _normalize_for_clash(raw_node: dict[str, Any]) -> dict[str, Any] | None:
    n = _resolve_temp_node(raw_node)
    item = {k: v for k, v in n.items() if k not in ("share", "raw") and v not in ("", None)}
    if "name" not in item:
        return None
    typ = str(item.get("type") or "").lower()
    if typ in ("", "unknown", "temp") and not item.get("server"):
        return None
    if typ == "shadowsocks":
        item["type"] = "ss"
    elif typ == "hy2":
        item["type"] = "hysteria2"

    # 兼容来自 sing-box outbounds 的字段
    if "server_port" in item:
        port_val = item.pop("server_port")
        item.setdefault("port", port_val)
    if "method" in item and item.get("type") == "ss":
        method_val = item.pop("method")
        item.setdefault("cipher", method_val)
    if "alter_id" in item and item.get("type") == "vmess":
        aid_val = item.pop("alter_id")
        item.setdefault("alterId", aid_val)
    if "security" in item and item.get("type") == "vmess":
        sec_val = item.pop("security")
        item.setdefault("cipher", sec_val)
    if isinstance(item.get("tls"), dict):
        tls_dict = item.pop("tls")
        item["tls"] = bool(tls_dict.get("enabled", True))
        if tls_dict.get("server_name"):
            key_name = "sni" if item.get("type") in ("trojan", "hysteria2", "tuic") else "servername"
            item.setdefault(key_name, tls_dict["server_name"])
        if "insecure" in tls_dict:
            item.setdefault("skip-cert-verify", bool(tls_dict["insecure"]))
        if tls_dict.get("alpn"):
            item.setdefault("alpn", tls_dict["alpn"])
    if isinstance(item.get("transport"), dict):
        tr = item.pop("transport")
        tr_type = tr.get("type")
        if tr_type:
            item.setdefault("network", tr_type)
        if tr_type == "ws":
            ws_opts: dict[str, Any] = {}
            if tr.get("path"):
                ws_opts["path"] = tr["path"]
            if tr.get("headers"):
                ws_opts["headers"] = tr["headers"]
            if ws_opts:
                item.setdefault("ws-opts", ws_opts)
        elif tr_type == "grpc" and tr.get("service_name"):
            item.setdefault("grpc-opts", {"grpc-service-name": tr["service_name"]})
    if not item.get("server"):
        return None
    return item


def to_clash(nodes: list[dict[str, Any]]) -> str:
    proxies = []
    names = []
    used_names: set[str] = set()
    for n in nodes:
        item = _normalize_for_clash(n)
        if not item:
            continue
        item["name"] = _unique_node_name(str(item["name"]), used_names)
        proxies.append(item)
        names.append(item["name"])
    data = {
        "proxies": proxies,
        "proxy-groups": [
            {"name": "🚀 节点选择", "type": "select", "proxies": names or ["DIRECT"]},
            {"name": "♻️ 自动选择", "type": "url-test", "proxies": names or ["DIRECT"], "url": "http://www.gstatic.com/generate_204", "interval": 300},
        ],
        "rules": ["MATCH,🚀 节点选择"],
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def to_surge(nodes: list[dict[str, Any]]) -> str:
    lines = ["[Proxy]"]
    names = []
    used_names: set[str] = set()
    for raw in nodes:
        n = _resolve_temp_node(raw)
        name = _unique_node_name(str(n.get("name") or "node"), used_names)
        typ = str(n.get("type") or "").lower()
        if typ == "shadowsocks":
            typ = "ss"
        elif typ == "hy2":
            typ = "hysteria2"
        server = n.get("server") or ""
        port = n.get("port") or n.get("server_port") or ""
        if typ == "ss" and server and port:
            names.append(name)
            cipher = n.get("cipher") or n.get("method") or "aes-256-gcm"
            lines.append(
                f"{name} = ss, {server}, {port}, encrypt-method={cipher}, password={n.get('password') or ''}"
            )
        elif typ == "trojan" and server and port:
            names.append(name)
            sni = n.get("sni") or n.get("servername") or server
            skip = ", skip-cert-verify=true" if n.get("skip-cert-verify") else ""
            lines.append(
                f"{name} = trojan, {server}, {port}, password={n.get('password') or ''}, sni={sni}{skip}"
            )
        elif typ == "vmess" and server and port:
            names.append(name)
            tls_val = n.get("tls")
            tls_on = tls_val.get("enabled") if isinstance(tls_val, dict) else bool(tls_val)
            ws_extra = ""
            ws_opts = n.get("ws-opts") if isinstance(n.get("ws-opts"), dict) else {}
            if n.get("network") == "ws" or ws_opts:
                ws_path = ws_opts.get("path") or "/"
                ws_extra = f", ws=true, ws-path={ws_path}"
            lines.append(
                f"{name} = vmess, {server}, {port}, username={n.get('uuid') or ''}, tls={'true' if tls_on else 'false'}{ws_extra}"
            )
        elif typ == "vless" and server and port:
            names.append(name)
            tls_val = n.get("tls")
            tls_on = tls_val.get("enabled") if isinstance(tls_val, dict) else bool(tls_val)
            lines.append(
                f"{name} = vless, {server}, {port}, username={n.get('uuid') or ''}, tls={'true' if tls_on else 'false'}"
            )
        elif typ == "hysteria2" and server and port:
            names.append(name)
            sni = n.get("sni") or n.get("servername") or server
            skip = ", skip-cert-verify=true" if n.get("skip-cert-verify") else ""
            lines.append(
                f"{name} = hysteria2, {server}, {port}, password={n.get('password') or ''}, sni={sni}{skip}"
            )
        elif typ == "tuic" and server and port:
            names.append(name)
            sni = n.get("sni") or n.get("servername") or server
            lines.append(
                f"{name} = tuic, {server}, {port}, token={n.get('password') or ''}, uuid={n.get('uuid') or ''}, sni={sni}"
            )
        else:
            share = node_to_share_link(n)
            if share:
                lines.append(f"# {name}: {share}")
    lines += ["", "[Proxy Group]", f"Proxy = select, {', '.join(names) if names else 'DIRECT'}", "", "[Rule]", "FINAL,Proxy"]
    return "\n".join(lines) + "\n"


def to_qx(nodes: list[dict[str, Any]]) -> str:
    lines = []
    for raw in nodes:
        n = _resolve_temp_node(raw)
        share = n.get("share")
        if share:
            lines.append(str(share))
            continue
        name = str(n.get("name") or "node")
        typ = str(n.get("type") or "").lower()
        if typ == "shadowsocks":
            typ = "ss"
        server = n.get("server") or ""
        port = n.get("port") or n.get("server_port") or ""
        if typ == "ss" and server and port:
            cipher = n.get("cipher") or n.get("method") or "aes-256-gcm"
            lines.append(
                f"shadowsocks={server}:{port}, method={cipher}, password={n.get('password') or ''}, tag={name}"
            )
        elif typ == "trojan" and server and port:
            sni = n.get("sni") or n.get("servername") or server
            lines.append(
                f"trojan={server}:{port}, password={n.get('password') or ''}, over-tls=true, tls-host={sni}, tag={name}"
            )
        elif typ == "vmess" and server and port:
            lines.append(
                f"vmess={server}:{port}, method=chacha20-ietf-poly1305, password={n.get('uuid') or ''}, tag={name}"
            )
        else:
            synthesized = node_to_share_link(n)
            if synthesized:
                lines.append(synthesized)
    return "\n".join(lines) + ("\n" if lines else "")


def to_singbox(nodes: list[dict[str, Any]]) -> str:
    outbounds = []
    tags = []
    used_tags: set[str] = set()
    for raw in nodes:
        n = _resolve_temp_node(raw)
        server = n.get("server")
        port = n.get("server_port") or n.get("port")
        if not server or not port:
            continue
        tag = _unique_node_name(str(n.get("name") or "node"), used_tags)
        typ = str(n.get("type") or "unknown").lower()
        if typ == "ss":
            typ = "shadowsocks"
        elif typ == "hy2":
            typ = "hysteria2"

        ob: dict[str, Any] = {
            "tag": tag,
            "type": typ,
            "server": str(server),
            "server_port": int(port),
        }

        if typ == "shadowsocks":
            ob["method"] = str(n.get("method") or n.get("cipher") or "aes-256-gcm")
            ob["password"] = str(n.get("password") or "")
        elif typ == "vmess":
            if n.get("uuid"):
                ob["uuid"] = str(n["uuid"])
            ob["security"] = str(n.get("security") or n.get("cipher") or "auto")
            ob["alter_id"] = int(n.get("alter_id") or n.get("alterId") or 0)
        elif typ == "vless":
            if n.get("uuid"):
                ob["uuid"] = str(n["uuid"])
            if n.get("flow"):
                ob["flow"] = str(n["flow"])
        elif typ in ("trojan", "hysteria2"):
            if n.get("password") is not None:
                ob["password"] = str(n["password"])
        elif typ == "tuic":
            if n.get("uuid"):
                ob["uuid"] = str(n["uuid"])
            if n.get("password") is not None:
                ob["password"] = str(n["password"])
            if n.get("congestion-controller") or n.get("congestion_control"):
                ob["congestion_control"] = str(n.get("congestion-controller") or n.get("congestion_control"))
            if n.get("udp-relay-mode") or n.get("udp_relay_mode"):
                ob["udp_relay_mode"] = str(n.get("udp-relay-mode") or n.get("udp_relay_mode"))
        else:
            for k in ("uuid", "password", "flow"):
                if n.get(k):
                    ob[k] = n[k]

        # TLS 配置映射
        raw_tls = n.get("tls")
        if isinstance(raw_tls, dict):
            ob["tls"] = dict(raw_tls)
        elif raw_tls is True or typ in ("trojan", "hysteria2", "tuic") or isinstance(n.get("reality-opts"), dict):
            tls_obj: dict[str, Any] = {"enabled": True}
            sni = str(n.get("sni") or n.get("servername") or server or "").strip()
            if sni:
                tls_obj["server_name"] = sni
            if n.get("skip-cert-verify") is not None:
                tls_obj["insecure"] = bool(n["skip-cert-verify"])
            if isinstance(n.get("alpn"), list) and n["alpn"]:
                tls_obj["alpn"] = list(n["alpn"])
            if n.get("client-fingerprint"):
                tls_obj["utls"] = {"enabled": True, "fingerprint": str(n["client-fingerprint"])}
            if isinstance(n.get("reality-opts"), dict):
                ro = n["reality-opts"]
                tls_obj["reality"] = {
                    "enabled": True,
                    "public_key": str(ro.get("public-key") or ro.get("public_key") or ""),
                    "short_id": str(ro.get("short-id") or ro.get("short_id") or ""),
                }
            ob["tls"] = tls_obj

        # Transport 传输层映射
        if isinstance(n.get("transport"), dict):
            ob["transport"] = dict(n["transport"])
        else:
            net = str(n.get("network") or "").lower()
            if net == "ws":
                ws_opts = n.get("ws-opts") if isinstance(n.get("ws-opts"), dict) else {}
                tr: dict[str, Any] = {"type": "ws"}
                if ws_opts.get("path"):
                    tr["path"] = ws_opts["path"]
                if ws_opts.get("headers"):
                    tr["headers"] = ws_opts["headers"]
                ob["transport"] = tr
            elif net == "grpc":
                grpc_opts = n.get("grpc-opts") if isinstance(n.get("grpc-opts"), dict) else {}
                tr = {"type": "grpc"}
                if grpc_opts.get("grpc-service-name"):
                    tr["service_name"] = grpc_opts["grpc-service-name"]
                ob["transport"] = tr

        outbounds.append(ob)
        tags.append(tag)
    data = {
        "outbounds": [
            {"type": "selector", "tag": "proxy", "outbounds": ["auto", *tags] if tags else ["direct"]},
            {"type": "urltest", "tag": "auto", "outbounds": tags or ["direct"], "url": "http://www.gstatic.com/generate_204", "interval": "5m"},
            {"type": "direct", "tag": "direct"},
            *outbounds,
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_bytes_gb(val: float | None) -> str:
    if val is None:
        return "?"
    return f"{val:.2f} GB"


def format_expire(ts: int | None) -> str:
    if not ts:
        return "长期有效"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def remain_text(ts: int | None) -> str:
    if not ts:
        return "长期有效"
    now = int(datetime.now(tz=timezone.utc).timestamp())
    delta = ts - now
    if delta < 0:
        return "已过期"
    days = delta // 86400
    hours = (delta % 86400) // 3600
    return f"{days}天{hours}小时"
