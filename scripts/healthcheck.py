#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订阅源健康检测
==============
在 scripts/update.py 抓到一份 TVBox/影视仓单仓配置后，对配置内部数据做可用性检测。
检测遵循"宁可保留、不可误杀"的原则，避免把有效源/有效站点误删：

1. 站点检测：对 `sites[]` 中 api 为 HTTP(S) 的接口做**多策略**探测
   （GET、GET+ac=list、POST ac=list、POST ac=videolist）；只要任一策略返回
   2xx/3xx 即视为可用。**默认宽容模式**：仅当所有策略都在网络层失败
   （DNS/连接/超时/SSL）时才剔除；服务端返回 4xx/5xx 的站点保留，
   避免误杀需要 POST 或特殊参数、或带反爬的接口。
   非 HTTP 的接口（csp_/js:/py: 等 spider）无法探测，原样保留。
2. 直播检测：对 `lives[]` 的播放列表地址、内联 `channels[].urls`
   做可达性检测（支持解析 proxy://...ext=<url> 形式），并对地址去重；
   同样**默认宽容**：只要有一条地址在 HTTP 层有响应即保留该项。

探测行为可用环境变量配置，检测报告写入 status.json。
"""
import os
import re
import socket
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

USER_AGENT = "Mozilla/5.0"
HTTP_PREFIXES = ("http://", "https://")
_PROXY_HINT = re.compile(r"^(?:proxy|rtp|rtsp|rtmp)://", re.I)
_EXT_PARAM = re.compile(r"[?&](?:ext|url|m3u8|play)=([^&]+)", re.I)

# 站点探测策略：不同接口对 HTTP 方法与参数要求不同，逐一尝试，任一命中即视为可用。
_SITE_STRATEGIES = (
    ("GET", ""),
    ("GET", "ac=list"),
    ("POST", {"ac": "list"}),
    ("POST", {"ac": "videolist"}),
)


def env_flag(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def is_http_url(value):
    return isinstance(value, str) and value.startswith(HTTP_PREFIXES)


def resolve_url(base_url, value):
    """把相对地址（如 ./lives/x.txt）按源配置的地址解析成绝对地址。"""
    if not isinstance(value, str) or not value.strip():
        return value
    value = value.strip()
    if is_http_url(value) or value.startswith("//"):
        return value
    if not base_url:
        return value
    return urllib.parse.urljoin(base_url, value)


def extract_real_url(value):
    """从 proxy://do=live&type=txt&ext=<url> 之类的地址中取出真实可探测地址。"""
    if not isinstance(value, str):
        return None
    if is_http_url(value):
        return value
    match = _EXT_PARAM.search(value)
    if match:
        candidate = urllib.parse.unquote(match.group(1))
        if is_http_url(candidate):
            return candidate
    return None


_DNS_HINTS = (
    "name or service not known", "nodename nor servname",
    "no address associated", "temporary failure in name resolution",
    "failed to resolve", "getaddrinfo failed", "name resolution",
)


def _is_dns_error(exc) -> bool:
    """沿异常链判断是否为 DNS 解析失败（最可靠的一种"真连不上"）。

    同时检查底层 socket.gaierror 与异常消息关键词，兼容不同平台/代理包装。
    """
    node = exc
    for _ in range(6):
        if isinstance(node, socket.gaierror):
            return True
        if any(hint in str(node).lower() for hint in _DNS_HINTS):
            return True
        node = getattr(node, "__cause__", None) or getattr(node, "__context__", None)
        if node is None:
            return False
    return False


def _classify_exception(exc) -> str:
    """把请求异常分为 'dead'（确定性不可达）与 'unknown'（不确定，默认保留）。

    - 'dead'   ：仅 DNS 解析失败、非法 URL、连接被拒/重置——与方法/参数无关，
                 基本可确定目标不可用；
    - 'unknown'：SSL 握手失败、超时、其他网络异常——多为反爬/瞬时/证书链问题，
                 **不代表站点失效**，默认保留，避免误杀有效源。
    """
    if isinstance(exc, requests.exceptions.InvalidURL):
        return "dead"
    if _is_dns_error(exc):
        return "dead"
    if isinstance(exc, requests.exceptions.SSLError):
        return "unknown"
    if isinstance(exc, requests.exceptions.Timeout):
        return "unknown"
    if isinstance(exc, requests.exceptions.ConnectionError):
        message = str(exc).lower()
        if "connection refused" in message or "connection reset" in message:
            return "dead"
        return "unknown"
    return "unknown"


def probe_once(url, timeout, method="GET", data=None):
    """单次可达性探测，返回 (state, detail)：

    - 'alive'：2xx/3xx，明确可用；
    - 'http' ：服务端有 HTTP 响应但状态码非 2xx/3xx（4xx/5xx，不确定，默认保留）；
    - 'unknown'：SSL/超时等异常，**不确定**（默认保留）；
    - 'dead' ：DNS 解析失败 / 非法 URL / 连接被拒（确定性不可达）。
    """
    if not url:
        return "dead", "empty"
    method = (method or "GET").upper()
    try:
        request = requests.post if method == "POST" else requests.get
        kwargs = {
            "headers": {"User-Agent": USER_AGENT, "Accept": "*/*"},
            "timeout": timeout,
            "allow_redirects": True,
            "stream": True,
        }
        if method == "POST":
            kwargs["data"] = data or {}
        response = request(url, **kwargs)
        status = response.status_code
        response.close()
        if 200 <= status < 400:
            return "alive", str(status)
        return "http", str(status)
    except Exception as exc:  # noqa: BLE001
        return _classify_exception(exc), type(exc).__name__


def probe(url, timeout, extra_query=""):
    """兼容旧接口：返回 (alive, detail)，仅 2xx/3xx 视为可用。"""
    target = url
    if extra_query:
        target = f"{url}{'&' if '?' in url else '?'}{extra_query}"
    state, detail = probe_once(target, timeout)
    return state == "alive", detail


def _probe_site(site, base_url, timeout):
    """多策略探测单个站点接口，返回 state ∈ {'alive','http','dead'}。

    - 任一策略 2xx/3xx → 'alive'（保留）；
    - 任一策略有 HTTP 响应但非 2xx/3xx → 继续尝试其余策略，全试完仍无 alive → 'http'（保留）；
    - 任一策略网络层失败（DNS/连接/SSL）→ 直接 'dead'：网络层失败与 HTTP
      方法/参数无关，无需再换策略，避免对失效站点发起 4 次无谓请求。
    """
    api = resolve_url(base_url, site.get("api"))
    if not is_http_url(api):
        return "dead"
    for method, payload in _SITE_STRATEGIES:
        if method == "POST":
            state, _ = probe_once(api, timeout, method="POST", data=payload)
        else:
            target = api if not payload else f"{api}{'&' if '?' in api else '?'}{payload}"
            state, _ = probe_once(target, timeout)
        if state == "alive":
            return "alive"
        if state == "dead":
            return "dead"
    return "http"


def filter_sites(sites, base_url, timeout, workers, strict=False):
    """剔除无响应的 HTTP 站点接口，保留无法探测的 spider 站点。

    默认宽容：仅当判定为**确定性不可达**（DNS 解析失败 / 连接被拒，state == 'dead'）
    时才剔除；服务端返回 4xx/5xx、或 SSL/超时等不确定情况（http/unknown）一律保留，
    避免误杀需要 POST/特殊参数或带反爬的接口。strict=True 时改为只保留明确 2xx/3xx 的站点。
    返回 (kept_sites, report)。
    """
    sites = [s for s in (sites or []) if isinstance(s, dict)]
    candidates = [
        s for s in sites
        if is_http_url(s.get("api")) and not _PROXY_HINT.match(str(s.get("api")))
    ]
    if not candidates:
        return sites, {"checked": 0, "dropped": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: _probe_site(s, base_url, timeout), candidates))
    if strict:
        dead = {id(site) for site, state in zip(candidates, results) if state != "alive"}
    else:
        dead = {id(site) for site, state in zip(candidates, results) if state == "dead"}
    kept = [s for s in sites if id(s) not in dead]
    return kept, {"checked": len(candidates), "dropped": len(dead)}


def _live_targets(entry, base_url, max_channel_probes):
    targets = []
    limit = max_channel_probes or None  # 0/None = 不限制
    url = entry.get("url")
    if isinstance(url, str) and url.strip():
        targets.append(extract_real_url(resolve_url(base_url, url)) or resolve_url(base_url, url))
    channels = entry.get("channels") or entry.get("urls")
    if isinstance(channels, list):
        for channel in channels[:limit]:
            urls = channel.get("urls") if isinstance(channel, dict) else None
            if not isinstance(urls, list):
                continue
            for item in urls[:limit]:
                real = extract_real_url(item) or resolve_url(base_url, item)
                if real:
                    targets.append(real)
    # 去重并保持顺序，避免对同一地址重复探测（大列表下可显著减少请求量）
    seen = set()
    uniq = []
    for target in targets:
        if target and target not in seen:
            seen.add(target)
            uniq.append(target)
    return uniq


def _probe_live(urls, timeout):
    """探测一个 lives 项的全部地址：任一 2xx/3xx 直接判 'alive'；
    否则只要有任何地址有响应或为不确定状态（http/unknown）即保留；
    只有全部地址都**确定性不可达**（dead）才判 'dead'。"""
    saw_soft = False
    for url in urls:
        state, _ = probe_once(url, timeout)
        if state == "alive":
            return "alive"
        if state != "dead":
            saw_soft = True
    return "http" if saw_soft else "dead"


def filter_lives(lives, base_url, timeout, workers, max_channel_probes=0, strict=False):
    """剔除播放列表与内联直播地址都不可达的 lives 项。

    默认宽容：仅当全部地址都**确定性不可达**（dead）时才剔除；有响应或不确定状态的保留。
    strict=True 时改为只保留明确 2xx/3xx 的 lives 项。
    """
    lives = [item for item in (lives or []) if isinstance(item, dict)]
    kept, dropped, checked, probes = [], 0, 0, []
    pending = []
    for entry in lives:
        entry_probes = _live_targets(entry, base_url, max_channel_probes)
        if not entry_probes:
            kept.append(entry)  # 无可探测地址（如纯 spider 直播），原样保留
            continue
        pending.append((entry, entry_probes))

    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                id(entry): pool.submit(_probe_live, urls, timeout)
                for entry, urls in pending
            }
            states = {key: future.result() for key, future in futures.items()}
        for entry, urls in pending:
            checked += 1
            probes.extend(urls)
            state = states[id(entry)]
            ok = (state == "alive") if strict else (state != "dead")
            if ok:
                kept.append(entry)
            else:
                dropped += 1
    return kept, {"checked": checked, "dropped": dropped, "probes": len(probes)}


def filter_config(config_data, base_url, *, check_sites=True, check_lives=True,
                  timeout=8, workers=16, max_live_probes=0, strict=False):
    """就地过滤一份单仓配置的 sites/lives，返回检测报告。"""
    report = {}
    if check_sites:
        config_data["sites"], sites_report = filter_sites(
            config_data.get("sites"), base_url, timeout, workers, strict=strict
        )
        report["sites"] = sites_report
    if check_lives:
        config_data["lives"], lives_report = filter_lives(
            config_data.get("lives"), base_url, timeout, workers,
            max_channel_probes=max_live_probes, strict=strict,
        )
        report["lives"] = lives_report
    return report
