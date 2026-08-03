"""
UI界面模块
负责处理游戏内的表单界面
"""

import asyncio
import json
from endstone.form import ModalForm, Label, TextInput, Header, Divider
from endstone import ColorFormat
from ..utils.helpers import is_valid_qq_number


class UIManager:
    """UI管理器"""
    
    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger
    
    def show_qq_binding_form(self, player):
        """显示QQ绑定表单"""
        if not self._is_valid_player(player):
            self.logger.warning("尝试对已失效的玩家对象显示绑定表单，操作已跳过")
            return
            
        try:
            controls = [
                Header("QQ群服互通 - 可选绑定"),
                Divider(),
                Label("绑定QQ号后可享受群服互通功能"),
                Label("您也可以选择不绑定，不影响正常游戏"),
                Divider(),
            ]
            
            # 添加输入框
            controls.append(
                TextInput(
                    label="请输入您的QQ号",
                    placeholder="例如: 2899659758 (5-11位数字)",
                    default_value=""
                )
            )
            
            form = ModalForm(
                title="ARC QQ Sync - QQ绑定",
                controls=controls,
                submit_button="确认绑定",
                icon="textures/ui/icon_multiplayer"
            )
            
            form.on_submit = lambda p, form_data: self._handle_qq_form_submit(p, form_data) if self._is_valid_player(p) else None
            close_message = f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.AQUA}QQ绑定已取消，您可以正常游戏{ColorFormat.RESET}"
            form.on_close = lambda p: p.send_message(close_message) if self._is_valid_player(p) else None
            
            player.send_form(form)
            
        except Exception as e:
            self.logger.error(f"显示QQ绑定表单失败: {e}")
            player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}绑定表单加载失败，请使用命令 /bindqq{ColorFormat.RESET}")
    
    def _handle_qq_form_submit(self, player, form_data):
        """处理QQ绑定表单提交 - 直接绑定，无需验证码"""
        if not self._is_valid_player(player):
            self.logger.warning("尝试处理已失效玩家的表单提交，操作已跳过")
            return

        try:
            # 解析表单数据
            qq_input = self._extract_form_input(form_data)

            if not is_valid_qq_number(qq_input):
                player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}请输入有效的QQ号（5-11位数字）！{ColorFormat.RESET}")
                return

            # 检查玩家是否被封禁
            if self.plugin.data_manager.is_player_banned(player.name):
                player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}[拒绝] 您已被封禁，无法绑定QQ！{ColorFormat.RESET}")
                player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.YELLOW}如有疑问请联系管理员{ColorFormat.RESET}")
                return

            # 检查QQ号是否已被其他玩家绑定
            existing_player = self.plugin.data_manager.get_qq_player(qq_input)
            if existing_player:
                player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}该QQ号已被玩家 {existing_player} 绑定！{ColorFormat.RESET}")
                return

            # 检查QQ号对应的用户是否在群里
            if hasattr(self.plugin, 'group_members') and self.plugin.group_members:
                if qq_input not in self.plugin.group_members:
                    player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}该QQ号未加入QQ群，无法绑定！{ColorFormat.RESET}")
                    player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.YELLOW}请先加入QQ群后再试{ColorFormat.RESET}")
                    return

            # 直接绑定
            if self.plugin.data_manager.bind_player_qq(player.name, player.xuid, qq_input):
                # 发送成功消息给玩家
                player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.GREEN}[成功] QQ绑定成功！{ColorFormat.RESET}")
                player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.AQUA}您的QQ {qq_input} 已与游戏账号绑定{ColorFormat.RESET}")

                # 发送QQ群播报并设置群名片
                if hasattr(self.plugin, '_current_ws') and self.plugin._current_ws:
                    from ..websocket.handlers import send_group_msg_with_at, set_group_card_in_all_groups
                    asyncio.run_coroutine_threadsafe(
                        send_group_msg_with_at(self.plugin._current_ws, group_id=int(self.plugin.config_manager.get_config("target_groups", [0])[0]),
                                               user_id=int(qq_input), text=f"玩家 {player.name} 已成功绑定QQ！"),
                        self.plugin._loop
                    )
                    if self.plugin.config_manager.get_config("sync_group_card", True):
                        asyncio.run_coroutine_threadsafe(
                            set_group_card_in_all_groups(self.plugin._current_ws, user_id=int(qq_input), card=player.name),
                            self.plugin._loop
                        )
            else:
                player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}绑定失败，请重试！{ColorFormat.RESET}")

        except Exception as e:
            self.logger.error(f"处理QQ绑定表单失败: {e}")
            player.send_message(f"{ColorFormat.GRAY}[ARC QQ Sync] {ColorFormat.RED}绑定过程出错，请重试！{ColorFormat.RESET}")
    
    def _extract_form_input(self, form_data: str) -> str:
        """从表单数据中提取输入内容"""
        try:
            # 尝试解析JSON格式
            data_list = json.loads(form_data)
            # TextInput总是在表单控件的最后位置，从末尾获取
            return data_list[-1] if len(data_list) > 0 else ""
        except (json.JSONDecodeError, IndexError):
            # 如果不是JSON，按逗号分割
            data_parts = form_data.split(',') if form_data else []
            return data_parts[-1].strip() if len(data_parts) > 0 else ""
    
    def _is_valid_player(self, player) -> bool:
        """检查玩家对象是否有效且在线 - 委托给插件实例"""
        return self.plugin.is_valid_player(player)
