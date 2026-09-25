# subs-bot

Telegram 订阅管理机器人，复刻自 @MxlDYBot。

## 仓库信息

- **GitHub**: `https://github.com/Wumingyd/subs-bot`
- **默认服务名**: `subs-bot.service`（systemd）
- **默认端口**: `8787`
- **配置密钥**: 统一存放在 `.env` 中，严禁上传代码仓库

## 功能

| 功能 | 说明 |
|------|------|
| 订阅管理 | 发送订阅链接自动添加，支持 Clash YAML / sing-box JSON / Base64 |
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

## 部署位置

生产环境在小米 10 的 Droidspaces 容器 `server` 里：

- 代码目录：`/opt/subs-bot`
- 数据目录：`/opt/subs-bot/data`
- systemd：`subs-bot.service`
- 健康检查：`http://127.0.0.1:8787/health`
- Magisk 看门狗：`99-mi10-server.sh` 连续 3 次 health 失败后 `systemctl restart subs-bot`

## 迁移到新 VPS

```bash
# 1. clone
git clone git@github.com:Wumingyd/subs-bot.git /opt/subs-bot
cd /opt/subs-bot

# 2. 配置
cp .env.example .env
nano .env   # 必填 BOT_TOKEN, ALLOWED_USER_IDS, PUBLIC_BASE_URL
# 可选：GITHUB_TOKEN（提高 /g 搜索 API 限额）

# 3. 一键部署（venv + systemd）
./deploy.sh

# 4. 验证
curl http://localhost:8787/health
journalctl -u subs-bot -f
```

## 迁移数据

数据库是 SQLite，直接拷文件：

```bash
# 旧机器
scp /opt/subs-bot/data/subs.db 新机器:/opt/subs-bot/data/
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
