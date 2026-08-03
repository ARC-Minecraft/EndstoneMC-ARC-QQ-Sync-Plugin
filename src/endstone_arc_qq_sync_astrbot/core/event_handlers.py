"""
事件处理模块
负责进服/离服/聊天的 QQ 群同步（经弧光消息中心）。
游戏时长 / 进服次数由 ARCCore 维护。
"""

from endstone.event import (
    event_handler,
    PlayerChatEvent,
    PlayerJoinEvent,
    PlayerQuitEvent,
)


class EventHandlers:
    """事件处理器 —— QQ 同步；时长统计委托 ARCCore。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger

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

        except Exception as e:
            self.logger.error(f"处理玩家离开事件失败: {e}")

    @event_handler
    def on_player_chat(self, event: PlayerChatEvent):
        """玩家聊天：同步 QQ 群（不做刷屏/关键词拦截）。"""
        try:
            player = event.player
            message = event.message

            if message.startswith("/"):
                return

            # ARCCore cancels PlayerChatEvent to rebroadcast styled chat; still
            # forward to QQ here.
            display_name = self._resolve_display_name(player)
            self.plugin.api_send_event("chat", display_name, player.name, message)

        except Exception as e:
            self.logger.error(f"处理玩家聊天事件失败: {e}")
