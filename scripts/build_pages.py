#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 output/ 下的配置 JSON 打包成一个可浏览的 GitHub Pages 站点。

用法：
  python scripts/build_pages.py --destination pages-site --base-url https://<owner>.github.io/<repo>

站点内容：
  - 复制 output/*.json 到目标目录（保持文件名，App 可直接拉取）
  - 生成 index.html：列出每个文件、可复制链接、打开/下载
"""
import argparse
import datetime
import html
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"

# 常见产物的展示信息；其余 *.json 自动罗列
KNOWN_FILES = {
    "多仓订阅.json": ("多仓订阅（推荐）", "App 内可切换各源；填入影视仓 / TVBox 的「订阅 / 多仓」"),
    "单仓聚合.json": ("单仓聚合", "所有源 sites / lives 合并为一个，开箱即用"),
    "status.json": ("各源更新状态", "机器可读的状态文件（各源成功 / 失败与使用地址）"),
    "shield.json": ("状态徽章数据", "shields.io endpoint 徽章 JSON"),
}

PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>影视仓配置聚合 / TVBox Configs</title>
  <style>
    :root { color-scheme: light dark; --bg:#f5f7fb; --card:#ffffff; --text:#16233b; --muted:#5a6b86; --border:#dbe3f0; --primary:#2563eb; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font:16px/1.6 ui-sans-serif, system-ui, "Segoe UI", sans-serif; }
    main { width:min(1080px, calc(100% - 32px)); margin:0 auto; padding:40px 0 64px; }
    h1 { margin:0 0 8px; font-size:clamp(24px,4vw,38px); }
    .sub { margin:0 0 28px; color:var(--muted); }
    .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); gap:16px; }
    .card { padding:20px; border:1px solid var(--border); border-radius:16px; background:var(--card); }
    .card-head { display:flex; align-items:baseline; justify-content:space-between; gap:12px; }
    .card h3 { margin:0; font-size:17px; }
    .size { color:var(--muted); font-size:12px; flex:none; }
    .desc { margin:8px 0 14px; color:var(--muted); font-size:14px; }
    code { display:block; padding:10px 12px; overflow-wrap:anywhere; border-radius:10px; background:var(--bg); color:var(--primary); font:12px/1.5 ui-monospace, Consolas, monospace; }
    .actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:14px; }
    .actions button, .actions a { min-height:40px; display:inline-flex; align-items:center; padding:8px 14px; border:1px solid var(--border); border-radius:10px; background:var(--card); color:var(--text); font:inherit; font-size:13px; font-weight:700; text-decoration:none; cursor:pointer; }
    .actions a { border-color:var(--primary); background:var(--primary); color:#fff; }
    .actions button.is-copied { border-color:#16a34a; background:#dcfce7; color:#166534; }
    footer { margin-top:40px; color:var(--muted); font-size:13px; }
    @media (prefers-color-scheme: dark) {
      :root { --bg:#0b1526; --card:#111f38; --text:#eaf1ff; --muted:#9fb1cc; --border:#25405f; --primary:#8ab4ff; }
      .actions a { color:#0b1526; }
      .actions button.is-copied { background:#153d2a; color:#bbf7d0; }
    }
  </style>
</head>
<body>
  <main>
    <h1>影视仓 / TVBox 配置聚合</h1>
    <p class="sub">复制下方链接填入 App 的「配置地址 / 订阅」即可使用。生成时间：__GENERATED_AT__</p>
    <div class="grid">
__CARDS__
    </div>
    <footer>由 tvbox-config 自动生成；所有配置来自互联网公开分享，仅供学习交流。</footer>
  </main>
  <script>
    document.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-copy-url]");
      if (!button) return;
      const text = button.dataset.copyUrl;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          const area = document.createElement("textarea");
          area.value = text; area.style.position = "fixed"; area.style.opacity = "0";
          document.body.appendChild(area); area.select(); document.execCommand("copy"); area.remove();
        }
        const original = button.textContent;
        button.textContent = "已复制";
        button.classList.add("is-copied");
        setTimeout(() => { button.textContent = original; button.classList.remove("is-copied"); }, 1600);
      } catch (error) {
        button.textContent = "复制失败";
      }
    });
  </script>
</body>
</html>
"""


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _render_card(base_url: str, name: str, title: str, desc: str, size: int) -> str:
    url = f"{base_url}/{name}"
    escaped_url = html.escape(url, quote=True)
    return (
        '      <article class="card">\n'
        '        <div class="card-head">\n'
        f'          <h3>{html.escape(title)}</h3>\n'
        f'          <span class="size">{_human_size(size)}</span>\n'
        '        </div>\n'
        f'        <p class="desc">{html.escape(desc)}</p>\n'
        f'        <code>{escaped_url}</code>\n'
        '        <div class="actions">\n'
        f'          <button type="button" data-copy-url="{escaped_url}">复制链接</button>\n'
        f'          <a href="{escaped_url}" target="_blank" rel="noopener">打开</a>\n'
        f'          <a href="{escaped_url}" download>下载</a>\n'
        '        </div>\n'
        '      </article>'
    )


def build(destination, base_url, output_dir=OUTPUT_DIR, generated_at=""):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Pages destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("base_url is required")

    output_dir = Path(output_dir)
    files = sorted(
        (path for path in output_dir.glob("*.json") if path.is_file()),
        key=lambda item: item.name,
    )
    if not files:
        raise ValueError(f"no .json files found in {output_dir}")

    cards = []
    for path in files:
        shutil.copyfile(path, destination / path.name)
        title, desc = KNOWN_FILES.get(path.name, (path.stem, "TVBox / 影视仓配置"))
        cards.append(_render_card(base_url, path.name, title, desc, path.stat().st_size))

    stamp = generated_at or datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")
    index_html = (
        PAGE_TEMPLATE
        .replace("__CARDS__", "\n".join(cards))
        .replace("__GENERATED_AT__", html.escape(stamp))
    )
    (destination / "index.html").write_text(index_html, encoding="utf-8")
    return [path.name for path in files]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a GitHub Pages site for output/*.json")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--generated-at", default="")
    args = parser.parse_args()

    published = build(args.destination, args.base_url, generated_at=args.generated_at)
    print(f"Pages site built: {len(published)} file(s) -> {args.destination}")
    for name in published:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
