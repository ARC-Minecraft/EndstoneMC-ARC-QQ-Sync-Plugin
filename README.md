# ARC QQ Sync (AstrBot Version)

Endstone 服务器端 QQ 互通插件，通过 **AstrBot 弧光消息中心** 实现跨设备群服消息与指令联动。

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11+-green.svg)
![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)

仓库：[ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin)

## 架构说明

```
QQ / 其他平台
      ↕  AstrBot 平台适配器
AstrBot 插件「弧光消息中心」（WebSocket，默认 :19136）
      ↕  Hub JSON 协议
本插件（各 MC 子服，仅客户端）
```

- **AstrBot**：QQ 侧机器人框架，安装并启用「弧光消息中心」插件
- **弧光消息中心**：在 AstrBot 上开启中心服务，统一对接 QQ，并向各 MC 子服转发；**QQ ↔ 游戏账号绑定**权威数据保存在中心（`data.json` / data_rpc）
- **本插件**：安装在各 Minecraft 服务器上，连接弧光消息中心；自行上报进服 / 离服 / 聊天等事件，响应群指令；死亡播报等可由 ARCCore 通过 `api_send_event` / `api_send_raw` 调用

跨服 QQ 消息扇出由 **AstrBot 弧光消息中心** 完成，**不再经 ARCCore SyncServer 中继**。  
游戏时长 / 进服次数由 **ARCCore** 写入本服 SQLite（`player_local_info`），本插件仅查询展示。

## 当前功能

1. **消息转发**  
   进服 / 离服 / 聊天等由本插件上报至消息中心；QQ ↔ 游戏由中心双向转发；跨子服事件由中心扇出。

2. **指令响应**  
   支持群内远程指令，例如 `/cmd`、`/list`、`/tps`、`/info`、`/servers` 等。已识别的 ARC 服指令会拦截，避免 AstrBot AI 再回复。

3. **QQ 绑定**  
   游戏内绑定 / 解绑 / 查询；数据经 data_rpc 读写 AstrBot 中心，不再把时长统计写入绑定 JSON。

4. **对外 API**  
   `api_send_message` / `api_send_event` / `api_send_raw` 供 ARCCore 等插件发送群消息（如死亡、成就）。

## 前置要求

- Endstone `0.11+`（Python `3.11+`）
- 已部署并可连接的 **AstrBot 弧光消息中心**（插件目录：`astrbot_plugin_endstone_arc`）
- AstrBot 已接入 QQ（如 aiocqhttp / NapCat）
- （推荐）同服安装 **ARCCore**：用于时长统计查询与带标题的显示名

## 安装

从 [Releases](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin/releases) 或 [Actions](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin/actions) 下载 wheel，放入 Endstone 插件目录：

```text
~/bedrock_server/plugins/
```

包名：`endstone-arc-qq-sync-astrbot`（版本 `1.0.0`）

## 配置

首次启动会在插件数据目录生成 `config.json`：

```text
~/bedrock_server/plugins/arc_qq_sync_astrbot/config.json
```

与消息中心连接相关的主要项：

```json
{
  "server_name": "生存服",
  "hub_host": "127.0.0.1",
  "hub_port": 19136,
  "hub_token": "",
  "cross_server_broadcast": true
}
```

| 配置项 | 说明 |
|--------|------|
| `server_name` | 本子服显示名称 |
| `hub_host` / `hub_port` | AstrBot 弧光消息中心地址与端口（本机默认 `127.0.0.1:19136`；跨设备经 FRP 用公网 IP） |
| `hub_token` | 连接令牌（与中心 `auth_token` 一致） |
| `cross_server_broadcast` | 是否参与跨服广播（由中心扇出） |
| `admins` | 群指令管理员 QQ 号 |

端口约定（与 ARC 部署一致时）：

| 端口 | 用途 |
|------|------|
| **19135** | ARCCore 群服数据同步（`SYNC_SERVER_PORT` / FRP） |
| **19136** | AstrBot 弧光消息中心（本插件 `hub_port`） |

AstrBot 弧光消息中心侧需配置：`ws_port=19136`、`target_groups`、`platform_id`。

## 常用群指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/help` | 帮助信息 | 全员 |
| `/list` | 在线玩家 | 全员 |
| `/tps` | TPS / MSPT | 全员 |
| `/info` | 服务器信息 | 全员 |
| `/servers` | 查看已连接子服（由消息中心回复） | 全员 |
| `/cmd [子服编号] <命令>` | 执行控制台命令 | 管理员 |
| `/who <玩家名\|QQ号>` | 查询玩家绑定与 ARCCore 游戏统计 | 管理员 |

示例：

```text
/cmd say 欢迎大家
/cmd 2 list
/who Steve
```

## 与 ARCCore 的分工

| 职责 | 负责方 |
|------|--------|
| QQ ↔ MC 消息 / 跨服扇出 | AstrBot 弧光消息中心 + 本插件 |
| QQ 账号绑定 | AstrBot 中心（本插件 data_rpc） |
| 游戏时长 / 进服次数 | ARCCore `player_local_info`（本服库） |
| 跨服经济 / 签到 / 密码等 | ARCCore 数据同步（`player_basic_info` 等） |
| 死亡 / 成就等定制播报 | ARCCore 调用本插件 `api_send_*` |

## 路线图

- [x] 子服连接中心、指令响应
- [x] 全面切换为 AstrBot + 弧光消息中心对接
- [x] 跨服 QQ 中继移出 ARCCore，由消息中心扇出
- [x] 玩家时长 / 次数迁至 ARCCore SQLite；绑定权威在 AstrBot

## 许可证

MIT
