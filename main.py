"""上下班开关插件（duty_switch，AstrBot）——按群独立生效 + 人性化提示。

让兔兔在【每个群里】像上班/下班一样作息，各群状态互相独立：
- 上班：恢复该群的正常接收、回复，各插件与 LLM 能力照常运行；
- 下班：该群所有消息被静默拦截（不回复、不触发任何插件、不调用 LLM），
        直到这个群的管理员再次发送「上班」；其他群不受影响。

提示文案风格参考 AmiyaBot amiyabot-admin 的「工作/休息」模块（阿米娅人设语气）：
- 打卡上班啦~ / 主人真是太过分了！哼~ >.< / ^_^ 等口语化表情文案；
- 下班时记录休息起始时间，上班时提示「休息了多久」（才/一共 + 天/小时/分钟/秒）。

称呼不再硬编码：机器人自称由配置 duty_bot_name 决定（默认“兔兔”），
对用户的称呼由配置 duty_user_title 决定（默认“主人”），可在插件管理面板修改。

设计要点（预留扩展）：
1. 下班拦截使用 CustomFilter + 高优先级 handler：
   —— 拦截 handler 注册了自定义过滤器 OffDutyBlockFilter（仅「该群在下班状态 AND 群消息」
      放行），且优先级 priority=10，先于其他插件 handler 执行，event.stop_event() 后整条消息静默；
   —— 上班/下班指令 priority=100，先于拦截器执行，因此下班群也能被管理员叫醒；
2. CustomFilter 在插件加载时实例化、无法拿到插件实例，因此插件与过滤器共享模块级
   _STATE 状态容器；
3. 状态持久化到 data/plugin_data/duty_switch_state.json（get_astrbot_plugin_data_path），
   结构为 {groups: {群ID: {off: 是否下班, sleep_since: 下班起始时间戳}}}，
   AstrBot 重启后保持各群状态；兼容 v1.1.0 的纯 bool 旧格式；
4. 默认状态：未操作过的群按配置 duty_start_on 决定（默认上班）。
   显式切换后只记录与默认不同的群，状态文件保持最小；
5. 扩展点：
   —— 如需下班时同时静默后台主动推送（如动态监听）：在各推送入口判断
      _STATE.off_groups.get(群ID, default_off) 后再发送；
   —— 如需私聊批量管理多个群：给「上班/下班」命令加群参数（如「上班 群号」）即可。

命令（需 @ 或唤醒词触发，如「@兔兔 下班」「兔兔下班」；仅对当前所在群生效）：
  上班  该群恢复工作状态
  下班  该群进入休息状态（静默拦截群消息）
"""

import json
import logging
import os
import time
from datetime import timedelta

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

# 提示文案阈值（参考 AmiyaBot amiyabot-admin）
SHORT_REST_SECONDS = 600  # 休息不足 10 分钟：用「才休息了」
LONG_REST_SECONDS = 21600  # 休息超过 6 小时：提示「充足休息」
SLEEP_WHISPER_SECONDS = 60  # 下班后 1 分钟内重复下班：当作刚睡着


class _DutyState:
    """插件实例与 CustomFilter 共享的运行状态（按群）。"""

    def __init__(self) -> None:
        self.enabled = True  # 插件总开关（对应配置 duty_enabled）
        self.default_off = False  # 未操作过的群的默认状态（对应配置 duty_start_on）
        # 群ID(str) -> 是否下班(bool)。只记录与默认状态不同的群
        self.off_groups: dict[str, bool] = {}
        # 群ID(str) -> 下班起始时间戳(float)。仅记录处于下班状态的群，用于上班时提示休息时长
        self.sleep_since: dict[str, float] = {}


_STATE = _DutyState()


class OffDutyBlockFilter(CustomFilter):
    """CustomFilter：仅当消息所在群处于下班状态时放行（放行=交给拦截 handler 静默吞掉）。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if not _STATE.enabled:
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        gid = str(event.get_group_id() or "")
        if not gid:
            return False
        return bool(_STATE.off_groups.get(gid, _STATE.default_off))


class DutySwitchPlugin(star.Star):
    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config = config or {}
        _STATE.enabled = bool(self.config.get("duty_enabled", True))
        _STATE.default_off = not bool(self.config.get("duty_start_on", True))
        self._load_state()

    # ------------------------------------------------------------------
    # 配置 / 权限 / 群信息辅助
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default)
        return default if value is None else value

    def _bot_name(self) -> str:
        """机器人自称（可配置，默认“兔兔”）。"""
        return str(self._cfg("duty_bot_name", "兔兔") or "").strip() or "兔兔"

    def _user_title(self) -> str:
        """对用户的称呼（可配置，默认“主人”）。"""
        return str(self._cfg("duty_user_title", "主人") or "").strip() or "主人"

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

    def _group_label(self, event: AstrMessageEvent, gid: str) -> str:
        """尽量带出群名，没有群名就只用群号。"""
        group = getattr(getattr(event, "message_obj", None), "group", None)
        name = getattr(group, "name", None) if group else None
        name = str(name or "").strip()
        return f"{name}({gid})" if name else gid

    @staticmethod
    def _format_duration(seconds) -> str:
        """把秒数格式化为「X天X小时X分钟X秒」（去掉零部分；参考 AmiyaBot TimeRecorder）。"""
        try:
            td = timedelta(seconds=max(0, int(seconds)))
        except (OverflowError, ValueError, TypeError):
            return "很久"
        day = td.days
        hour, minute, sec = [int(n) for n in str(td).split(",")[-1].split(":")]
        parts = []
        if day:
            parts.append(f"{day}天")
        if hour:
            parts.append(f"{hour}小时")
        if minute:
            parts.append(f"{minute}分钟")
        if sec:
            parts.append(f"{sec}秒")
        return "".join(parts) or "0秒"

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
        return (
            f"这个操作需要管理员权限哦~请联系{self._user_title()}把您的账号 ID 加入插件配置 "
            f"duty_admin_ids，或让群主/管理员来操作。"
        )

    def _is_off(self, gid: str) -> bool:
        """该群当前是否下班。"""
        return bool(_STATE.off_groups.get(gid, _STATE.default_off))

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        """从 state.json 恢复各群状态（文件不存在时用配置默认值）。"""
        try:
            if not os.path.exists(STATE_FILE):
                return
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("groups"), dict):
                off_groups: dict[str, bool] = {}
                sleep_since: dict[str, float] = {}
                for k, v in data["groups"].items():
                    gid = str(k).strip()
                    if not gid:
                        continue
                    if isinstance(v, dict):
                        # v1.2.0 新格式：{off: bool, sleep_since: float}
                        off_groups[gid] = bool(v.get("off", False))
                        since = v.get("sleep_since")
                        if off_groups[gid] and isinstance(since, (int, float)) and since > 0:
                            sleep_since[gid] = float(since)
                    else:
                        # v1.1.0 旧格式：纯 bool
                        off_groups[gid] = bool(v)
                _STATE.off_groups = off_groups
                _STATE.sleep_since = sleep_since
                logger.info(f"[duty_switch] 已从 {STATE_FILE} 恢复 {len(off_groups)} 个群的状态")
            else:
                # v1.0.0 极旧全局状态格式（off_duty=bool），无法映射到具体群，忽略
                logger.info("[duty_switch] 检测到 v1.0.0 旧版全局状态文件，已忽略（v1.1.0 起按群独立生效）")
        except (OSError, ValueError) as e:
            logger.warning(f"[duty_switch] 读取状态文件失败（使用默认状态）: {e}")

    def _save_state(self, event: AstrMessageEvent) -> None:
        """保存当前各群状态（duty_persist=false 时跳过）。"""
        if not bool(self._cfg("duty_persist", True)):
            return
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            groups = {}
            for gid, off in _STATE.off_groups.items():
                entry: dict = {"off": off}
                since = _STATE.sleep_since.get(gid)
                if off and since:
                    entry["sleep_since"] = since
                groups[str(gid)] = entry
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "groups": groups,
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
    # 上班 / 下班（priority 100：确保先于下班拦截器执行；仅对当前群生效）
    # ------------------------------------------------------------------
    @filter.command(CMD_ON, priority=100)
    async def cmd_go_on_duty(self, event: AstrMessageEvent):
        """让兔兔在【当前群】上班：恢复接收/回复群消息。"""
        async for r in self._toggle(event, go_on_duty=True):
            yield r

    @filter.command(CMD_OFF, priority=100)
    async def cmd_go_off_duty(self, event: AstrMessageEvent):
        """让兔兔在【当前群】下班：静默拦截该群所有消息。"""
        async for r in self._toggle(event, go_on_duty=False):
            yield r

    async def _toggle(self, event: AstrMessageEvent, go_on_duty: bool):
        """切换当前群的上班/下班状态，并返回人性化确认文案。"""
        bot = self._bot_name()
        user = self._user_title()

        if not _STATE.enabled:
            event.stop_event()
            yield event.make_result().message(
                f"上下班开关已在管理面板中关闭（duty_enabled=false），{bot}就不管这件事啦。",
            )
            return

        gid = str(event.get_group_id() or "")
        if not gid:
            # 私聊没有群归属，按群生效的开关无法定位目标群
            event.stop_event()
            yield event.make_result().message(
                f"上班/下班是按群生效的哦~请到对应的群里 @{bot} 发送指令（如「@{bot} 下班」）。",
            )
            return

        denied = self._check_admin(event)
        if denied:
            event.stop_event()
            yield event.make_result().message(denied)
            return

        now = time.time()
        was_off = self._is_off(gid)
        # 休息起始时间要在状态更新前取，否则上班后会因记录被清除而丢失
        rest_since = _STATE.sleep_since.get(gid) if was_off else None
        off = not go_on_duty
        if off == _STATE.default_off:
            # 与默认状态一致时删掉记录，保持状态文件最小、语义清晰
            _STATE.off_groups.pop(gid, None)
        else:
            _STATE.off_groups[gid] = off
        if off:
            _STATE.sleep_since.setdefault(gid, now)
        else:
            _STATE.sleep_since.pop(gid, None)
        self._save_state(event)

        event.stop_event()
        if not bool(self._cfg("duty_off_reply", True)):
            return  # 配置为静默生效时不回复

        label = self._group_label(event, gid)
        if go_on_duty:
            if was_off:
                since = rest_since or now
                elapsed = now - since
                total = self._format_duration(elapsed)
                brief = "才" if elapsed < SHORT_REST_SECONDS else "一共"
                if elapsed < LONG_REST_SECONDS:
                    text = (
                        f"打卡上班啦~（群「{label}」）{bot}{brief}休息了{total}……"
                        f"{user}真是太过分了！哼~ >.<"
                    )
                else:
                    text = (
                        f"打卡上班啦~（群「{label}」）{bot}{brief}休息了{total}。"
                        f"充足的休息才能更好的工作，{user}也要记得休息哦 ^_^"
                    )
            else:
                text = f"{bot}没有偷懒哦{user}~群「{label}」一直好好工作着呢~"
        else:
            if was_off:
                since = rest_since or now
                total = self._format_duration(now - since)
                if now - since < SLEEP_WHISPER_SECONDS:
                    text = f"（{bot}刚刚才下班，好像已经睡着了...）"
                elif now - since < LONG_REST_SECONDS:
                    text = f"（{bot}似乎已经睡着了...）"
                else:
                    text = (
                        f"{bot}已经休息了{total}啦~群「{label}」先休息休息，"
                        f"{user}需要的时候请让{bot}上班吧 ^_^"
                    )
            else:
                text = (
                    f"打卡下班啦~群「{label}」的{bot}要开始休息咯，"
                    f"{user}需要的时候再叫我上班吧 ^_^"
                )
        yield event.make_result().message(text)

    # ------------------------------------------------------------------
    # 下班静默拦截（priority 10：优先于其他插件，但低于上班/下班指令）
    # 说明：拦截器不 yield 任何消息，仅 stop_event()；空 result 不会被发送。
    # ------------------------------------------------------------------
    @filter.custom_filter(OffDutyBlockFilter, priority=10)
    async def gate_off_duty(self, event: AstrMessageEvent):
        """下班群的群消息：吞掉（不回复、不触发插件、不调用 LLM）。"""
        event.stop_event()

    async def terminate(self):
        """插件卸载时清理（本插件无后台任务，留空即可）。"""