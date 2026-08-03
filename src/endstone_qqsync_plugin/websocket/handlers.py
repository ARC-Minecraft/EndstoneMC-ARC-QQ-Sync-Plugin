"""
WebSocket消息处理函数
"""

import asyncio
import json
import datetime
from typing import Optional
from endstone import ColorFormat
from ..utils.time_utils import TimeUtils
from endstone.command import CommandSenderWrapper
from endstone.lang import Language,Translatable
from ..utils.helpers import format_playtime
from ..utils.message_utils import strip_minecraft_format_codes
import queue
import html


# 全局变量引用
_plugin_instance = None
_current_ws = None


def set_plugin_instance(plugin):
    """设置插件实例引用"""
    global _plugin_instance
    _plugin_instance = plugin


def _parse_group_id_from_member_list_echo(echo: str):
    """从 get_group_member_list 的 echo 中解析 group_id（格式 get_group_member_list:{group_id}:...）"""
    if not echo.startswith("get_group_member_list:"):
        return None
    parts = echo.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _release_member_list_pending(group_id: int):
    """收到某群的成员列表（成功或失败）后，减少等待计数并在全部完成时通知。"""
    if not _plugin_instance:
        return
    pending = getattr(_plugin_instance, "member_list_pending_groups", None)
    if pending is None or group_id not in pending:
        return
    pending.discard(group_id)
    ev = getattr(_plugin_instance, "member_list_ready_event", None)
    if not pending and ev and not ev.is_set():
        ev.set()


async def send_group_msg(ws, group_id: int, text: str):
    """发送群消息 - OneBot V11 API"""
    try:
        text = strip_minecraft_format_codes(text)
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": text
            },
            "echo": f"send_group_msg_{int(TimeUtils.get_timestamp())}"
        }
        await ws.send(json.dumps(payload))
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"发送群消息失败: {e}")


async def send_group_msg_to_all_groups(ws, text: str):
    """向所有配置的群组发送消息"""
    try:
        target_groups = _plugin_instance.config_manager.get_config("target_groups", [])
        # 添加类型转换，确保group_id为整数类型
        target_groups = [int(gid) for gid in target_groups]
        for group_id in target_groups:
            await send_group_msg(ws, group_id, text)
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"向所有群组发送消息失败: {e}")


async def send_group_msg_with_at(ws, group_id: int, user_id: int, text: str):
    """发送@用户的群消息 - OneBot V11 API"""
    try:
        text = strip_minecraft_format_codes(text)
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": [
                    {"type": "at", "data": {"qq": str(user_id)}},
                    {"type": "text", "data": {"text": f" {text}"}}
                ]
            },
            "echo": f"bind_success_msg_{int(TimeUtils.get_timestamp())}"
        }
        await ws.send(json.dumps(payload))
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"发送@消息失败: {e}")


async def delete_msg(ws, message_id: int):
    """删除消息 - OneBot V11 API"""
    try:
        payload = {
            "action": "delete_msg",
            "params": {
                "message_id": message_id
            },
            "echo": f"delete_msg_{int(TimeUtils.get_timestamp())}"
        }
        await ws.send(json.dumps(payload))
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"删除消息失败: {e}")


async def set_group_card(ws, group_id: int, user_id: int, card: str):
    """设置群昵称 - OneBot V11 API"""
    try:
        payload = {
            "action": "set_group_card",
            "params": {
                "group_id": group_id,
                "user_id": user_id,
                "card": card
            },
            "echo": f"set_group_card_{int(TimeUtils.get_timestamp())}"
        }
        if _plugin_instance:
            _plugin_instance.logger.info(f"🏷️ 尝试设置群昵称: QQ={user_id}, 群={group_id}, 昵称='{card}'")
        await ws.send(json.dumps(payload))
    except Exception as e:
        raise e


async def set_group_card_in_all_groups(ws, user_id: int, card: str):
    """在所有配置的群组中设置群昵称"""
    try:
        target_groups = _plugin_instance.config_manager.get_config("target_groups", [])
        # 添加类型转换，确保group_id为整数类型
        target_groups = [int(gid) for gid in target_groups]
        for group_id in target_groups:
            await set_group_card(ws, group_id, user_id, card)
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"在所有群组中设置群昵称失败: {e}")


async def prepare_group_member_cache_and_wait(ws, timeout: float = 25.0):
    """
    请求各群成员列表并写入 group_member_cards，等待全部返回或超时。
    需在消息循环已运行的情况下调用，否则无法收到 API 响应。
    """
    if not _plugin_instance:
        return
    target_groups = _plugin_instance.config_manager.get_config("target_groups", [])
    target_groups = [int(gid) for gid in target_groups]
    if not target_groups:
        return

    plugin = _plugin_instance
    plugin.group_member_cards = {}
    plugin.member_list_pending_groups = set(target_groups)
    plugin.member_list_ready_event = asyncio.Event()

    await get_all_groups_member_list(ws)

    try:
        await asyncio.wait_for(plugin.member_list_ready_event.wait(), timeout=timeout)
        plugin.logger.info("群成员列表已全部返回，群名片缓存可用于启动同步")
    except asyncio.TimeoutError:
        pending_left = getattr(plugin, "member_list_pending_groups", None)
        plugin.logger.warning(
            f"等待群成员列表超时 ({timeout}s)，仍未返回的群: {pending_left}"
        )
    finally:
        plugin.member_list_pending_groups = None
        plugin.member_list_ready_event = None


async def sync_all_group_cards(ws):
    """启动时批量同步：仅对群名片与游戏名不一致的成员调用 set_group_card（依赖 prepare_group_member_cache_and_wait）。"""
    try:
        binding_data = _plugin_instance.data_manager.binding_data
        target_groups = _plugin_instance.config_manager.get_config("target_groups", [])
        target_groups = [int(gid) for gid in target_groups]
        cards_root = getattr(_plugin_instance, "group_member_cards", None) or {}

        updated_count = 0
        skipped_count = 0

        for player_name, data in binding_data.items():
            qq_number = data.get("qq", "")
            if not qq_number or not str(qq_number).strip():
                continue
            qq_str = str(int(qq_number))
            desired = player_name.strip()

            for group_id in target_groups:
                group_cache = cards_root.get(group_id)
                if group_cache is not None:
                    if qq_str not in group_cache:
                        skipped_count += 1
                        continue
                    current_card = (group_cache.get(qq_str) or "").strip()
                    if current_card == desired:
                        skipped_count += 1
                        continue

                try:
                    await set_group_card(ws, group_id, int(qq_str), desired)
                    updated_count += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    _plugin_instance.logger.warning(
                        f"同步玩家 {player_name} (QQ: {qq_number}) 群 {group_id} 名片失败: {e}"
                    )

        _plugin_instance.logger.info(
            f"启动时群名片同步完成：实际修改 {updated_count} 次，跳过（已一致或不在群）{skipped_count} 次"
        )
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"群名片批量同步失败: {e}")


async def get_group_member_list(ws, group_id: int):
    """获取群成员列表 - OneBot V11 API"""
    try:
        payload = {
            "action": "get_group_member_list",
            "params": {
                "group_id": group_id
            },
            "echo": f"get_group_member_list:{group_id}:{int(TimeUtils.get_timestamp())}"
        }
        await ws.send(json.dumps(payload))
        if _plugin_instance:
            _plugin_instance.logger.debug(f"已发送OneBot V11群成员列表请求: 群{group_id}")
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"获取群成员列表失败: {e}")


async def get_all_groups_member_list(ws):
    """获取所有配置群组的成员列表"""
    try:
        target_groups = _plugin_instance.config_manager.get_config("target_groups", [])
        # 添加类型转换，确保group_id为整数类型
        target_groups = [int(gid) for gid in target_groups]
        for group_id in target_groups:
            await get_group_member_list(ws, group_id)
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"获取所有群组成员列表失败: {e}")


async def handle_message(ws, data: dict):
    """处理接收到的消息"""
    try:
        # 只处理群消息
        if data.get("message_type") != "group":
            return
            
        group_id = data.get("group_id")
        user_id = data.get("user_id")
        raw_message = data.get("raw_message", "")
        sender = data.get("sender", {})
        nickname = sender.get("nickname", "未知")
        card = sender.get("card", "")
        
        # 只打印监听的群聊消息
        if _plugin_instance:
            target_groups = _plugin_instance.config_manager.get_config("target_groups", [])
            target_groups = [int(gid) for gid in target_groups]
            if group_id in target_groups:
                _plugin_instance.logger.info(f"[MSG] [群ID: {group_id}] [QQ: {user_id}] [昵称: {card if card else nickname}] - 内容: {raw_message}")
        
        if not _plugin_instance:
            return
        
        # 先检查是否是目标群组，避免不必要的数据库查询
        target_groups = _plugin_instance.config_manager.get_config("target_groups", [])
        # 确保target_groups中的元素都是整数类型，与group_id保持一致
        target_groups = [int(gid) for gid in target_groups]
        if group_id not in target_groups:
            return
        
        # 检查用户是否已绑定QQ，如果已绑定则使用玩家游戏ID
        bound_player = _plugin_instance.data_manager.get_qq_player(str(user_id))
        if bound_player:
            display_name = bound_player  # 使用玩家游戏ID作为显示名
            if _plugin_instance.config_manager.get_config("sync_group_card", True):
                current_card = (card or "").strip()
                if current_card != bound_player:
                    try:
                        await set_group_card_in_all_groups(ws, int(user_id), bound_player)
                    except Exception as e:
                        _plugin_instance.logger.warning(f"发言时纠正群名片失败 (QQ={user_id}, 游戏名={bound_player}): {e}")
        else:
            display_name = card if card else nickname  # 使用QQ群昵称或QQ昵称
        
        # 处理群内命令（包括管理员和普通用户命令）
        if raw_message.startswith("/"):
            await _handle_group_command(
                ws, user_id, raw_message, display_name, group_id, sender=sender
            )
            return
        
        # 转发消息到游戏
        await _forward_message_to_game(data, display_name)
            
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"处理群消息失败: {e}")


def _resolve_target(input_str: str):
    """
    智能解析目标玩家
    
    逻辑：
    1. 尝试作为QQ号查找绑定的玩家
    2. 尝试作为玩家名查找已存在的玩家数据
    
    返回: (player_name, match_type) 或 (None, None)
    match_type: "QQ" 或 "Name"
    """
    if not _plugin_instance:
        return None, None
        
    # 1. 尝试作为QQ号查找
    if input_str.isdigit():
        # 1.1 优先查找当前绑定
        target = _plugin_instance.data_manager.get_qq_player(input_str)
        if target:
            return target, "QQ"
            
        # 1.2 尝试查找历史绑定 (用于解封已解绑的玩家)
        target = _plugin_instance.data_manager.get_qq_player_history(input_str)
        if target:
            return target, "QQ (History)"
    
    # 2. 尝试作为玩家名查找
    # 检查是否在数据中有记录
    if input_str in _plugin_instance.data_manager.binding_data:
        return input_str, "Name"
            
    return None, None


def _run_server_console_command(command_to_execute: str) -> str:
    """在服务器主线程执行控制台命令，返回发往QQ群的回复文本。"""
    command_to_execute = html.unescape(command_to_execute.strip())
    if not command_to_execute:
        return "❌ 命令为空"

    msg_ret = []
    error_ret = []
    success = False

    try:
        result_queue = queue.Queue()
        language = _plugin_instance.server.language

        def on_message(msg):
            if isinstance(msg, str):
                msg_ret.append(msg)
            else:
                try:
                    translated = language.translate(msg, language.locale)
                    msg_ret.append(translated)
                except Exception as e:
                    msg_ret.append(f"[消息翻译失败: {e}]")

        def on_error(err):
            if isinstance(err, str):
                error_ret.append(err)
            else:
                try:
                    translated = language.translate(err)
                    error_ret.append(translated)
                except Exception as e:
                    error_ret.append(f"[错误翻译失败: {e}]")

        wrapper = CommandSenderWrapper(
            sender=_plugin_instance.server.command_sender,
            on_message=on_message,
            on_error=on_error
        )

        def server_thread():
            try:
                success_result = _plugin_instance.server.dispatch_command(wrapper, command_to_execute)
                result_queue.put((success_result, None))
            except Exception as e:
                result_queue.put((False, str(e)))

        _plugin_instance.server.scheduler.run_task(_plugin_instance, server_thread, 0, 0)
        success, error = result_queue.get(block=True, timeout=10)

        if error:
            raise Exception(error)

        lines = []
        lines.extend(msg_ret)
        lines.extend([f"[ERROR] {e}" for e in error_ret])
        output_text = "\n".join(lines) if lines else "无返回值"
        status = "成功" if success else "失败, 请检查命令语法或权限"
        return f"✅ 命令已执行: /{command_to_execute}\n状态: {status}\n输出:\n{output_text}"

    except queue.Empty:
        return "❌ 命令执行超时"
    except Exception as e:
        return f"❌ 命令执行失败: {str(e)}"


async def _handle_restart_vote_local(ws, user_id: int, group_id: int) -> str:
    """单机（无 Hub）时在本地处理 /重启 投票。"""
    if not _plugin_instance:
        return "❌ 插件未就绪"

    qq_str = str(user_id)
    player_name = _plugin_instance.data_manager.get_qq_player(qq_str)
    if not player_name:
        return "❌ 请先使用 /绑定 <玩家名> 绑定游戏角色后再参与重启投票"

    online_names = {p.name for p in _plugin_instance.server.online_players}
    if player_name not in online_names:
        return "❌ 您当前未在线，无法发起或参与重启投票"

    server_name = _plugin_instance.server_name
    server_id = getattr(_plugin_instance, "hub_numeric_server_id", None) or 1
    vote_mgr = _plugin_instance.restart_vote_manager

    async def on_timeout(sid: int, vote_state):
        await send_group_msg(
            ws,
            vote_state.group_id,
            f"❌ [{vote_state.server_name}] 重启投票已结束，未能在规定时间内获得足够票数",
        )

    async def on_passed(vote_state):
        await send_group_msg(
            ws,
            vote_state.group_id,
            f"✅ [{vote_state.server_name}] 重启投票已通过！服务器即将关闭…",
        )
        vote_mgr.clear_vote(vote_state.server_id)
        _run_server_console_command("stop")

    active = vote_mgr.get_vote(server_id)
    if active:
        reply, passed = vote_mgr.add_vote(server_id, player_name, on_passed)
    else:
        reply, passed = vote_mgr.start_vote(
            server_id=server_id,
            server_name=server_name,
            group_id=group_id,
            online_players=online_names,
            initiator_name=player_name,
            on_timeout=on_timeout,
            on_passed=on_passed,
        )

    if passed:
        vote = vote_mgr.get_vote(server_id)
        if vote:
            await on_passed(vote)
    return reply


def _is_qq_group_admin(sender: Optional[dict]) -> bool:
    """判断发言者是否为 QQ 群主或群管理员（OneBot sender.role）。"""
    role = (sender or {}).get("role", "member")
    return role in ("owner", "admin")


def _hub_command_target_applies_here(target_sid: Optional[int]) -> bool:
    """Hub 子服定向：无编号或本机编号匹配时才执行。"""
    if target_sid is None:
        return True
    my_id = getattr(_plugin_instance, "hub_numeric_server_id", None)
    if my_id is None:
        return True
    return my_id == target_sid


async def _handle_group_command(
    ws,
    user_id: int,
    raw_message: str,
    display_name: str,
    group_id: int,
    sender: Optional[dict] = None,
):
    """处理群内命令"""
    try:
        from ..utils.message_utils import parse_hub_command_routing

        effective_line, route_sid = parse_hub_command_routing(raw_message)
        if not _hub_command_target_applies_here(route_sid):
            return

        # 解析命令（使用去掉子服编号后的文本）
        cmd_parts = effective_line.strip().split()
        if not cmd_parts:
            return
        
        cmd = cmd_parts[0][1:] if cmd_parts[0].startswith('/') else cmd_parts[0]  # 去掉/前缀
        args = cmd_parts[1:] if len(cmd_parts) > 1 else []
        
        admins = _plugin_instance.config_manager.get_config("admins", [])
        is_config_admin = str(user_id) in admins
        is_group_admin = _is_qq_group_admin(sender)
        can_use_cmd = is_config_admin or is_group_admin
        
        reply = ""
        
        # /help 命令
        if cmd == "help":
            if is_config_admin:
                reply = _plugin_instance.config_manager.get_help_text_with_admin()
            elif is_group_admin:
                reply = _plugin_instance.config_manager.get_help_text_with_group_admin()
            else:
                reply = _plugin_instance.config_manager.get_help_text()

        # /servers — Hub 子服编号列表（所有用户可用）
        elif cmd in ("servers", "hublist"):
            cat = _plugin_instance.get_hub_server_catalog_display()
            my_id = getattr(_plugin_instance, "hub_numeric_server_id", None)
            if not cat:
                reply = (
                    "🌐 当前未处于 Hub 模式或未同步子服列表。\n"
                    "💡 使用 Hub 中转并联机后，可在此查看各服数字编号。"
                )
            else:
                lines = [
                    "🌐 Hub 服务器编号（省略编号则命令在所有子服执行；"
                    "指定编号则仅该服执行）：",
                ]
                for item in cat:
                    lines.append(f"• {item.get('id')}: {item.get('name')}")
                if my_id is not None:
                    lines.append(f"\n本群命令当前执行端编号: {my_id}")
                reply = "\n".join(lines)
        
        # /list 命令 - 查看在线玩家列表
        elif cmd == "list":
            online_players = _plugin_instance.server.online_players
            if not online_players:
                reply = "🎮 当前没有玩家在线"
            else:
                player_list = []
                for player in online_players:
                    try:
                        ping = player.ping
                        ping_display = f"{ping}ms"
                    except Exception:
                        ping_display = "N/A"
                    player_list.append(f"• {player.name} [{ping_display}]")
                
                reply = f"🎮 在线玩家 ({len(online_players)}/{_plugin_instance.server.max_players}):\n" + "\n".join(player_list)
        
        # /tps 命令 - 查看服务器性能
        elif cmd == "tps":
            try:
                current_tps = _plugin_instance.server.current_tps
                average_tps = _plugin_instance.server.average_tps
                current_mspt = _plugin_instance.server.current_mspt
                average_mspt = _plugin_instance.server.average_mspt
                current_tick_usage = _plugin_instance.server.current_tick_usage
                average_tick_usage = _plugin_instance.server.average_tick_usage
                
                reply = f"📊 服务器性能状态:\n"
                reply += f"• 当前TPS: {current_tps:.2f}/20.0"
                
                # TPS状态指示
                if current_tps >= 19.0:
                    reply += " ✅ 良好\n"
                elif current_tps >= 15.0:
                    reply += " ⚠️ 轻微延迟\n"
                else:
                    reply += " ❌ 严重延迟\n"
                
                reply += f"• 平均TPS: {average_tps:.2f}/20.0\n"
                reply += f"• 当前MSPT: {current_mspt:.2f}ms\n"
                reply += f"• 平均MSPT: {average_mspt:.2f}ms\n"
                reply += f"• 当前Tick使用率: {current_tick_usage:.1f}%\n"
                reply += f"• 平均Tick使用率: {average_tick_usage:.1f}%"
                
            except Exception as e:
                reply = "📊 无法获取服务器性能数据"
                if _plugin_instance:
                    _plugin_instance.logger.error(f"获取服务器性能数据失败: {e}")
        
        # /info 命令 - 查看服务器信息
        elif cmd == "info":
            try:
                from ..utils.time_utils import TimeUtils
                from ..utils.info import get_system_info_dict
                
                online_count = len(_plugin_instance.server.online_players)
                max_players = _plugin_instance.server.max_players
                server_name = _plugin_instance.server_name
                version = _plugin_instance.server.version
                minecraft_version = _plugin_instance.server.minecraft_version
                start_time = _plugin_instance.server.start_time
                
                # 获取插件统计信息
                total_bindings = len(_plugin_instance.data_manager.binding_data)
                
                # 使用时间工具模块获取当前时间和运行时长
                time_info = TimeUtils.get_current_time_info()
                uptime_info = TimeUtils.calculate_uptime(start_time)
                
                # 获取系统硬件信息
                system_info = get_system_info_dict()
                
                reply = f"ℹ️ 服务器信息:\n"

                # === 服务器基本信息 ===
                reply += f"• 服务器名称: {server_name}\n"
                reply += f"• Endstone版本: {version}\n"
                reply += f"• Minecraft版本: {minecraft_version}\n"
                reply += f"• 启动时间: {TimeUtils.format_datetime(start_time)}\n"
                reply += f"• 当前时间: {time_info['formatted_time']} ({time_info['source']})\n"
                reply += f"• 运行时长: {uptime_info['uptime_str']}\n"
                reply += f"• 在线玩家: {online_count}/{max_players}\n"
                reply += f"• 总绑定数: {total_bindings}\n"
                
                # === 系统硬件信息 ===
                reply += f"\n🖥️ 系统信息:\n"
                reply += f"• 操作系统: {system_info['os']}\n"
                
                # CPU信息
                cpu_info = system_info['cpu']
                cpu_model = cpu_info['model'][:50] + "..." if len(cpu_info['model']) > 50 else cpu_info['model']  # 限制长度
                reply += f"• CPU型号: {cpu_model}\n"
                
                if cpu_info['max_freq_ghz']:
                    reply += f"• CPU主频: {cpu_info['max_freq_ghz']:.2f}GHz"
                    if cpu_info['current_freq_ghz']:
                        reply += f" (当前: {cpu_info['current_freq_ghz']:.2f}GHz)"
                    reply += "\n"
                
                reply += f"• CPU核心: {cpu_info['physical_cores']}核{cpu_info['logical_cores']}线程\n"
                reply += f"• CPU使用率: {cpu_info['usage_percent']:.1f}%\n"
                
                # 内存信息
                mem_info = system_info['memory']
                reply += f"• 内存: {mem_info['used_gb']:.1f}GB / {mem_info['total_gb']:.1f}GB ({mem_info['percent']:.1f}%)\n"
                
                # 硬盘信息
                disk_info = system_info['disks']
                if disk_info:
                    for disk in disk_info:
                        if 'error' in disk:
                            continue
                        reply += f"• 硬盘({disk['device']}): {disk['used_gb']:.1f}GB / {disk['total_gb']:.1f}GB ({disk['percent']:.1f}%)\n"
                
                reply += f"\n• QQSync群服互通: 运行中 ✅"
                
            except Exception as e:
                reply = "ℹ️ 无法获取服务器信息"
                if _plugin_instance:
                    _plugin_instance.logger.error(f"获取服务器信息失败: {e}")
                    # 提供基础信息作为回退
                    try:
                        online_count = len(_plugin_instance.server.online_players)
                        max_players = _plugin_instance.server.max_players
                        reply = f"ℹ️ 服务器基础信息:\n• 在线玩家: {online_count}/{max_players}\n• QQSync: 运行中 ✅"
                    except:
                        reply = "ℹ️ 服务器信息获取失败"
        
        # /重启 — 由 Hub 集中处理；非 Hub 单机时在本机处理
        elif cmd == "重启":
            hub = getattr(_plugin_instance, "_hub_server", None)
            if hub is not None:
                return
            reply = await _handle_restart_vote_local(ws, user_id, group_id)

        # /绑定 <玩家名> — 在群内将当前QQ绑定到指定游戏角色（需服务器记录中存在该角色）
        elif cmd == "绑定":
            if len(args) != 1:
                reply = "❌ 命令格式错误\n💡 正确用法：/绑定 <游戏内玩家名>\n💡 例如：/绑定 DEVILENMO"
            else:
                target_player_name = args[0].strip()
                qq_str = str(user_id)
                dm = _plugin_instance.data_manager

                if not target_player_name:
                    reply = "❌ 玩家名不能为空"
                elif dm.is_player_banned(target_player_name):
                    reply = f"❌ 玩家 {target_player_name} 已被封禁，无法绑定QQ"
                else:
                    existing_for_qq = dm.get_qq_player(qq_str)
                    if existing_for_qq and existing_for_qq != target_player_name:
                        reply = (
                            f"❌ 您的QQ已绑定游戏角色「{existing_for_qq}」\n"
                            "💡 如需改绑请先联系管理员解绑，避免恶意占用多个角色"
                        )
                    elif target_player_name not in dm.binding_data:
                        reply = (
                            f"❌ 服务器记录中找不到名为「{target_player_name}」的玩家\n"
                            "💡 请先在游戏中至少登录一次，再于群内绑定"
                        )
                    elif dm.is_player_bound(target_player_name):
                        bound_qq = dm.get_player_qq(target_player_name)
                        if bound_qq == qq_str:
                            reply = f"✅ 您的QQ已与游戏角色「{target_player_name}」绑定，无需重复操作"
                        else:
                            reply = (
                                f"❌ 游戏角色「{target_player_name}」已绑定其他QQ\n"
                                f"💡 该角色当前绑定QQ: {bound_qq}\n"
                                "💡 若需更换绑定请联系管理员，请勿抢绑他人账号"
                            )
                    else:
                        group_ok = True
                        if hasattr(_plugin_instance, "group_members") and _plugin_instance.group_members:
                            if qq_str not in _plugin_instance.group_members:
                                group_ok = False
                                reply = "❌ 无法确认您的QQ在本群内，请稍后再试或联系管理员刷新群成员缓存"
                        if group_ok:
                            stored = dm.binding_data.get(target_player_name, {})
                            player_xuid = (stored.get("xuid") or "").strip()
                            target_player_obj = None
                            for p in _plugin_instance.server.online_players:
                                if p.name == target_player_name:
                                    target_player_obj = p
                                    player_xuid = p.xuid
                                    break
                            if dm.bind_player_qq(target_player_name, player_xuid, qq_str):
                                _plugin_instance.logger.info(
                                    f"群内 /绑定 成功: 角色 {target_player_name} <- QQ {qq_str}"
                                )

                                def notify_bound_player():
                                    try:
                                        if target_player_obj and _plugin_instance.is_valid_player(target_player_obj):
                                            target_player_obj.send_message(
                                                f"{ColorFormat.GRAY}[QQsync] {ColorFormat.GREEN}[成功] QQ绑定成功！{ColorFormat.RESET}"
                                            )
                                            target_player_obj.send_message(
                                                f"{ColorFormat.GRAY}[QQsync] {ColorFormat.AQUA}您的QQ {qq_str} 已与游戏账号绑定{ColorFormat.RESET}"
                                            )
                                    except Exception as notify_err:
                                        _plugin_instance.logger.error(f"通知玩家绑定成功失败: {notify_err}")

                                _plugin_instance.server.scheduler.run_task(
                                    _plugin_instance, notify_bound_player, delay=1
                                )
                                await send_group_msg_with_at(
                                    ws, group_id, int(qq_str), f"玩家 {target_player_name} 已成功绑定QQ！"
                                )
                                if _plugin_instance.config_manager.get_config("sync_group_card", True):
                                    await set_group_card_in_all_groups(ws, user_id=int(qq_str), card=target_player_name)
                            else:
                                reply = "❌ 绑定失败，请稍后重试或联系管理员"
        
        # /cmd — 群管理员或插件配置管理员
        elif cmd == "cmd":
            if not can_use_cmd:
                reply = "❌ 该命令仅限群主/群管理员或插件配置管理员使用"
            elif len(args) < 1:
                reply = (
                    "❌ 命令格式错误\n"
                    "💡 用法：/cmd [子服编号] <控制台命令>\n"
                    "💡 例如：/cmd say hello、/cmd stop、/cmd 2 list"
                )
            else:
                reply = _run_server_console_command(" ".join(args))

        # === 插件配置管理员命令 ===
        elif is_config_admin:
            if cmd == "who" and len(args) >= 1:
                # 查询玩家信息
                # 修复包含空格的玩家名处理问题
                search_input = " ".join(args)
                # 处理带双引号的玩家名
                if search_input.startswith('"') and search_input.endswith('"') and len(search_input) >= 2:
                    search_input = search_input[1:-1]
                target_player = None
                player_data = None
                
                target_player, match_type = _resolve_target(search_input)
                
                if not target_player:
                    reply = f"❌ 未找到玩家 {search_input} 的记录"
                else:
                    player_data = _plugin_instance.data_manager.binding_data.get(target_player, {})
                
                if target_player and player_data:
                    from ..utils.time_utils import TimeUtils
                    
                    player_qq = player_data.get("qq", "")
                    
                    reply = f"= 玩家 {target_player} 详细信息 =\n"
                    reply += f"绑定QQ: {player_qq if player_qq else '未绑定'}\n"
                    
                    xuid = player_data.get("xuid", "")
                    if xuid:
                        reply += f"XUID: {xuid}\n"
                    
                    # 检查在线状态
                    is_online = any(player.name == target_player for player in _plugin_instance.server.online_players)
                    reply += f"当前状态: {'在线' if is_online else '离线'}\n"
                    
                    # 检查封禁状态
                    if _plugin_instance.data_manager.is_player_banned(target_player):
                        ban_time = player_data.get("ban_time", "")
                        ban_by = player_data.get("ban_by", "未知")
                        ban_reason = player_data.get("ban_reason", "无原因")
                        reply += f"封禁状态: 已封禁 ❌\n"
                        
                        # 格式化封禁时间
                        if ban_time:
                            try:
                                ban_time_dt = datetime.datetime.fromtimestamp(ban_time)
                                ban_time_str = TimeUtils.format_datetime(ban_time_dt)
                                reply += f"封禁时间: {ban_time_str}\n"
                            except (ValueError, TypeError):
                                reply += f"封禁时间: 格式错误\n"
                        
                        reply += f"封禁操作者: {ban_by}\n"
                        reply += f"封禁原因: {ban_reason}\n"
                    else:
                        reply += "封禁状态: 正常 ✅\n"
                    
                    # 添加游戏统计信息
                    reply += "\n📊 游戏统计:\n"
                    
                    # 游戏时长
                    total_playtime = player_data.get("total_playtime", 0)
                    if total_playtime > 0:
                        playtime_str = format_playtime(total_playtime)
                        reply += f"总游戏时长: {playtime_str}\n"
                    else:
                        reply += "总游戏时长: 无记录\n"
                    
                    # 会话统计
                    session_count = player_data.get("session_count", 0)
                    reply += f"登录次数: {session_count}次\n"
                    
                    # 最后登录时间
                    last_join_time = player_data.get("last_join_time")
                    if last_join_time:
                        try:
                            last_join_dt = datetime.datetime.fromtimestamp(last_join_time)
                            last_join_str = TimeUtils.format_datetime(last_join_dt)
                            reply += f"最后登录: {last_join_str}\n"
                        except (ValueError, TypeError):
                            reply += f"最后登录: 时间格式错误\n"
                    else:
                        reply += "最后登录: 无记录\n"
                    
                    # 最后退出时间
                    last_quit_time = player_data.get("last_quit_time")
                    if last_quit_time:
                        try:
                            last_quit_dt = datetime.datetime.fromtimestamp(last_quit_time)
                            last_quit_str = TimeUtils.format_datetime(last_quit_dt)
                            reply += f"最后退出: {last_quit_str}\n"
                        except (ValueError, TypeError):
                            reply += f"最后退出: 时间格式错误\n"
                    else:
                        reply += "最后退出: 无记录\n"
                    
                    # 绑定历史
                    reply += "\n🔗 绑定历史:\n"
                    
                    # 初始绑定时间
                    bind_time = player_data.get("bind_time")
                    if bind_time:
                        try:
                            bind_time_dt = datetime.datetime.fromtimestamp(bind_time)
                            bind_time_str = TimeUtils.format_datetime(bind_time_dt)
                            reply += f"初始绑定: {bind_time_str}\n"
                        except (ValueError, TypeError):
                            reply += f"初始绑定: 时间格式错误\n"
                    
                    # 重新绑定时间
                    rebind_time = player_data.get("rebind_time")
                    if rebind_time:
                        try:
                            rebind_time_dt = datetime.datetime.fromtimestamp(rebind_time)
                            rebind_time_str = TimeUtils.format_datetime(rebind_time_dt)
                            reply += f"重新绑定: {rebind_time_str}\n"
                        except (ValueError, TypeError):
                            reply += f"重新绑定: 时间格式错误\n"
                    
                    # 解绑历史
                    unbind_time = player_data.get("unbind_time")
                    if unbind_time:
                        try:
                            unbind_time_dt = datetime.datetime.fromtimestamp(unbind_time)
                            unbind_time_str = TimeUtils.format_datetime(unbind_time_dt)
                            unbind_by = player_data.get("unbind_by", "未知")
                            unbind_reason = player_data.get("unbind_reason", "无原因")
                            reply += f"解绑时间: {unbind_time_str}\n"
                            reply += f"解绑操作者: {unbind_by}\n"
                            reply += f"解绑原因: {unbind_reason}\n"
                        except (ValueError, TypeError):
                            reply += f"解绑时间: 时间格式错误\n"
                    
                    # 原始QQ（如果有解绑记录）
                    original_qq = player_data.get("original_qq")
                    if original_qq:
                        reply += f"原绑定QQ: {original_qq}\n"
                    
                    # 解封历史（如果有）
                    unban_time = player_data.get("unban_time")
                    if unban_time:
                        try:
                            unban_time_dt = datetime.datetime.fromtimestamp(unban_time)
                            unban_time_str = TimeUtils.format_datetime(unban_time_dt)
                            unban_by = player_data.get("unban_by", "未知")
                            reply += f"\n🔓 解封历史:\n"
                            reply += f"解封时间: {unban_time_str}\n"
                            reply += f"解封操作者: {unban_by}"
                        except (ValueError, TypeError):
                            reply += f"\n🔓 解封时间: 时间格式错误"
                
                # 如果没有设置reply且没有找到数据，设置默认错误消息
                if not reply:
                    reply = f"❌ 未找到玩家 {search_input} 的数据"
            
            elif cmd == "ban" and len(args) >= 1:
                # 封禁玩家
                search_input = args[0]
                target_player, match_type = _resolve_target(search_input)
                
                if not target_player:
                    reply = f"❌ 未找到玩家 {search_input} 的记录，无法封禁"
                else:
                    player_name = target_player
                    ban_reason = " ".join(args[1:]) if len(args) > 1 else "管理员封禁"
                    
                    if _plugin_instance.data_manager.ban_player(player_name, display_name, ban_reason):
                        reply = f"✅ 已封禁玩家 {player_name}"
                        if match_type == "QQ":
                            reply += f" (通过QQ查找)"
                        reply += f"\n原因: {ban_reason}"
                    else:
                        reply = f"❌ 封禁失败: 未知错误"
            
            elif cmd == "unban" and len(args) == 1:
                # 解封玩家
                search_input = args[0]
                target_player, match_type = _resolve_target(search_input)
                
                if not target_player:
                    reply = f"❌ 未找到玩家 {search_input} 的记录"
                elif _plugin_instance.data_manager.unban_player(target_player):
                    reply = f"✅ 已解封玩家 {target_player}"
                else:
                    reply = f"❌ 解封失败，玩家 {target_player} 未被封禁"
            
            elif cmd == "banlist":
                # 查看封禁列表
                banned_players = _plugin_instance.data_manager.get_banned_players()
                if not banned_players:
                    reply = "📋 当前没有被封禁的玩家"
                else:
                    reply = f"📋 封禁列表 ({len(banned_players)}):\n"
                    for banned_info in banned_players[:10]:  # 最多显示10个
                        player_name = banned_info["name"]
                        ban_by = banned_info["ban_by"]
                        ban_reason = banned_info["ban_reason"]
                        reply += f"• {player_name} (by {ban_by}): {ban_reason}\n"
                    
                    if len(banned_players) > 10:
                        reply += f"... 还有 {len(banned_players) - 10} 个被封禁的玩家"
            
            elif cmd == "unbindqq" and len(args) == 1:
                # 解绑QQ
                search_input = args[0]
                target_player = None
                
                search_input = args[0]
                target_player, match_type = _resolve_target(search_input)
                
                if not target_player:
                    reply = f"❌ 未找到匹配的玩家: {search_input}"
                
                if target_player and _plugin_instance.data_manager.unbind_player_qq(target_player, display_name):
                    reply = f"✅ 已解绑玩家 {target_player} 的QQ绑定"
                else:
                    reply = f"❌ 解绑失败，玩家 {target_player} 不存在或未绑定QQ"
            
            elif cmd == "reload":
                # 重新加载配置
                try:
                    _plugin_instance.config_manager.reload_config()
                    reply = "✅ 配置文件已重新加载"
                except Exception as e:
                    reply = f"❌ 重新加载配置失败: {str(e)}"
            
            # 非本插件指令：静默忽略，避免干扰群内其他 / 指令系统
        
        else:
            if cmd == "cmd":
                reply = "❌ 该命令仅限群主/群管理员或插件配置管理员使用"
            elif cmd in ["who", "ban", "unban", "banlist", "unbindqq", "reload"]:
                reply = "❌ 该命令仅限插件配置管理员使用"
            # 非本插件指令：静默忽略，避免干扰群内其他 / 指令系统
        
        # 发送回复（服务器名单独一行，便于群内阅读）
        if reply:
            server_name = _plugin_instance.server_name if _plugin_instance else "未知服务器"
            await send_group_msg(ws, group_id, f"[{server_name}]\n{reply}")

    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"处理群内命令失败: {e}")
            server_name = _plugin_instance.server_name if _plugin_instance else "未知服务器"
            await send_group_msg(ws, group_id, f"[{server_name}]\n❌ 命令处理失败: {str(e)}")


async def _forward_message_to_game(message_data: dict, display_name: str):
    """转发消息到游戏"""
    try:
        # 使用新的消息解析工具来处理消息
        from ..utils.message_utils import (
            parse_qq_message,
            clean_message_text,
            truncate_message,
            format_qq_group_chat_game_message,
        )
        
        # 获取群组ID和名称
        group_id = message_data.get("group_id")
        group_name = ""
        if _plugin_instance and group_id:
            group_names = _plugin_instance.config_manager.get_config("group_names", {})
            group_name = group_names.get(str(group_id), "")
        
        # 解析QQ消息，处理emoji和CQ码
        parsed_message = parse_qq_message(message_data)
        parsed_message = clean_message_text(parsed_message)
        parsed_message = truncate_message(parsed_message, max_length=150)
        
        if not parsed_message or parsed_message == "[空消息]":
            return
        
        # 转发到游戏 - 伪装为跨服聊天（地球Online服务器）
        game_message = format_qq_group_chat_game_message(
            display_name, parsed_message, group_name
        )
        
        if _plugin_instance:
            _plugin_instance.logger.info(game_message)

            # 为webui写入聊天历史记录
            webui = _plugin_instance.server.plugin_manager.get_plugin('qqsync_webui_plugin')
            if webui:
                try:
                    webui.on_message_sent(sender=display_name, content=parsed_message, msg_type="chat", direction="qq_to_game")
                except Exception as e:
                    _plugin_instance.logger.warning(f"webui on_message_sent调用失败: {e}")

            # 经 ARC Sync 下发到无本机 qqsync、走主机转发的子服
            try:
                _plugin_instance.notify_arc_qq_chat(
                    display_name, parsed_message, group_name
                )
            except Exception:
                pass
        
        def send_to_players():
            """在主线程中发送消息给所有玩家"""
            try:
                for player in _plugin_instance.server.online_players:
                    player.send_message(game_message)

            except Exception as e:
                if _plugin_instance:
                    _plugin_instance.logger.error(f"发送游戏消息失败: {e}")
        
        # 使用调度器在主线程执行
        if _plugin_instance:
            _plugin_instance.server.scheduler.run_task(_plugin_instance, send_to_players, delay=1)
            
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"转发消息到游戏失败: {e}")


async def handle_api_response(data: dict):
    """处理API响应"""
    try:
        status = data.get("status")
        retcode = data.get("retcode")
        response_data = data.get("data", {})
        echo = data.get("echo", "")

        action = None
        if echo.startswith("get_group_member_list"):
            action = "get_group_member_list"
        elif echo.startswith("set_group_card"):
            action = "set_group_card"

        if not _plugin_instance:
            return

        if status == "ok" and retcode != 0:
            error_msg = data.get("message", "未知错误")
            if action == "set_group_card":
                _plugin_instance.logger.warning(
                    f"❌ 设置群名片失败: retcode={retcode}, msg={error_msg}, echo={echo}"
                )
            elif action == "get_group_member_list":
                gid = _parse_group_id_from_member_list_echo(echo)
                if gid is not None:
                    _release_member_list_pending(gid)
                _plugin_instance.logger.warning(
                    f"❌ 获取群成员列表失败: retcode={retcode}, msg={error_msg}, echo={echo}"
                )
            else:
                _plugin_instance.logger.warning(
                    f"❌ API请求失败: retcode={retcode}, msg={error_msg}, echo={echo}"
                )

        elif action == "get_group_member_list":
            gid = _parse_group_id_from_member_list_echo(echo)
            success = status == "ok" and retcode == 0 and isinstance(response_data, list)
            if success:
                members = response_data
                group_members = set()
                for member in members:
                    user_id = str(member.get("user_id", ""))
                    if user_id:
                        group_members.add(user_id)

                added_count = 0
                if hasattr(_plugin_instance, 'group_members'):
                    old_count = len(_plugin_instance.group_members)
                    _plugin_instance.group_members.update(group_members)
                    new_count = len(_plugin_instance.group_members)
                    added_count = new_count - old_count

                if gid is not None:
                    if gid not in _plugin_instance.group_member_cards:
                        _plugin_instance.group_member_cards[gid] = {}
                    for member in members:
                        uid = str(member.get("user_id", ""))
                        if not uid:
                            continue
                        _plugin_instance.group_member_cards[gid][uid] = (member.get("card") or "").strip()
                    _release_member_list_pending(gid)

                if hasattr(_plugin_instance, 'group_members'):
                    _plugin_instance.logger.info(
                        f"已更新群成员列表，当前共 {len(_plugin_instance.group_members)} 人 (本次新增 {added_count} 人)"
                    )
            else:
                if gid is not None:
                    _release_member_list_pending(gid)
                    _plugin_instance.logger.warning(
                        f"获取群成员列表异常: group_id={gid}, status={status}, retcode={retcode}, echo={echo}"
                    )

    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"处理API响应失败: {e}")


async def handle_group_member_change(data: dict):
    """处理群成员变动"""
    try:
        if not _plugin_instance:
            return
            
        notice_type = data.get("notice_type")
        user_id = str(data.get("user_id", ""))
        group_id = data.get("group_id")
        
        target_groups = _plugin_instance.config_manager.get_config("target_groups", [])
        if group_id not in target_groups:
            return
        
        if notice_type == "group_increase":
            # 有人加群
            if hasattr(_plugin_instance, 'group_members'):
                _plugin_instance.group_members.add(user_id)
            _plugin_instance.logger.info(f"用户 {user_id} 加入群聊")
            
        elif notice_type == "group_decrease":
            # 有人退群
            if hasattr(_plugin_instance, 'group_members'):
                _plugin_instance.group_members.discard(user_id)
            
            # 检查是否有玩家绑定了这个QQ
            player_name = _plugin_instance.data_manager.get_qq_player(user_id)
            if player_name:
                _plugin_instance.logger.info(f"绑定玩家 {player_name} 的QQ {user_id} 退出群聊")
            else:
                _plugin_instance.logger.info(f"用户 {user_id} 退出群聊")
                
    except Exception as e:
        if _plugin_instance:
            _plugin_instance.logger.error(f"处理群成员变动失败: {e}")