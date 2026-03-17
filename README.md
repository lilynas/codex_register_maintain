# codex_register_maintain

这个仓库包含两条链路：

- **GitHub Actions（产出端）**：定时运行你的 `task_runner.py`，生成 `codex/*.json`，打包后发布到 GitHub Releases（可选 age 加密）。
- **VPS / Docker（维护端，可选）**：使用 WebUI 或脚本定时拉取 release、校验、解密/解压、落盘、全量 wham/usage 检测、移除 401、备份 402、调用 CPA 重载、Telegram 汇报。

---

## 目录结构

```
.
├── app/                        # Python WebUI（FastAPI）
│   ├── main.py                 # 路由与 API
│   ├── maintainer.py           # 核心维护逻辑（同步、检查、备份轮转）
│   └── templates/index.html    # 前端仪表盘
├── scheduler.py                # 后台定时调度器
├── entrypoint.sh               # Docker 启动脚本
├── Dockerfile                  # 容器镜像定义
├── docker-compose.yml          # 一键部署
├── vps_token_maintain.sh       # 独立 Bash 脚本（无需 WebUI）
├── .env.example                # Bash 脚本环境变量示例
├── requirements.txt            # Python 依赖
├── README.md                   # 本文件
├── README_repo_root.md         # Actions 生成端说明
└── README_vps_token_maintain.md # Bash 脚本维护端说明
```

---

## A) GitHub Actions（生成 + 发布 Release）

### 1) 你需要提供的业务脚本

仓库中需要包含：

- `task_runner.py`（或你自己的脚本入口）
- `requirements.txt`

要求：脚本运行后产出 `codex/*.json`。

### 2) Actions Secrets（按需配置）

#### 强烈推荐：加密发布

- `AGE_RECIPIENT`（推荐）：age 公钥（形如 `age1...`）
  - 设置后：Release 上传 `tokens.zip.age` + `manifest.json`。

#### 不加密发布（危险，不推荐 public repo）

- 如果 **未设置** `AGE_RECIPIENT`：workflow 默认**跳过 Release 上传**（避免误把明文 token 公开）。
- 只有你显式设置 `ALLOW_PLAINTEXT_RELEASE=true`（Secret），才允许上传明文 `tokens.zip`。

#### Telegram 通知（可选）

- `TG_BOT_TOKEN` / `TG_CHAT_ID`：有则发轻量通知；没配就自动跳过。

### 3) permissions

workflow 已包含：

```yaml
permissions:
  contents: write
```

因此使用 `${{ github.token }}` 即可创建/更新 release，无需 PAT。

---

## B-1) Docker 部署（推荐，含 WebUI）

### 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/lilynas/codex_register_maintain
cd codex_register_maintain

# 2. 启动容器
docker compose up -d

# 3. 打开 WebUI
# http://your-server-ip:8000
```

### 配置步骤（WebUI）

1. 打开浏览器访问 `http://your-server-ip:8000`
2. 点击左侧「**配置**」菜单
3. 填写各项参数，保存后立即生效：
   - **REPO_LIST**：要拉取的 GitHub 仓库，如 `owner/repo1,owner/repo2`
   - **AUTH_DIR / BACKUP_DIR**：Token 目录（容器内路径，默认 `/data/auths` 和 `/data/auths_backup`）
   - **age 加密**：勾选「启用 age 解密」后填写私钥路径
   - **CPA 配置**：API 基础 URL、认证路径、备用认证路径、Token
   - **Telegram 通知**（可选）：Bot Token 和 Chat ID
4. 回到「**仪表盘**」点击「▶ 立即执行」验证

### 挂载 age 私钥

```yaml
# docker-compose.yml 中取消注释：
volumes:
  - /etc/age/token-sync.key:/data/age.key:ro
```

或者在 WebUI 配置中填写容器内路径 `/data/age.key`，然后手动将私钥拷贝到 Docker 卷：

```bash
# 查找 volume 在宿主机的实际路径
docker volume inspect codex_register_maintain_token_data

# 拷贝私钥
sudo cp /etc/age/token-sync.key /var/lib/docker/volumes/codex_register_maintain_token_data/_data/age.key
sudo chmod 600 /var/lib/docker/volumes/codex_register_maintain_token_data/_data/age.key
```

### 调整定时间隔

默认每 1 小时自动运行一次。修改 `docker-compose.yml`：

```yaml
environment:
  SCHEDULE_HOURS: "2"   # 改为 2 小时；设 0 则禁用自动运行（只走 WebUI 手动触发）
```

---

## B-2) VPS 纯脚本部署（无 WebUI，原版 Bash 脚本）

> 不需要 WebUI，只想跑 Bash 脚本的用户看这里。

### 1) 依赖

```bash
apt-get update -y
apt-get install -y curl jq unzip coreutils age
```

### 2) 放置脚本

```bash
chmod +x ./vps_token_maintain.sh
```

### 3) 配置 env

```bash
install -m 600 .env.example /etc/vps_token_maintain.env
nano /etc/vps_token_maintain.env
```

### 4) 设置定时任务

**cron（最简）：**

```bash
crontab -e
# 加一行：
0 * * * * ENV_FILE=/etc/vps_token_maintain.env bash /path/to/vps_token_maintain.sh >/dev/null 2>&1
```

**systemd timer（推荐）：**

```bash
# 创建 service 文件
cat >/etc/systemd/system/vps-token-maintain.service <<'EOF'
[Unit]
Description=VPS token maintain
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=ENV_FILE=/etc/vps_token_maintain.env
ExecStart=/bin/bash /path/to/vps_token_maintain.sh
EOF

# 创建 timer 文件
cat >/etc/systemd/system/vps-token-maintain.timer <<'EOF'
[Unit]
Description=Run VPS token maintain hourly

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now vps-token-maintain.timer
```

---

## C) age 加密完整指南

> 本节从零开始讲解 age 加密如何与本项目配合，适合第一次使用的用户。

### 什么是 age？

[age](https://github.com/FiloSottile/age) 是一个现代、简单的文件加密工具。本项目的工作原理：

1. **Actions（生产者）** 用你的 **age 公钥** 加密 `tokens.zip` → 产出 `tokens.zip.age`，上传到 GitHub Release。
2. **VPS / Docker（消费者）** 用你的 **age 私钥** 解密 `tokens.zip.age`，得到实际的 token 文件。

⚠️ 私钥只存在你的 VPS 上，GitHub 只有公钥，即使 Release 被人下载也无法解密。

---

### Step 1：在 VPS 上安装 age

```bash
# Debian / Ubuntu
apt-get update -y && apt-get install -y age

# 验证安装
age --version
```

---

### Step 2：生成密钥对

```bash
# 创建存放目录（权限 700，只有 root 能读）
install -d -m 700 /etc/age

# 生成密钥（写入私钥文件，同时在 stdout 打印公钥）
age-keygen -o /etc/age/token-sync.key
# 输出示例：
# Public key: age1abcdefghij...xyz   ← 这是公钥，复制它！
# 私钥已写入 /etc/age/token-sync.key

# 锁定私钥权限（重要！）
chmod 600 /etc/age/token-sync.key
```

如果你忘记复制公钥，可以随时再次查看：

```bash
age-keygen -y /etc/age/token-sync.key
```

---

### Step 3：把公钥存入 GitHub Secrets

1. 打开你的 GitHub 仓库页面
2. 进入 **Settings → Secrets and variables → Actions**
3. 点击 **New repository secret**
4. Name 填 `AGE_RECIPIENT`，Value 粘贴刚才的公钥（`age1...` 开头的那行）
5. 保存

这样 Actions 下次运行时就会用该公钥加密 Release 资产。

---

### Step 4：在 VPS / Docker 中配置私钥路径

**Docker（WebUI）用户：**

在「配置」页面：
- 勾选「启用 age 解密」
- AGE_IDENTITY 私钥路径填 `/data/age.key`

然后拷贝私钥到数据卷（见上文「挂载 age 私钥」章节）。

**Bash 脚本用户：**

```bash
nano /etc/vps_token_maintain.env
# 添加或修改：
USE_AGE=1
AGE_IDENTITY="/etc/age/token-sync.key"
```

---

### Step 5：验证

**Actions 侧验证：**  
触发一次 Actions，检查 Release 是否产出了 `tokens.zip.age` 和 `manifest.json`。若仅产出 `tokens.zip`，说明 `AGE_RECIPIENT` Secret 未生效。

**VPS / Docker 侧验证：**  
手动触发一次运行（WebUI 点「▶ 立即执行」或 `bash vps_token_maintain.sh`），在日志中确认：

```
sha256 ok repo=... tag=...
sync done repo=... added=N
```

若看到 `age decryption failed`，请检查：
1. 私钥文件路径和权限（`chmod 600`）
2. 公钥和私钥是否配对（用正确的 `age-keygen -y` 重新确认公钥）
3. Release 资产是否确实为 `.zip.age`（Actions 的 `AGE_RECIPIENT` 是否设置正确）

---

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `age: permission denied` | 私钥权限过大 | `chmod 600 /etc/age/token-sync.key` |
| `age: no identity matched` | 公私钥不匹配 | 重新生成密钥对，更新 GitHub Secret |
| `sha256 mismatch` | 下载被截断或损坏 | 重新触发 Actions 生成新 Release |
| systemd 读不到私钥 | `ProtectHome=yes` 限制 | 把私钥放 `/etc/age/`，不要放 `~/.config/age/` |
| Docker 卷内私钥缺失 | 未挂载/拷贝私钥 | 参考「挂载 age 私钥」章节 |

---

## 数据目录结构（Docker /data 卷）

```
/data/
├── config.json       # WebUI 保存的配置
├── age.key           # age 私钥（需手动放置，chmod 600）
├── auths/            # 活跃 token 文件（*.json）
├── auths_backup/     # 额度耗尽(402) token 备份
├── logs/             # 运行日志（run-YYYYMMDD-HHMMSS.log）
├── state/            # 每个 repo 的增量同步状态
├── inbox/            # 下载的临时资产（自动清理）
└── work/             # 解压临时目录（自动清理）
```

---

## 安全说明

- 日志不会输出 TG Bot Token、age 私钥或 access_token 明文。
- wham/usage 检查使用 httpx 请求，Authorization header 不记录到日志。
- 建议：私钥 `chmod 600`，config.json 包含敏感信息时也应限制读取权限。
- Docker 容器内 `/data` 卷建议只允许运行用户访问。
