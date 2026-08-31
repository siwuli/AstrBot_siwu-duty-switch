# 兔兔 - 上下班开关（duty_switch）

AstrBot 插件：让兔兔在**每个群里**像上班/下班一样作息，各群状态互相独立。
在某个群发「下班」，**只有这个群**的消息全部静默——不回复、不触发任何插件、不调用 LLM，
其他群完全不受影响；在这个群再发「上班」即恢复。

> 插件 id：`duty_switch`　当前版本：`1.1.0`　适用 AstrBot `>=4.0.0`

## 功能

| 指令 | 效果（仅对当前所在群生效） |
| --- | --- |
| 「上班」（@兔兔 上班 / 兔兔上班） | 该群恢复接收/回复，各插件与 LLM 能力照常运行 |
| 「下班」（@兔兔 下班 / 兔兔下班） | 该群消息全部静默拦截，不回复、不触发插件、不调用 LLM；其他群不受影响 |

- 指令需要 **@ 机器人或唤醒词** 触发（与 AstrBot 其他指令一致），默认**仅群主/管理员**可执行，
  也可配置用户 ID 白名单。私聊无法定位目标群，请在要操作的群里发送。
- 状态按**群 ID 独立**持久化到 `data/plugin_data/duty_switch_state.json`（`{"groups": {群ID: 是否下班}}`），
  重启 AstrBot 后保持各群状态。
- 下班拦截使用 **高优先级 handler + CustomFilter**：下班群的群消息整个被吞掉（优先级 10，
  先于其他插件执行并 `event.stop_event()`）；上班/下班指令优先级 100，先于拦截器执行，
  因此下班群的管理员也能发「上班」叫醒。

## 配置项（管理面板 → 插件设置）

| 配置 | 说明 |
| --- | --- |
| `duty_enabled` | 插件总开关。关闭后不拦截任何消息 |
| `duty_permission_enabled` | 指令是否需要管理员权限（默认开） |
| `duty_admin_ids` | 可执行开关的用户 ID 白名单（QQ 号，每行一个） |
| `duty_admin_role` | 群内可执行开关的角色（默认 `owner`/`admin`） |
| `duty_start_on` | 未操作过的群的默认状态（默认上班） |
| `duty_persist` | 持久化开关状态（默认开） |
| `duty_off_reply` | 切换后是否回复确认（默认开） |

## 目录结构

| 文件 | 作用 |
| --- | --- |
| `metadata.yaml` | 插件元数据 |
| `main.py` | 插件主体：`DutySwitchPlugin(star.Star)`，上班/下班指令 + 下班静默拦截 |
| `_conf_schema.json` | 网页端插件配置表单 |
| `requirements.txt` | 第三方依赖（当前无） |
| `README.md` | 本说明 |
| `build.py` | 打包脚本，产出 `dist/siwu-duty-switch-<version>.zip` |

## 打包 / 安装

```bash
python plugins/astrbot/siwu-duty-switch-1_0/build.py
```

产物：`plugins/astrbot/dist/siwu-duty-switch-1.1.0.zip`。

安装方式二选一：

1. AstrBot 管理面板 → 插件管理 → 安装插件 → 上传 zip；
2. 解压 zip 到 `data/plugins/duty_switch/`（含 `main.py`/`metadata.yaml`），重启 AstrBot。

## 验证（按群独立）

1. 在群 A 对兔兔说「@兔兔 下班」→ 收到「群 A」的下班确认；
2. 群 A 里任何人发消息（含 @兔兔）都不再回复、不触发其他插件；**群 B 一切照常**；
3. 在群 A 说「@兔兔 上班」→ 群 A 恢复响应，群 B 状态不受影响。

> 注意：指令需 @ 或唤醒词触发，直接发「下班」不会触发（会被当作普通群消息，
> 且若该群已下班会被静默吞掉）。私聊发送会提示请到群里操作。

## 扩展预留

- **私聊批量管理**：当前按群独立，需在每个群里分别操作。后续可给「上班/下班」加群参数
  （如「上班 群号」）实现私聊批量管理。
- **静默后台推送**：下班拦截只处理「收到群消息」链路；如有主动推送（如动态监听），
  需要时可在推送入口判断 `_STATE.off_groups.get(群ID, _STATE.default_off)`。
- **更多作息指令**（如午休/请假/定时上下班）：在 `_toggle` 基础上加指令与状态位即可。

## Git

独立 git 仓库（master 分支），`.gitignore` 排除 `__pycache__` / `dist/` 等产物；
提交信息用 `v版本号: 改动说明` 风格。
