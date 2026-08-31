"""上下班开关插件（duty_switch，AstrBot）。

让兔兔像上班/下班一样作息：
- 上班：恢复工作状态，正常接收、回复群消息，各插件与 LLM 能力照常运行；
- 下班：进入休息状态，群内所有消息被静默拦截（不回复、不触发任何插件、不调用 LLM），
        直到管理员再次发送「上班」。

设计要点（预留扩展）：
1. 下班拦截使用 CustomFilter + 高优先级 handler：
   —— 拦截 handler 注册了自定义过滤器 OffDutyBlockFilter（仅「下班状态 AND 群消息」放行），
      且优先级 priority=10，先于其他插件 handler 执行，event.stop_event() 后整条消息静默；
   —— 上班/下班指令 priority=100，先于拦截器执行，因此下班状态也能被管理员叫醒；
2. CustomFilter 在插件加载时实例化、无法拿到插件实例，因此插件与过滤器共享模块级
   _STATE 状态容器；
3. 状态持久化到 data/plugin_data/duty_switch_state.json（get_astrbot_plugin_data_path），
   AstrBot 重启后仍保持下班状态（duty_persist=false 时不保存）；
4. 扩展点：
   —— 如需按群独立开关：把 _STATE.off_duty 改成 {group_id: off_duty} 字典，
      拦截器按 event.get_group_id() 判断即可；
   —— 如需下班时同时静默后台主动推送（如动态监听）：在各推送入口判断 _STATE.off_duty
      后再发送。

命令（需 @ 或唤醒词触发，如「@兔兔 下班」「兔兔下班」）：
  上班  恢复工作状态
  下班  进入休息状态（静默拦截群消息）
"""

import json
import logging
import os
import time

from astrbot.api import star
from astrbot.api.all import AstrBotConfig, AstrMessageEvent
from astrbot.api.event import filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.platform import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

logger = logging.getLogger("astrbot")

STATE_FILE = os.path.join(get_astrbot_plugin_data_path(), "duty_switch_state.json")

CMD_ON = "上班"
CMD_OFF = "下班"


class _DutyState:
    """插件实例与 CustomFilter 共享的运行状态。"""

    def __init__(self) -> None:
        self.enabled = True  # 插件总开关（对应配置 duty_enabled）
        self.off_duty = False  # 是否处于下班状态


_STATE = _DutyState()


class OffDutyBlockFilter(CustomFilter):
    """CustomFilter：仅在下班状态 + 群消息时放行（放行=交给拦截 handler 静默吞掉）。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return (
            _STATE.enabled
            and _STATE.off_duty
            and event.get_message_type() == MessageType.GROUP_MESSAGE
        )


class DutySwitchPlugin(star.Star):
    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config = config or {}
        _STATE.enabled = bool(self.config.get("duty_enabled", True))
        _STATE.off_duty = not bool(self.config.get("duty_start_on", True))
        self._load_state()

    # ------------------------------------------------------------------
    # 配置 / 权限辅助
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default)
        return default if value is None else value

    @staticmethod
    def _norm_list(value) -> list[str]:
        """把配置项归一化为字符串列表（兼容 list 与换行/逗号分隔的字符串）。"""
        if value is None:
            return []
        if isinstance(value, list):
            items = value
        else:
            # 逗号（中英文）或换行分隔 → 归一为列表
            text = str(value).replace(",", "\n").replace("，", "\n")
            items = text.split("\n")
        return [str(x).strip() for x in items if str(x).strip()]

    @staticmethod
    def _sender_field(event: AstrMessageEvent, field: str, default="") -> str:
        """兼容多种 sender 结构读取字段，返回字符串。"""
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        value = None
        if isinstance(sender, dict):
            value = sender.get(field)
        else:
            value = getattr(sender, field, None)
            if value is None and sender is not None and callable(getattr(sender, "get", None)):
                value = sender.get(field)
        if value in (None, ""):
            # aiocqhttp：message_obj.sender 是 MessageMember（无 role 字段），
            # 原始 OneBot 事件在 raw_message 中，sender 为 dict
            raw = getattr(message_obj, "raw_message", None)
            if raw is not None:
                raw_sender = raw.get("sender") if isinstance(raw, dict) else getattr(raw, "sender", None)
                if isinstance(raw_sender, dict):
                    raw_value = raw_sender.get(field)
                    if raw_value not in (None, ""):
                        return str(raw_value)
        return str(value or default or "")

    def _check_admin(self, event: AstrMessageEvent) -> str | None:
        """上班/下班指令权限校验。返回 None 表示放行，否则返回拒绝提示。"""
        if not bool(self._cfg("duty_permission_enabled", True)):
            return None
        sender_id = self._sender_field(event, "user_id").strip()
        if sender_id and sender_id in self._norm_list(self._cfg("duty_admin_ids")):
            return None
        role = self._sender_field(event, "role").lower()
        admin_roles = {
            x.lower()
            for x in self._norm_list(self._cfg("duty_admin_role", ["owner", "admin"]))
        }
        if role and role in admin_roles:
            return None
        return "该操作需要管理权限：请联系管理员把你的账号 ID 加入插件配置 duty_admin_ids，或由群主/管理员来执行上班/下班。"

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        """从 state.json 恢复下班状态（文件不存在时用配置默认值）。"""
        try:
            if not os.path.exists(STATE_FILE):
                return
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _STATE.off_duty = bool(data.get("off_duty", False))
                logger.info(
                    f"[duty_switch] 已从 {STATE_FILE} 恢复状态："
                    f"{'下班（静默拦截群消息）' if _STATE.off_duty else '上班（正常响应）'}",
                )
        except (OSError, ValueError) as e:
            logger.warning(f"[duty_switch] 读取状态文件失败（使用默认状态）: {e}")

    def _save_state(self, event: AstrMessageEvent) -> None:
        """保存当前状态（duty_persist=false 时跳过）。"""
        if not bool(self._cfg("duty_persist", True)):
            return
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "off_duty": _STATE.off_duty,
                        "updated_at": time.time(),
                        "updated_by": str(event.get_sender_id() or ""),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError as e:
            logger.warning(f"[duty_switch] 保存状态失败: {e}")

    # ------------------------------------------------------------------
    # 上班 / 下班（priority 100：确保先于下班拦截器执行）
    # ------------------------------------------------------------------
    @filter.command(CMD_ON, priority=100)
    async def cmd_go_on_duty(self, event: AstrMessageEvent):
        """让兔兔上班：恢复接收/回复群消息。"""
        async for r in self._toggle(event, go_on_duty=True):
            yield r

    @filter.command(CMD_OFF, priority=100)
    async def cmd_go_off_duty(self, event: AstrMessageEvent):
        """让兔兔下班：静默拦截群内所有消息。"""
        async for r in self._toggle(event, go_on_duty=False):
            yield r

    async def _toggle(self, event: AstrMessageEvent, go_on_duty: bool):
        if not _STATE.enabled:
            event.stop_event()
            yield event.make_result().message(
                "上下班开关已在插件配置中关闭（duty_enabled=false），当前不拦截任何消息。",
            )
            return

        denied = self._check_admin(event)
        if denied:
            event.stop_event()
            yield event.make_result().message(denied)
            return

        was_off = _STATE.off_duty
        _STATE.off_duty = not go_on_duty
        self._save_state(event)

        event.stop_event()
        if not bool(self._cfg("duty_off_reply", True)):
            return  # 配置为静默生效时不回复

        if go_on_duty:
            if was_off:
                text = "兔兔上班啦！已恢复接收群消息，各插件与能力正常运行。"
            else:
                text = "兔兔本来就在上班哦～当前已正常接收群消息与运行插件能力。"
        else:
            if was_off:
                text = "兔兔已经在下班状态了～群内消息继续静默，需要开工时发送「上班」即可。"
            else:
                text = "兔兔下班啦！已进入休息状态：群内消息不再回复，也不会触发任何插件/能力，直到发送「上班」唤醒。"
        yield event.make_result().message(text)

    # ------------------------------------------------------------------
    # 下班静默拦截（priority 10：优先于其他插件，但低于上班/下班指令）
    # 说明：拦截器不 yield 任何消息，仅 stop_event()；空 result 不会被发送。
    # ------------------------------------------------------------------
    @filter.custom_filter(OffDutyBlockFilter, priority=10)
    async def gate_off_duty(self, event: AstrMessageEvent):
        """下班状态：吞掉群内所有消息（不回复、不触发插件、不调用 LLM）。"""
        event.stop_event()

    async def terminate(self):
        """插件卸载时清理（本插件无后台任务，留空即可）。"""