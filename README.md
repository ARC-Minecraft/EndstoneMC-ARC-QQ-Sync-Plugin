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
AstrBot 插件「弧光消息中心」（WebSocket :19321）
      ↕  Hub JSON 协议
本插件（各 MC 子服，仅客户端）
```

- **AstrBot**：QQ 侧机器人框架，安装并启用「弧光消息中心」插件
- **弧光消息中心**：在 AstrBot 上开启中心服务，统一对接 QQ，并向各 MC 子服转发
- **本插件**：安装在各 Minecraft 服务器上，连接弧光消息中心，上报游戏事件并响应群指令

消息转发策略由 **ARCCore（弧光核心）** 负责管理；本插件侧重子服侧连接、指令执行与临时统计。

## 当前功能

1. **消息转发**  
   游戏事件 / 聊天等上报至消息中心；QQ ↔ 游戏由中心双向转发。

2. **指令响应**  
   支持群内远程指令，例如 `/cmd`、`/list`、`/tps`、`/info` 等。

3. **玩家统计（过渡）**  
   在线时长、游玩次数等由消息中心集中存储；本插件经 data_rpc 读写。

## 前置要求

- Endstone `0.11+`（Python `3.11+`）
- 已部署并可连接的 **AstrBot 弧光消息中心**（插件目录：`astrbot_plugin_endstone_arc`）
- AstrBot 已接入 QQ（如 aiocqhttp / NapCat）

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
  "hub_port": 19135,
  "hub_token": "",
  "cross_server_broadcast": true
}
```

| 配置项 | 说明 |
|--------|------|
| `server_name` | 本子服显示名称 |
| `hub_host` / `hub_port` | AstrBot 弧光消息中心地址与端口（本机默认 `127.0.0.1:19135`；跨设备经 FRP 用公网 IP） |
| `hub_token` | 连接令牌（与中心 `auth_token` 一致） |
| `cross_server_broadcast` | 是否参与跨服广播（由中心扇出） |
| `admins` | 群指令管理员 QQ 号 |

`api_send_message` / `api_send_event` / `api_send_raw` 始终可用，供 ARCCore 等插件调用，无需额外开关。

AstrBot 弧光消息中心侧需配置：`ws_port=19135`、`target_groups`、`platform_id`。

## 常用群指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/help` | 帮助信息 | 全员 |
| `/list` | 在线玩家 | 全员 |
| `/tps` | TPS / MSPT | 全员 |
| `/info` | 服务器信息 | 全员 |
| `/servers` | 查看已连接子服（由消息中心回复） | 全员 |
| `/cmd [子服编号] <命令>` | 执行控制台命令 | 管理员 |
| `/who <玩家名\|QQ号>` | 查询玩家信息（含统计） | 管理员 |

示例：

```text
/cmd say 欢迎大家
/cmd 2 list
/who Steve
```

## 路线图

- [x] 子服连接中心、指令响应、基础统计
- [x] 全面切换为 AstrBot + 弧光消息中心对接
- [ ] 消息转发策略完全交由 ARCCore
- [ ] 玩家统计迁移至弧光核心

## 许可证

MIT
