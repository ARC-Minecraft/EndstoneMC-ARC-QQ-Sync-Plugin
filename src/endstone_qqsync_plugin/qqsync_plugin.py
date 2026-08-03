import asyncio
import json
import threading
import time
from pathlib import Path

from endstone.plugin import Plugin
from endstone import ColorFormat

# 导入核心模块
from .core import (
    ConfigManager,
    DataManager,
    EventHandlers
)
from .core.restart_vote_manager import RestartVoteManager
from .websocket import WebSocketClient
from .websocket.handlers import set_plugin_instance, send_group_msg_to_all_groups
from .websocket.hub_server import HubServer
from .websocket.hub_client import HubClient
from .ui import UIManager
from .utils.time_utils import TimeUtils


class qqsync(Plugin):
    """QQsync群服互通插件主类"""

    api_version = "0.11"
    
    # 定义命令
    commands = {
        "bindqq": {
            "description": "QQ绑定相关命令",
            "usages": ["/bindqq"],
            "aliases": ["qq"],
            "permissions": ["qqsync.command.bindqq"]
        }
    }
    
    # 定义权限
    permissions = {
        "qqsync.command.bindqq": {
            "description": "允许使用 /bindqq 命令",
            "default": True
        }
    }
    
    def on_load(self) -> None:
        self.logger.info(f"{ColorFormat.BLUE}qqsync_plugin {ColorFormat.WHITE}正在加载...{ColorFormat.RESET}")
        


    def on_enable(self) -> None:
        """插件启用"""
        try:
            # 初始化管理器
            self._init_managers()

            # Hub 子服编号（由 Hub 欢迎包或本机启动 Hub 时写入）
            self.hub_numeric_server_id = None
            self.hub_server_catalog = []
            
            # 初始化WebSocket相关
            self._init_websocket()
            
            # 设置启动消息标志
            self._send_startup_message = True
            
            # 注册事件处理器
            self.register_events(self.event_handlers)
            
            # 设置全局插件实例引用
            set_plugin_instance(self)

            # 启动消息
            startup_msg = f"{ColorFormat.GREEN}qqsync_plugin {ColorFormat.YELLOW}已启用{ColorFormat.RESET}"
            self.logger.info(startup_msg)
            welcome_msg = f"{ColorFormat.BLUE}欢迎使用QQsync群服互通插件，{ColorFormat.YELLOW}作者：yuexps{ColorFormat.RESET}"
            self.logger.info(welcome_msg)
            
        except Exception as e:
            self.logger.error(f"插件启用失败: {e}")
            raise

    def _init_managers(self):
        """初始化各种管理器"""
        # 配置管理器
        self.config_manager = ConfigManager(Path(self.data_folder), self.logger)
        
        # 数据管理器
        self.data_manager = DataManager(Path(self.data_folder), self.logger, self)
        
        # 事件处理器
        self.event_handlers = EventHandlers(self)
        
        # UI管理器
        self.ui_manager = UIManager(self)

        # 重启投票（Hub 模式下由 HubServer 持有独立实例；单机回退用此实例）
        self.restart_vote_manager = RestartVoteManager(self.logger)
        
        # 群成员缓存
        self.group_members = set()
        # 群成员列表 API 返回的群名片：group_id -> { qq_str -> card }
        self.group_member_cards = {}
        self.logged_left_players = set()
        
        self.logger.info(f"{ColorFormat.AQUA}管理器初始化完成{ColorFormat.RESET}")

    def _init_websocket(self):
        """初始化WebSocket连接 — 根据配置决定启动 Hub 还是客户端"""
        # 确保没有重复的事件循环
        if hasattr(self, '_loop') and self._loop:
            self.logger.warning("检测到旧的事件循环，正在清理")
            self._loop.call_soon_threadsafe(self._loop.stop)

        # 创建专用事件循环
        self._loop = asyncio.new_event_loop()

        # 在新线程里启动该循环
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # hub_is_hub=True：无现有 Hub 时在本机创建 Hub；检测到已有 Hub 则只连接并写入 hub_is_hub=False
        # hub_is_hub=False：始终只连接，不监听 Hub 端口
        if self.config_manager.get_config("hub_is_hub", True):
            self._start_hub_when_free_or_connect()
        else:
            self._start_hub_client()

    def _start_hub_server(self):
        """启动 Hub 服务端模式"""
        self.data_manager.enable_remote_hub_mode(False)
        self.logger.info("以 Hub 模式启动...")
        self._hub_server = HubServer(self, self.logger)
        self._hub_client = None
        self.ws_client = None
        self._current_ws = None

        future = asyncio.run_coroutine_threadsafe(self._hub_server.start(), self._loop)
        self._task = future

    def _capture_legacy_binding_snapshot_if_any(self) -> None:
        """子服启动前读取本地 data.json，连接 Hub 后合并（避免各服历史数据丢失）。"""
        self._pending_legacy_binding_merge = None
        try:
            path = self.data_manager.binding_file
            if not path.exists():
                return
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and raw:
                self._pending_legacy_binding_merge = json.loads(json.dumps(raw))
                self.logger.info(
                    f"已缓存本地 data.json（{len(self._pending_legacy_binding_merge)} 名玩家），"
                    "将在连接 Hub 后合并到 Hub"
                )
        except Exception as e:
            self.logger.warning(f"读取本地 data.json 用于 Hub 合并失败: {e}")

    def _start_hub_client(self):
        """启动客户端模式"""
        self._capture_legacy_binding_snapshot_if_any()
        self.data_manager.enable_remote_hub_mode(True)
        hub_host = self.config_manager.get_config("hub_host", "127.0.0.1")
        hub_port = self.config_manager.get_config("hub_port", 19321)
        self.logger.info(f"以客户端模式启动，连接 Hub ws://{hub_host}:{hub_port}...")
        self._hub_client = HubClient(self, self.logger)
        self._hub_server = None
        self.ws_client = None
        self._current_ws = None

        future = asyncio.run_coroutine_threadsafe(self._hub_client.connect_forever(), self._loop)
        self._task = future

    def _persist_hub_is_client_only(self):
        """已存在外部 Hub 时持久化为仅连接，便于新服自动加入集群。"""
        if not self.config_manager.get_config("hub_is_hub", True):
            return
        self.config_manager.set_config("hub_is_hub", False)
        self.config_manager.save_config()
        self.logger.info(
            "检测到已有 Hub 在运行，已将配置 hub_is_hub 设为 false（此后本机仅连接，不再尝试创建 Hub）"
        )

    def _start_hub_when_free_or_connect(self):
        """可作为 Hub：端口已被占用则假定已有 Hub，连接并降级配置；否则在本机启动 Hub。"""
        import socket

        hub_host = self.config_manager.get_config("hub_host", "127.0.0.1")
        hub_port = self.config_manager.get_config("hub_port", 19321)

        hub_running = False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((hub_host, hub_port))
            sock.close()
            hub_running = (result == 0)
        except Exception:
            hub_running = False

        if hub_running:
            self.logger.info(f"检测到 Hub 已在 {hub_host}:{hub_port} 监听，以客户端连接并更新配置")
            self._persist_hub_is_client_only()
            self._start_hub_client()
        else:
            self.logger.info("未检测到现有 Hub，在本机启动 Hub 服务")
            self._start_hub_server()

    def _run_loop(self):
        """运行异步事件循环"""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def on_command(self, sender, command, args):
        """处理插件命令"""
        if command.name == "bindqq":
            return self._handle_bindqq_command(sender, command, args)
        return False

    def _handle_bindqq_command(self, sender, command, args):
        """处理 /bindqq 命令"""
        try:
            # 检查发送者是否为玩家
            if not hasattr(sender, 'name') or not hasattr(sender, 'xuid'):
                sender.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.RED}此命令只能由玩家使用！{ColorFormat.RESET}")
                return True
            
            player = sender
            player_name = player.name
            
            # 检查玩家是否已绑定QQ
            if self.data_manager.is_player_bound(player_name, player.xuid):
                player_qq = self.data_manager.get_player_qq(player_name)
                player.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.GREEN}您的QQ绑定状态：{ColorFormat.RESET}")
                player.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.AQUA}已绑定QQ: {player_qq}{ColorFormat.RESET}")
                player.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.YELLOW}如需重新绑定，请联系管理员{ColorFormat.RESET}")
            else:
                # 玩家未绑定，显示绑定表单（可选绑定，不强制）
                player.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.YELLOW}您尚未绑定QQ，绑定后可与QQ群互通。{ColorFormat.RESET}")
                self.server.scheduler.run_task(
                    self,
                    lambda p=player: self.ui_manager.show_qq_binding_form(p) if self.is_valid_player(p) else None,
                    delay=5
                )
            
            return True
        except Exception as e:
            self.logger.error(f"处理 /bindqq 命令失败: {e}")
            if hasattr(sender, 'send_message'):
                sender.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.RED}命令执行出错，请重试！{ColorFormat.RESET}")
            return False

    def is_valid_player(self, player) -> bool:
        """检查玩家对象是否有效且在线"""
        try:
            return (player and
                    hasattr(player, "send_message") and
                    hasattr(player, "name") and
                    hasattr(player, "xuid") and
                    getattr(player, "is_online", True))
        except Exception:
            return False

    @property
    def server_name(self) -> str:
        """获取服务器名称（优先配置文件，回退到 self.server.name）"""
        configured = self.config_manager.get_config("server_name", "")
        return configured if configured else self.server.name

    def get_hub_server_catalog_display(self) -> list:
        """供 QQ 命令展示：Hub 本机用实时表，子服用上次欢迎包中的缓存。"""
        hub = getattr(self, "_hub_server", None)
        if hub is not None:
            return hub.get_server_catalog()
        return list(getattr(self, "hub_server_catalog", None) or [])
        
    def api_send_message(self, text: str) -> bool:
        """
        QQ消息API（简单文本） — 通过 Hub 系统发送
        保持向后兼容，外部插件可直接传入已格式化的文本。
        注意：此方法不会自动加服务器前缀，调用方需自行处理。
        """
        return self._send_text_to_qq(text)

    def api_send_raw(self, text: str, source_server_name: str = None,
                     source_server_id: str = None) -> bool:
        """
        发送已格式化的原始文本到 QQ 群（自动加服务器前缀）。

        :param text: 已格式化的消息文本（如 ARCCore 构建的死亡播报）
        :param source_server_name: 可选；来源子服显示名（主机代发时传入）
        :param source_server_id: 可选；来源子服 ID（主机代发时传入，供扩展使用）
        :return: 是否发送成功
        """
        if not self.config_manager.get_config("api_qq_enable", False):
            self.logger.warning("QQ消息API功能未启用！")
            return False

        _ = source_server_id  # 预留，供后续扩展
        server_label = (source_server_name or "").strip() or self.server_name

        hub = getattr(self, "_hub_server", None)
        client = getattr(self, "_hub_client", None)
        target = hub if hub else client
        if target:
            try:
                asyncio.run_coroutine_threadsafe(
                    target.send_game_event(
                        "custom", "", text, source_server_name=server_label
                    ),
                    self._loop,
                )
                return True
            except Exception:
                return False

        return self._send_text_to_qq(f"[{server_label}]\n{text}")

    def api_send_event(self, event_type: str, display_name: str,
                       raw_player_name: str, message: str = "",
                       source_server_name: str = None,
                       source_server_id: str = None) -> bool:
        """
        事件驱动 API — 由 ARCCore 等外部插件调用。

        :param event_type: 事件类型 "join" | "quit" | "chat" | "death" | "custom"
        :param display_name: 带头衔的显示名 (含 § 颜色码)，如 "§6[传奇]§r玩家名"
        :param raw_player_name: 原始玩家名，用于查询游戏次数、时长等统计
        :param message: 额外消息内容（聊天内容 / 死亡原因 / 自定义文本）
        :param source_server_name: 可选；来源子服显示名（主机代发时传入）
        :param source_server_id: 可选；来源子服 ID（主机代发时传入）
        :return: 是否发送成功
        """
        _ = source_server_id  # 预留，供后续扩展
        server_label = (source_server_name or "").strip() or None

        formatted = self._format_event_message(
            event_type, display_name, raw_player_name, message,
            source_server_name=server_label,
        )
        if not formatted:
            return False
        if not self.config_manager.get_config("api_qq_enable", False):
            self.logger.warning("QQ消息API功能未启用！")
            return False

        if self._send_structured_game_event(
            event_type, display_name, raw_player_name, message,
            source_server_name=server_label,
        ):
            return True

        return self._send_text_to_qq(formatted)

    def _send_structured_game_event(
        self,
        event_type: str,
        display_name: str,
        raw_player_name: str,
        message: str,
        source_server_name: str = None,
    ) -> bool:
        """
        通过 Hub / 客户端发送 type=game_event，由 Hub 统一发 QQ 并 _broadcast_to_others 跨服。
        若当前不是 Hub 模式且无客户端连接，返回 False，由调用方回退到仅发 QQ 文本。
        """
        hub = getattr(self, "_hub_server", None)
        client = getattr(self, "_hub_client", None)
        target = hub if hub else client
        if not target:
            return False

        from .utils.message_utils import strip_minecraft_format_codes

        clean_display = strip_minecraft_format_codes(display_name)
        # 代发子服事件时本机未必有玩家属地统计，避免错绑到主机数据
        is_remote = bool(source_server_name and source_server_name != self.server_name)
        stats = None
        if not is_remote:
            stats = self.data_manager.get_player_playtime_info(
                raw_player_name, self.server.online_players
            )

        session_count = None
        playtime_str = ""
        if event_type == "join":
            session_count = stats.get("session_count", 0) if stats else None
        elif event_type == "quit":
            if stats:
                total_playtime = stats.get("total_playtime", 0) or 0
                hours = total_playtime // 3600
                minutes = (total_playtime % 3600) // 60
                playtime_str = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"

        try:
            kwargs = {
                "session_count": session_count,
                "playtime_str": playtime_str,
            }
            if source_server_name:
                kwargs["source_server_name"] = source_server_name
            asyncio.run_coroutine_threadsafe(
                target.send_game_event(
                    event_type,
                    player=clean_display,
                    message=message,
                    **kwargs,
                ),
                self._loop,
            )
            return True
        except Exception:
            return False

    def get_player_stats(self, raw_player_name: str) -> dict:
        """
        获取玩家统计信息 API — 供外部插件查询。

        :param raw_player_name: 原始玩家名
        :return: {"session_count": int, "total_playtime": int(秒), "is_online": bool,
                  "last_join_time": timestamp, "last_quit_time": timestamp}
        """
        info = self.data_manager.get_player_playtime_info(raw_player_name, self.server.online_players)
        if not info:
            return {
                "session_count": 0,
                "total_playtime": 0,
                "is_online": False,
                "last_join_time": None,
                "last_quit_time": None,
            }
        return info

    def _format_event_message(self, event_type: str, display_name: str,
                              raw_player_name: str, message: str,
                              source_server_name: str = None) -> str:
        """根据事件类型构建发往 QQ 群的消息（带服务器前缀）。"""
        server_name = (source_server_name or "").strip() or self.server_name
        # 去掉 display_name 中的 § 颜色码（QQ 群是纯文本）
        from .utils.message_utils import strip_minecraft_format_codes
        clean_display = strip_minecraft_format_codes(display_name)

        is_remote = bool(source_server_name and source_server_name != self.server_name)
        stats = None
        if not is_remote:
            stats = self.data_manager.get_player_playtime_info(
                raw_player_name, self.server.online_players
            )

        if event_type == "join":
            session_count = stats.get("session_count", 0) if stats else None
            if session_count is None:
                return f"[{server_name}]\n🟢 {clean_display} 加入游戏"
            if session_count <= 1:
                return f"[{server_name}]\n🌟 {clean_display} 首次进入服务器！"
            else:
                return f"[{server_name}]\n🟢 {clean_display} 加入游戏 (第{session_count}次游戏)"

        elif event_type == "quit":
            if not stats:
                return f"[{server_name}]\n🔴 {clean_display} 离开游戏"
            total_playtime = stats.get("total_playtime", 0) if stats else 0
            hours = total_playtime // 3600
            minutes = (total_playtime % 3600) // 60
            playtime_str = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
            return f"[{server_name}]\n🔴 {clean_display} 离开游戏 (总游戏时长: {playtime_str})"

        elif event_type == "chat":
            return f"[{server_name}]\n💬 {clean_display}: {message}"

        elif event_type == "death":
            return f"[{server_name}]\n💀 {clean_display} {message}"

        elif event_type == "custom":
            return f"[{server_name}]\n{message}" if message else ""

        else:
            return f"[{server_name}]\n{message}" if message else ""

    def notify_arc_qq_chat(
        self, display_name: str, message: str, group_name: str = ""
    ) -> None:
        """
        通知 ARC Core 经同步中心把 QQ 群聊下发到 QQ_RELAY_MODE=host 的子服。
        主机本机玩家仍由本插件广播；无 SyncServer 时此调用为空操作。
        """
        try:
            arc = self.server.plugin_manager.get_plugin("arc_core")
            if arc is None or not hasattr(arc, "api_broadcast_qq_chat"):
                return
            arc.api_broadcast_qq_chat(display_name, message, group_name or "")
        except Exception as e:
            self.logger.debug(f"notify_arc_qq_chat: {e}")

    def _send_text_to_qq(self, text: str) -> bool:
        """内部方法：通过 Hub 系统发送文本到 QQ 群。"""
        api_qq_enabled = self.config_manager.get_config("api_qq_enable", False)
        if not api_qq_enabled:
            self.logger.warning("QQ消息API功能未启用！")
            return False

        try:
            hub = getattr(self, '_hub_server', None)
            client = getattr(self, '_hub_client', None)

            if hub:
                asyncio.run_coroutine_threadsafe(hub.send_api_message(text), self._loop)
                return True
            elif client:
                asyncio.run_coroutine_threadsafe(client.send_api_message(text), self._loop)
                return True
            elif hasattr(self, '_current_ws') and self._current_ws:
                asyncio.run_coroutine_threadsafe(
                    send_group_msg_to_all_groups(self._current_ws, text=text),
                    self._loop
                )
                return True
            else:
                self.logger.warning("无法发送消息：Hub 和 NapCat WS 均不可用")
                return False
        except Exception:
            return False

    def on_disable(self) -> None:
        """插件禁用"""
        try:
            self.logger.info("正在禁用插件...")

            # 发送服务器停止消息（通过 Hub 系统）
            if hasattr(self, '_loop') and self._loop and not self._loop.is_closed() and self._loop.is_running():
                try:
                    hub = getattr(self, '_hub_server', None)
                    client = getattr(self, '_hub_client', None)
                    server_name = self.server_name

                    if hub:
                        future = asyncio.run_coroutine_threadsafe(
                            hub.send_game_event("server_stop"),
                            self._loop
                        )
                        future.result(timeout=3)
                        self.logger.info("服务器停止消息已通过 Hub 发送")
                    elif client:
                        future = asyncio.run_coroutine_threadsafe(
                            client.send_game_event("server_stop"),
                            self._loop
                        )
                        future.result(timeout=3)
                        self.logger.info("服务器停止消息已通过客户端发送")
                    elif hasattr(self, '_current_ws') and self._current_ws:
                        # 回退到旧模式
                        server_end_msg = f"[{server_name}]\n[QQSync] 服务器已停止！"
                        future = asyncio.run_coroutine_threadsafe(
                            send_group_msg_to_all_groups(self._current_ws, server_end_msg),
                            self._loop
                        )
                        future.result(timeout=3)
                        self.logger.info("服务器停止消息已直接发送")
                except Exception as msg_error:
                    self.logger.warning(f"发送关闭消息失败（这是正常的）: {msg_error}")

            # 保存数据（子服经 Hub 集中存储：关服前把本机在线玩家的计时同步到 Hub）
            if hasattr(self, 'data_manager'):
                if getattr(self, '_hub_client', None):
                    try:
                        for p in list(self.server.online_players):
                            self.data_manager.stop_player_timer(p.name)
                            self.data_manager.update_player_quit(p.name)
                    except Exception as e:
                        self.logger.warning(f"向 Hub 同步在线时长与离场时间失败: {e}")
                else:
                    self.data_manager.cleanup_timer_system()
                self.data_manager.save_data()

            # 停止 Hub 服务端（先转移角色）
            if hasattr(self, '_hub_server') and self._hub_server:
                if hasattr(self, '_loop') and self._loop and not self._loop.is_closed() and self._loop.is_running():
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self._hub_server.transfer_and_stop(), self._loop)
                        future.result(timeout=5)
                    except Exception as e:
                        self.logger.warning(f"Hub 转移超时，直接停止: {e}")
                        self._hub_server.stop()
                else:
                    self._hub_server.stop()

            # 停止 Hub 客户端
            if hasattr(self, '_hub_client') and self._hub_client:
                self._hub_client.stop()

            # 停止旧模式 WebSocket 连接
            if hasattr(self, 'ws_client') and self.ws_client:
                self.ws_client.stop()

            # 停止事件循环
            if hasattr(self, '_loop') and self._loop:
                try:
                    if not self._loop.is_closed():
                        if self._loop.is_running():
                            self._loop.call_soon_threadsafe(self._loop.stop)
                        else:
                            self.logger.info("事件循环已停止，无需再次停止")
                    else:
                        self.logger.info("事件循环已关闭")
                except Exception as loop_error:
                    self.logger.warning(f"停止事件循环时出错: {loop_error}")

                # 等待线程结束
                if hasattr(self, '_thread') and self._thread and self._thread.is_alive():
                    try:
                        self._thread.join(timeout=5)
                        if self._thread.is_alive():
                            self.logger.warning("事件循环线程未能在5秒内正常结束")
                    except Exception as thread_error:
                        self.logger.warning(f"等待线程结束时出错: {thread_error}")

            self.logger.info(f"{ColorFormat.YELLOW}qqsync_plugin 已禁用{ColorFormat.RESET}")

        except Exception as e:
            self.logger.error(f"插件禁用过程中出错: {e}")
            try:
                if hasattr(self, 'data_manager'):
                    self.data_manager.save_data()
            except Exception as save_error:
                self.logger.error(f"保存数据失败: {save_error}")