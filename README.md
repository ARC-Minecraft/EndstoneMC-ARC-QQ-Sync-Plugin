# ARC QQ Sync (AstrBot Version)

Endstone 服务器端 QQ 互通插件，通过 **AstrBot 弧光 EndStone 消息中枢** 实现跨设备群服消息与指令联动。

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11+-green.svg)
![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)

仓库：[ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin)

## 架构说明

```
QQ / 其他平台
      ↕  AstrBot 平台适配器
AstrBot 插件「弧光EndStone消息中枢」（WebSocket，默认 :19136）
      ↕  Hub JSON 协议
本插件（各 MC 子服，仅客户端）
```

- **AstrBot**：QQ 侧机器人框架，安装并启用「弧光EndStone消息中枢」插件（目录名 `astrbot_plugin_endstone_arc`）
- **弧光 EndStone 消息中枢**：统一对接 QQ，并向各 MC 子服转发；**QQ ↔ 游戏账号绑定**权威数据保存在中枢（`data.json` / data_rpc）；群指令统一 `/mc` 前缀
- **本插件**：安装在各 Minecraft 服务器上，只配置 `hub_host` / `hub_port` / `hub_token`（及可选 `server_name`）连接中枢；上报进服 / 离服 / 聊天等；响应剥前缀后的群指令；死亡播报等可由 ARCCore 通过 `api_send_event` 调用

跨服事件扇出由 **消息中枢固定开启**（join / quit / chat / death / custom，以及中枢侧的 server_connected|disconnected），**不再经 ARCCore SyncServer 中继**。  
子服强制关闭时可能发不出 `server_stop`，故其他子服的启停提示以中枢 WebSocket 连上/断开为准。  
游戏时长 / 进服次数由 **ARCCore** 写入跨服/本服库，本插件仅查询展示。

## 当前功能

1. **消息转发**  
   进服 / 离服 / 聊天等由本插件上报至中枢；QQ ↔ 游戏由中枢双向转发；跨子服事件由中枢扇出。

2. **指令响应**  
   群内使用 `/mc help`、`/mc list`、`/mc cmd …` 等；中枢剥掉 `/mc` 后下发本插件（内部仍为 `/help`、`/list`、`/cmd`），避免与 AstrBot 自带指令冲突。

3. **QQ 绑定**  
   游戏内绑定 / 解绑 / 查询；数据经 data_rpc 读写 AstrBot 中枢。

4. **对外 API**  
   `api_send_message` / `api_send_event` / `api_send_raw` 供 ARCCore 等插件发送群消息（死亡请用 `event=death`，成就可用 `custom`）。

## 前置要求

- Endstone `0.11+`（Python `3.11+`）
- 已部署并可连接的 **AstrBot 弧光 EndStone 消息中枢**（插件目录：`astrbot_plugin_endstone_arc`）
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

**本插件连接配置**（群号、管理员、帮助、改群名片等均在 AstrBot「弧光EndStone消息中枢」）：

```json
{
  "hub_host": "127.0.0.1",
  "hub_port": 19136,
  "hub_token": "",
  "server_name": "弧光基岩重塑服务器"
}
```

| 配置项 | 说明 |
|--------|------|
| `hub_host` / `hub_port` | AstrBot 中枢地址与端口（本机默认 `127.0.0.1:19136`；跨设备经 FRP 用公网 IP） |
| `hub_token` | 连接令牌（与中枢 `auth_token` 一致） |
| `server_name` | **可选**；中枢注册名 / QQ 前缀。多服必须互不相同；留空则用 Endstone `server.name` |

跨服事件扇出由中枢固定开启，无需配置。

端口约定（与 ARC 部署一致时）：

| 端口 | 用途 |
|------|------|
| **19135** | ARCCore 群服数据同步（`SYNC_SERVER_PORT` / FRP） |
| **19136** | AstrBot 弧光 EndStone 消息中枢（本插件 `hub_port`） |

AstrBot 中枢侧需配置：`ws_port=19136`、`target_groups`、`admins`、`sync_group_card`、`platform_id`。

## 常用群指令

群内请以 **`/mc`** 开头（由消息中枢识别并转换）；下表为中枢剥前缀后本插件实际处理的指令形式。

| 群内写法 | 本插件收到 | 说明 | 权限 |
|------|------|------|------|
| `/mc help` | `/help` | 帮助信息 | 全员 |
| `/mc list` | `/list` | 在线玩家 | 全员 |
| `/mc tps` | `/tps` | TPS / MSPT | 全员 |
| `/mc info` | `/info` | 服务器信息 | 全员 |
| `/mc servers` | `/servers` | 查看已连接子服（可由消息中枢直接回复） | 全员 |
| `/mc cmd [子服编号] <命令>` | `/cmd …` | 执行控制台命令 | 管理员 |
| `/mc who <玩家名\|QQ号>` | `/who …` | 查询玩家绑定与 ARCCore 游戏统计 | 管理员 |
| `/mc 绑定 <玩家名>` | `/绑定 …` | 绑定 QQ 到游戏角色 | 全员 |
| `/mc 重启` | `/重启` | 重启投票 | 已绑定且在线 |

示例：

```text
/mc help
/mc cmd say 欢迎大家
/mc cmd 2 list
/mc who Steve
/mc 绑定 Steve
```

## 与 ARCCore 的分工

| 职责 | 负责方 |
|------|--------|
| QQ ↔ MC 消息 / 跨服扇出 | AstrBot 弧光 EndStone 消息中枢 + 本插件 |
| QQ 账号绑定 | AstrBot 中枢（本插件 data_rpc） |
| 游戏时长 / 进服次数 | ARCCore（跨服 `player_basic_info` 等） |
| 跨服经济 / 签到 / 密码等 | ARCCore 数据同步 |
| 死亡播报 | ARCCore `api_send_event("death", …)` → 本插件 → 中枢扇出 |
| 成就等定制播报 | ARCCore `api_send_event("custom", …)` / `api_send_*` |

## 路线图

- [x] 子服连接中心、指令响应
- [x] 全面切换为 AstrBot + 弧光消息中心对接
- [x] 跨服 QQ 中继移出 ARCCore，由消息中心扇出
- [x] 玩家时长 / 次数迁至 ARCCore SQLite；绑定权威在 AstrBot

## 许可证

MIT
