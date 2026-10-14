#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订阅源健康检测
==============
在 scripts/update.py 抓到一份 TVBox/影视仓单仓配置后，对配置内部数据做可用性检测：

1. 站点检测：对 `sites[]` 中 api 为 HTTP(S) 的接口做一次轻量请求，无响应的站点被剔除；
   非 HTTP 的接口（csp_/js:/py: 等 spider）无法探测，原样保留。
2. 直播检测：对 `lives[]` 的播放列表地址做可达性检测；内联 `channels[].urls`
   取少量样本检测（支持解析 proxy://...ext=<url> 形式）。

所有探测都可配置，并返回检测报告，便于写进 status.json。
"""
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

USER_AGENT = "Mozilla/5.0"
HTTP_PREFIXES = ("http://", "https://")
_PROXY_HINT = re.compile(r"^(?:proxy|rtp|rtsp|rtmp)://", re.I)
_EXT_PARAM = re.compile(r"[?&](?:ext|url|m3u8|play)=([^&]+)", re.I)


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


def probe(url, timeout, extra_query=""):
    """轻量可达性探测：2xx/3xx 视为可用。返回 (alive, detail)。"""
    if not url:
        return False, "empty"
    target = url
    if extra_query:
        target = f"{url}{'&' if '?' in url else '?'}{extra_query}"
    try:
        response = requests.get(
            target,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        status = response.status_code
        response.close()
        return 200 <= status < 400, str(status)
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _probe_site(site, base_url, timeout):
    api = resolve_url(base_url, site.get("api"))
    alive, _ = probe(api, timeout)
    if not alive:
        alive, _ = probe(api, timeout, extra_query="ac=list")
    return alive


def filter_sites(sites, base_url, timeout, workers):
    """剔除无响应的 HTTP 站点接口，保留无法探测的 spider 站点。"""
    sites = [s for s in (sites or []) if isinstance(s, dict)]
    candidates = [
        s for s in sites
        if is_http_url(s.get("api")) and not _PROXY_HINT.match(str(s.get("api")))
    ]
    if not candidates:
        return sites, {"checked": 0, "dropped": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: _probe_site(s, base_url, timeout), candidates))
    dead = {id(site) for site, alive in zip(candidates, results) if not alive}
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
    return [t for t in targets if t]


def filter_lives(lives, base_url, timeout, workers, max_channel_probes=0):
    """剔除播放列表与内联直播地址都不可达的 lives 项。"""
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
            future_map = {
                pool.submit(lambda urls: any(probe(u, timeout)[0] for u in urls), urls): entry
                for entry, urls in pending
            }
            alive_map = {}
            for future in future_map:
                alive_map[id(future_map[future])] = future.result()
        for entry, urls in pending:
            checked += 1
            probes.extend(urls)
            if alive_map.get(id(entry)):
                kept.append(entry)
            else:
                dropped += 1
    return kept, {"checked": checked, "dropped": dropped, "probes": len(probes)}


def filter_config(config_data, base_url, *, check_sites=True, check_lives=True,
                  timeout=8, workers=16, max_live_probes=0):
    """就地过滤一份单仓配置的 sites/lives，返回检测报告。"""
    report = {}
    if check_sites:
        config_data["sites"], sites_report = filter_sites(
            config_data.get("sites"), base_url, timeout, workers
        )
        report["sites"] = sites_report
    if check_lives:
        config_data["lives"], lives_report = filter_lives(
            config_data.get("lives"), base_url, timeout, workers,
            max_channel_probes=max_live_probes,
        )
        report["lives"] = lives_report
    return report
