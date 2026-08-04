"""
配置管理模块
负责插件配置的加载、保存和管理。

MC 侧仅保留连接弧光 EndStone 消息中枢所需的三项；
群号 / 管理员 / 帮助 / 改群名片等均在 AstrBot 中枢配置。
"""

import json
from pathlib import Path
from typing import Any, Dict


class ConfigManager:
    """配置管理器（仅 hub_host / hub_port / hub_token）。"""

    def __init__(self, data_folder: Path, logger):
        self.data_folder = data_folder
        self.logger = logger
        self.config_file = data_folder / "config.json"
        self._config: Dict[str, Any] = {}
        self._color_format = None
        self.default_config = {
            "hub_host": "127.0.0.1",
            "hub_port": 19136,
            "hub_token": "",
            # Optional: unique hub register / QQ prefix. Empty → Endstone server.name.
            "server_name": "",
        }
        self._init_config()

    @property
    def color_format(self):
        """延迟加载 ColorFormat 以避免循环依赖。"""
        if self._color_format is None:
            from endstone import ColorFormat

            self._color_format = ColorFormat
        return self._color_format

    def _init_config(self):
        """初始化配置文件。"""
        if not self.config_file.exists():
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            self._config = self.default_config.copy()
            self.save_config()
            self.logger.info(f"已创建默认配置文件: {self.config_file}")
        else:
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
            except Exception as e:
                self.logger.error(f"读取配置文件失败: {e}")
                self._config = self.default_config.copy()

        migrated = self._migrate_to_slim_config()
        for key, value in self.default_config.items():
            if key not in self._config:
                self._config[key] = value
                migrated = True
                self.logger.info(f"添加新配置项: {key}")

        if migrated:
            self.save_config()

        self._log_config_info()

    def _migrate_to_slim_config(self) -> bool:
        """Drop obsolete keys; keep only hub connection fields on disk."""
        obsolete = (
            "admins",
            "force_bind_qq",
            "sync_group_card",
            "check_group_member",
            "cross_server_broadcast",
            "help_msg",
            "napcat_ws",
            "access_token",
            "target_groups",
            "target_group",
            "group_names",
            "hub_is_hub",
            "hub_mode",
            "api_qq_enable",
            "chat_count_limit",
            "chat_ban_time",
            "stop_event_on_forward",
        )
        updated = False
        for key in obsolete:
            if key in self._config:
                del self._config[key]
                updated = True
                self.logger.info(f"已清理废弃配置项: {key}")
        # Drop any other unknown keys so config.json stays minimal.
        keep = set(self.default_config.keys())
        for key in list(self._config.keys()):
            if key not in keep:
                del self._config[key]
                updated = True
                self.logger.info(f"已清理非连接配置项: {key}")
        return updated

    def _get_help_commands(
        self,
        include_bind: bool = True,
        include_admin: bool = False,
        include_group_admin: bool = False,
        mark_sections: bool = False,
    ) -> str:
        """Hardcoded fallback help (hub normally answers /mc help)."""
        basic_commands = [
            "/help — 显示本帮助信息",
            "/servers — 查看 Hub 子服数字编号",
            "/list — 查看在线玩家列表",
            "/tps — 查看服务器性能指标",
            "/info — 查看服务器综合信息",
        ]
        bind_commands = [
            "/绑定 <玩家名> — 在群内将您的QQ绑定到该游戏角色",
            "/重启 — 为当前所在子服发起/参与重启投票",
        ]
        group_admin_commands = [
            "/cmd [子服编号] <控制台命令> — 执行控制台命令（群管理员可用）",
        ]
        admin_commands = [
            "/who <玩家名|QQ号> [子服编号] — 查询玩家详细信息",
            "/unbindqq <玩家名|QQ号> — 解绑玩家的QQ绑定",
            "/ban <玩家名> [原因] — 封禁玩家",
            "/unban <玩家名> — 解除玩家封禁",
            "/banlist — 查看封禁列表",
            "/reload — 重新加载配置文件",
        ]

        result = [
            "ARC QQ Sync - 命令：",
            "（群内请使用 /mc 前缀，由弧光 EndStone 消息中枢识别）",
        ]
        if mark_sections and include_admin:
            result.append("\n[查询命令]（所有用户可用）：")
        else:
            result.append("\n[查询命令]：")
        result.extend(basic_commands)
        if include_bind:
            result.extend(bind_commands)
        if include_group_admin:
            if mark_sections:
                result.append("\n[群管理命令]（群主/群管理员可用）：")
            else:
                result.append("\n[群管理命令]：")
            result.extend(group_admin_commands)
        if include_admin:
            if mark_sections:
                result.append("\n[插件管理命令]（中枢 admins 可用）：")
            else:
                result.append("\n[插件管理命令]：")
            result.extend(admin_commands)
            if not include_group_admin:
                result.extend(group_admin_commands)
        result.append(
            "\n多数命令可在末尾加编号以指定单服；省略编号则所有子服执行。"
        )
        return "\n".join(result)

    def get_help_text(self) -> str:
        """普通用户帮助（回退用；优先由中枢 /mc help 回复）。"""
        return self._get_help_commands(include_bind=True, include_admin=False)

    def get_help_text_with_admin(self) -> str:
        """含管理员命令的帮助。"""
        return self._get_help_commands(
            include_bind=True,
            include_admin=True,
            include_group_admin=True,
            mark_sections=True,
        )

    def get_help_text_with_group_admin(self) -> str:
        """含群管理命令的帮助。"""
        return self._get_help_commands(
            include_bind=True,
            include_admin=False,
            include_group_admin=True,
            mark_sections=True,
        )

    def _log_config_info(self):
        """记录配置信息。"""
        ColorFormat = self.color_format
        self.logger.info(f"{ColorFormat.AQUA}配置文件已加载{ColorFormat.RESET}")
        self.logger.info(
            f"{ColorFormat.GOLD}弧光 EndStone 消息中枢: {ColorFormat.WHITE}"
            f"ws://{self._config.get('hub_host')}:{self._config.get('hub_port')}"
            f"{ColorFormat.RESET}"
        )

    def get_config(self, key: str, default=None) -> Any:
        """获取配置项。"""
        return self._config.get(key, default)

    def set_config(self, key: str, value: Any):
        """设置配置项。"""
        self._config[key] = value

    def save_config(self):
        """Save only hub connection keys."""
        try:
            slim = {
                "hub_host": self._config.get("hub_host", "127.0.0.1"),
                "hub_port": int(self._config.get("hub_port", 19136)),
                "hub_token": self._config.get("hub_token", "") or "",
                "server_name": self._config.get("server_name", "") or "",
            }
            self._config = slim
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(slim, f, indent=2, ensure_ascii=False)
            self.logger.info("配置已保存")
        except Exception as e:
            self.logger.error(f"保存配置失败: {e}")

    def reload_config(self) -> bool:
        """重新加载配置文件。"""
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                self._config = json.load(f)
            updated = self._migrate_to_slim_config()
            for key, value in self.default_config.items():
                if key not in self._config:
                    self._config[key] = value
                    updated = True
            if updated:
                self.save_config()
            ColorFormat = self.color_format
            self.logger.info(
                f"{ColorFormat.GREEN}配置已重新加载{ColorFormat.RESET}"
            )
            return True
        except Exception as e:
            self.logger.error(f"重新加载配置失败: {e}")
            return False

    @property
    def config(self) -> Dict[str, Any]:
        """获取完整配置副本。"""
        return self._config.copy()
