# subs-bot

面向 Telegram 的订阅管理与格式转换工具，支持订阅维护、节点聚合、流量与到期信息查询、短链生成及私密分享，适用于个人使用和群组协作管理。

## 仓库信息

- **GitHub**: `https://github.com/yyywming/subs-bot`
- **默认服务名**: `subs-bot.service`（systemd）
- **默认端口**: `8787`
- **配置密钥**: 统一存放在 `.env` 中，严禁上传代码仓库

## 功能

| 功能 | 说明 |
|------|------|
| 订阅管理 | 发送订阅链接自动添加，支持 Clash YAML / sing-box JSON / Base64 |
| 群组协作 | 群内授权成员共用订阅、回收站、路径规则和临时节点，私聊数据独立 |
| 消息识别 | 精确匹配菜单与主动输入，忽略群内无关聊天 |
| 授权管理 | 管理员在私聊通过 `/auth` 查看、添加和撤销授权，变更持久化 |
| 流量/到期 | 从响应头 `subscription-userinfo` 提取，列表按钮直接显示 |
| 自动命名 | 从 `Content-Disposition` / `profile-title` 取配置名 |
| 格式转换 | Clash Meta / Sing-box / Base64 / Surge / QX，点按钮生成短链 |
| 导出节点 | 全部订阅聚合导出，支持分页 |
| 临期列表 | 14 天内到期的订阅 |
| 回收站 | 删除后 30 天可恢复 |
| 路径对应 | 关键词→配置名称映射，支持按钮新增/删除 |
| GitHub 搜索 | `/g <关键词或链接>` 搜索公开代码 |
| 内联查询 | `@bot 关键词` 返回当前用户的私有订阅结果 |
| 私密分享 | `@bot Share [x份数] [id用户ID] [s分钟] 内容` 生成私密提取卡片 |
| 短链服务 | `/s/{code}` 重定向 |
| 临时节点 | 发送 ss:// 等分享链接加入临时列表 |

## 使用说明

### 私聊与群组

- 私聊管理个人订阅；群聊管理该群的共用订阅。不同群组和私聊之间相互隔离。
- 同一群内的授权成员可查看、添加、更新、重命名、删除、恢复和导出共用订阅。请只授权可信成员。
- 既有个人订阅不会自动转为群订阅；需要共用时，在群内重新添加。个人内联查询仍只返回个人订阅。
- 群内普通聊天、正文夹带的链接、单独的数字和 `x=1` 等文本不会触发提示或自动修改配置。
- 群内使用 `/list` 等命令、完整菜单文本、独立的订阅/节点链接，或以 `@机器人用户名` 开头的输入。查看编号详情可点击列表按钮，或发送 `@机器人用户名 1` / 回复 Bot 消息后发送编号。
- 路径规则推荐使用 `/path 关键词 配置名称`，或点击路径页面的新增按钮。
- 要让 Bot 接收群内的普通链接和菜单文本，需要在 BotFather 的 `/setprivacy` 中关闭隐私模式，或赋予 Bot 群管理员身份。否则使用命令、提及或回复 Bot 消息。

### 授权管理

管理员在与 Bot 的私聊中使用：

```text
/auth list
/auth add 123456789
/auth remove 123456789
```

也可以回复对方的消息后发送 `/auth add` 或 `/auth remove`。授权变更立即生效，并保存在 SQLite 中；重启后仍然有效。授权用户不能继续授权他人；管理员不能通过 Bot 指令撤销。

- `ALLOWED_USER_IDS`：初始使用白名单。留空表示公开使用，已显式撤销的用户仍会被拒绝。
- `ADMIN_USER_IDS`：授权管理权限，建议仅配置维护者。未设置该变量时，沿用 `ALLOWED_USER_IDS` 作为管理员名单，便于已有部署直接升级。
- 显式设置 `ADMIN_USER_IDS=` 会关闭 Bot 内授权管理；不会自动把第一个使用者提升为管理员。
- 授权名单只在管理员私聊中展示，不在群内公布。可使用 `/help` 查看 Bot 内帮助。

## 部署

```bash
# 1. clone
git clone https://github.com/yyywming/subs-bot.git /opt/subs-bot
cd /opt/subs-bot

# 2. 配置
cp .env.example .env
nano .env   # 必填 BOT_TOKEN, ALLOWED_USER_IDS, PUBLIC_BASE_URL
# 可选：ADMIN_USER_IDS（独立管理员名单）、GITHUB_TOKEN（提高 /g 搜索 API 限额）

# 3. 一键部署（venv + systemd）
./deploy.sh

# 4. 验证
curl http://localhost:8787/health
journalctl -u subs-bot -f
```

需要 Python 3.11+、systemd 和 Git。`.env` 中的 Token、授权名单和实际服务地址不要提交到仓库。同一 Bot Token 只运行一个轮询实例，避免 Telegram `getUpdates` 冲突。

默认 HTTP 端口为 8787；自定义端口时，健康检查使用对应的 `HTTP_PORT`。建议通过反向代理提供 HTTPS，并将 HTTP 服务绑定到回环地址。

## 迁移数据

订阅和动态授权均存放在 SQLite 中。先停止旧服务，再复制数据库，避免遗漏 WAL 中未合并的写入；迁移到新实例时保留 `.env` 中的管理员配置。

```bash
# 旧机器
systemctl stop subs-bot
scp /opt/subs-bot/data/subs.db 新机器:/opt/subs-bot/data/
systemctl start subs-bot
```

## 文件说明

```
bot.py          主程序（Telegram bot + HTTP server）
config.py       环境变量加载
convert.py      订阅解析 + 节点格式转换
db.py           SQLite 数据层
deploy.sh       一键部署脚本
requirements.txt Python 依赖
subs-bot.service systemd 服务模板
.env.example    配置模板（不含真实密钥）
```

## 常用运维

```bash
# 重启
systemctl restart subs-bot

# 看日志
journalctl -u subs-bot -f

# 更新代码
cd /opt/subs-bot && git pull && systemctl restart subs-bot
```
