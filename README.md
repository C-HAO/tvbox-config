# 影视仓配置聚合仓库（TVBox Config Aggregator）

![配置状态](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FOWNER%2FREPO%2FBRANCH%2Foutput%2Fshield.json)
![更新频率](https://img.shields.io/badge/%E6%9B%B4%E6%96%B0-%E6%AF%8F%E5%A4%A9%202%20%E6%AC%A1-blue)

聚合多个公开的影视仓 / TVBox 单仓配置，通过 **GitHub Actions 定时自动更新**，把结果提交回本仓库并发布为 GitHub Pages，供影视仓、TVBox、OK影视等软件随时拉取最新接口。

> 所有配置均来自互联网公开分享，版权归原作者所有；本仓库仅做聚合与镜像，仅供学习交流，请勿商用。

---

## 一、源可用性状态

<!-- STATUS_START -->

> 🔄 最近更新：&lt;由工作流自动写入&gt; · 成功 **&lt;N/M&gt;** 个源
<!-- STATUS_END -->

## 二、仓库结构

```
repo/
├── .github/workflows/update.yml   # GitHub Actions：更新配置并发布 GitHub Pages
├── config/sources.json            # 配置源列表（主地址 + 备用地址），可自行增删改
├── scripts/update.py              # 核心更新脚本（下载→校验→合并→落盘→更新状态）
├── scripts/healthcheck.py         # 健康检测（站点接口 / 直播地址可用性）
├── scripts/build_pages.py         # 把 output/*.json 打包成 GitHub Pages 站点
├── output/                        # 更新产物（由脚本自动生成并提交）
│   ├── 多仓订阅.json               # 多仓聚合订阅（推荐，App 内可切换各源）
│   ├── 单仓聚合.json               # 单仓聚合（所有源 sites 合并成一个，开箱即用）
│   ├── shield.json                # shields.io 状态徽章数据
│   ├── status.json                # 各源更新状态（机器可读，含检测报告）
│   └── &lt;源id&gt;.json                # 各源独立配置（文件名对应 sources.json 的 id）
├── requirements.txt
└── README.md
```

## 三、在影视仓 / TVBox 中使用

部署完成后，把下面任一地址填入 App 的「设置 → 配置地址」：

| 类型 | 地址 | 说明 |
| --- | --- | --- |
| **多仓订阅（推荐）** | `https://raw.githubusercontent.com/OWNER/REPO/BRANCH/output/多仓订阅.json` | App 内可在各源之间切换 |
| **在线浏览站（Pages）** | `https://OWNER.github.io/REPO/` | 网页里浏览 / 复制 / 下载 `output/` 下所有配置 |
| **单仓聚合** | `https://raw.githubusercontent.com/OWNER/REPO/BRANCH/output/单仓聚合.json` | 所有源站点合并成一个，无需切换 |
| 单仓 · 单个源 | `https://raw.githubusercontent.com/OWNER/REPO/BRANCH/output/&lt;源id&gt;.json` | 单独使用某一个源，文件名即 `sources.json` 里的 `id` |

> 多仓订阅与单仓文件里的地址由脚本根据 `GITHUB_REPOSITORY` 环境变量自动生成。若国内直连 raw.githubusercontent.com 慢，可套一层 `ghproxy` / `gh-proxy` 等加速前缀。

> Pages 在线浏览站需要在仓库 `Settings → Pages → Build and deployment → Source` 选择 **GitHub Actions**；推荐把默认分支与更新工作流保持一致。

## 四、自动更新机制

- 推送后，GitHub 自动识别 `.github/workflows/update.yml`。
- 默认每天 **02:00 和 14:00（UTC，即北京时间 10:00 和 22:00）** 自动更新一次。
- 也可在 **Actions → Auto Update TVBox Configs → Run workflow** 手动触发。
- 更新结果自动提交回当前分支（默认分支），README 状态区（默认仅汇总）与徽章同步刷新。
- 工作流使用内置 `GITHUB_TOKEN`，无需配置任何密钥。

## 五、自定义配置源

编辑 `config/sources.json` 即可增删配置源或更换地址：

```json
{
  "id": "mysource",
  "name": "我的源",
  "urls": [
    "https://example.com/tv",
    "https://backup.example.com/tv"
  ]
}
```

- `id`：输出文件名（建议英文小写）。
- `name`：在多仓订阅与状态表中展示的名字。
- `urls`：依次尝试的地址列表，第一个成功的会被使用；可按"主地址在前、备用在后"排列。
- 修改后推送到默认分支即会自动触发一次更新。

## 六、本地手动更新（可选）

```bash
pip install -r requirements.txt
REPO=OWNER/REPO BRANCH=BRANCH python scripts/update.py
```

## 七、脚本特性

- **中文域名自动转 punycode**：`中文域名.com` → `xn--fiq228c.com`，避免 DNS 解析失败。
- **多地址回退**：每个源按顺序尝试主/备用地址，单个地址失败不影响其他源。
- **智能重试**：仅对超时、连接错误、空响应等瞬态问题退避重试；4xx/5xx 等确定性失败直接跳过。
- **反爬兼容**：使用简洁 UA（部分接口对完整浏览器 UA 下发挑战页），自动剔除 JSON 内嵌的 `//` 注释，兼容未转义控制字符。
- **健康检测，只生成有效源**：
  - 源级（默认严格）：某源的全部地址抓取失败即视为无效，**不再沿用旧缓存**，直接不进输出；
  - 站点级：对每个成功源的 `sites[]` 中 api 为 HTTP(S) 的接口做可达性探测，无响应的站点被剔除（`csp_`/`js:`/`py:` 等无法探测的 spider 原样保留）；
  - 直播级：对 `lives[]` 的播放列表地址、以及内联 `channels[].urls`（含 `proxy://...ext=<url>`）做可达性探测，全部不可达的 lives 项被剔除。
  - 可通过环境变量调整：`CHECK_SITES`、`CHECK_LIVES`（开关）、`PROBE_TIMEOUT`（默认 8 秒）、`PROBE_WORKERS`（默认 16）、`LIVE_CHANNEL_PROBES`（每个 lives 项最多探测的内联频道数，默认 0=不限制）、`ALLOW_STALE_SOURCES=1`（恢复"失败沿用缓存"）。每次检测结果写入 `status.json` 的 `sources[].checks`。
- **聚合单仓**：把所有成功源的 `sites` 按 key 去重合并（重复 key 自动加源前缀），`lives/parses` 等列表合并去重。
- **状态可视化**：每次更新自动刷新 shields.io 徽章；README 状态区默认只写汇总（`成功 N/M 个源`），不暴露各源名称与地址；需要显示明细时设环境变量 `README_STATUS_DETAILS=1`（完整状态始终保存在 `status.json`）。
- **部分成功可用**：只要有一个源成功就产出可用订阅，失败源记录在 `status.json` 与 README 状态表。

## 八、常见问题

- **某个源一直失败？** 大部分源做了地区/频率限制，GitHub Actions 与本地网络环境不同，可能某个环境能访问、另一个不能。脚本会按 `urls` 顺序自动尝试所有备用地址，只把成功的写入。
- **自动提交会不会死循环？** 不会。push 触发只监听 `config/`、`scripts/`、`requirements.txt` 的变更，自动提交只改 `output/` 与 README 状态区，不会再次触发。

## 九、免责声明

本项目仅用于学习、测试与个人使用；聚合的接口资源来自互联网公开分享，版权归原作者所有。若涉及侵权，请联系删除相应内容。
