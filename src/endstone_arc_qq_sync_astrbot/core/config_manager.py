"""
配置管理模块
负责插件配置的加载、保存和管理
"""

import json
from pathlib import Path
from typing import Any, Dict, List


class ConfigManager:
    """配置管理器"""
    
    def __init__(self, data_folder: Path, logger):
        self.data_folder = data_folder
        self.logger = logger
        self.config_file = data_folder / "config.json"
        self.custom_ban_words_file = data_folder / "custom_ban_words.txt"
        self._config: Dict[str, Any] = {}
        self._color_format = None
        self.default_config = {
            "server_name": "",
            "admins": ["593405016"],
            "force_bind_qq": False,
            "sync_group_card": True,
            "check_group_member": False,
            "chat_count_limit": 20,
            "chat_ban_time": 300,
            "hub_host": "127.0.0.1",
            "hub_port": 19136,
            "hub_token": "",
            "cross_server_broadcast": True,
        }
        self._init_config()
        self._init_custom_ban_words()
    
    @property
    def color_format(self):
        """延迟加载ColorFormat以避免循环依赖"""
        if self._color_format is None:
            from endstone import ColorFormat
            self._color_format = ColorFormat
        return self._color_format
    
    def _init_config(self):
        """初始化配置文件"""
        # 如果配置文件不存在，创建默认配置
        if not self.config_file.exists():
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.default_config, f, indent=2, ensure_ascii=False)
            self.logger.info(f"已创建默认配置文件: {self.config_file}")
        
        # 读取配置
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
        except Exception as e:
            self.logger.error(f"读取配置文件失败: {e}")
            self._config = self.default_config.copy()
        
        config_updated = False
        config_updated |= self._migrate_obsolete_keys()
        
        # 检查并合并新的配置项
        for key, value in self.default_config.items():
            if key not in self._config:
                self._config[key] = value
                config_updated = True
                self.logger.info(f"添加新配置项: {key}")
        
        # 生成动态帮助信息
        self._config["help_msg"] = self._generate_help_message()
        
        # 如果有新配置项，保存到文件
        if config_updated:
            self.save_config()
        
        self._log_config_info()

    def _migrate_obsolete_keys(self) -> bool:
        """移除旧版 NapCat / 本机 Hub 相关配置项。"""
        updated = False
        obsolete = (
            "napcat_ws",
            "access_token",
            "target_groups",
            "target_group",
            "group_names",
            "hub_is_hub",
            "hub_mode",
            "api_qq_enable",
        )
        for key in obsolete:
            if key in self._config:
                del self._config[key]
                updated = True
                self.logger.info(f"已清理废弃配置项: {key}")
        return updated

    def _init_custom_ban_words(self):
        """初始化自定义封禁词列表"""
        self.custom_ban_words = []
        
        # 如果自定义封禁词文件不存在，创建默认文件
        if not self.custom_ban_words_file.exists():
            default_ban_words = [
                "这是一个自定义违禁词",
                "这是另一个自定义违禁词"
            ]
            self.custom_ban_words_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.custom_ban_words_file, 'w', encoding='utf-8') as f:
                for word in default_ban_words:
                    f.write(word + '\n')
            self.logger.info(f"已创建默认自定义封禁词文件: {self.custom_ban_words_file}")
        
        # 读取自定义封禁词
        try:
            with open(self.custom_ban_words_file, 'r', encoding='utf-8') as f:
                self.custom_ban_words = [line.strip() for line in f.readlines() if line.strip()]
            self.logger.info(f"已加载 {len(self.custom_ban_words)} 个自定义封禁词")
        except Exception as e:
            self.logger.error(f"读取自定义封禁词文件失败: {e}")
            self.custom_ban_words = []
    
    def _get_help_commands(
        self,
        include_bind: bool = True,
        include_admin: bool = False,
        include_group_admin: bool = False,
        mark_sections: bool = False,
    ) -> str:
        """获取帮助命令文本的通用方法"""
        basic_commands = [
            "/help — 显示本帮助信息",
            "/servers — 查看 Hub 子服数字编号（非 Hub 时提示无列表）",
            "/list — 查看在线玩家列表", 
            "/tps — 查看服务器性能指标",
            "/info — 查看服务器综合信息"
        ]
        
        bind_commands = [
            "/绑定 <玩家名> — 在群内将您的QQ绑定到该游戏角色（需先进服产生记录）",
            "/重启 — 为当前所在子服发起/参与重启投票（须在线，1分钟内≥半数通过）",
        ]
        
        group_admin_commands = [
            "/cmd [子服编号] <控制台命令> — 执行控制台命令（群管理员可用），例：/cmd stop、/cmd 2 say hi",
        ]

        admin_commands = [
            "/who <玩家名|QQ号> [子服编号] — 查询玩家详细信息",
            "/unbindqq <玩家名|QQ号> — 解绑玩家的QQ绑定", 
            "/ban <玩家名> [原因] — 封禁玩家",
            "/unban <玩家名> — 解除玩家封禁",
            "/banlist — 查看封禁列表",
            "/reload — 重新加载配置文件"
        ]
        
        # 构建命令列表
        result = ["ARC QQ Sync - 命令："]
        
        # 查询命令分节
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
            # 管理命令分节
            if mark_sections:
                result.append("\n[插件管理命令]（config.json admins 可用）：")
            else:
                result.append("\n[插件管理命令]：")

            # 过滤管理员命令（如果没有绑定功能，则移除绑定相关命令）
            filtered_admin = admin_commands if include_bind else [cmd for cmd in admin_commands if "QQ" not in cmd]
            result.extend(filtered_admin)
            if not include_group_admin:
                result.extend(group_admin_commands)

        result.append(
            "\n（Hub 集群）使用 /servers 查看各服编号。"
            "多数命令可在末尾加编号以指定单服，例如 /list 2、/tps 3、/who 玩家名 2、/info 2；"
            "省略编号则所有已连接子服都会执行并分别回复。"
        )
            
        return "\n".join(result)

    def _generate_help_message(self) -> str:
        """根据当前配置动态生成帮助信息"""
        force_bind_enabled = self.get_config("force_bind_qq", False)
        return self._get_help_commands(include_bind=force_bind_enabled, include_admin=True)
    
    def get_help_text(self) -> str:
        """获取普通用户帮助文本"""
        force_bind_enabled = self.get_config("force_bind_qq", False)
        return self._get_help_commands(include_bind=force_bind_enabled, include_admin=False)
    
    def get_help_text_with_admin(self) -> str:
        """获取包含管理员命令的帮助文本"""
        force_bind_enabled = self.get_config("force_bind_qq", False)
        return self._get_help_commands(
            include_bind=force_bind_enabled,
            include_admin=True,
            include_group_admin=True,
            mark_sections=True,
        )

    def get_help_text_with_group_admin(self) -> str:
        """获取群管理员可用的帮助文本（含 /cmd）"""
        force_bind_enabled = self.get_config("force_bind_qq", False)
        return self._get_help_commands(
            include_bind=force_bind_enabled,
            include_admin=False,
            include_group_admin=True,
            mark_sections=True,
        )
    
    def _log_config_info(self):
        """记录配置信息"""
        ColorFormat = self.color_format
        
        self.logger.info(f"{ColorFormat.AQUA}配置文件已加载{ColorFormat.RESET}")
        self.logger.info(
            f"{ColorFormat.GOLD}弧光消息中心: {ColorFormat.WHITE}"
            f"ws://{self._config.get('hub_host')}:{self._config.get('hub_port')}"
            f"{ColorFormat.RESET}"
        )
        self.logger.info(f"{ColorFormat.GOLD}管理员列表: {ColorFormat.WHITE}{self._config.get('admins')}{ColorFormat.RESET}")
        
        sync_card_enabled = self._config.get('sync_group_card', True)
        self.logger.info(f"{ColorFormat.GOLD}强制QQ绑定: {ColorFormat.WHITE}已移除，不再因未绑定限制玩家操作{ColorFormat.RESET}")
        self.logger.info(f"{ColorFormat.GOLD}同步群昵称: {ColorFormat.WHITE}{'启用' if sync_card_enabled else '禁用'}{ColorFormat.RESET}")
        self.logger.info(
            f"{ColorFormat.GOLD}跨服广播: {ColorFormat.WHITE}"
            f"{'启用' if self._config.get('cross_server_broadcast', True) else '禁用'}"
            f"{ColorFormat.RESET}"
        )
    
    def get_config(self, key: str, default=None) -> Any:
        """获取配置项"""
        return self._config.get(key, default)
    
    def set_config(self, key: str, value: Any):
        """设置配置项"""
        self._config[key] = value
    
    def save_config(self):
        """保存配置到文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
            self.logger.info("配置已保存")
        except Exception as e:
            self.logger.error(f"保存配置失败: {e}")
    
    def reload_config(self) -> bool:
        """重新加载配置文件"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
            
            # 检查并合并新的配置项（与_init_config保持一致）
            config_updated = False
            config_updated |= self._migrate_obsolete_keys()
            for key, value in self.default_config.items():
                if key not in self._config:
                    self._config[key] = value
                    config_updated = True
                    self.logger.info(f"添加新配置项: {key}")
            
            # 重新生成动态帮助信息
            self._config["help_msg"] = self._generate_help_message()
            
            # 如果有新配置项，保存到文件
            if config_updated:
                self.save_config()
            
            # 重新加载自定义封禁词列表
            self._init_custom_ban_words()
            
            ColorFormat = self.color_format
            reload_msg = f"{ColorFormat.GREEN}配置已重新加载{ColorFormat.RESET}"
            self.logger.info(reload_msg)
            return True
        except Exception as e:
            self.logger.error(f"重新加载配置失败: {e}")
            return False
    
    def get_custom_ban_words(self) -> List[str]:
        """获取自定义封禁词列表"""
        return self.custom_ban_words.copy()
    
    @property
    def config(self) -> Dict[str, Any]:
        """获取完整配置"""
        return self._config.copy()