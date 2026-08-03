"""
事件处理模块
负责刷屏检测，以及进服/离服/聊天的 QQ 群同步（经弧光消息中心）。
游戏时长 / 进服次数由 ARCCore 维护。
"""

import time
from collections import defaultdict, deque
from endstone.event import (
    event_handler,
    PlayerChatEvent,
    PlayerJoinEvent,
    PlayerQuitEvent,
)
from endstone import ColorFormat


class EventHandlers:
    """事件处理器 —— QQ 同步 + 刷屏检测；时长统计委托 ARCCore。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger

        # 刷屏检测配置
        self.chat_count_limit = plugin.config_manager.get_config("chat_count_limit", 20)
        self.chat_ban_time = plugin.config_manager.get_config("chat_ban_time", 300)
        self.spam_window = 60

        # 玩家聊天记录
        self.player_last_chat = {}
        self.player_chat_history = defaultdict(deque)
        self.player_spam_penalty = {}

    def check_chat_cooldown(self, player_name):
        """检查玩家聊天冷却"""
        current_time = time.time()

        if self._is_admin_player(player_name):
            return True, ""

        if player_name in self.player_spam_penalty:
            penalty_end = self.player_spam_penalty[player_name]
            if current_time < penalty_end:
                remaining = int(penalty_end - current_time)
                minutes = remaining // 60
                seconds = remaining % 60
                time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"
                return False, f"您正在刷屏惩罚中，还需等待 {time_str}"
            else:
                del self.player_spam_penalty[player_name]

        return True, ""

    def check_spam_detection(self, player_name):
        """检查刷屏行为"""
        current_time = time.time()

        if self.chat_count_limit == -1:
            return False, ""

        if self._is_admin_player(player_name):
            return False, ""

        chat_history = self.player_chat_history[player_name]
        while chat_history and current_time - chat_history[0] > self.spam_window:
            chat_history.popleft()
        chat_history.append(current_time)

        if len(chat_history) > self.chat_count_limit:
            self.player_spam_penalty[player_name] = current_time + self.chat_ban_time
            self.player_chat_history[player_name].clear()
            ban_minutes = self.chat_ban_time // 60
            self.logger.warning(f"玩家 {player_name} 触发刷屏检测，被禁言 {ban_minutes} 分钟")
            return True, f"检测到刷屏行为，您被禁言 {ban_minutes} 分钟"

        return False, ""

    def _is_admin_player(self, player_name):
        """检查玩家是否是管理员"""
        try:
            qq_number = self.plugin.data_manager.get_player_qq(player_name)
            if qq_number:
                admins = self.plugin.config_manager.get_config("admins", [])
                return qq_number in admins
            return False
        except Exception as e:
            self.logger.error(f"检查管理员状态失败: {e}")
            return False

    def update_chat_time(self, player_name):
        """更新玩家最后聊天时间"""
        self.player_last_chat[player_name] = time.time()

    def cleanup_player_chat_data(self, player_name):
        """清理玩家聊天相关数据"""
        if player_name in self.player_last_chat:
            del self.player_last_chat[player_name]
        if player_name in self.player_chat_history:
            del self.player_chat_history[player_name]
        if player_name in self.player_spam_penalty:
            del self.player_spam_penalty[player_name]

    def _resolve_display_name(self, player) -> str:
        """Prefer ARCCore title/guild display label when available."""
        try:
            arc = self.plugin.server.plugin_manager.get_plugin("arc_core")
            if arc is not None and hasattr(arc, "format_player_display_label_with_guild"):
                equipped = None
                if hasattr(arc, "title_system") and arc.title_system is not None:
                    equipped = arc.title_system.get_equipped_title(player)
                return arc.format_player_display_label_with_guild(
                    player.name, equipped, str(player.xuid)
                )
        except Exception as e:
            self.logger.debug(f"resolve display name via ARCCore failed: {e}")
        return player.name

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent):
        """玩家加入：同步 QQ 群；改名检测；时长由 ARCCore 记录。"""
        try:
            player = event.player
            player_name = player.name
            player_xuid = player.xuid

            self.logger.info(f"玩家 {player_name} (XUID: {player_xuid}) 加入游戏")

            existing_player = self.plugin.data_manager.get_player_by_xuid(player_xuid)
            if existing_player and existing_player.get("name") != player_name:
                old_name = existing_player.get("name")
                self.plugin.data_manager.update_player_name(old_name, player_name, player_xuid)

            display_name = self._resolve_display_name(player)
            # Delay 1 tick so ARCCore can update session_count / playtime first.
            plugin = self.plugin
            name = player_name

            def _send_join():
                plugin.api_send_event("join", display_name, name)

            plugin.server.scheduler.run_task(plugin, _send_join, delay=1)

        except Exception as e:
            self.logger.error(f"处理玩家加入事件失败: {e}")

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent):
        """玩家离开：同步 QQ 群；时长由 ARCCore 结算。"""
        try:
            player = event.player
            player_name = player.name
            player_xuid = player.xuid

            self.logger.info(f"玩家 {player_name} (XUID: {player_xuid}) 离开游戏")

            display_name = self._resolve_display_name(player)
            plugin = self.plugin
            name = player_name

            def _send_quit():
                plugin.api_send_event("quit", display_name, name)

            plugin.server.scheduler.run_task(plugin, _send_quit, delay=1)

            self.cleanup_player_chat_data(player_name)

        except Exception as e:
            self.logger.error(f"处理玩家离开事件失败: {e}")

    @event_handler
    def on_player_chat(self, event: PlayerChatEvent):
        """玩家聊天：刷屏检测 + 同步 QQ 群。"""
        try:
            player = event.player
            player_name = player.name
            message = event.message

            if message.startswith("/"):
                return

            can_chat, cooldown_msg = self.check_chat_cooldown(player_name)
            if not can_chat:
                event.is_cancelled = True
                player.send_message(
                    f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}{cooldown_msg}{ColorFormat.RESET}"
                )
                return

            is_spam, spam_msg = self.check_spam_detection(player_name)
            if is_spam:
                event.is_cancelled = True
                player.send_message(
                    f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}{spam_msg}{ColorFormat.RESET}"
                )
                player.send_message(
                    f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.YELLOW}"
                    f"请文明聊天，避免刷屏行为{ColorFormat.RESET}"
                )
                return

            self.update_chat_time(player_name)

            # ARCCore cancels PlayerChatEvent to rebroadcast styled chat; still
            # forward to QQ here (spam/cooldown already returned above).
            display_name = self._resolve_display_name(player)
            self.plugin.api_send_event("chat", display_name, player_name, message)

        except Exception as e:
            self.logger.error(f"处理玩家聊天事件失败: {e}")
