"""
事件处理模块
负责游戏内数据追踪（进服/退出/在线时长/刷屏检测）。
不再主动发送 QQ 消息 —— 由 ARCCore 等外部插件通过 api_send_event 驱动。
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
    """事件处理器 —— 仅负责数据追踪，不发送 QQ 消息"""

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

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent):
        """玩家加入事件 —— 仅记录数据，不发送 QQ 消息（由 ARCCore 驱动）"""
        try:
            player = event.player
            player_name = player.name
            player_xuid = player.xuid

            self.logger.info(f"玩家 {player_name} (XUID: {player_xuid}) 加入游戏")

            # 记录玩家加入时间和进服次数
            self.plugin.data_manager.update_player_join(player_name, player_xuid)
            self.plugin.data_manager.start_player_timer(player_name, player_xuid)

            # 检查玩家名称是否发生变化
            existing_player = self.plugin.data_manager.get_player_by_xuid(player_xuid)
            if existing_player and existing_player.get("name") != player_name:
                old_name = existing_player.get("name")
                if self.plugin.data_manager.update_player_name(old_name, player_name, player_xuid):
                    if (hasattr(self.plugin, '_current_ws') and self.plugin._current_ws and
                        self.plugin.config_manager.get_config("sync_group_card", True)):
                        qq_number = existing_player.get("qq")
                        if qq_number:
                            import asyncio
                            from ..websocket.handlers import set_group_card_in_all_groups
                            asyncio.run_coroutine_threadsafe(
                                set_group_card_in_all_groups(self.plugin._current_ws, user_id=int(qq_number), card=player_name),
                                self.plugin._loop
                            )

        except Exception as e:
            self.logger.error(f"处理玩家加入事件失败: {e}")

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent):
        """玩家离开事件 —— 仅记录数据，不发送 QQ 消息（由 ARCCore 驱动）"""
        try:
            player = event.player
            player_name = player.name
            player_xuid = player.xuid

            self.logger.info(f"玩家 {player_name} (XUID: {player_xuid}) 离开游戏")

            # 计算在线时长
            self.plugin.data_manager.stop_player_timer(player_name)
            self.plugin.data_manager.update_player_quit(player_name)

            # 清理聊天缓存
            self.cleanup_player_chat_data(player_name)

        except Exception as e:
            self.logger.error(f"处理玩家离开事件失败: {e}")

    @event_handler
    def on_player_chat(self, event: PlayerChatEvent):
        """玩家聊天事件 —— 仅刷屏检测，不发送 QQ 消息（由 ARCCore 驱动）"""
        try:
            player = event.player
            player_name = player.name
            message = event.message

            # 过滤命令消息
            if message.startswith('/'):
                return

            # 检查刷屏冷却
            can_chat, cooldown_msg = self.check_chat_cooldown(player_name)
            if not can_chat:
                event.is_cancelled = True
                player.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.RED}{cooldown_msg}{ColorFormat.RESET}")
                return

            # 检查刷屏行为
            is_spam, spam_msg = self.check_spam_detection(player_name)
            if is_spam:
                event.is_cancelled = True
                player.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.RED}{spam_msg}{ColorFormat.RESET}")
                player.send_message(f"{ColorFormat.GRAY}[QQsync] {ColorFormat.YELLOW}请文明聊天，避免刷屏行为{ColorFormat.RESET}")
                return

            self.update_chat_time(player_name)

        except Exception as e:
            self.logger.error(f"处理玩家聊天事件失败: {e}")
