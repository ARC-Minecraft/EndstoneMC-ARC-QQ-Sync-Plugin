"""
服务器重启投票管理
"""

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Set


VOTE_DURATION_SEC = 60


@dataclass
class RestartVoteState:
    server_id: int
    server_name: str
    group_id: int
    started_at: float
    expires_at: float
    online_players: Set[str]
    voted_player_names: Set[str] = field(default_factory=set)
    initiator_name: str = ""


class RestartVoteManager:
    """按子服编号维护重启投票状态（由 Hub 进程持有）。"""

    def __init__(self, logger):
        self.logger = logger
        self._votes: Dict[int, RestartVoteState] = {}
        self._timeout_tasks: Dict[int, asyncio.Task] = {}

    @staticmethod
    def required_votes(online_count: int) -> int:
        if online_count <= 0:
            return 0
        return math.ceil(online_count / 2)

    def get_vote(self, server_id: int) -> Optional[RestartVoteState]:
        vote = self._votes.get(server_id)
        if not vote:
            return None
        if time.time() >= vote.expires_at:
            return None
        return vote

    def is_passed(self, vote: RestartVoteState) -> bool:
        required = self.required_votes(len(vote.online_players))
        return len(vote.voted_player_names) >= required

    def clear_vote(self, server_id: int) -> None:
        self._votes.pop(server_id, None)
        task = self._timeout_tasks.pop(server_id, None)
        if task and not task.done():
            task.cancel()

    def cancel_all(self) -> None:
        for server_id in list(self._votes.keys()):
            self.clear_vote(server_id)

    def _schedule_timeout(
        self,
        server_id: int,
        on_timeout: Callable[[int, RestartVoteState], Awaitable[Any]],
    ) -> None:
        old = self._timeout_tasks.pop(server_id, None)
        if old and not old.done():
            old.cancel()

        async def _runner():
            try:
                vote = self._votes.get(server_id)
                if not vote:
                    return
                wait_sec = max(0.0, vote.expires_at - time.time())
                await asyncio.sleep(wait_sec)
                if server_id not in self._votes:
                    return
                current = self._votes.get(server_id)
                if not current or current.expires_at != vote.expires_at:
                    return
                if self.is_passed(current):
                    return
                snapshot = current
                self.clear_vote(server_id)
                await on_timeout(server_id, snapshot)
            except asyncio.CancelledError:
                pass

        loop = asyncio.get_event_loop()
        self._timeout_tasks[server_id] = loop.create_task(_runner())

    def start_vote(
        self,
        server_id: int,
        server_name: str,
        group_id: int,
        online_players: Set[str],
        initiator_name: str,
        on_timeout: Callable[[int, RestartVoteState], Awaitable[Any]],
        on_passed: Callable[[RestartVoteState], Awaitable[Any]],
    ) -> tuple[str, bool]:
        """
        发起投票并让发起人计入一票。
        返回 (qq_reply_text, passed_immediately)。
        """
        if not online_players:
            return "❌ 当前服务器没有在线玩家，无法发起重启投票", False

        existing = self.get_vote(server_id)
        if existing:
            return self.add_vote(server_id, initiator_name, on_passed)

        now = time.time()
        vote = RestartVoteState(
            server_id=server_id,
            server_name=server_name,
            group_id=group_id,
            started_at=now,
            expires_at=now + VOTE_DURATION_SEC,
            online_players=set(online_players),
            initiator_name=initiator_name,
        )
        vote.voted_player_names.add(initiator_name)
        self._votes[server_id] = vote
        self._schedule_timeout(server_id, on_timeout)

        required = self.required_votes(len(vote.online_players))
        reply = (
            f"🗳️ [{server_name}] 重启投票已开始\n"
            f"• 当前在线: {len(vote.online_players)} 人\n"
            f"• 需要票数: {required} 票（≥半数）\n"
            f"• 倒计时: {VOTE_DURATION_SEC} 秒\n"
            f"• 发起人: {initiator_name}\n"
            f"💡 请在本群发送 /重启 参与投票（仅 {server_name} 在线玩家有效）"
        )
        if self.is_passed(vote):
            return reply, True
        reply += f"\n✅ {initiator_name} 已投票 ({len(vote.voted_player_names)}/{required})"
        return reply, False

    def add_vote(
        self,
        server_id: int,
        voter_name: str,
        on_passed: Callable[[RestartVoteState], asyncio.Future],
    ) -> tuple[str, bool]:
        """为进行中的投票追加一票。返回 (reply, passed)。"""
        vote = self.get_vote(server_id)
        if not vote:
            return "❌ 当前没有进行中的重启投票", False

        if voter_name not in vote.online_players:
            return (
                f"❌ 您当前不在 [{vote.server_name}] 的在线玩家名单中，无法参与本次投票",
                False,
            )

        if voter_name in vote.voted_player_names:
            required = self.required_votes(len(vote.online_players))
            return (
                f"⚠️ 您已投过票 ({len(vote.voted_player_names)}/{required})",
                False,
            )

        vote.voted_player_names.add(voter_name)
        required = self.required_votes(len(vote.online_players))
        reply = (
            f"✅ {voter_name} 已投票\n"
            f"• 服务器: {vote.server_name}\n"
            f"• 进度: {len(vote.voted_player_names)}/{required} 票"
        )
        if self.is_passed(vote):
            return reply, True
        remaining = max(0, int(vote.expires_at - time.time()))
        reply += f"\n• 剩余时间: {remaining} 秒"
        return reply, False
