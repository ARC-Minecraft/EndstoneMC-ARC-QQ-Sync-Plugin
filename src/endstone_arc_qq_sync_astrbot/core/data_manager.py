"""
数据管理模块
负责QQ绑定数据的存储、查询和管理
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.time_utils import TimeUtils


class DataManager:
    """数据管理器"""
    
    def __init__(self, data_folder: Path, logger, plugin=None):
        self.data_folder = data_folder
        self.logger = logger
        self._plugin = plugin
        self._remote_data_mode = False
        self.binding_file = data_folder / "data.json"
        self._binding_data: Dict[str, Any] = {}
        self._auto_save_enabled = True
        
        self._init_binding_data()
    
    def _init_binding_data(self):
        """初始化QQ绑定数据文件"""
        # 如果绑定数据文件不存在，创建空数据
        if not self.binding_file.exists():
            self.binding_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.binding_file, 'w', encoding='utf-8') as f:
                json.dump({}, f, indent=2, ensure_ascii=False)
            self.logger.info(f"已创建QQ绑定数据文件: {self.binding_file}")
        
        # 读取绑定数据
        try:
            with open(self.binding_file, 'r', encoding='utf-8') as f:
                self._binding_data = json.load(f)
        except Exception as e:
            self.logger.error(f"读取QQ绑定数据失败: {e}")
            self._binding_data = {}
        
        # 更新旧数据结构兼容性
        self._update_data_structure()
        
        from endstone import ColorFormat
        self.logger.info(f"{ColorFormat.AQUA}QQ绑定数据已加载，已绑定玩家: {len(self._binding_data)}{ColorFormat.RESET}")
    
    def enable_remote_hub_mode(self, enabled: bool):
        """
        Hub 客户端为 True：运行时绑定与统计一律走 Hub RPC，不读写本地 data.json。
        Hub 服务端为 False：从本地 data.json 加载并作为唯一权威数据源。
        """
        self._remote_data_mode = enabled
        if enabled:
            self._binding_data = {}
            self.logger.info("已启用 Hub 集中数据：绑定由 AstrBot 中心维护，时长由 ARCCore 维护")
        else:
            try:
                if self.binding_file.exists():
                    with open(self.binding_file, "r", encoding="utf-8") as f:
                        self._binding_data = json.load(f)
                else:
                    self.binding_file.parent.mkdir(parents=True, exist_ok=True)
                    self._binding_data = {}
                    with open(self.binding_file, "w", encoding="utf-8") as f:
                        json.dump({}, f, indent=2, ensure_ascii=False)
                self._update_data_structure()
            except Exception as e:
                self.logger.error(f"从本地 data.json 加载失败: {e}")
                self._binding_data = {}
            from endstone import ColorFormat
            self.logger.info(
                f"{ColorFormat.AQUA}本地 data.json 已作为权威数据，记录数: {len(self._binding_data)}{ColorFormat.RESET}"
            )

    def _use_remote(self) -> bool:
        return self._remote_data_mode

    def _rpc(self, action: str, args: Optional[Dict[str, Any]] = None) -> Any:
        if not self._plugin:
            raise RuntimeError("DataManager 未关联插件，无法访问 Hub")
        client = getattr(self._plugin, "_hub_client", None)
        loop = getattr(self._plugin, "_loop", None)
        if not client or not loop:
            raise RuntimeError("Hub 客户端未就绪")
        fut = asyncio.run_coroutine_threadsafe(client.data_rpc(action, args or {}), loop)
        return fut.result(timeout=125)

    def _rpc_safe(self, action: str, args: Optional[Dict[str, Any]], default: Any):
        try:
            return self._rpc(action, args)
        except Exception as e:
            self.logger.warning(f"Hub 数据 RPC ({action}) 失败: {e}")
            return default

    def clear_local_binding_file_only(self) -> None:
        """子服将历史 data.json 合并到 Hub 成功后，清空本地文件（不经过 RPC）。"""
        try:
            self.binding_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.binding_file, "w", encoding="utf-8") as f:
                json.dump({}, f, indent=2, ensure_ascii=False)
            self.logger.info("本地 data.json 已清空（历史记录已并入 Hub）")
        except Exception as e:
            self.logger.warning(f"清空本地 data.json 失败: {e}")

    @staticmethod
    def _max_optional_ts(a, b):
        pool = [x for x in (a, b) if x is not None]
        return max(pool) if pool else None

    @staticmethod
    def _min_optional_ts(a, b):
        pool = [x for x in (a, b) if x is not None]
        return min(pool) if pool else None

    def merge_legacy_binding_snapshot(
        self, snapshot: Dict[str, Any], source_server: str = ""
    ) -> Dict[str, int]:
        """
        将子服导出的 data.json 合并进当前库（仅 Hub 进程调用）。
        已存在同名玩家：累计时长与进服次数相加，时间戳取较新/绑定时间取较早等；不存在则整段导入。
        """
        if self._use_remote():
            raise RuntimeError("merge_legacy_binding_snapshot 仅在 Hub 端执行")
        if not isinstance(snapshot, dict):
            return {"created": 0, "merged": 0, "skipped": 0}

        src = f"[{source_server}] " if source_server else ""
        stats = {"created": 0, "merged": 0, "skipped": 0}
        required_fields = {
        }

        for key, inc in snapshot.items():
            if not isinstance(inc, dict):
                stats["skipped"] += 1
                continue
            name = str(key)
            for field, default_value in required_fields.items():
                if field not in inc:
                    inc[field] = default_value

            if name not in self._binding_data:
                new_rec = json.loads(json.dumps(inc))
                new_rec["name"] = name
                self._binding_data[name] = new_rec
                stats["created"] += 1
            else:
                self._merge_legacy_into_existing(self._binding_data[name], inc, name)
                stats["merged"] += 1

        self.save_data()
        self.logger.info(
            f"{src}子服历史 data.json 已合并至 Hub: 新建 {stats['created']} 条, "
            f"合并 {stats['merged']} 条, 跳过 {stats['skipped']} 条"
        )
        return stats

    def merge_legacy_binding_one(
        self, player_name: str, player_data: dict, source_server: str = ""
    ) -> str:
        """
        合并单名玩家（仅 Hub）。不写盘，请随后调用 merge_legacy_binding_persist。
        返回 created | merged | skipped
        """
        if self._use_remote():
            raise RuntimeError("merge_legacy_binding_one 仅在 Hub 端执行")
        if not isinstance(player_data, dict):
            return "skipped"
        required_fields = {
        }
        inc = json.loads(json.dumps(player_data))
        name = str(player_name)
        for field, default_value in required_fields.items():
            if field not in inc:
                inc[field] = default_value

        if name not in self._binding_data:
            new_rec = json.loads(json.dumps(inc))
            new_rec["name"] = name
            self._binding_data[name] = new_rec
            if source_server:
                self.logger.debug(f"[{source_server}] 历史合并 新建: {name}")
            return "created"
        self._merge_legacy_into_existing(self._binding_data[name], inc, name)
        if source_server:
            self.logger.debug(f"[{source_server}] 历史合并 并入: {name}")
        return "merged"

    def merge_legacy_binding_persist(self) -> None:
        """在逐条 merge_legacy_binding_one 之后落盘（仅 Hub）。"""
        if self._use_remote():
            raise RuntimeError("merge_legacy_binding_persist 仅在 Hub 端执行")
        self.save_data()

    def _merge_legacy_into_existing(self, hub: dict, inc: dict, name: str) -> None:
        # playtime/session_count owned by ARCCore SQLite — do not merge into binding JSON

        hub["last_join_time"] = self._max_optional_ts(
            hub.get("last_join_time"), inc.get("last_join_time")
        )
        hub["last_quit_time"] = self._max_optional_ts(
            hub.get("last_quit_time"), inc.get("last_quit_time")
        )

        def nz(v) -> bool:
            return bool(v and str(v).strip())

        if not nz(hub.get("qq")) and nz(inc.get("qq")):
            hub["qq"] = inc.get("qq", "")
        if not nz(hub.get("xuid")) and nz(inc.get("xuid")):
            hub["xuid"] = inc.get("xuid", "")
        elif nz(hub.get("xuid")) and nz(inc.get("xuid")) and str(hub["xuid"]) != str(inc["xuid"]):
            self.logger.warning(
                f"合并玩家 [{name}] 时 XUID 与子服不一致，保留 Hub 侧 XUID"
            )

        early_bind = self._min_optional_ts(hub.get("bind_time"), inc.get("bind_time"))
        if early_bind is not None:
            hub["bind_time"] = early_bind

        hub["rebind_time"] = self._max_optional_ts(hub.get("rebind_time"), inc.get("rebind_time"))

        if not nz(hub.get("qq")):
            for fld in ("unbind_time", "unbind_by", "original_qq", "unbind_reason"):
                if not hub.get(fld) and inc.get(fld):
                    hub[fld] = inc[fld]

        hb = bool(hub.get("is_banned", False))
        ib = bool(inc.get("is_banned", False))
        if ib and not hb:
            hub["is_banned"] = True
            for fld in ("ban_time", "ban_by", "ban_reason"):
                if inc.get(fld) is not None:
                    hub[fld] = inc[fld]
        elif hb and ib:
            hub["is_banned"] = True
            hub["ban_time"] = (
                self._min_optional_ts(hub.get("ban_time"), inc.get("ban_time"))
                or hub.get("ban_time")
                or inc.get("ban_time")
            )
        elif hb:
            hub["is_banned"] = True

        hub_unban = hub.get("unban_time")
        inc_unban = inc.get("unban_time")
        hub["unban_time"] = self._max_optional_ts(hub_unban, inc_unban)
        if inc.get("unban_by") and not hub.get("unban_by"):
            hub["unban_by"] = inc["unban_by"]

    def _update_data_structure(self):
        """更新数据结构以保持兼容性（不再删除任何玩家记录）"""
        data_updated = False
        
        for player_name, data in self._binding_data.items():
            # 添加缺失的字段，仅做补全，不做删除
            required_fields = {
            }
            
            for field, default_value in required_fields.items():
                if field not in data:
                    data[field] = default_value
                    data_updated = True
        
        if data_updated:
            self.save_data()
            self.logger.info("已更新绑定数据结构以支持在线时间统计（未删除任何玩家记录）")
    
    def save_data(self):
        """保存QQ绑定数据到文件"""
        if self._use_remote():
            return
        try:
            # 创建临时文件，避免写入过程中的数据损坏
            temp_file = self.binding_file.with_suffix('.tmp')
            
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self._binding_data, f, indent=2, ensure_ascii=False)
            
            # 原子性替换文件
            temp_file.replace(self.binding_file)
            
        except Exception as e:
            self.logger.error(f"保存QQ绑定数据失败: {e}")
            # 如果临时文件存在，清理它
            temp_file = self.binding_file.with_suffix('.tmp')
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except:
                    pass
    
    def trigger_save(self, reason: str = "数据变更"):
        """触发数据保存"""
        if self._use_remote():
            return
        if self._auto_save_enabled:
            self.save_data()
            self.logger.info(f"数据保存: {reason}")
    
    # 玩家绑定相关方法
    def is_player_bound(self, player_name: str, player_xuid: str = None) -> bool:
        """检查玩家是否已绑定QQ"""
        if self._use_remote():
            return bool(
                self._rpc_safe(
                    "is_player_bound",
                    {"player_name": player_name, "player_xuid": player_xuid},
                    False,
                )
            )
        # 如果提供了XUID，优先通过XUID查找
        if player_xuid:
            player_data = self._get_player_by_xuid(player_xuid)
            if player_data:
                qq_number = player_data.get("qq", "")
                return bool(qq_number and qq_number.strip())
            else:
                # 通过XUID没有找到，继续用玩家名查找（向后兼容）
                if player_name in self._binding_data:
                    qq_number = self._binding_data[player_name].get("qq", "")
                    return bool(qq_number and qq_number.strip())
                return False
        
        # 仅基于玩家名的检查
        if player_name not in self._binding_data:
            return False
        
        qq_number = self._binding_data[player_name].get("qq", "")
        return bool(qq_number and qq_number.strip())
    
    def _get_player_by_xuid(self, xuid: str) -> Dict[str, Any]:
        """根据XUID获取玩家绑定信息"""
        for name, data in self._binding_data.items():
            if data.get("xuid") == xuid:
                return data
        return {}
    
    def get_player_qq(self, player_name: str) -> str:
        """获取玩家绑定的QQ号"""
        if self._use_remote():
            r = self._rpc_safe("get_player_qq", {"player_name": player_name}, "")
            return r if isinstance(r, str) else ""
        return self._binding_data.get(player_name, {}).get("qq", "")
    
    def get_qq_player(self, qq_number: str) -> str:
        """根据QQ号获取绑定的玩家名"""
        if self._use_remote():
            r = self._rpc_safe("get_qq_player", {"qq_number": qq_number}, "")
            return r if isinstance(r, str) else ""
        for name, data in self._binding_data.items():
            current_qq = data.get("qq", "")
            if current_qq and current_qq.strip() == qq_number:
                return name
        return ""
    
    def get_qq_player_history(self, qq_number: str) -> str:
        """根据QQ号获取历史绑定的玩家名（包括已解绑的）"""
        if self._use_remote():
            r = self._rpc_safe("get_qq_player_history", {"qq_number": qq_number}, "")
            return r if isinstance(r, str) else ""
        for name, data in self._binding_data.items():
            # 首先检查当前绑定的QQ号
            if data.get("qq") == qq_number:
                return name
            # 检查原QQ号（用于被解绑或封禁的玩家历史查询）
            if data.get("original_qq") == qq_number:
                return name
        return ""
    
    def get_player_by_xuid(self, xuid: str) -> Dict[str, Any]:
        """根据XUID获取玩家绑定信息"""
        if self._use_remote():
            r = self._rpc_safe("get_player_by_xuid", {"xuid": xuid}, {})
            return r if isinstance(r, dict) else {}
        return self._get_player_by_xuid(xuid)
    
    def bind_player_qq(self, player_name: str, player_xuid: str, qq_number: str) -> bool:
        """绑定玩家QQ"""
        # 验证参数
        if not qq_number or not qq_number.strip():
            self.logger.error(f"尝试绑定空QQ号给玩家 {player_name}，操作被拒绝")
            return False
        
        qq_clean = qq_number.strip()
        if not qq_clean.isdigit() or len(qq_clean) < 5 or len(qq_clean) > 11:
            self.logger.error(f"尝试绑定无效QQ号 {qq_clean} 给玩家 {player_name}，QQ号必须是5-11位数字")
            return False
        
        if not player_name or not player_name.strip():
            self.logger.error(f"尝试绑定QQ {qq_clean} 给空玩家名，操作被拒绝")
            return False

        if self._use_remote():
            try:
                return bool(
                    self._rpc(
                        "bind_player_qq",
                        {
                            "player_name": player_name.strip(),
                            "player_xuid": player_xuid,
                            "qq_number": qq_clean,
                        },
                    )
                )
            except Exception as e:
                self.logger.error(f"Hub 绑定 QQ 失败: {e}")
                return False
        
        # 检查是否已有该玩家的数据
        if player_name in self._binding_data:
            # 保留现有的游戏数据，更新绑定信息
            player_data = self._binding_data[player_name]
            old_qq = player_data.get("qq", "")
            
            # 更新绑定信息
            player_data["qq"] = qq_clean
            player_data["xuid"] = player_xuid
            
            if old_qq:
                # 重新绑定
                player_data["rebind_time"] = int(TimeUtils.get_timestamp())
                player_data["previous_qq"] = old_qq
                self.logger.info(f"玩家 {player_name} 重新绑定QQ: {old_qq} → {qq_clean}")
            else:
                # 首次绑定或解绑后重新绑定
                if "unbind_time" in player_data:
                    player_data["rebind_time"] = int(TimeUtils.get_timestamp())
                    self.logger.info(f"玩家 {player_name} 解绑后重新绑定QQ: {qq_clean}")
                else:
                    player_data["bind_time"] = int(TimeUtils.get_timestamp())
                    self.logger.info(f"玩家 {player_name} 首次绑定QQ: {qq_clean}")
        else:
            # 全新的玩家数据
            self._binding_data[player_name] = {
                "name": player_name,
                "xuid": player_xuid,
                "qq": qq_clean,
                "bind_time": int(TimeUtils.get_timestamp()),
            }
            self.logger.info(f"玩家 {player_name} 已绑定QQ: {qq_clean}")
        
        self.trigger_save(f"绑定QQ: {player_name} → {qq_clean}")
        return True
    
    def unbind_player_qq(self, player_name: str, admin_name: str = "system") -> bool:
        """解绑玩家QQ（保留游戏数据）"""
        if self._use_remote():
            try:
                return bool(
                    self._rpc(
                        "unbind_player_qq",
                        {"player_name": player_name, "admin_name": admin_name},
                    )
                )
            except Exception as e:
                self.logger.error(f"Hub 解绑失败: {e}")
                return False
        if player_name not in self._binding_data:
            return False
        
        player_data = self._binding_data[player_name]
        original_qq = player_data.get("qq", "")
        
        if not original_qq or not original_qq.strip():
            return False
        
        # 保留所有游戏数据，只清空QQ相关信息
        player_data["qq"] = ""
        player_data["unbind_time"] = int(TimeUtils.get_timestamp())
        player_data["unbind_by"] = admin_name
        player_data["original_qq"] = original_qq
        
        self.trigger_save(f"解绑QQ: {player_name} (原QQ: {original_qq})")
        self.logger.info(f"玩家 {player_name} 的QQ绑定已被 {admin_name} 解除 (原QQ: {original_qq})，游戏数据已保留")
        return True
    
    def update_player_name(self, old_name: str, new_name: str, xuid: str) -> bool:
        """更新玩家名称（处理改名情况）"""
        if self._use_remote():
            try:
                return bool(
                    self._rpc(
                        "update_player_name",
                        {"old_name": old_name, "new_name": new_name, "xuid": xuid},
                    )
                )
            except Exception as e:
                self.logger.error(f"Hub 同步改名失败: {e}")
                return False
        if old_name in self._binding_data:
            # 保存原有数据
            player_data = self._binding_data[old_name].copy()
            # 更新名称
            player_data["name"] = new_name
            player_data["last_name_update"] = int(TimeUtils.get_timestamp())
            
            # 删除旧记录，添加新记录
            del self._binding_data[old_name]
            self._binding_data[new_name] = player_data
            
            self.trigger_save(f"玩家改名: {old_name} → {new_name}")
            self.logger.info(f"玩家改名: {old_name} → {new_name} (XUID: {xuid})")
            return True
        return False
    
    # 游戏统计相关方法
    def update_player_join(self, player_name: str, player_xuid: str = None) -> None:
        """Deprecated: playtime/session_count live in ARCCore DB."""
        if self._hub_mode:
            self._rpc("update_player_join", player_name=player_name, player_xuid=player_xuid)
            return
        return


    def update_player_quit(self, player_name: str) -> None:
        """Deprecated: playtime lives in ARCCore DB."""
        if self._hub_mode:
            self._rpc("update_player_quit", player_name=player_name)
            return
        return


    def get_player_playtime_info(self, player_name: str, online_players: List[Any] = None) -> Dict[str, Any]:
        """Deprecated: use ARCCore.api_get_player_playtime instead."""
        _ = online_players
        return {
            "session_count": 0,
            "total_playtime": 0,
            "is_online": False,
            "last_join_time": None,
            "last_quit_time": None,
        }


    def is_player_banned(self, player_name: str) -> bool:
        """检查玩家是否被封禁"""
        if self._use_remote():
            return bool(
                self._rpc_safe("is_player_banned", {"player_name": player_name}, False)
            )
        if player_name not in self._binding_data:
            return False
        return self._binding_data[player_name].get("is_banned", False)
    
    def ban_player(self, player_name: str, admin_name: str = "system", reason: str = "") -> bool:
        """封禁玩家"""
        if self._use_remote():
            try:
                return bool(
                    self._rpc(
                        "ban_player",
                        {
                            "player_name": player_name,
                            "admin_name": admin_name,
                            "reason": reason or "",
                        },
                    )
                )
            except Exception as e:
                self.logger.error(f"Hub 封禁失败: {e}")
                return False
        # 确保玩家数据存在
        if player_name not in self._binding_data:
            self._binding_data[player_name] = {
                "name": player_name,
                "xuid": "",
                "qq": "",
            }
        
        # 设置封禁状态
        player_data = self._binding_data[player_name]
        player_data["is_banned"] = True
        player_data["ban_time"] = int(TimeUtils.get_timestamp())
        player_data["ban_by"] = admin_name
        player_data["ban_reason"] = reason or "管理员封禁"
        
        # 如果玩家已绑定QQ，解除绑定
        if player_data.get("qq"):
            original_qq = player_data["qq"]
            player_data["qq"] = ""
            player_data["unbind_time"] = int(TimeUtils.get_timestamp())
            player_data["unbind_by"] = admin_name
            player_data["unbind_reason"] = "封禁时自动解绑"
            player_data["original_qq"] = original_qq
            self.logger.info(f"玩家 {player_name} 被封禁时自动解除QQ绑定 (原QQ: {original_qq})")
        
        self.trigger_save(f"封禁玩家: {player_name} (原因: {reason or '管理员封禁'})")
        self.logger.info(f"玩家 {player_name} 已被 {admin_name} 封禁，原因：{reason or '管理员封禁'}")
        return True
    
    def unban_player(self, player_name: str, admin_name: str = "system") -> bool:
        """解封玩家"""
        if self._use_remote():
            try:
                return bool(
                    self._rpc(
                        "unban_player",
                        {"player_name": player_name, "admin_name": admin_name},
                    )
                )
            except Exception as e:
                self.logger.error(f"Hub 解封失败: {e}")
                return False
        if player_name not in self._binding_data:
            return False
        
        player_data = self._binding_data[player_name]
        if not player_data.get("is_banned", False):
            return False
        
        # 解除封禁
        player_data["is_banned"] = False
        player_data["unban_time"] = int(TimeUtils.get_timestamp())
        player_data["unban_by"] = admin_name
        
        self.trigger_save(f"解封玩家: {player_name}")
        self.logger.info(f"玩家 {player_name} 已被 {admin_name} 解封")
        return True
    
    def get_banned_players(self) -> List[Dict[str, Any]]:
        """获取所有被封禁的玩家列表"""
        if self._use_remote():
            r = self._rpc_safe("get_banned_players", {}, [])
            return r if isinstance(r, list) else []
        banned_players = [
            {
                "name": player_name,
                "ban_time": data.get("ban_time"),
                "ban_by": data.get("ban_by", "unknown"),
                "ban_reason": data.get("ban_reason", "无原因")
            }
            for player_name, data in self._binding_data.items()
            if data.get("is_banned", False)
        ]
        return banned_players
    
    def get_player_binding_history(self, player_name: str) -> Dict[str, Any]:
        """获取玩家绑定历史信息"""
        if self._use_remote():
            r = self._rpc_safe(
                "get_player_binding_history",
                {"player_name": player_name},
                {},
            )
            return r if isinstance(r, dict) else {}
        if player_name not in self._binding_data:
            return {}
        
        data = self._binding_data[player_name]
        
        history = {
            "current_qq": data.get("qq", ""),
            "is_bound": bool(data.get("qq", "").strip()),
            "bind_time": data.get("bind_time"),
            "unbind_time": data.get("unbind_time"),
            "rebind_time": data.get("rebind_time"),
            "unbind_by": data.get("unbind_by"),
            "original_qq": data.get("original_qq"),
            "previous_qq": data.get("previous_qq"),
            "total_playtime": data.get("total_playtime", 0),
            "session_count": data.get("session_count", 0),
        }
        
        # 计算绑定状态
        if history["is_bound"]:
            if history["rebind_time"]:
                history["status"] = "重新绑定"
            else:
                history["status"] = "已绑定"
        else:
            if history["unbind_time"]:
                history["status"] = "已解绑"
            else:
                history["status"] = "从未绑定"
        
        return history
    
    def get_complete_player_binding_status(self, player_name: str, player_xuid: str) -> Dict[str, Any]:
        """获取玩家完整的绑定状态信息"""
        if self._use_remote():
            r = self._rpc_safe(
                "get_complete_player_binding_status",
                {"player_name": player_name, "player_xuid": player_xuid},
                {},
            )
            return r if isinstance(r, dict) else {}
        result = {
            "is_bound": False,
            "qq_number": "",
            "binding_source": "",
            "data_consistent": True,
            "issues": []
        }
        
        # 检查基于玩家名的绑定
        name_bound = False
        name_qq = ""
        if player_name in self._binding_data:
            name_qq = self._binding_data[player_name].get("qq", "")
            name_bound = bool(name_qq and name_qq.strip())
        
        # 检查基于XUID的绑定
        xuid_data = self._get_player_by_xuid(player_xuid)
        xuid_bound = False
        xuid_qq = ""
        if xuid_data:
            xuid_qq = xuid_data.get("qq", "")
            xuid_bound = bool(xuid_qq and xuid_qq.strip())
        
        # 分析绑定状态
        if name_bound and xuid_bound:
            if name_qq == xuid_qq:
                result["is_bound"] = True
                result["qq_number"] = name_qq
                result["binding_source"] = "both"
            else:
                result["is_bound"] = False
                result["data_consistent"] = False
                result["issues"].append(f"QQ号不一致: 玩家名对应{name_qq}, XUID对应{xuid_qq}")
        elif name_bound and not xuid_bound:
            result["is_bound"] = name_bound
            result["qq_number"] = name_qq
            result["binding_source"] = "name"
            result["issues"].append("仅玩家名有绑定记录，XUID无对应数据")
        elif not name_bound and xuid_bound:
            result["is_bound"] = xuid_bound
            result["qq_number"] = xuid_qq
            result["binding_source"] = "xuid"
            result["issues"].append("仅XUID有绑定记录，当前玩家名无对应数据")
        else:
            result["is_bound"] = False
            result["binding_source"] = "none"
        
        return result
    
    @property
    def binding_data(self) -> Dict[str, Any]:
        """获取完整绑定数据的副本"""
        if self._use_remote():
            r = self._rpc_safe("get_binding_data", {}, {})
            return r if isinstance(r, dict) else {}
        return self._binding_data.copy()

    # 在线时长：进服时记录时间，离服时根据内存中的进服时间计算本次时长并累加
    def start_player_timer(self, player_name: str, player_xuid: str = None):
        """Deprecated no-op — ARCCore tracks session timers."""
        _ = (player_name, player_xuid)
        return


    def stop_player_timer(self, player_name: str):
        """Deprecated no-op — ARCCore tracks session timers."""
        _ = player_name
        return


    def cleanup_timer_system(self):
        """Deprecated no-op — ARCCore settles playtime on disable."""
        return

