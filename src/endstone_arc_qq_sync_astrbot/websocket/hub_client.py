"""
弧光消息中心客户端
MC 服务器插件通过此模块连接到 AstrBot 弧光消息中心，发送游戏事件并接收 QQ 消息和跨服事件。
"""

import asyncio
import json
import uuid
from typing import Any, Dict, Optional

from ..utils.imports import import_websockets
websockets = import_websockets()


class HubClient:
    """Hub 客户端 —— 连接到 Hub 中转平台"""

    def __init__(self, plugin, logger):
        self.plugin = plugin
        self.logger = logger

        # Hub 连接信息
        self.hub_host = plugin.config_manager.get_config("hub_host", "127.0.0.1")
        self.hub_port = plugin.config_manager.get_config("hub_port", 19135)
        self.token = plugin.config_manager.get_config("hub_token", "")
        self.server_name = plugin.server_name

        # WS 连接
        self.ws: Optional[object] = None
        self._running = False
        self._pending_rpc: Dict[str, asyncio.Future] = {}

        # 回调：收到 QQ 消息时调用
        self._on_qq_message = None
        # 回调：收到跨服事件时调用
        self._on_cross_server_event = None

    def _apply_hub_welcome_plugin_state(self, welcome: dict) -> None:
        """解析 Hub 欢迎包中的本机编号与子服目录（仅首个 welcome 走 recv，须在此写入插件状态）。"""
        if welcome.get("type") != "hub_welcome":
            return
        my_sid = welcome.get("my_server_id")
        if my_sid is not None:
            self.plugin.hub_numeric_server_id = my_sid
        catalog = welcome.get("server_catalog")
        if isinstance(catalog, list):
            self.plugin.hub_server_catalog = catalog
        if my_sid is not None and isinstance(catalog, list) and catalog:
            labels = ", ".join(
                f"[{c.get('id')}]{c.get('name')}" for c in catalog
            )
            self.logger.info(f"本机在 Hub 中的编号: {my_sid}；当前集群: {labels}")

    def _cleanup_hub_session(self) -> None:
        """单次 Hub WebSocket 会话结束：丢弃旧连接引用并唤醒挂起的 data_rpc。"""
        self.ws = None
        exc = ConnectionError("Hub 连接已断开")
        for fut in list(self._pending_rpc.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending_rpc.clear()

    async def connect_forever(self):
        """持续连接 Hub"""
        if self._running:
            self.logger.warning("Hub 客户端已在运行")
            return

        self._running = True
        self.logger.info(f"正在连接弧光消息中心 ws://{self.hub_host}:{self.hub_port} ...")

        delay = 1
        consecutive_failures = 0

        while self._running:
            try:
                uri = f"ws://{self.hub_host}:{self.hub_port}"
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                ) as ws:
                    self.ws = ws
                    consecutive_failures = 0
                    try:
                        # 发送注册消息
                        register_msg = {
                            "type": "register",
                            "server_name": self.server_name,
                        }
                        if self.token:
                            register_msg["token"] = self.token
                        await ws.send(json.dumps(register_msg))

                        # 等待欢迎消息
                        welcome = {}
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                            welcome = json.loads(raw)
                            if welcome.get("type") == "hub_welcome":
                                connected = welcome.get("connected_servers", [])
                                self.logger.info(f"✅ 已连接 Hub，当前在线服务器: {connected}")
                                self._apply_hub_welcome_plugin_state(welcome)
                            else:
                                self.logger.warning(f"收到非预期的欢迎消息: {welcome}")
                        except asyncio.TimeoutError:
                            self.logger.warning("等待 Hub 欢迎消息超时")

                        # 必须先启动收包循环，否则 data_rpc 无人 recv，会一直等到超时
                        reader_task = None
                        try:
                            reader_task = asyncio.create_task(self._message_loop(ws))
                            if welcome.get("type") == "hub_welcome":
                                await self._flush_legacy_binding_merge_if_pending()

                            # 发送服务器启动消息
                            if hasattr(self.plugin, "_send_startup_message") and self.plugin._send_startup_message:
                                await self.send_game_event("server_start")
                                self.plugin._send_startup_message = False

                            await reader_task
                        finally:
                            if reader_task is not None:
                                reader_task.cancel()
                                try:
                                    await reader_task
                                except asyncio.CancelledError:
                                    pass
                    finally:
                        self._cleanup_hub_session()

            except Exception as e:
                self._cleanup_hub_session()
                consecutive_failures += 1

                if self._running:
                    delay = min(30, delay * 1.5 if consecutive_failures > 1 else 5)
                    self.logger.warning(f"Hub 连接失败 ({consecutive_failures}): {e}，{delay:.1f}s 后重试")
                    await asyncio.sleep(delay)
                else:
                    break
            else:
                delay = 1

        self.logger.info("Hub 客户端已停止")

    async def _message_loop(self, ws):
        """消息循环"""
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                    await self._handle_hub_message(data)
                except json.JSONDecodeError:
                    self.logger.warning(f"Hub 发来无效 JSON")
        except websockets.exceptions.ConnectionClosed:
            self.logger.warning("Hub 连接已断开")
        except Exception as e:
            self.logger.error(f"Hub 消息循环错误: {e}")

    async def _handle_hub_message(self, data: dict):
        """处理来自 Hub 的消息"""
        msg_type = data.get("type")

        if msg_type == "qq_message":
            # QQ 群消息，转发到本机游戏
            if self._on_qq_message:
                self._on_qq_message(data)
            else:
                self._default_handle_qq_message(data)

        elif msg_type == "cross_server_event":
            # 跨服事件
            if self._on_cross_server_event:
                self._on_cross_server_event(data)
            else:
                self._default_handle_cross_server_event(data)

        elif msg_type == "pong":
            pass  # 心跳回复

        elif msg_type == "hub_welcome":
            connected = data.get("connected_servers", [])
            self.logger.info(f"Hub 服务器列表更新: {connected}")
            my_sid = data.get("my_server_id")
            if my_sid is not None:
                self.plugin.hub_numeric_server_id = my_sid
            catalog = data.get("server_catalog")
            if isinstance(catalog, list):
                self.plugin.hub_server_catalog = catalog
                if my_sid is not None:
                    labels = ", ".join(
                        f"[{c.get('id')}]{c.get('name')}" for c in catalog
                    )
                    self.logger.info(
                        f"本机在 Hub 中的编号: {my_sid}；当前集群: {labels}"
                    )

        elif msg_type == "hub_transfer":
            # 旧版本机 Hub 角色转移已废弃；消息中心固定在 AstrBot。
            self.logger.warning(
                "收到已废弃的 hub_transfer 消息，已忽略（请使用 AstrBot 弧光消息中心）"
            )

        elif msg_type == "command_forward":
            # Hub 转发的 QQ 群命令，在本机执行并回复
            await self._handle_command_forward(data)

        elif msg_type == "restart_vote_online_list":
            await self._handle_restart_vote_online_list(data)

        elif msg_type == "restart_vote_execute_stop":
            await self._handle_restart_vote_execute_stop()

        elif msg_type == "data_rpc_response":
            rid = data.get("request_id")
            fut = self._pending_rpc.get(rid)
            if fut and not fut.done():
                fut.set_result(data)

        else:
            self.logger.debug(f"未知 Hub 消息类型: {msg_type}")

    async def _flush_legacy_binding_merge_if_pending(self) -> None:
        """将启动前缓存的子服 data.json 逐条合并到 Hub，最后统一落盘；成功后清空本地文件。"""
        snap = getattr(self.plugin, "_pending_legacy_binding_merge", None)
        if not snap:
            return
        items = list(snap.items())
        total = len(items)
        created = merged = skipped = 0
        try:
            self.logger.info(f"正在逐条将本地 {total} 名玩家合并到 Hub（避免单次请求过大超时）...")
            for i, (name, pdata) in enumerate(items):
                if not isinstance(pdata, dict):
                    skipped += 1
                    continue
                status = await self.data_rpc(
                    "merge_legacy_binding_one",
                    {
                        "player_name": str(name),
                        "player_data": pdata,
                        "source_server": self.server_name,
                    },
                    timeout=30,
                )
                if status == "created":
                    created += 1
                elif status == "merged":
                    merged += 1
                else:
                    skipped += 1
                if total >= 20 and (i + 1) % 50 == 0:
                    self.logger.info(f"历史数据合并进度: {i + 1}/{total}")
                await asyncio.sleep(0)

            await self.data_rpc("merge_legacy_binding_persist", {}, timeout=120)
            self.logger.info(
                f"Hub 合并完成: 新建 {created} 条, 合并 {merged} 条, 跳过 {skipped} 条"
            )
        except Exception as e:
            self.logger.error(
                f"合并本地 data.json 到 Hub 失败: {e}，将保留本地文件以便下次连接重试"
            )
            try:
                await self.data_rpc("merge_legacy_binding_persist", {}, timeout=120)
            except Exception:
                pass
            return
        self.plugin._pending_legacy_binding_merge = None
        self.plugin.data_manager.clear_local_binding_file_only()

    async def data_rpc(
        self,
        action: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 30,
    ) -> Any:
        """向 Hub 请求集中式 data.json 读写（仅 Hub 客户端使用）。"""
        if not self.ws:
            raise ConnectionError("Hub 未连接，无法访问集中数据")
        args = args or {}
        req_id = str(uuid.uuid4())
        fut = asyncio.get_event_loop().create_future()
        self._pending_rpc[req_id] = fut
        try:
            await self.ws.send(
                json.dumps(
                    {
                        "type": "data_rpc",
                        "request_id": req_id,
                        "action": action,
                        "args": args,
                    },
                    ensure_ascii=False,
                )
            )
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"data_rpc 超时: {action}") from None
        finally:
            self._pending_rpc.pop(req_id, None)

        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "data_rpc 失败"))
        return resp.get("result")

    def _default_handle_qq_message(self, data: dict):
        """默认处理 QQ 消息：发送给本机所有在线玩家"""
        from ..utils.message_utils import format_qq_group_chat_game_message

        display_name = data.get("display_name", "")
        message = data.get("message", "")
        if not display_name or not message:
            return

        game_message = format_qq_group_chat_game_message(
            display_name, message, data.get("group_name", "")
        )

        def send():
            try:
                for player in self.plugin.server.online_players:
                    player.send_message(game_message)
            except Exception as e:
                self.logger.error(f"发送 QQ 消息到游戏失败: {e}")

        self.plugin.server.scheduler.run_task(self.plugin, send, delay=1)

        # webui
        webui = self.plugin.server.plugin_manager.get_plugin('qqsync_webui_plugin')
        if webui:
            try:
                display_name = data.get("display_name", "")
                message = data.get("message", "")
                webui.on_message_sent(sender=display_name, content=message,
                                     msg_type="chat", direction="qq_to_game")
            except Exception:
                pass

    def _default_handle_cross_server_event(self, data: dict):
        """默认处理跨服事件：在本机游戏内广播"""
        event = data.get("event")
        if event == "hub_transferring":
            new_hub = data.get("message", "未知")
            self.logger.info(f"Hub 角色正在转移给 [{new_hub}]，等待重新连接...")
            return

        from ..utils.message_utils import format_cross_server_event_game_message

        game_msg = format_cross_server_event_game_message(data, self.server_name)
        if not game_msg:
            return

        def send():
            try:
                for player_obj in self.plugin.server.online_players:
                    player_obj.send_message(game_msg)
            except Exception as e:
                self.logger.error(f"发送跨服消息失败: {e}")

        self.plugin.server.scheduler.run_task(self.plugin, send, delay=1)

    async def _handle_restart_vote_online_list(self, data: dict) -> None:
        """响应 Hub 查询本机在线玩家（用于重启投票）。"""
        if not self.ws:
            return
        request_id = data.get("request_id")
        if not request_id:
            return
        try:
            players = [p.name for p in self.plugin.server.online_players]
            await self.ws.send(json.dumps({
                "type": "restart_vote_online_list_response",
                "request_id": request_id,
                "players": players,
            }))
        except Exception as e:
            self.logger.warning(f"回复在线玩家查询失败: {e}")

    async def _handle_restart_vote_execute_stop(self) -> None:
        """Hub 投票通过后在本机执行 stop。"""
        try:
            from .handlers import _run_server_console_command
            _run_server_console_command("stop")
            self.logger.info("重启投票已通过，正在执行 stop 命令")
        except Exception as e:
            self.logger.error(f"执行 stop 失败: {e}")

    async def _handle_command_forward(self, data: dict):
        """处理 Hub 转发的 QQ 群命令，在本机执行并通过 Hub 回复"""
        raw_message = data.get("raw_message", "")
        command_line = data.get("command_line") or raw_message
        target_sid = data.get("target_server_id")
        user_id = data.get("user_id")
        display_name = data.get("display_name", "")
        group_id = data.get("group_id")

        if not raw_message or not raw_message.startswith("/"):
            return

        eff_cmd = (command_line or raw_message).strip().split()[0].lstrip("/")
        if eff_cmd == "重启":
            return

        my_id = getattr(self.plugin, "hub_numeric_server_id", None)
        if target_sid is not None and my_id is not None and my_id != target_sid:
            return

        try:
            from .handlers import _handle_group_command

            # 创建一个 mock ws 来捕获回复内容
            captured_replies = []

            class MockWS:
                async def send(self, msg):
                    import json
                    try:
                        data = json.loads(msg)
                        if data.get("action") == "send_group_msg":
                            captured_replies.append(data.get("params", {}).get("message", ""))
                    except Exception:
                        pass

            mock_ws = MockWS()
            sender = {"role": data.get("sender_role", "member")}
            await _handle_group_command(
                mock_ws, user_id, command_line, display_name, group_id, sender=sender
            )

            # 将捕获的回复通过 Hub 发送到 QQ 群（回复已包含服务器名前缀）
            for reply in captured_replies:
                if reply:
                    await self.send_api_message(reply)
        except Exception as e:
            self.logger.error(f"处理转发命令失败: {e}")

    async def send_game_event(self, event: str, player: str = "", message: str = "",
                               session_count=None, playtime_str: str = "",
                               source_server_name: str = None):
        """发送游戏事件到 Hub。

        :param source_server_name: 可选；代发时覆盖 payload 中的 server_name。
        """
        if not self.ws:
            self.logger.warning("Hub 未连接，无法发送事件")
            return

        payload = {
            "type": "game_event",
            "server_name": (source_server_name or "").strip() or self.server_name,
            "event": event,
            "player": player,
            "message": message,
        }
        if session_count is not None:
            payload["session_count"] = session_count
        if playtime_str:
            payload["playtime_str"] = playtime_str

        try:
            await self.ws.send(json.dumps(payload))
        except Exception as e:
            self.logger.error(f"发送游戏事件到 Hub 失败: {e}")

    async def send_api_message(self, text: str):
        """通过 Hub 发送消息到 QQ 群"""
        if not self.ws:
            self.logger.warning("Hub 未连接，无法发送消息")
            return

        try:
            await self.ws.send(json.dumps({"type": "api_send", "text": text}))
        except Exception as e:
            self.logger.error(f"通过 Hub 发送消息失败: {e}")

    def stop(self):
        """停止客户端"""
        self.logger.info("正在断开 Hub 连接...")
        self._running = False

        for fut in list(self._pending_rpc.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("Hub 客户端已停止"))
        self._pending_rpc.clear()

        if self.ws:
            try:
                asyncio.create_task(self.ws.close())
            except Exception:
                pass
            self.ws = None

        self.logger.info("Hub 客户端已断开")
