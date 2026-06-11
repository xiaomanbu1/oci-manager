# OCI Manager

一个轻量的**甲骨文云（Oracle Cloud / OCI）管理工具**，仿 R-Bot 的核心思路用 Python 重写。
同时提供 **Telegram 机器人** 和 **Web 控制台** 两套界面，单文件前端无构建、Docker 一键起。

## 功能

- **多账号** — 一份 config 管多个租户/区域，或直接在网页上传
- **网页配置账号** — 在「配置」页粘贴 OCI 标准配置文本（可一次贴多个 profile）+ 上传 PEM 私钥，自动解析、持久化、热加载，不用登服务器改文件
- **概览统计** — 按月成本、流量（估算）、订阅状态、配额（ARM核心/内存/硬盘）；Profile 列表按区域分组、可折叠
- **实例管理** — 列表、开机 / 关机 / 重启 / 终止
- **用户管理** — 列出 IAM 用户、创建用户、重置控制台密码、清除 2FA、删除用户
- **换 IP** — 删除当前临时公网 IP 并重新分配（甲骨文经典需求）
- **抢机** — 创建实例时容量不足（`Out of host capacity`）自动轮换可用域、间隔重试，后台任务跑，实时看日志
- **Telegram 机器人** — 内联键盘：选账号 → 选实例 → 操作，带白名单鉴权
- **Web 控制台** — 控制台风格单页，概览 / 实例 / 用户 / 抢机 / 任务 五个页面

## 快速开始

```bash
# 1. 配置
cp config.example.yaml config.yaml
mkdir keys                     # 把各账号私钥 pem 放这里
vim config.yaml                # 填 OCI API 参数、Telegram、Web 口令

# 2a. Docker（推荐）
docker compose up -d
docker compose logs -f

# 2b. 或直接跑
pip install -r requirements.txt
python -m app.main
```

启动后访问 `http://你的IP:9527`。

## 部署最省心的方式（环境变量，无需 config.yaml）

`docker-compose.yml` 里直接用环境变量设访问口令，不依赖 config.yaml 文件，**彻底避开 Docker 把不存在的挂载文件建成目录的坑**：

```yaml
environment:
  - OCI_MANAGER_WEB_PASSWORD=你的访问口令
  # 想用 Telegram：
  # - OCI_MANAGER_TG_ENABLED=true
  # - OCI_MANAGER_TG_TOKEN=...
  # - OCI_MANAGER_TG_ADMINS=123456789
```

改完 `docker compose up -d` 即可。账号到网页「配置」页上传，持久化在 `data/`。

> 如果你更习惯用 config.yaml 文件，记得**先** `cp config.example.yaml config.yaml` 建好文件再 `up`，否则 Docker 会把它建成目录导致启动报 `IsADirectoryError`。

## 推荐：先部署，再网页上传账号

账号多时不用在服务器一个个传文件。`config.yaml` 里 `accounts` 留空（或干脆没有），直接起服务，然后：

1. 浏览器打开面板 → 顶部「配置」页
2. 把每个账号的 OCI 配置文本贴进去（控制台 API Keys 页的 *Configuration File Preview* 那段，支持一次贴多个 `[profile]`）
3. 选上对应的 PEM 私钥文件（可多选）
4. 点「上传配置」

匹配规则：profile 里 `key_file` 的文件名对上你上传的文件名；如果只传一个 PEM，就套用到所有 profile。上传后立即生效并持久化在 `data/` 目录（Docker 已挂卷，重启不丢）。右侧「已配置 N 个 Profile」可看状态、删除账号。

## OCI API 参数怎么拿

控制台右上角头像 → **User Settings → API Keys → Add API Key** → 生成并下载私钥。
页面会弹出 *Configuration File Preview*，里面的 `user` / `tenancy` / `fingerprint` / `region`
直接抄进 `config.yaml`，下载的 `.pem` 放进 `keys/`。

## Telegram 机器人

1. 找 @BotFather 建 bot 拿 token，填进 `config.yaml` 的 `telegram.token`
2. `enabled: true`，把你的 user id 填进 `admin_ids` 白名单
3. 给 bot 发 `/start`

命令：`/start` 操作面板 ｜ `/jobs` 查看抢机任务

## 抢机说明

Web 面板「抢机」页填好 Subnet OCID、Image OCID、规格，提交后到「任务」页看实时日志。
弹性规格（`A1.Flex` / `E*.Flex`）才需填 OCPU 和内存；固定规格留空。
容量不足会按设定的间隔和次数自动重试，并轮换可用域。

## 项目结构

```
app/
  config.py       配置加载（多账号）
  oci_client.py   OCI SDK 封装（核心操作都在这）
  service.py      服务层 + 后台抢机任务管理
  bot.py          Telegram 机器人
  web.py          FastAPI 后端
  main.py         入口（同进程跑 Web + Bot）
  templates/index.html   单文件前端
```

## 安全建议

- 公网部署务必设置 `web.password`，并配 `admin_ids` 白名单
- 私钥只存在你自己的服务器，别外泄
- 建议套一层反代 + HTTPS，别让 9527 裸奔

## 免责声明

仅供学习研究与个人云资源管理使用。使用者需遵守 OCI 服务条款及当地法律，
因使用本工具产生的任何后果由使用者自行承担。
