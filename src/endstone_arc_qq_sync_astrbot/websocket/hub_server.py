"""
Hub 中转平台服务端
第一个启动的 MC 服务器插件会启动此 Hub，后续服务器作为客户端连入。
Hub 统一管理与 NapCat（QQ Bot）的 WebSocket 连接，负责跨服消息路由和 QQ 群消息收发。
"""

import asyncio
import json
import time
import uuid
from typing import Dict, List, Optional, Set

from ..utils.imports import import_websockets
websockets = import_websockets()

from ..utils.time_utils import TimeUtils
from ..utils.message_utils import strip_minecraft_format_codes
from ..core.restart_vote_manager import RestartVoteManager


class HubServer:
    """Hub 中转平台服务端"""

    def __init__(self, plugin, logger):
        self.plugin = plugin
        self.logger = logger

        # 配置
        self.host = plugin.config_manager.get_config("hub_host", "127.0.0.1")
        self.port = plugin.config_manager.get_config("hub_port", 19321)
        self.token = plugin.config_manager.get_config("hub_token", "")
        self.cross_server_broadcast = plugin.config_manager.get_config("cross_server_broadcast", True)

        # 已连接的 MC 服务器 {server_name: ws}
        self.connected_servers: Dict[str, object] = {}
        # ws -> server_name 反向映射
        self.ws_to_server: Dict[object, str] = {}

        # NapCat WS 连接
        self.napcat_ws: Optional[object] = None

        # 运行状态
        self._running = False
        self._server = None

        # 本服务器名称
        self.local_server_name = plugin.server_name

        # 子服稳定数字编号（同一名称重连保持同一编号，直至 Hub 进程退出）
        self._server_numeric_id_by_name: Dict[str, int] = {}
        self._next_server_numeric_id = 1

        self.restart_vote_manager = RestartVoteManager(logger)
        self._online_query_waiters: Dict[str, dict] = {}

    def _ensure_server_numeric_id(self, server_name: str) -> int:
        if server_name not in self._server_numeric_id_by_name:
            self._server_numeric_id_by_name[server_name] = self._next_server_numeric_id
            self._next_server_numeric_id += 1
        return self._server_numeric_id_by_name[server_name]

    def get_server_catalog(self) -> List[Dict]:
        """当前已分配编号的所有服务器（含 Hub 本机），按编号排序。"""
        items = [
            {"id": sid, "name": name}
            for name, sid in self._server_numeric_id_by_name.items()
        ]
        items.sort(key=lambda x: x["id"])
        return items

    async def start(self):
        """启动 Hub 服务端"""
        if self._running:
            self.logger.warning("Hub 服务端已在运行")
            return

        self._running = True
        local_id = self._ensure_server_numeric_id(self.local_server_name)
        self.plugin.hub_numeric_server_id = local_id
        self.logger.info(
            f"Hub 中转平台启动中... 监听 {self.host}:{self.port}（本机在 Hub 中的编号: {local_id}）"
        )

        # 启动 WS 服务器 + NapCat 连接
        await asyncio.gather(
            self._run_ws_server(),
            self._connect_napcat(),
        )

    async def _run_ws_server(self):
        """运行 WebSocket 服务器，接受 MC 服务器插件连接"""
        import logging as _logging
        # 抑制 websockets 内部对握手失败的 ERROR 级别日志（非 WS 客户端连接时会产生噪音）
        ws_logger = _logging.getLogger("websockets")
        old_level = ws_logger.level
        ws_logger.setLevel(_logging.CRITICAL)
        try:
            self._server = await websockets.serve(
                self._handle_server_connection,
                self.host,
                self.port,
                ping_interval=20,
                ping_timeout=10,
            )
            self.logger.info(f"Hub WebSocket 服务器已启动 ws://{self.host}:{self.port}")
            await self._server.wait_closed()
        except Exception as e:
            self.logger.error(f"Hub WebSocket 服务器启动失败: {e}")
            self._running = False
        finally:
            ws_logger.setLevel(old_level)

    async def _handle_server_connection(self, websocket, path=None):
        """处理 MC 服务器插件的连接"""
        server_name = None
        try:
            self.logger.info(f"新的服务器连接: {websocket.remote_address}")

            # 等待注册消息
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=15)
                data = json.loads(raw)
            except asyncio.TimeoutError:
                self.logger.warning("连接超时，未收到注册消息")
                await websocket.close()
                return
            except json.JSONDecodeError:
                self.logger.warning("收到无效的注册消息")
                await websocket.close()
                return

            # 验证注册消息
            if data.get("type") != "register":
                self.logger.warning(f"期望注册消息，收到: {data.get('type')}")
                await websocket.close()
                return

            # 验证 token
            if self.token and data.get("token") != self.token:
                self.logger.warning(f"Token 验证失败: {data.get('server_name')}")
                await websocket.close(1008, "认证失败")
                return

            server_name = data.get("server_name", "未知服务器")

            # 检查是否已有同名服务器
            if server_name in self.connected_servers:
                self.logger.warning(f"服务器 {server_name} 已存在，断开旧连接")
                old_ws = self.connected_servers[server_name]
                try:
                    await old_ws.close()
                except Exception:
                    pass
                self.ws_to_server.pop(old_ws, None)

            # 注册
            self.connected_servers[server_name] = websocket
            self.ws_to_server[websocket] = server_name
            remote_id = self._ensure_server_numeric_id(server_name)
            self.logger.info(
                f"✅ 服务器 [{server_name}] 已连接 Hub（子服编号: {remote_id}）"
            )

            # 发送欢迎消息
            welcome = {
                "type": "hub_welcome",
                "connected_servers": list(self.connected_servers.keys()),
                "hub_server_name": self.local_server_name,
                "my_server_id": remote_id,
                "server_catalog": self.get_server_catalog(),
            }
            await websocket.send(json.dumps(welcome))

            # 通知其他服务器（及 Hub 本机游戏内）有新成员连入
            if self.cross_server_broadcast:
                await self._fanout_cross_server(server_name, {
                    "type": "cross_server_event",
                    "from_server": server_name,
                    "event": "server_connected",
                })

            # 消息循环
            async for message in websocket:
                try:
                    msg_data = json.loads(message)
                    await self._handle_server_message(server_name, websocket, msg_data)
                except json.JSONDecodeError:
                    self.logger.warning(f"[{server_name}] 收到无效 JSON")

        except websockets.exceptions.ConnectionClosed:
            if server_name:
                self.logger.info(f"服务器 [{server_name}] 断开连接")
        except Exception as e:
            self.logger.error(f"处理服务器连接出错: {e}")
        finally:
            # 清理
            if server_name:
                self.connected_servers.pop(server_name, None)
                self.logger.info(f"服务器 [{server_name}] 已从 Hub 移除")
                if self.cross_server_broadcast:
                    await self._fanout_cross_server(server_name, {
                        "type": "cross_server_event",
                        "from_server": server_name,
                        "event": "server_disconnected",
                    })
            if websocket in self.ws_to_server:
                del self.ws_to_server[websocket]

    async def _handle_server_message(self, server_name: str, ws, data: dict):
        """处理来自 MC 服务器的消息"""
        msg_type = data.get("type")

        if msg_type == "game_event":
            await self._handle_game_event(server_name, data)
        elif msg_type == "api_send":
            # 其他插件通过 api_send_message 调用
            text = data.get("text", "")
            if text:
                await self._send_to_napcat_all_groups(text)
        elif msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif msg_type == "data_rpc":
            await self._handle_data_rpc(ws, data)
        elif msg_type == "restart_vote_online_list_response":
            self._handle_online_list_response(server_name, data)
        else:
            self.logger.debug(f"[{server_name}] 未知消息类型: {msg_type}")

    async def _handle_data_rpc(self, ws, data: dict):
        """Hub 集中处理各服的 data.json 读写"""
        request_id = data.get("request_id")
        action = data.get("action")
        args = data.get("args") or {}
        try:
            result = self._run_data_manager_action(action, args)
            await ws.send(
                json.dumps(
                    {
                        "type": "data_rpc_response",
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            self.logger.error(f"[data_rpc] {action} 失败: {e}")
            await ws.send(
                json.dumps(
                    {
                        "type": "data_rpc_response",
                        "request_id": request_id,
                        "ok": False,
                        "error": str(e),
                    },
                    ensure_ascii=False,
                )
            )

    def _run_data_manager_action(self, action: str, args: dict):
        dm = self.plugin.data_manager
        if action == "is_player_bound":
            return dm.is_player_bound(args["player_name"], args.get("player_xuid"))
        if action == "get_player_qq":
            return dm.get_player_qq(args["player_name"])
        if action == "get_qq_player":
            return dm.get_qq_player(args["qq_number"])
        if action == "get_qq_player_history":
            return dm.get_qq_player_history(args["qq_number"])
        if action == "get_player_by_xuid":
            return dm.get_player_by_xuid(args["xuid"])
        if action == "bind_player_qq":
            return dm.bind_player_qq(args["player_name"], args["player_xuid"], args["qq_number"])
        if action == "unbind_player_qq":
            return dm.unbind_player_qq(args["player_name"], args.get("admin_name", "system"))
        if action == "update_player_name":
            return dm.update_player_name(args["old_name"], args["new_name"], args["xuid"])
        if action == "update_player_join":
            return dm.update_player_join(args["player_name"], args.get("player_xuid"))
        if action == "update_player_quit":
            return dm.update_player_quit(args["player_name"])
        if action == "get_player_playtime_info":
            return dm.get_player_playtime_info(
                args["player_name"], self.plugin.server.online_players
            )
        if action == "is_player_banned":
            return dm.is_player_banned(args["player_name"])
        if action == "ban_player":
            return dm.ban_player(
                args["player_name"], args.get("admin_name", "system"), args.get("reason", "")
            )
        if action == "unban_player":
            return dm.unban_player(args["player_name"], args.get("admin_name", "system"))
        if action == "get_banned_players":
            return dm.get_banned_players()
        if action == "get_player_binding_history":
            return dm.get_player_binding_history(args["player_name"])
        if action == "get_complete_player_binding_status":
            return dm.get_complete_player_binding_status(args["player_name"], args["player_xuid"])
        if action == "start_player_timer":
            return dm.start_player_timer(args["player_name"], args.get("player_xuid"))
        if action == "stop_player_timer":
            return dm.stop_player_timer(args["player_name"])
        if action == "get_binding_data":
            return dm.binding_data
        if action == "merge_legacy_binding_snapshot":
            return dm.merge_legacy_binding_snapshot(
                args.get("snapshot") or {},
                args.get("source_server", ""),
            )
        if action == "merge_legacy_binding_one":
            return dm.merge_legacy_binding_one(
                args["player_name"],
                args.get("player_data") or {},
                args.get("source_server", ""),
            )
        if action == "merge_legacy_binding_persist":
            dm.merge_legacy_binding_persist()
            return None
        raise ValueError(f"未知 data_rpc 动作: {action}")

    async def _handle_game_event(self, server_name: str, data: dict):
        """处理游戏事件：发到 QQ 群 + 跨服广播"""
        event = data.get("event")
        player = data.get("player", "")
        message = data.get("message", "")
        session_count = data.get("session_count")
        playtime_str = data.get("playtime_str", "")

        # 构建 QQ 群消息（加服务器前缀）
        qq_msg = self._format_qq_message(server_name, event, player, message, session_count, playtime_str)
        if qq_msg:
            await self._send_to_napcat_all_groups(qq_msg)

        # 跨服广播（远端 WS 客户端 + Hub 本机 MC，若本机也是一台服务器）
        if self.cross_server_broadcast:
            await self._fanout_cross_server(server_name, {
                "type": "cross_server_event",
                "from_server": server_name,
                "event": event,
                "player": player,
                "message": message,
            })

    async def _fanout_cross_server(self, origin_server: str, data: dict):
        """向其它已连接的 MC 服推送；来源非本机时也在 Hub 所在进程的游戏内播报。"""
        await self._broadcast_to_others(origin_server, data)
        if origin_server != self.local_server_name:
            self._post_cross_server_to_local_players(data)

    def _post_cross_server_to_local_players(self, data: dict):
        """Hub 进程内运行的 MC 未接入自身 WS，需单独向本机在线玩家投递跨服消息。"""
        from ..utils.message_utils import format_cross_server_event_game_message

        game_msg = format_cross_server_event_game_message(data, self.local_server_name)
        if not game_msg:
            return

        def send():
            try:
                for player_obj in self.plugin.server.online_players:
                    player_obj.send_message(game_msg)
            except Exception as e:
                self.logger.error(f"Hub 本机跨服消息投递失败: {e}")

        self.plugin.server.scheduler.run_task(self.plugin, send, delay=1)

    def _format_qq_message(self, server_name: str, event: str, player: str,
                           message: str, session_count=None, playtime_str: str = "") -> str:
        """格式化发往 QQ 群的消息"""
        prefix = f"[{server_name}]\n"

        if event == "chat":
            return f"{prefix}💬 {player}: {message}"
        elif event == "join":
            if session_count is None:
                return f"{prefix}🟢 {player} 加入游戏"
            if session_count <= 1:
                return f"{prefix}🌟 {player} 首次进入服务器！"
            return f"{prefix}🟢 {player} 加入游戏 (第{session_count}次游戏)"
        elif event == "quit":
            if playtime_str:
                return f"{prefix}🔴 {player} 离开游戏 (总游戏时长: {playtime_str})"
            return f"{prefix}🔴 {player} 离开游戏"
        elif event == "death":
            return f"{prefix}💀 {player} {message}"
        elif event == "custom":
            return f"{prefix}{message}"
        elif event == "server_start":
            return f"{prefix}[ARC QQ Sync] 服务器已启动！"
        elif event == "server_stop":
            return f"{prefix}[ARC QQ Sync] 服务器已停止！"
        else:
            return f"{prefix}{message}" if message else ""

    async def _broadcast_to_others(self, from_server: str, data: dict):
        """将消息广播给除发送者外的所有服务器"""
        msg = json.dumps(data)
        for name, ws in list(self.connected_servers.items()):
            if name != from_server:
                try:
                    await ws.send(msg)
                except Exception:
                    pass

    async def _broadcast_to_all(self, data: dict):
        """将消息广播给所有连接的服务器"""
        msg = json.dumps(data)
        for name, ws in list(self.connected_servers.items()):
            try:
                await ws.send(msg)
            except Exception:
                pass

    # ─── NapCat 连接管理 ───

    async def _connect_napcat(self):
        """连接 NapCat WS（QQ Bot）"""
        napcat_ws_url = self.plugin.config_manager.get_config("napcat_ws")
        access_token = self.plugin.config_manager.get_config("access_token")
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}

        self.logger.info(f"Hub 正在连接 NapCat WS: {napcat_ws_url}")

        delay = 1
        consecutive_failures = 0

        while self._running:
            try:
                async with websockets.connect(
                    napcat_ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                ) as napcat_ws:
                    self.napcat_ws = napcat_ws
                    self.plugin._current_ws = napcat_ws
                    consecutive_failures = 0
                    self.logger.info("✅ Hub 已连接 NapCat WS")

                    # 消息循环与连接后初始化并行：成员列表响应需在消息循环中处理
                    napcat_msg_task = asyncio.ensure_future(self._napcat_message_loop(napcat_ws))
                    try:
                        await self._run_post_connect_tasks(napcat_ws)
                        await napcat_msg_task
                    except Exception:
                        napcat_msg_task.cancel()
                        raise

            except Exception as e:
                self.napcat_ws = None
                self.plugin._current_ws = None
                consecutive_failures += 1

                if self._running:
                    delay = min(30, delay * 1.5 if consecutive_failures > 1 else 5)
                    self.logger.warning(f"NapCat WS 连接失败 ({consecutive_failures}): {e}，{delay:.1f}s 后重试")
                    await asyncio.sleep(delay)
                else:
                    break

    async def _run_post_connect_tasks(self, napcat_ws):
        """连接成功后：拉取成员列表缓存 → 按需同步群名片 → 发送启动播报"""
        try:
            from .handlers import prepare_group_member_cache_and_wait, sync_all_group_cards
            await prepare_group_member_cache_and_wait(napcat_ws)
            if self.plugin.config_manager.get_config("sync_group_card", True):
                await sync_all_group_cards(napcat_ws)
        except Exception as e:
            self.logger.warning(f"Hub 启动时群成员/群名片同步失败: {e}")

        try:
            if hasattr(self.plugin, "_send_startup_message") and self.plugin._send_startup_message:
                from .handlers import send_group_msg_to_all_groups
                await send_group_msg_to_all_groups(napcat_ws,
                    f"[{self.local_server_name}]\n[ARC QQ Sync] Hub 中转平台已启动！")
                self.plugin._send_startup_message = False
        except Exception as e:
            self.logger.warning(f"发送 Hub 启动消息失败: {e}")

    async def _napcat_message_loop(self, napcat_ws):
        """处理来自 NapCat 的消息"""
        try:
            async for message in napcat_ws:
                try:
                    data = json.loads(message)
                    await self._handle_napcat_message(napcat_ws, data)
                except json.JSONDecodeError:
                    self.logger.warning(f"NapCat 无效 JSON: {message}")
        except websockets.exceptions.ConnectionClosed:
            self.logger.warning("NapCat WS 连接已关闭")

    async def _handle_napcat_message(self, napcat_ws, data: dict):
        """处理 NapCat 消息，将 QQ 群消息转发给所有服务器"""
        from .handlers import handle_message, handle_api_response, handle_group_member_change

        post_type = data.get("post_type")

        if post_type == "message":
            # 处理 QQ 群消息
            if data.get("message_type") == "group":
                group_id = data.get("group_id")
                target_groups = self.plugin.config_manager.get_config("target_groups", [])
                target_groups = [int(gid) for gid in target_groups]

                if group_id in target_groups:
                    # 解析消息显示名
                    user_id = data.get("user_id")
                    sender = data.get("sender", {})
                    nickname = sender.get("nickname", "未知")
                    card = sender.get("card", "")
                    raw_message = data.get("raw_message", "")

                    # 获取绑定玩家名
                    bound_player = self.plugin.data_manager.get_qq_player(str(user_id))
                    if bound_player:
                        display_name = bound_player
                        # 同步群名片
                        if self.plugin.config_manager.get_config("sync_group_card", True):
                            current_card = (card or "").strip()
                            if current_card != bound_player:
                                try:
                                    from .handlers import set_group_card_in_all_groups
                                    await set_group_card_in_all_groups(napcat_ws, int(user_id), bound_player)
                                except Exception:
                                    pass
                    else:
                        display_name = card if card else nickname

                    # 命令：Hub 本地处理 + 转发给所有连接的服务器
                    if raw_message.startswith("/"):
                        from ..utils.message_utils import parse_hub_command_routing
                        from .handlers import handle_message, send_group_msg

                        eff_line, route_sid = parse_hub_command_routing(raw_message)
                        eff_cmd = eff_line.strip().split()[0].lstrip("/") if eff_line.strip() else ""
                        if eff_cmd == "重启":
                            await self._handle_restart_vote_command(
                                napcat_ws, int(user_id), int(group_id)
                            )
                            return
                        if route_sid is not None:
                            known = {c["id"] for c in self.get_server_catalog()}
                            if known and route_sid not in known:
                                await send_group_msg(
                                    napcat_ws,
                                    int(group_id),
                                    f"[{self.local_server_name}]\n"
                                    f"❌ 无编号 {route_sid} 的服务器。\n"
                                    f"💡 发送 /servers 查看当前子服列表",
                                )
                                return

                        try:
                            await handle_message(napcat_ws, data)
                        except Exception as cmd_err:
                            self.logger.error(f"Hub 本机处理群内命令失败: {cmd_err}")
                        finally:
                            # Hub 本机处理失败时仍转发子服，避免仅 NapCat/本机异常导致集群内命令全无响应
                            if self.connected_servers:
                                sender = data.get("sender") or {}
                                await self._broadcast_to_all({
                                    "type": "command_forward",
                                    "raw_message": raw_message,
                                    "command_line": eff_line,
                                    "target_server_id": route_sid,
                                    "user_id": user_id,
                                    "display_name": display_name,
                                    "group_id": group_id,
                                    "sender_role": sender.get("role", "member"),
                                })
                        return

                    # 转发 QQ 消息给所有服务器
                    from ..utils.message_utils import (
                        parse_qq_message,
                        clean_message_text,
                        truncate_message,
                    )
                    group_names = self.plugin.config_manager.get_config("group_names", {})
                    group_name = group_names.get(str(group_id), "")

                    parsed_message = parse_qq_message(data)
                    parsed_message = clean_message_text(parsed_message)
                    parsed_message = truncate_message(parsed_message, max_length=150)

                    if parsed_message and parsed_message != "[空消息]":
                        await self._broadcast_to_all({
                            "type": "qq_message",
                            "display_name": display_name,
                            "message": parsed_message,
                            "group_name": group_name,
                        })

                        # Hub 本机也要显示
                        self._send_to_local_players(
                            display_name, parsed_message, group_name
                        )

                        # 经 ARC Sync 下发到 QQ_RELAY_MODE=host 的子服
                        try:
                            self.plugin.notify_arc_qq_chat(
                                display_name, parsed_message, group_name
                            )
                        except Exception:
                            pass

                        # webui
                        webui = self.plugin.server.plugin_manager.get_plugin('qqsync_webui_plugin')
                        if webui:
                            try:
                                webui.on_message_sent(sender=display_name, content=parsed_message,
                                                     msg_type="chat", direction="qq_to_game")
                            except Exception:
                                pass

            # 非群消息交给原有处理
            else:
                await handle_message(napcat_ws, data)

        elif "echo" in data:
            await handle_api_response(data)
        elif post_type == "notice":
            notice_type = data.get("notice_type")
            if notice_type in ["group_increase", "group_decrease"]:
                await handle_group_member_change(data)
        elif post_type == "meta_event":
            pass  # 心跳等，忽略

    def _send_to_local_players(
        self, display_name: str, message: str, group_name: str = ""
    ):
        """发送消息给 Hub 本机的在线玩家"""
        from ..utils.message_utils import format_qq_group_chat_game_message

        game_message = format_qq_group_chat_game_message(
            display_name, message, group_name
        )

        def send():
            try:
                for player in self.plugin.server.online_players:
                    player.send_message(game_message)
            except Exception as e:
                self.logger.error(f"发送本地消息失败: {e}")

        self.plugin.server.scheduler.run_task(self.plugin, send, delay=1)

    async def _send_to_napcat_all_groups(self, text: str):
        """通过 NapCat 向所有 QQ 群发送消息"""
        if not self.napcat_ws:
            self.logger.warning("NapCat WS 未连接，无法发送消息")
            return

        from .handlers import send_group_msg_to_all_groups
        try:
            await send_group_msg_to_all_groups(self.napcat_ws, text=text)
        except Exception as e:
            self.logger.error(f"Hub 发送 QQ 群消息失败: {e}")

    # ─── 重启投票 ───

    def _get_server_id_by_name(self, server_name: str) -> Optional[int]:
        for name, sid in self._server_numeric_id_by_name.items():
            if name == server_name:
                return sid
        return None

    def _local_online_player_names(self) -> List[str]:
        try:
            return [p.name for p in self.plugin.server.online_players]
        except Exception:
            return []

    def _handle_online_list_response(self, server_name: str, data: dict) -> None:
        request_id = data.get("request_id")
        if not request_id:
            return
        waiter = self._online_query_waiters.get(request_id)
        if not waiter:
            return
        waiter["data"][server_name] = list(data.get("players") or [])
        pending: Set[str] = waiter["pending"]
        pending.discard(server_name)
        if not pending and not waiter["future"].done():
            waiter["future"].set_result(True)

    async def query_all_servers_online(self, timeout: float = 2.0) -> Dict[str, List[str]]:
        """查询 Hub 本机及所有已连接子服的在线玩家名单。"""
        result: Dict[str, List[str]] = {
            self.local_server_name: self._local_online_player_names()
        }
        if not self.connected_servers:
            return result

        request_id = f"rq_{uuid.uuid4().hex}"
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._online_query_waiters[request_id] = {
            "future": future,
            "pending": set(self.connected_servers.keys()),
            "data": {},
        }
        payload = json.dumps({
            "type": "restart_vote_online_list",
            "request_id": request_id,
        })
        for name, ws in list(self.connected_servers.items()):
            try:
                await ws.send(payload)
            except Exception as e:
                self.logger.warning(f"向 [{name}] 查询在线玩家失败: {e}")
                self._handle_online_list_response(name, {
                    "request_id": request_id,
                    "players": [],
                })

        try:
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            waiter = self._online_query_waiters.pop(request_id, None)
            if waiter:
                result.update(waiter.get("data", {}))

        return result

    def _find_player_server(
        self, player_name: str, online_by_server: Dict[str, List[str]]
    ) -> Optional[str]:
        matches = [
            server_name
            for server_name, players in online_by_server.items()
            if player_name in players
        ]
        if not matches:
            return None
        if len(matches) > 1:
            self.logger.warning(
                f"玩家 {player_name} 在多台服务器同时在线: {matches}，取首台"
            )
        return matches[0]

    async def _send_restart_vote_group_msg(self, group_id: int, text: str) -> None:
        if not self.napcat_ws:
            return
        from .handlers import send_group_msg
        await send_group_msg(self.napcat_ws, group_id, text)

    async def _on_restart_vote_timeout(self, server_id: int, vote_state) -> None:
        await self._send_restart_vote_group_msg(
            vote_state.group_id,
            f"❌ [{vote_state.server_name}] 重启投票已结束，未能在规定时间内获得足够票数",
        )

    async def _on_restart_vote_passed(self, vote_state) -> None:
        server_name = vote_state.server_name
        group_id = vote_state.group_id
        server_id = vote_state.server_id
        await self._send_restart_vote_group_msg(
            group_id,
            f"✅ [{server_name}] 重启投票已通过！服务器即将关闭…",
        )
        self.restart_vote_manager.clear_vote(server_id)
        await self._execute_stop_on_server(server_name)

    async def _execute_stop_on_server(self, server_name: str) -> None:
        if server_name == self.local_server_name:
            from .handlers import _run_server_console_command
            _run_server_console_command("stop")
            return
        ws = self.connected_servers.get(server_name)
        if not ws:
            self.logger.warning(f"无法向 [{server_name}] 发送 stop：未连接")
            return
        try:
            await ws.send(json.dumps({"type": "restart_vote_execute_stop"}))
        except Exception as e:
            self.logger.error(f"向 [{server_name}] 发送重启指令失败: {e}")

    async def _handle_restart_vote_command(
        self, napcat_ws, user_id: int, group_id: int
    ) -> None:
        from .handlers import send_group_msg

        qq_str = str(user_id)
        dm = self.plugin.data_manager
        player_name = dm.get_qq_player(qq_str)
        if not player_name:
            await send_group_msg(
                napcat_ws,
                group_id,
                "❌ 请先使用 /绑定 <玩家名> 绑定游戏角色后再参与重启投票",
            )
            return

        online_by_server = await self.query_all_servers_online()
        target_server = self._find_player_server(player_name, online_by_server)
        if not target_server:
            await send_group_msg(
                napcat_ws,
                group_id,
                "❌ 您当前未在任何子服在线，无法发起或参与重启投票",
            )
            return

        server_id = self._get_server_id_by_name(target_server)
        if server_id is None:
            server_id = self._ensure_server_numeric_id(target_server)

        active_vote = self.restart_vote_manager.get_vote(server_id)
        if active_vote and active_vote.server_name != target_server:
            await send_group_msg(
                napcat_ws,
                group_id,
                f"❌ 投票目标服务器不一致，请稍后再试",
            )
            return

        if active_vote:
            if player_name not in active_vote.online_players:
                await send_group_msg(
                    napcat_ws,
                    group_id,
                    f"❌ 您当前在 [{target_server}] 在线，"
                    f"但本次投票针对 [{active_vote.server_name}]，无法参与",
                )
                return
            reply, passed = self.restart_vote_manager.add_vote(
                server_id,
                player_name,
                self._on_restart_vote_passed,
            )
        else:
            online_set = set(online_by_server.get(target_server, []))
            reply, passed = self.restart_vote_manager.start_vote(
                server_id=server_id,
                server_name=target_server,
                group_id=group_id,
                online_players=online_set,
                initiator_name=player_name,
                on_timeout=self._on_restart_vote_timeout,
                on_passed=self._on_restart_vote_passed,
            )

        await send_group_msg(napcat_ws, group_id, f"[{target_server}]\n{reply}")
        if passed:
            vote = self.restart_vote_manager.get_vote(server_id)
            if vote:
                await self._on_restart_vote_passed(vote)

    # ─── 外部接口 ───

    async def send_game_event(self, event: str, player: str = "", message: str = "",
                               session_count=None, playtime_str: str = "",
                               source_server_name: str = None):
        """供本机事件处理器 / 主机代发调用，发送游戏事件。

        :param source_server_name: 可选；代发时指定来源子服名，QQ 前缀与跨服广播均用此名。
        """
        origin = (source_server_name or "").strip() or self.local_server_name
        await self._handle_game_event(origin, {
            "type": "game_event",
            "event": event,
            "player": player,
            "message": message,
            "session_count": session_count,
            "playtime_str": playtime_str,
        })

    async def send_api_message(self, text: str):
        """供 api_send_message 调用"""
        await self._send_to_napcat_all_groups(text)

    async def transfer_and_stop(self):
        """通知连接的服务器后停止 Hub"""
        self.logger.info("Hub 正在关闭...")
        self.restart_vote_manager.cancel_all()
        self._running = False

        # 通知所有连接的服务器 Hub 即将关闭
        if self.connected_servers:
            for name, ws in list(self.connected_servers.items()):
                try:
                    await ws.send(json.dumps({
                        "type": "cross_server_event",
                        "from_server": self.local_server_name,
                        "event": "server_stop",
                        "message": "Hub 已关闭",
                    }))
                except Exception:
                    pass

        # 关闭所有服务器连接
        for name, ws in list(self.connected_servers.items()):
            try:
                await ws.close()
            except Exception:
                pass
        self.connected_servers.clear()
        self.ws_to_server.clear()

        # 关闭 NapCat 连接
        if self.napcat_ws:
            try:
                await self.napcat_ws.close()
            except Exception:
                pass
            self.napcat_ws = None

        # 关闭 WS 服务器并等待端口释放
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        self.logger.info("Hub 中转平台已停止")

    def stop(self):
        """停止 Hub（同步版本，用于 on_disable）"""
        self.logger.info("正在停止 Hub 中转平台...")
        self._running = False

        # 关闭所有服务器连接
        for name, ws in list(self.connected_servers.items()):
            try:
                asyncio.create_task(ws.close())
            except Exception:
                pass
        self.connected_servers.clear()
        self.ws_to_server.clear()

        # 关闭 NapCat 连接
        if self.napcat_ws:
            try:
                asyncio.create_task(self.napcat_ws.close())
            except Exception:
                pass
            self.napcat_ws = None

        # 关闭 WS 服务器
        if self._server:
            self._server.close()

        self.logger.info("Hub 中转平台已停止")
