from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

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
                body = await resp.content.read(MAX_SUBSCRIPTION_BYTES + 1)
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


def to_share_links(nodes: list[dict[str, Any]]) -> list[str]:
    links = []
    for n in nodes:
        share = n.get("share")
        if share:
            links.append(str(share))
    return links


def to_base64_sub(nodes: list[dict[str, Any]]) -> str:
    links = to_share_links(nodes)
    raw = "\n".join(links).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def to_clash(nodes: list[dict[str, Any]]) -> str:
    proxies = []
    names = []
    for n in nodes:
        item = {k: v for k, v in n.items() if k not in ("share", "raw") and v not in ("", None)}
        if "name" not in item:
            continue
        # minimal required
        if item.get("type") in (None, "unknown") and not item.get("server"):
            continue
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
    for n in nodes:
        name = str(n.get("name") or "node")
        names.append(name)
        typ = str(n.get("type") or "")
        server = n.get("server") or ""
        port = n.get("port") or ""
        if typ == "ss":
            lines.append(
                f"{name} = ss, {server}, {port}, encrypt-method={n.get('cipher') or 'aes-256-gcm'}, password={n.get('password') or ''}"
            )
        elif typ == "trojan":
            lines.append(
                f"{name} = trojan, {server}, {port}, password={n.get('password') or ''}, sni={n.get('sni') or server}"
            )
        elif typ == "vmess":
            lines.append(
                f"{name} = vmess, {server}, {port}, username={n.get('uuid') or ''}, tls={'true' if n.get('tls') else 'false'}"
            )
        elif typ == "vless":
            lines.append(
                f"{name} = vless, {server}, {port}, username={n.get('uuid') or ''}, tls={'true' if n.get('tls') else 'false'}"
            )
        else:
            share = n.get("share")
            if share:
                lines.append(f"# {name}: {share}")
    lines += ["", "[Proxy Group]", f"Proxy = select, {', '.join(names) if names else 'DIRECT'}", "", "[Rule]", "FINAL,Proxy"]
    return "\n".join(lines) + "\n"


def to_qx(nodes: list[dict[str, Any]]) -> str:
    lines = []
    for n in nodes:
        share = n.get("share")
        if share:
            lines.append(str(share))
            continue
        name = str(n.get("name") or "node")
        typ = str(n.get("type") or "")
        server = n.get("server") or ""
        port = n.get("port") or ""
        if typ == "ss":
            lines.append(
                f"shadowsocks={server}:{port}, method={n.get('cipher') or 'aes-256-gcm'}, password={n.get('password') or ''}, tag={name}"
            )
        elif typ == "trojan":
            lines.append(
                f"trojan={server}:{port}, password={n.get('password') or ''}, over-tls=true, tls-host={n.get('sni') or server}, tag={name}"
            )
        elif typ == "vmess":
            lines.append(
                f"vmess={server}:{port}, method=chacha20-ietf-poly1305, password={n.get('uuid') or ''}, tag={name}"
            )
    return "\n".join(lines) + ("\n" if lines else "")


def to_singbox(nodes: list[dict[str, Any]]) -> str:
    outbounds = []
    tags = []
    for n in nodes:
        tag = str(n.get("name") or "node")
        typ = n.get("type") or "unknown"
        ob: dict[str, Any] = {"tag": tag, "type": typ}
        for k in ("server", "server_port", "port", "uuid", "password", "flow", "tls", "transport"):
            if k in n:
                ob[k if k != "port" else "server_port"] = n[k]
        if "port" in n and "server_port" not in ob:
            ob["server_port"] = n["port"]
        outbounds.append(ob)
        tags.append(tag)
    data = {
        "outbounds": [
            {"type": "selector", "tag": "proxy", "outbounds": tags or ["direct"]},
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
