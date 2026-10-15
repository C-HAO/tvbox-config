#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影视仓 / TVBox 配置自动更新脚本
=================================
功能：
  1. 读取 config/sources.json 中的配置源列表
  2. 对每个源依次尝试"主线 + 备用"多个地址，自动做中文域名 punycode 转换
  3. 识别配置类型：单仓直接使用；多仓订阅（如 hkuc）则递归展开成多个单仓
  4. 宽容解析：支持整行 // / # / ## 注释、被截断的 JSON 自动补全、多段 JSON 取首段
  5. 健康检测（scripts/healthcheck.py）：多策略探测 sites 的 HTTP 接口与 lives 的直播地址，
     默认只剔除网络层确实连不上的项（服务端有响应即保留），避免误杀有效源；
     源的全部地址抓取失败时默认不再沿用旧缓存（严格模式）
  6. 将每个源（及多仓展开出的子源）的最新配置保存到 output/<id>.json
  7. 生成多仓订阅文件 output/多仓订阅.json（可直接填进影视仓的"订阅"或"多仓"）
  8. 生成聚合单仓 output/单仓聚合.json（所有源 sites/lives/parses 合并去重），
     并清理本次未再产出的过期产物
  9. 生成 shields 徽章 output/shield.json，并把各源状态写回 README
兼容：本脚本同时兼容 GitHub Actions 自动运行 与 本地手动运行。
在 GitHub Actions 中，环境变量 GITHUB_REPOSITORY 会被自动注入（格式 owner/repo），
用于拼出多仓订阅里指向本仓库 raw 文件的完整 URL。
用法：
  python scripts/update.py
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.parse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import healthcheck  # noqa: E402  (与 update.py 同目录)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "config", "sources.json")
OUTPUT_DIR = os.path.join(ROOT, "output")

DEFAULT_REPO = "YOUR_USERNAME/tvbox-ysc-config"  # 本地运行时的占位仓库名，GitHub Actions 会自动覆盖

# 注意：部分接口的反爬会针对"完整 Chrome UA"下发挑战页，反而用简洁 UA 能正常返回 JSON
USER_AGENT = "Mozilla/5.0"

TIMEOUT = 20  # 单个地址超时（秒）
MAX_RETRIES = 2  # 单个地址最大尝试次数（仅对超时/连接类瞬态错误重试）
RETRY_BACKOFF = 3  # 重试基础等待秒数
URL_INTERVAL = 1.5  # 每个地址之间的小间隔秒数（降低被限流概率）
SOURCE_INTERVAL = 3  # 每个源之间的间隔秒数（降低被限流概率）

# 健康检测开关（可用环境变量覆盖）
CHECK_SITES = healthcheck.env_flag("CHECK_SITES", True)      # 探测 sites 里的 HTTP 接口
CHECK_LIVES = healthcheck.env_flag("CHECK_LIVES", True)      # 探测 lives 里的直播地址
PROBE_TIMEOUT = healthcheck.env_int("PROBE_TIMEOUT", 8)      # 单个探测超时（秒）
PROBE_WORKERS = healthcheck.env_int("PROBE_WORKERS", 16)     # 探测并发
# 每个 lives 项最多探测多少条内联频道地址；0 = 不限制
LIVE_CHANNEL_PROBES = healthcheck.env_int("LIVE_CHANNEL_PROBES", 0, minimum=0)
# 默认严格：源全部地址失败时不再沿用旧缓存（只生成本次真正抓取成功的源）
ALLOW_STALE_SOURCES = healthcheck.env_flag("ALLOW_STALE_SOURCES", False)
# README 状态区是否写各源明细（默认只写汇总，避免对外暴露源名称/地址）
README_STATUS_DETAILS = healthcheck.env_flag("README_STATUS_DETAILS", False)
# 检测严格度：默认 False（宽容）——只剔除网络层确实连不上的站点/直播，
# 服务端有响应（含 4xx/5xx）的一律保留，避免误杀需要 POST/特殊参数的接口。
STRICT_CHECKS = healthcheck.env_flag("STRICT_CHECKS", False)
# 多仓订阅递归展开的最大深度（0 = 不展开多仓；默认 2 层，够覆盖"多仓套多仓"）
MAX_MULTI_DEPTH = healthcheck.env_int("MAX_MULTI_DEPTH", 2, minimum=0)
# 单个多仓订阅最多展开的子源数（防止被投毒的多仓指向海量外部地址被自动镜像）
MAX_SUB_SOURCES = healthcheck.env_int("MAX_SUB_SOURCES", 64)
# 多仓子源之间的抓取间隔，降低被源站限流的概率
SUB_URL_INTERVAL = 0.8

# 判定"像 TVBox 单仓配置"的顶层字段
TVBOX_KEYS = ("sites", "spiders", "lives", "store", "exts", "parses", "flags")


_COMMENT_LINE = re.compile(r"^\s*(?://|#)")


def _strip_js_comments(text: str) -> str:
    """剔除 JSON 中内嵌的**整行**注释。

    兼容 `// 注释`、`# 注释`、`## 注释`（部分接口混用 Markdown 风格注释）。
    只删除整行注释，不动行尾内容，避免误伤 `http://` 等含 `//` 的字符串。
    """
    lines = text.split("\n")
    cleaned = [ln for ln in lines if not _COMMENT_LINE.match(ln)]
    return "\n".join(cleaned)


def _close_json(text: str) -> str:
    """尝试补全被截断的 JSON：闭合未结束的字符串、补齐未配对的 {}/[]，
    并去掉悬空的尾逗号。用于抢救内容在传输中被截断的接口。
    """
    stack = []
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    out = text
    if in_str:
        out += '"'
    out = out.rstrip()
    if out.endswith(","):
        out = out[:-1]
    return out + "".join(reversed(stack))


def parse_json_lenient(text: str):
    """宽容解析：去 BOM/空白、剔整行注释后优先整体 json.loads；
    失败时尝试补全被截断的括号；再失败则取第一段有效对象（兼容多段 JSON 拼接）。"""
    text = text.lstrip("\ufeff").strip()
    text = _strip_js_comments(text)
    candidates = [text]
    repaired = _close_json(text)
    if repaired != text:
        candidates.append(repaired)
    for candidate in candidates:
        try:
            # strict=False 允许字符串中出现未转义的控制字符（部分接口的 JSON 不规范）
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            # 兼容"返回了多个 JSON 拼接在一起"的接口
            decoder = json.JSONDecoder(strict=False)
            try:
                obj, _ = decoder.raw_decode(candidate)
                return obj
            except Exception:  # noqa: BLE001
                continue
    # 全部尝试失败：抛出原始文本的整体解析错误，保留可读报错
    return json.loads(candidates[0], strict=False)


def idn_encode(url: str) -> str:
    """将 URL 中的中文（国际化）域名转成 punycode，避免 DNS 解析失败。
    例如：http://www.饭太硬.com/tv -> http://www.xn--sss604efuw.com/tv
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    try:
        encoded_host = host.encode("idna").decode("ascii")
    except Exception:
        encoded_host = host  # 已是 ascii 或转换失败，原样使用
    netloc = encoded_host
    if parsed.port:
        netloc = f"{encoded_host}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def classify_config(data) -> str:
    """判断配置类型：
    - 'single' ：单仓配置（含 sites/spiders/lives 等字段）
    - 'multi'  ：多仓订阅（顶层 urls 列表，或纯数组的 [{name,url}] / ["url"]）
    - 'unknown'：无法识别
    """
    if isinstance(data, dict):
        if any(key in data for key in TVBOX_KEYS):
            return "single"
        if isinstance(data.get("urls"), list):
            return "multi"
    if isinstance(data, list):
        if any(
            (isinstance(it, dict) and isinstance(it.get("url"), str))
            or (isinstance(it, str) and it.strip())
            for it in data
        ):
            return "multi"
    return "unknown"


def extract_sub_urls(data):
    """从多仓配置中提取子源 [(name, url), ...]。

    兼容 `{"urls":[{"name","url"}]}` 与 `[{"name","url"}]` 两种多仓格式。
    """
    items = data.get("urls") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if isinstance(it, dict):
            url = it.get("url")
            if isinstance(url, str) and url.strip():
                out.append((it.get("name") or "", url.strip()))
        elif isinstance(it, str) and it.strip():
            out.append(("", it.strip()))
    return out


def is_valid_tvbox_config(data) -> bool:
    """粗略校验：返回内容是否像 TVBox/影视仓 **单仓** 配置。"""
    return classify_config(data) == "single"


def _should_retry(err: Exception, resp_text_len: int = -1) -> bool:
    """判断失败是否值得重试：
    - 超时/连接类瞬态错误：重试
    - 空响应 / JSON 解析失败（疑似服务端反爬冷却）：重试（加长等待）
    - 4xx/5xx、返回了内容但结构不对（确定性失败）：不重试
    """
    if isinstance(err, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(err, requests.exceptions.HTTPError):
        return False  # 4xx/5xx 为确定性失败
    if isinstance(err, json.JSONDecodeError):
        return True  # 空响应或非 JSON，可能是反爬/冷却，给一次机会
    if resp_text_len <= 0:
        return True  # 没有拿到任何内容
    return False


def fetch_config(url: str) -> dict:
    """抓取单个地址并解析为 JSON dict；失败时抛出异常。"""
    target = idn_encode(url)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                target,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            resp.raise_for_status()
            data = parse_json_lenient(resp.text)
            if classify_config(data) == "unknown":
                raise ValueError(
                    "返回内容不是 TVBox/影视仓 配置（既无 sites/spiders/lives 等字段，也不是多仓订阅）"
                )
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            text_len = len(getattr(resp, "text", "") or "") if "resp" in locals() else -1
            if _should_retry(e, text_len) and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * attempt * 2
                print(f"      (第 {attempt} 次失败：{e}，{wait}s 后重试)")
                time.sleep(wait)
            else:
                break
    raise last_err


def expand_source(entry_url, sid, name, max_depth=None):
    """从一个入口地址抓取配置；若是**多仓订阅**则递归展开为多个单仓。

    返回 [(sub_id, sub_name, data, used_url), ...]；入口地址抓取失败时抛出异常。
    子源抓取失败只跳过该子源，不影响其他子源（避免个别失效子源拖垮整个多仓）。
    """
    if max_depth is None:
        max_depth = MAX_MULTI_DEPTH
    results = []
    queue = [(sid, name, entry_url, 0)]
    seen = set()
    while queue:
        cur_id, cur_name, url, depth = queue.pop(0)
        key = idn_encode(url)
        if key in seen:
            continue
        seen.add(key)
        if depth > 0:
            time.sleep(SUB_URL_INTERVAL)  # 子源之间留间隔，降低被限流概率
        try:
            data = fetch_config(url)
        except Exception as exc:  # noqa: BLE001
            if depth == 0:
                raise  # 入口失败交由上层尝试备用地址
            print(f"      (子源失败 {cur_name}: {exc})")
            continue
        kind = classify_config(data)
        if kind == "single":
            results.append((cur_id, cur_name, data, url))
        elif kind == "multi" and depth < max_depth:
            subs = extract_sub_urls(data)
            for index, (sub_name, sub_url) in enumerate(subs):
                if len(queue) + len(results) >= MAX_SUB_SOURCES:
                    print(f"      (多仓 {cur_name} 展开达上限 {MAX_SUB_SOURCES}，停止展开)")
                    break
                child_id = f"{cur_id}-{index}"
                child_name = sub_name or f"{cur_name}-{index}"
                queue.append((child_id, child_name, sub_url, depth + 1))
        else:
            print(f"      (跳过无法识别的配置 {cur_name}: {kind})")
    return results


def _merge_list(fetched, field):
    """合并各源中的列表字段（lives/parses/doh 等），按 name/url 去重。"""
    seen = set()
    out = []
    for _sid, _name, data in fetched:
        for item in data.get(field, []) or []:
            if isinstance(item, dict):
                ident = item.get("name") or item.get("url") or json.dumps(
                    item, ensure_ascii=False, sort_keys=True
                )
            else:
                ident = str(item)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(item)
    return out


def merge_configs(fetched):
    """把多个单仓配置合并成一个聚合单仓：
    - 以第一个成功源为基底（保留 spider/logo/wallpaper 等）
    - sites 按 key 去重，重复 key 加源 id 前缀
    - lives/parses/doh/rules/flags/exts 等列表合并去重
    fetched: [(sid, name, data_dict), ...]
    """
    if not fetched:
        return None
    _base_sid, base_name, base = fetched[0]
    merged = {}
    skip = {"sites", "lives", "parses", "doh", "rules", "flags", "exts", "ads"}
    for k, v in base.items():
        if k not in skip:
            merged[k] = v
    merged["warningText"] = (
        f"本配置由 {len(fetched)} 个公开源自动聚合（含{base_name}等），仅供学习交流，请勿商用"
    )

    seen_keys = set()
    sites = []
    for sid, _name, data in fetched:
        for s in data.get("sites", []) or []:
            if not isinstance(s, dict):
                continue
            key = s.get("key")
            if key is None:
                key = json.dumps(s, ensure_ascii=False, sort_keys=True)
            new_key = key
            if key in seen_keys:
                new_key = f"{sid}_{key}"
            if new_key in seen_keys:
                continue
            seen_keys.add(new_key)
            if new_key != key:
                s = dict(s)
                s["key"] = new_key
            sites.append(s)
    merged["sites"] = sites

    for field in ("lives", "parses", "doh", "rules", "flags", "exts"):
        merged[field] = _merge_list(fetched, field)

    return merged


README_PATH = os.path.join(ROOT, "README.md")
STATUS_START = "<!-- STATUS_START -->"
STATUS_END = "<!-- STATUS_END -->"


def update_readme(status, ok_count, total):
    """把各源更新状态以表格形式写回 README（在 STATUS_START/END 标记之间）。"""
    if not os.path.exists(README_PATH):
        return
    with open(README_PATH, "r", encoding="utf-8") as f:
        readme = f.read()
    if STATUS_START not in readme or STATUS_END not in readme:
        return  # 未放置标记则不改动

    lines = []
    lines.append(f"> 🔄 最近更新：{status['updated_at']}（北京时间） · 成功 **{ok_count}/{total}** 个源")
    lines.append("")
    # 默认只写汇总，不对外暴露各源名称 / 地址 / 报错；设 README_STATUS_DETAILS=1 才写明细
    if README_STATUS_DETAILS:
        lines.append("| 配置源 | 状态 | 使用地址 |")
        lines.append("| --- | --- | --- |")
        for s in status["sources"]:
            if s.get("cached"):
                lines.append(f"| {s['name']} | 📦 缓存 | 上次成功配置（本次更新未抓到，沿用缓存） |")
            elif s["ok"]:
                lines.append(f"| {s['name']} | ✅ 成功 | `{s['used_url']}` |")
            else:
                err = (s.get("error") or "").replace("|", "/")
                if len(err) > 60:
                    err = err[:57] + "..."
                lines.append(f"| {s['name']} | ❌ 失败 | {err} |")
    block = "\n".join(lines)

    prefix = readme.split(STATUS_START, 1)[0]
    suffix = readme.split(STATUS_END, 1)[1]
    new_readme = f"{prefix}{STATUS_START}\n{block}\n{STATUS_END}{suffix}"
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(new_readme)


def write_badge(ok_count, total):
    """生成 shields.io endpoint 徽章 JSON。"""
    if total == 0 or ok_count == 0:
        color = "red"
    elif ok_count < total:
        color = "orange"
    else:
        color = "green"
    badge = {
        "schemaVersion": 1,
        "label": "TVBox 配置",
        "message": f"{ok_count}/{total} 可用",
        "color": color,
    }
    with open(os.path.join(OUTPUT_DIR, "shield.json"), "w", encoding="utf-8") as f:
        json.dump(badge, f, ensure_ascii=False, indent=2)


def main() -> int:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 东八区时间，便于阅读
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now_str = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    # GitHub Actions 中自动注入 GITHUB_REPOSITORY=owner/repo
    repo = (
        os.environ.get("GITHUB_REPOSITORY")
        or os.environ.get("REPO")
        or DEFAULT_REPO
    )
    # 结果分支：跟随触发分支（默认 develop），本地可用 BRANCH 覆盖
    branch = (
        os.environ.get("BRANCH")
        or os.environ.get("GITHUB_REF_NAME")
        or "develop"
    )
    raw_base = f"https://raw.githubusercontent.com/{repo}/{branch}/output"

    status = {"updated_at": now_str, "repo": repo, "sources": []}
    subscriptions = []
    fetched = []  # [(sid, name, data_dict), ...] 用于聚合单仓
    generated = set()  # 本次实际产出的文件名，用于清理过期残留

    ok_count = 0
    for src in cfg.get("sources", []):
        name = src.get("name", "未知")
        sid = src.get("id", name)
        urls = src.get("urls", [])

        entry = {"id": sid, "name": name, "ok": False, "used_url": None, "error": None}
        saved = False

        for url in urls:
            try:
                # 抓取入口地址；若为多仓订阅则递归展开为多个单仓
                children = expand_source(url, sid, name)
                if not children:
                    raise ValueError("未解析出任何单仓配置")

                site_checked = site_dropped = live_checked = live_dropped = 0
                child_ids = []
                for sub_id, sub_name, data, used_url in children:
                    report = healthcheck.filter_config(
                        data,
                        used_url,
                        check_sites=CHECK_SITES,
                        check_lives=CHECK_LIVES,
                        timeout=PROBE_TIMEOUT,
                        workers=PROBE_WORKERS,
                        max_live_probes=LIVE_CHANNEL_PROBES,
                        strict=STRICT_CHECKS,
                    )
                    out_path = os.path.join(OUTPUT_DIR, f"{sub_id}.json")
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    generated.add(f"{sub_id}.json")
                    subscriptions.append(
                        {"name": sub_name, "url": f"{raw_base}/{sub_id}.json"}
                    )
                    fetched.append((sub_id, sub_name, data))
                    child_ids.append(sub_id)
                    sites_info = report.get("sites", {})
                    lives_info = report.get("lives", {})
                    site_checked += sites_info.get("checked", 0)
                    site_dropped += sites_info.get("dropped", 0)
                    live_checked += lives_info.get("checked", 0)
                    live_dropped += lives_info.get("dropped", 0)

                entry["checks"] = {
                    "sites": {"checked": site_checked, "dropped": site_dropped},
                    "lives": {"checked": live_checked, "dropped": live_dropped},
                }
                if len(child_ids) > 1:
                    entry["children"] = child_ids  # 多仓展开出的子源
                entry["ok"] = True
                entry["used_url"] = url
                entry["error"] = None
                ok_count += 1
                saved = True
                extra = f"（展开 {len(child_ids)} 个子源）" if len(child_ids) > 1 else ""
                print(
                    f"[OK]   {name} <- {url} {extra} "
                    f"(sites -{site_dropped}/{site_checked}"
                    f", lives -{live_dropped}/{live_checked})"
                )
                break
            except Exception as e:  # noqa: BLE001
                print(f"[FAIL] {name}  {url}  ->  {e}")
                entry["error"] = str(e)

            # 地址之间稍作间隔，避免被限流
            time.sleep(URL_INTERVAL)

        if not saved:
            # 严格模式（默认）：全部地址失败即视为无效源，不沿用旧缓存
            cache_path = os.path.join(OUTPUT_DIR, f"{sid}.json")
            if ALLOW_STALE_SOURCES and os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                    if is_valid_tvbox_config(cached):
                        subscriptions.append(
                            {"name": name, "url": f"{raw_base}/{sid}.json"}
                        )
                        fetched.append((sid, name, cached))
                        generated.add(f"{sid}.json")
                        entry["ok"] = True
                        entry["cached"] = True
                        entry["used_url"] = "(缓存)"
                        ok_count += 1
                        saved = True
                        print(f"[CACHE] {name}  使用上次缓存配置")
                except Exception:
                    pass
            if not saved:
                entry["error"] = entry["error"] or "所有地址均失败"
                print(f"[FAIL] {name}  全部地址失败")
        status["sources"].append(entry)

        # 源之间稍作间隔，避免集中请求被限流
        time.sleep(SOURCE_INTERVAL)

    # 生成多仓订阅文件（影视仓"订阅/多仓"格式）
    sub_path = os.path.join(OUTPUT_DIR, "多仓订阅.json")
    with open(sub_path, "w", encoding="utf-8") as f:
        json.dump({"urls": subscriptions}, f, ensure_ascii=False, indent=2)

    # 生成聚合单仓文件（把所有成功源的 sites/lives/parses 合并成一个单仓）
    merged = merge_configs(fetched)
    merged_path = os.path.join(OUTPUT_DIR, "单仓聚合.json")
    if merged is not None:
        with open(merged_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        print(f"聚合单仓：{merged_path}（共 {len(merged.get('sites', []))} 个站点）")

    # 生成更新状态文件（时间取全部抓取完成之后）
    status["updated_at"] = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(OUTPUT_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    # 生成 shields 徽章并更新 README 状态表
    total = len(cfg.get("sources", []))
    write_badge(ok_count, total)
    update_readme(status, ok_count, total)

    # 清理本次未再产出的旧产物（失效源 / 数量变化后多余的多仓子源），
    # 避免这些过期文件仍被 GitHub Pages 直链访问或误订阅。
    keep = {"status.json", "shield.json", "多仓订阅.json", "单仓聚合.json"}
    for fname in os.listdir(OUTPUT_DIR):
        if fname.endswith(".json") and fname not in generated and fname not in keep:
            try:
                os.remove(os.path.join(OUTPUT_DIR, fname))
                print(f"[CLEAN] 移除过期产物 {fname}")
            except OSError:
                pass

    print("\n" + "=" * 50)
    print(f"本次更新：{ok_count}/{total} 个源成功")
    print(f"多仓订阅：{sub_path}")
    if ok_count == 0:
        print("没有任何源更新成功，返回非 0 退出码。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
