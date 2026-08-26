# hermes-lark-streaming 审计 worklog（跨会话交接）

本文件记录多轮审计 + hermes-agent 兼容性核查的进展。每个会话/子代理追加新章节，勿覆盖。

---
Task ID: 会话2-启动
Agent: 主代理（Z.ai Code）
Task: 用户指出上个会话没拉 hermes agent 最新代码，要求先拉 hermes-agent 最新源码，再结合插件一起审计兼容性

Work Log:
- clone hermes-agent 仓库 → /home/z/hermes-agent，最新 tag = v2026.8.19，内部版本 v0.20.5（pyproject.toml: version = "0.20.5"）
- 插件在 /home/z/hermes-lark-streaming（DEV 分支，v1.6.0，CHANGELOG 显示上次适配 hermes v0.19.0）
- 装依赖：lark-oapi + PyYAML + pytest-asyncio（/home/z/.venv/bin/python -m pip）
- 跑集成测试：`HERMES_SRC_DIR=/home/z/hermes-agent /home/z/.venv/bin/python -m pytest tests/integration/ -v` → 26 passed, 4 skipped
- 读插件 patching/__init__.py + patching/hermes_adapter.py，理解 HermesCompat 跨版本解析 FeishuAdapter 的 3 路径 fallback
- 读 hermes v0.20.5 关键签名：_run_agent（gateway/run.py:27868）、send（adapter.py:1948）、edit_message（adapter.py:2015）、send_clarify（base.py:4268）、_on_card_action_trigger（adapter.py:2715）、_add_reaction（adapter.py:3161）、_remove_reaction（adapter.py:3201）
- 读插件 _wrap_run_agent（gateway.py:226）+ _wrap_run_conversation（gateway.py:482），确认两者都用 **kwargs 透传设计
- 读 docs/CHANGELOG.md 全历史，提取"已知已修"清单（见下）

Stage Summary（关键事实，子代理必读）:

1. **hermes 版本跨度**：插件 v1.6.0 适配 hermes v0.19.0；现 hermes 已到 v0.20.5（tag v2026.8.19）。跨 1 小版本，期间可能有接口新增/改名。
2. **hermes 路径迁移**：v0.20.5 里 `gateway/platforms/feishu/` 目录已**删除**（legacy 路径不存在）；feishu 全迁到 `plugins/platforms/feishu/adapter.py`（248KB）。`gateway/platforms/__init__.py` 用 PEP 562 `__getattr__` 延迟导入，只 re-export BasePlatformAdapter/MessageEvent/SendResult/QQAdapter/YuanbaoAdapter，**不含 feishu**。插件 HermesCompat 有 3 路径 fallback，前 2 个应能兜住（需子代理 Task 2-c 验证 hermes_plugins namespace）。
3. **_run_agent v0.20.5 新增参数**：`persist_user_display_kind: Optional[str]` 和 `message_type: Optional[str]`（gateway/run.py:27883-27884）。插件 _wrap_run_agent 用 **kwargs 透传，**兼容**（前提：hermes 用关键字参数调用，需验证）。
4. **reaction 方法**：v0.20.5 只剩 private 名 `_add_reaction`(adapter.py:3161)/`_remove_reaction`(adapter.py:3201)，public `add_reaction`/`delete_reaction` 已删。集成测试 OPTIONAL 目标因只查 `gateway.platforms.feishu` 路径（已删）误报 skip——**集成测试 bug：optional 目标没加 FeishuAdapter 多路径回退**。
5. **_on_card_action_trigger**：v0.20.5 在 FeishuAdapter 类内定义（adapter.py:2715），不再只是继承自 BasePlatformAdapter。但 v1.5.0 已删除 `_wrap_feishu_card_action_trigger`，改用 `_wrap_handle_card_action_event`（v1.4.2 主链路）。
6. **集成测试弱点**：26 passed 只证明"类/方法名字在 AST 里存在"，**不验签名匹配/参数透传/运行时行为**。真正的兼容性需深度对比源码。
7. **上轮 E2E 硬证据**（/home/z/e2e_spec_result.json）：
   - T7 body.elements 为空 → 230099 失败（ErrCode 200621 parse card json err）
   - T8b button 包在 `action` tag 内 → 230099 失败（ErrCode 200861 "cards of schema V2 no longer support this capability; unsupported tag action"）
   - T1-T6/T8/T9 全部成功：text_size=normal/notation/normal_v2、update_multi、header.template、standard_icon 无 color、button 顶层、streaming double-close、close 后补 summary 均合法
   - 结论：**飞书 V2 schema 废弃 `action` 容器**，button 必须作顶层元素。若插件卡片仍用 `{"tag":"action","actions":[...]}` 包裹 button 会失败。

**已知已修清单**（CHANGELOG 历史，子代理勿重复发现这些已修问题）：
- v1.6.0: hermes v0.19.0 升级后 clarify 卡片失效 → hook `platform_registry.create_adapter` 主链路修复（patching/__init__.py 新增 _wrap_platform_registry_create_adapter + _apply_create_adapter_hook）
- v1.5.0: 架构收敛——删 IM降级/全量重建/header颜色切换/懒重打/deferred 3套重打/_on_card_action_trigger patch（注：删过头，v1.6.0 补回 create_adapter hook）
- v1.4.2: /card unknown command → patch `_handle_card_action_event`（不是 _on_card_action_trigger，因 SDK 持 bound method 引用，类属性替换无效）
- v1.4.1: /card 防御拦截 + Phase 3 300315 context_loading_hint 死循环
- v1.4.0: hermes v0.17.0 deferred loading + delegate_task 卡片降级（_apply_feishu_adapter_patches + _patched_feishu_classes）
- v1.3.7: E2E 测试优化（真飞书消息数减少 85%）
- v1.3.6: /aowen hook 未注册 + concurrency seal 竞态
- v1.3.5: markdown 占位符泄漏 P12P + E2E 竞态超时
- v1.3.4: Task 强引用 + stale 配置 + inspect.signature 防御 + httpx 网络异常 + 99991400 频控 + ABORTED→COMPLETED 非法转换 + UnavailableGuard 死代码 + log_id 排查
- v1.3.3: 占位卡永久卡住（标志位死锁）+ Phase 2 网络异常未捕获
- v1.3.2: 配置 OSError/UnicodeDecode + nan/inf + docstring 矛盾
- v1.3.1: 尾部裁剪 + FeishuAdapter import 路径（3 路径 fallback）
- v1.2.x: header 颜色切换 + Config 单例
- v1.1.x: CardKit 降级保留内容 + 各种早期修复

**子代理任务分配**：
- Task 2-a（GatewayRunner + AIAgent + conversation_loop 侧 wrapper 深度对比）
- Task 2-b（FeishuAdapter 侧 wrapper + platform_registry.create_adapter hook 深度对比）
- Task 2-c（import 路径可达性 + hermes_plugins namespace 验证 + conversation_loop anchor discovery）

---
Task ID: 2-c
Agent: general-purpose（import 路径可达性验证）
Task: 验证插件 HermesCompat 的 3 路径 FeishuAdapter fallback + 3 策略 conversation_loop 解析 + _detect_version + cron scheduler + register() 全部 hermes import 在 hermes v0.20.5 的可达性

Work Log:
- 读 worklog.md 全文，确认 hermes 版本 v0.20.5、feishu 已迁到 plugins/platforms/feishu/、gateway/platforms/feishu/ 已删
- 读插件 4 个核心文件：patching/hermes_adapter.py（194 行 HermesCompat 类）、plugin/__init__.py（226 行 register()）、patching/__init__.py（630 行 apply_patches 主入口）、__main__.py（294 行 CLI 入口）
- 读 hermes-agent pyproject.toml：name="hermes-agent"，version="0.20.5"，requires-python ">=3.11,<3.14"，build-system setuptools==83.0.0；packages.find include 含 ["agent","agent.*","gateway","gateway.*","cron","cron.*","plugins","plugins.*","hermes_cli","hermes_cli.*",...]；py-modules 含 "run_agent"；package-data plugins=["**/plugin.yaml","**/plugin.yml"]
- 读 hermes-agent setup.py：明确禁止 pip wheel/sdist 构建（"pip/PyPI and Homebrew are no longer supported"），通过 shell installer / Docker / Nix 分发；说明 Nix sealed wheel 模式不 ship bundled 资产（含 plugin manifests），由 HERMES_BUNDLED_PLUGINS env var 在 Nix 包装器里指向外部 plugins 目录
- 读 hermes_cli/__init__.py:17 → `__version__ = "0.20.5"` ✓；pyproject.toml `[project].name = "hermes-agent"` ✓
- 读 hermes_cli/plugins.py 的 PluginManager + _parse_manifest + _directory_module_name + _load_directory_module + _register_deferred_platform：
  - `_NS_PARENT = "hermes_plugins"`（运行时 namespace 包，types.ModuleType + __path__=[]）
  - feishu plugin.yaml 的 `name: feishu-platform` → `_directory_module_name` slug=`"feishu-platform".replace("/","__").replace("-","_")` = `"feishu_platform"` → 模块名 `hermes_plugins.feishu_platform` ✓（与插件 Path A 完全一致）
  - bundled 平台 plugin（kind="platform"）走 **deferred 注册**（plugins.py:3996-3998）：`_register_deferred_platform(manifest)` 只在 platform_registry 注册一个轻量 loader，**不 import 真模块** —— 模块仅在 gateway/cron/setup 首次请求该平台时才被 import
  - feishu 的 `register(ctx)` (adapter.py:5875) 调 `ctx.register_platform(name="feishu", adapter_factory=_build_adapter, ...)`，`_build_adapter = lambda cfg: FeishuAdapter(cfg)` —— class 仅在 create_adapter 调用时实例化
- 实证测试 1（cd /home/z/hermes-agent 并把它加入 sys.path）：
  - `from gateway.run import GatewayRunner` ✓
  - `from run_agent import AIAgent` ✓，`AIAgent.run_conversation` 存在 ✓
  - `from agent.conversation_loop import run_conversation` ✓（Strategy 3 直接 import 成功）
  - `import cron.scheduler` → `_deliver_result` 函数存在 ✓（cron/scheduler.py:2652）
  - `import gateway.cron.scheduler` → ModuleNotFoundError ✓（gateway/cron/ 目录不存在，Path 2 失败，Path 1 已兜住）
  - `importlib.metadata.version("hermes-agent")` → PackageNotFoundError（源码模式无 metadata），fallback 到 `hermes_cli.__version__ = "0.20.5"` ✓
  - `import plugins.platforms.feishu.adapter` ✓（plugins.platforms 是 PEP 420 namespace package，无 __init__.py 但运行时可 import）
- 实证测试 2（手动模拟 hermes plugin loader 流程，注册 hermes_plugins namespace 包）：
  - 通过 spec_from_file_location 注册 `hermes_plugins.feishu_platform` 模块，submodule_search_locations 指向 /home/z/hermes-agent/plugins/platforms/feishu
  - `import hermes_plugins.feishu_platform.adapter` ✓，`adapter.FeishuAdapter` 类存在 ✓（class object 与 Path B `plugins.platforms.feishu.adapter.FeishuAdapter` 是不同对象 —— 即 worklog 提到的"真身" vs "替身"问题）
- 实证测试 3（setuptools find_packages 行为对比）：
  - `find_packages(where=".")` 严格模式 → **不**含 `plugins.platforms.*`（因为 plugins/platforms/ 无 __init__.py，被 strict find 跳过）
  - `find_namespace_packages(where=".")` PEP 420 模式 → 含 `plugins.platforms.feishu` 等
  - 但因 setup.py 禁止 wheel 构建，且 shell-installer/Docker 模式都从源码树运行，运行时 `plugins.platforms.feishu.adapter` 实际可达（已实证）
- 读 agent/conversation_loop.py:1762 → `def run_conversation(agent, user_message, system_message=None, conversation_history=None, task_id=None, stream_callback=None, persist_user_message=None, persist_user_timestamp=None, persist_user_display_kind=None, persist_user_display_metadata=None, moa_config=None)` —— v0.20.5 新增 `persist_user_display_kind` + `persist_user_display_metadata` 两参数（与 worklog 事实 3 的 _run_agent 新增参数呼应）
- 读 gateway/platforms/__init__.py → PEP 562 __getattr__ 只 re-export BasePlatformAdapter/MessageEvent/SendResult/QQAdapter/YuanbaoAdapter，无 feishu（已删 feishu 容器）
- 读 gateway/platform_registry.py:create_adapter —— 先查 platform_registry.is_registered → 命中走 plugin factory；feishu 在 elif chain **不存在**（feishu 完全 plugin 化，必须通过 platform_registry.create_adapter 入口）—— 这正是 v1.6.0 hook create_adapter 的合理性根基
- 读 gateway/run.py:15784-15913 `_create_adapter` —— feishu 没在 if/elif 内置链里，确认 feishu 必走 platform_registry

Stage Summary（关键结论）:

**可达性矩阵**:

| # | Import 路径 | v0.20.5 可达性 | 证据 | 插件 fallback 能否兜住 | 风险 |
|---|---|---|---|---|---|
| A | `hermes_plugins.feishu_platform.adapter` | **条件可达** —— 仅在 gateway 首次 create_adapter("feishu",...) 后才在 sys.modules | hermes_cli/plugins.py:3996-3998 bundled platform plugin 走 `_register_deferred_platform`（不 eager-import）；模块路径 slug 推导 `"feishu-platform"→"feishu_platform"` 与插件代码完全匹配（hermes_cli/plugins.py:4967 + 4320-4321） | **不能在 apply_patches 时兜住** —— Path A 在 apply_patches() 时 sys.modules 里没有，会 ImportError → 走 Path B；但 v1.6.0 hook `platform_registry.create_adapter` 会在 gateway 首次创建 FeishuAdapter 时用 _apply_feishu_adapter_patches(cls, is_repatch=True) 给"真身"打补丁，是该问题的"主链路兜底" | **P1 时序耦合风险**（详见下） |
| B | `plugins.platforms.feishu.adapter` | **可达**（shell-installer / Docker / editable 三种安装模式均可达；Nix sealed-wheel 模式不可达但 setup.py 明确禁止 wheel 构建，且 Nix 用 HERMES_BUNDLED_PLUGINS env var 指向外部 plugins 目录，对该路径不影响） | pyproject.toml:445 `packages.find include=["plugins","plugins.*",...]`；plugins/__init__.py 存在；plugins/platforms/ 无 __init__.py 但 PEP 420 namespace package 运行时自动生效；实证 `import plugins.platforms.feishu.adapter` 成功 | **能兜住** apply_patches 时 Path A 失败的场景；返回的是"替身" class A（与 gateway 后续要用的"真身" class B 是不同 class object，需 create_adapter hook 二次 patch） | **P2** —— 仅 Nix sealed-wheel 模式下不可达，但该模式 setup.py 明确禁止 wheel 构建（不发布 PyPI） |
| C | `gateway.platforms.feishu`（legacy） | **不可达** —— 路径已删 | gateway/platforms/__init__.py PEP 562 __getattr__ 只 re-export BasePlatformAdapter/MessageEvent/SendResult/QQAdapter/YuanbaoAdapter（无 FeishuAdapter）；gateway/platforms/feishu/ 目录不存在；实证 `from gateway.platforms.feishu import FeishuAdapter` ModuleNotFoundError | 不需要兜底（Path B 已先于 Path C 命中）；插件 fallback 顺序 A→B→C 设计正确 | **无风险**（路径已删但 fallback 顺序保证不会走到 C） |
| D1 | conversation_loop Strategy 1 sys.modules 缓存 | 仅在 agent.conversation_loop 已被某次 import 进 sys.modules 后可达 | hermes_adapter.py:113 | 若未命中走 Strategy 2/3 | 无 |
| D2 | conversation_loop Strategy 2 anchor-based | **可达** —— `gateway.run.__file__` → `site-packages/gateway/run.py` 或源码树 `gateway/run.py`，repo_root=parent 后再 .parent → site-packages 或源码根，`<root>/agent/conversation_loop.py` 存在（agent 在 packages.find） | 实证：源码模式下 `/home/z/hermes-agent/gateway/run.py` → repo_root=/home/z/hermes-agent → cl_file=/home/z/hermes-agent/agent/conversation_loop.py 存在；pip 安装模式下 site-packages 同样可达（agent 是 packages.find 候选） | 无需 fallback（Strategy 2 命中即返回） | **无风险**（但有个潜在问题：spec_from_file_location 加载的 module __package__ 不会自动设为 "agent"，conversation_loop.py 内部用 `from agent.X import Y` 绝对 import 仍可工作，因 `agent` 已被 sys.modules 缓存） |
| D3 | conversation_loop Strategy 3 `from agent.conversation_loop import run_conversation` | **可达** | 实证 ✓；agent 在 packages.find | 兜底 Strategy 2 失败场景 | 无 |
| E1 | `importlib.metadata.version("hermes-agent")` | **可达**（pip 安装）；源码模式不可达 | pyproject.toml:4 `name="hermes-agent"` | fallback 到 E2 | 无 |
| E2 | `import hermes_cli; hermes_cli.__version__` | **可达** | hermes_cli/__init__.py:17 `__version__="0.20.5"` | 兜底 E1 失败 | 无 |
| F1 | register() → `from ..patching import apply_patches` | 可达（包内相对 import） | plugin/__init__.py:180 | — | 无 |
| F2 | register() → `from ..config import Config` / `from ..controller import get_controller` / `from ..aowen import handle_pre_gateway_dispatch` | 可达（包内相对 import，插件自己的子模块） | plugin/__init__.py:154/189/211 | — | 无 |
| F3 | apply_patches() → `from gateway.platform_registry import platform_registry as _pr` | **可达**（gateway 在 packages.find） | patching/__init__.py:542；pyproject.toml:445 include gateway.* | — | 无 |
| F4 | apply_patches() → HermesCompat() 内部 `from gateway.run import GatewayRunner` / `from run_agent import AIAgent` | **可达** | 实证 ✓；gateway 和 run_agent 都在 packages.find/py-modules | — | 无 |
| F5 | register() → `ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)` | **可达** —— pre_gateway_dispatch 仍在 hermes v0.20.5 的 VALID_HOOKS 集合 | hermes_cli/plugins.py:233 `"pre_gateway_dispatch"` 在 VALID_HOOKS；handle_pre_gateway_dispatch 从插件 aowen 子包相对 import；register_hook 对 unknown hook 仅 warning 不 fail（plugins.py:3120-3127） | — | 无（plugin.yaml 的 provides_hooks 里其他 11 个 on_* hook 均不在 VALID_HOOKS，但它们只是元数据声明，不实际通过 ctx.register_hook 注册——真正注册的只有 pre_gateway_dispatch） |
| G1 | `cron.scheduler._deliver_result` | **可达** | cron/scheduler.py:2652；cron 在 packages.find；实证 ✓ | — | 无 |
| G2 | `gateway.cron.scheduler`（fallback Path 2） | **不可达** | gateway/cron/ 目录不存在；实证 ModuleNotFoundError | **不需要兜底**（Path G1 已先命中）；插件 fallback 顺序 `cron.scheduler` → `gateway.cron.scheduler` 设计正确 | 无 |

**确认的路径失效问题**:

**P1 — Path A 时序耦合（已知问题，已由 v1.6.0 create_adapter hook 主链路修复）**
- 问题：`hermes_plugins.feishu_platform.adapter` 在 apply_patches() 时不在 sys.modules（hermes v0.17+ 对 bundled 平台走 deferred 注册，plugins.py:3996-3998）。HermesCompat._resolve_feishu_adapter() Path A 调 `importlib.import_module("hermes_plugins.feishu_platform.adapter")` → ImportError → fallback Path B（替身 class A）。
- 证据：hermes_cli/plugins.py:3996-3998 `_register_deferred_platform(manifest)`；plugins.py:4494-4505 deferred loader 内部调 `_load_plugin_scoped(_manifest)` 才真正 import；plugins.py:4453-4467 `_platform_name_from_manifest` 对 manifest.name="feishu-platform" 剥 "-platform" 得 platform_name="feishu"；patching/__init__.py:503-567 `_apply_create_adapter_hook` 安装 platform_registry.create_adapter 包装器；patching/__init__.py:465-497 `_wrap_platform_registry_create_adapter` 内部 `_apply_feishu_adapter_patches(cls, is_repatch=True)` 给"真身" class B 补打补丁
- 用户影响（白话）：插件装好开机那一刻，飞书 adapter 类的"真身"还没被 hermes 装载，所以插件先去打补丁的对象其实是另一个"替身"对象（同源代码但不同 class）。等用户实际用飞书发消息时，hermes 才装载"真身"——此时若没有 v1.6.0 的 create_adapter 钩子，"真身"会裸奔（clarify 卡片 / 委派卡片 / reaction 等会退回纯文本）。**v1.6.0 已修**，但若用户禁用了 create_adapter hook（理论上不会但需验证）或 hermes 改了 platform_registry.create_adapter 的签名/位置，会回退到裸奔。
- 建议方向：保留 v1.6.0 hook；额外加一层防御——在 `_wrap_feishu_adapter_send` 入口处加 `if not _verify_feishu_patch_identity(self): _apply_feishu_adapter_patches(type(self), is_repatch=True)` 兜底（v1.5.0 的 on-demand 思路，但因 send 仅被已 patch class 触发，仍存在 chicken-and-egg，所以 create_adapter hook 是更可靠的主链路）

**P2 — Path B 在 Nix sealed-wheel 模式下不可达（理论问题，不实际发生）**
- 问题：`plugins.platforms.feishu.adapter` 在 Nix sealed-wheel 安装模式下不可达（setuptools.find_packages 严格模式不发现无 __init__.py 的 plugins/platforms/ 目录）。
- 证据：`find_packages(where=".")` 输出**不**含 `plugins.platforms.feishu`；`find_namespace_packages` 才含。但 hermes-agent setup.py:1-25 明确禁止 wheel 构建（`_GuardedBdistWheel.run()` 抛 RuntimeError），Nix 模式由 HERMES_BUNDLED_PLUGINS env var 指向外部源码 plugins 目录
- 用户影响：**无实际影响**——所有官方分发渠道（shell installer、Docker、Nix）都让 hermes 从源码树或外部 plugins 目录运行，plugins.platforms.feishu.adapter 实际可达。仅当用户尝试 `pip install hermes-agent`（PyPI 不存在该包）或自建 wheel 绕过 setup.py 守卫时才会触发，非现实场景
- 建议方向：无需修改；可选地在 hermes_adapter.py Path A 前再加一条 `try: from plugins.platforms.feishu.adapter import FeishuAdapter as _F; ...` 提前 return（已存在 Path B，路径顺序合理）

**P3 — Path C 失效无需修复（fallback 顺序保证不走到）**
- 问题：`gateway.platforms.feishu`（legacy）在 v0.20.5 已删
- 证据：gateway/platforms/__init__.py PEP 562 __getattr__ 只 re-export Base/QQ/Yuanbao；gateway/platforms/feishu/ 目录不存在
- 用户影响：**无**——插件 fallback 顺序 A→B→C，A 失败走 B 命中即返回，不会走到 C
- 建议方向：可在 Path B 命中后 log 一条 `legacy gateway.platforms.feishu path removed in hermes v0.17+` 调试信息便于 doctor 诊断（非必须）

**存疑待验证项**:

1. **需 Nix 实际环境验证**：本审计在源码树模式下实证（HERMES_SRC_DIR=/home/z/hermes-agent），未在 Nix sealed-venv 下跑过。理论上 HERMES_BUNDLED_PLUGINS env var 在 Nix 模式会让 hermes 加载外部 plugins 目录的 feishu plugin，但若 Nix 模式下 hermes_cli/plugins.py 自身的 `__file__` 路径推断与外部 plugins 目录不在同一棵树，`_load_directory_module` 的 `submodule_search_locations=[str(plugin_dir)]` 仍应能工作（plugin_dir 是绝对路径），但未实证

2. **create_adapter hook 防御验证**：v1.6.0 的 `_wrap_platform_registry_create_adapter` 假设 `platform_registry.create_adapter(name, config)` 的 2 参数签名不变。需 Task 2-b 验证 v0.20.5 是否仍保持 2 参数签名（platform_registry.py:618 `def create_adapter(self, name: str, config: Any)` —— 当前是 2 参数 ✓，但 hermes 是否会改成 3 参数如 `**kwargs` 需持续监控）

3. **integration test 测试盲区**：tests/integration/test_hermes_compat.py 的 `_FEISHU_ADAPTER_MODULE_CANDIDATES` 只覆盖 Path C（legacy，已删）+ Path B（源码路径），**未覆盖 Path A**（hermes_plugins.feishu_platform.adapter）。OPTIONAL_CLASS_METHOD_TARGETS（add_reaction/_add_reaction/delete_reaction/_remove_reaction）硬编码 `gateway.platforms.feishu` 路径，在 v0.20.5 全部误报 skip（实际它们都在 plugins.platforms.feishu.adapter 里以 `_add_reaction`/`_remove_reaction` 私有名存在，worklog 事实 4 已记录）。**集成测试需补 Path B 作为 OPTIONAL 目标的 fallback** —— 但这是测试代码问题，不是插件代码问题

4. **conversation_loop Strategy 2 边界情况**：`spec_from_file_location("agent.conversation_loop", str(cl_file))` 加载的 module 的 `__package__` 不会被 spec 自动设为 "agent"。conversation_loop.py 内部用绝对 import `from agent.codex_responses_adapter import ...`，因 `agent` 包已被 sys.modules 缓存（gateway.run 等模块加载时会顺带加载 agent.*），所以绝对 import 能成功。但若 hermes 启动顺序变化，agent 包尚未被加载时 Strategy 2 触发，`from agent.X import Y` 会 ImportError。**低风险**，因 apply_patches() 必然在 gateway 模块已加载后调用

5. **hermes_cli.__version__ 命名稳定性**：v0.20.5 在 hermes_cli/__init__.py:17 用 `__version__` 属性。若 hermes 改成在 build_info.py 或单独模块定义 `__version__`，E2 fallback 失效。建议加第 3 路径 `from hermes_cli.build_info import __version__`（如存在）—— 但当前 build_info.py 存在性未验证

6. **【回答 Task 2-a 的存疑项：模块级 run_conversation patch 的 module identity】**：
   - `gateway/run.py:61` 在模块级（非函数体内）做 `from agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX` —— gateway 启动加载 gateway.run 时就把 `agent.conversation_loop` 装载进 sys.modules
   - 插件 `register()` → `apply_patches()` → `HermesCompat._resolve_conversation_loop()` 的 Strategy 1（sys.modules 缓存）会**命中**该模块对象，且插件在同一个 module object 上替换 `mod.run_conversation = _wrap_run_conversation(orig)` —— 不会产生"替身 vs 真身"分裂
   - hermes 的 `AIAgent.run_conversation` 转发器（run_agent.py:8510）在**方法体内**做 `from agent.conversation_loop import run_conversation` —— 每次 AIAgent.run_conversation 被调用时从 sys.modules 查找并 getattr，因此会拾取插件 wrapper 替换后的函数 ✓
   - 三种策略（sys.modules 缓存 / anchor-based spec_from_file_location / 标准 import）在模块 identity 上都保持一致：Strategy 2 只有在 sys.modules 缓存 miss 时才会走（此时它写入 sys.modules 的模块对象就是 hermes 后续要用的），Strategy 3 用标准 import 机制更是同一对象
   - **结论：Task 2-a 的"模块级 run_conversation patch 是否被 hermes 转发器拾取"答案是 YES**，hermes 转发器的函数体内 lazy import 在调用时从 sys.modules 解析，插件 wrapper 会被正确拾取

---
Task ID: 2-b
Agent: general-purpose（FeishuAdapter 兼容性对比）
Task: 深度对比插件 7 个 FeishuAdapter wrapper + platform_registry.create_adapter hook 在 hermes v0.20.5 下的签名/调用兼容性，重点核查 create_adapter hook、send/edit_message、send_clarify、_on_card_action_trigger、_add/_remove_reaction、clarify 卡片 button 容器。

Work Log:
- 读 worklog.md 全文，吸收 7 条关键事实（hermes v0.20.5 路径迁移、_run_agent 新增参数、reaction 私有名、_on_card_action_trigger 在 FeishuAdapter 类内、上轮 E2E V2 schema 证据）
- 读插件 patching/adapter.py 全 969 行：提取 7 个 wrapper 签名 + orig 调用方式
- 读插件 patching/__init__.py 全 630 行：理解 _apply_feishu_adapter_patches 注册顺序（add_reaction→_add_reaction fallback / delete_reaction→_remove_reaction fallback）、_wrap_platform_registry_create_adapter hook 实现、_apply_create_adapter_hook 安装逻辑
- 读插件 cardkit/special.py 全 362 行：build_clarify_card（state 1）、build_clarify_submitted_card（state 2）、build_clarify_confirmed_card（state 3）的 V2 schema 结构
- 读 hermes v0.20.5 adapter.py:1948 send 签名 `(self, chat_id, content, reply_to=None, metadata=None) -> SendResult`
- 读 hermes v0.20.5 adapter.py:2015 edit_message 签名 `(self, chat_id, message_id, content, *, finalize=False) -> SendResult` — 无 metadata 参数，有 finalize kwarg-only
- 读 hermes v0.20.5 adapter.py:2715 _on_card_action_trigger 方法体（line 2715-2750）：确认仍调用 `self._submit_on_loop(loop, self._handle_card_action_event(data))`（line 2747）→ 插件 v1.4.2 设计（wrap _handle_card_action_event）仍有效
- 读 hermes v0.20.5 adapter.py:3062 _handle_card_action_event 方法体：构建 `/card {action_tag}` 合成 COMMAND，经 `_handle_message_with_guards` 派发
- 读 hermes v0.20.5 adapter.py:3161/3201 _add_reaction/_remove_reaction 签名：`(message_id, emoji_type)` / `(message_id, reaction_id)` — 无 public add/delete_reaction
- 读 hermes v0.20.5 adapter.py:278 _FEISHU_REACTION_IN_PROGRESS="Typing" / _FEISHU_REACTION_FAILURE="CrossMark" — Feishu-internal emoji codes（非 unicode emoji）
- 读 hermes v0.20.5 base.py:4268 BasePlatformAdapter.send_clarify 签名 `(self, chat_id, question, choices, clarify_id, session_key, metadata=None) -> SendResult` — FeishuAdapter 不 override（grep adapter.py 无 `def send_clarify`）
- grep hermes v0.20.5 FeishuAdapter 全仓：无 `def send_clarify` → FeishuAdapter 继承 base.send_clarify，插件 patch FeishuAdapter.send_clarify 阴影继承方法 ✓
- 读 hermes v0.20.5 platform_registry.py:618 `def create_adapter(self, name: str, config: Any) -> Optional[Any]`，singleton `platform_registry = PlatformRegistry()` 在 line 698
- grep hermes v0.20.5 create_adapter 调用点：run.py:15808 `platform_registry.create_adapter(platform.value, config)` 位置参数 ✓ 与插件 wrapper 签名 `_wrapped(name, config)` 匹配
- grep hermes v0.20.5 send/send_clarify/edit_message 调用点：stream_consumer.py:1459/1675 用关键字 `chat_id=/content=/reply_to=/metadata=`；run.py:6003 send_clarify 全关键字；run.py:4660 edit_message 带 metadata= 但仅 Slack native_failed 路径触发，Feishu 不会进入
- 读 stream_consumer.py:441-451：hermes 用 `inspect.signature(adapter.edit_message).parameters` 检查是否含 metadata/VAR_KEYWORD，决定是否传 metadata= → 插件 wrapper 有 metadata 参数，hermes 会传，wrapper 捕获后丢弃，调用 orig 时不传 metadata ✓
- grep hermes v0.20.5 `/card` 命令注册：无匹配 → 插件 _wrap_handle_card_action_event 抑制 /card 合成命令安全
- grep hermes v0.20.5 interactive button markers：仅 `hermes_action`（approval, line 2082）+ `hermes_update_prompt_action`（update prompt, line 2140）+ 插件 `hermes_clarify_action`。前两者在 _on_card_action_trigger 内（line 2738-2745）已被过滤，不到达插件 wrapper
- 读插件 CHANGELOG v1.4.2 条目确认：`_handle_clarify_card_action` 返回的 CallBackCard **故意丢弃**（"丢弃返回的 CallBackCard（async 路径无 sync 响应），suppress /card"）
- 综合 7 wrapper 对比表 + 兼容性分级

Stage Summary:
- **A. platform_registry.create_adapter hook**：✅ 完全兼容。hermes v0.20.5 `gateway/platform_registry.py:618` `def create_adapter(self, name, config) -> Optional[Any]` 仍是 public API，singleton `platform_registry` 在 line 698。run.py:15808 位置参数调用。插件 `_wrap_platform_registry_create_adapter._wrapped(name, config)` 不带 self（orig 是 bound method），调用 `orig_create_adapter(name, config)` 正确。hook 后返回的 adapter 实例：插件对 `type(adapter)` 跑 `_apply_feishu_adapter_patches(cls, is_repatch=True)`，再返回原 adapter。hermes 后续 `adapter.gateway_runner = self` 仍作用于已 patch 的实例 ✓。v0.17.0→v0.19.0 零 commits（CHANGELOG），v0.19.0→v0.20.5 浅克隆只有 1 commit 无法 git diff 验证，但当前签名稳定。
- **B. send/edit_message**：✅ 兼容（一处 P2 风险）。hermes send `(self, chat_id, content, reply_to=None, metadata=None)`，插件 wrapper 同签名 + **kwargs。hermes edit_message `(self, chat_id, message_id, content, *, finalize=False)` **无 metadata**，插件 wrapper 有 metadata=None（捕获 hermes stream_consumer 经 inspect.signature 检查后传来的 metadata=，调用 orig 时丢弃），finalize=True 经 **kwargs 透传给 orig ✓。**P2 风险**：插件 _intercepted_edit 的 TypeError fallback（line 293-295）`orig_edit(self_feishu, chat_id, message_id, content)` 丢弃 finalize=True。但 hermes edit_message 内部失败返回 SendResult(success=False)，不抛 TypeError，所以 except TypeError 实际不触发，纯防御代码。
- **C. send_clarify**：✅ 完全兼容。FeishuAdapter **不** override send_clarify（grep adapter.py 无 `def send_clarify`），继承 base.py:4268 的 BasePlatformAdapter.send_clarify。插件 patch `FeishuAdapter.send_clarify = _wrap_feishu_adapter_send_clarify(FeishuAdapter.send_clarify)` 阴影继承方法（Python 子类属性赋值覆盖继承）✓。wrapper 签名 `(self_feishu, chat_id, question, choices, clarify_id, session_key, metadata=None, **kwargs)` 与 hermes 完全一致。hermes 调用点 run.py:6003 全关键字 ✓。
- **D. _on_card_action_trigger**：✅ 兼容（v1.4.2 设计仍有效）。hermes v0.20.5 adapter.py:2715-2750 `_on_card_action_trigger` 仍调用 `self._submit_on_loop(loop, self._handle_card_action_event(data))`（line 2747，**动态查找**）。插件 wrap 的是 `_handle_card_action_event`（不是 `_on_card_action_trigger`），即使 SDK 持有 `_on_card_action_trigger` 的 stale bound method，方法体内 `self._handle_card_action_event` 仍动态查找当前类属性上的 wrapper ✓。**前置过滤**：hermes 先过滤 `hermes_action`（approval, line 2738）+ `hermes_update_prompt_action`（line 2740），不到达插件 wrapper。插件 wrapper 只看到 `hermes_clarify_action` + 无 marker 的 action，全部 suppress。
- **E. _add_reaction / _remove_reaction**：✅ 兼容（P3 dead feature）。hermes v0.20.5 只剩 private 名 `_add_reaction(message_id, emoji_type)` / `_remove_reaction(message_id, reaction_id)`，无 public。插件 patching/__init__.py:401-413 先尝试 public `add_reaction`/`delete_reaction`（v0.20.5 AttributeError），fallback 到 `_add_reaction`/`_remove_reaction` ✓。hermes 调用全位置参数（adapter.py:3247/3262/3270）。wrapper 签名 `(self_feishu, message_id, emoji, **kwargs)` 位置匹配。**P3 dead feature**：插件 `_REACTION_STATUS_MAP` 键是 unicode emoji（"👀"/"👍"/...），hermes 传 Feishu-internal code（"Typing"/"CrossMark"，adapter.py:278-279），map 永远 lookup 失败 → card-status indicator 路径死代码。reactions 仍正常 pass through 到 orig（无破坏）。
- **F. clarify 卡片 button 容器**：✅ 不破坏运行时（P3 latent bug）。cardkit/special.py:276-294 `build_clarify_submitted_card`（state 2）用 `{"tag":"action","actions":[{"tag":"button",...}]}` 包裹 retry button — V2 schema 废弃 action 容器（worklog 第7条 T8b 铁证 ErrCode 200861）。**但该卡片运行时从不发送**：`_handle_clarify_card_action` 返回的 P2CardActionTriggerResponse 被 `_wrap_handle_card_action_event` 丢弃（CHANGELOG v1.4.2 明文 "丢弃返回的 CallBackCard（async 路径无 sync 响应）"）。运行时只发 state 1（pending，V2 OK）+ state 3（confirmed，V2 OK，无 button）。**用户影响**：soft-lock state 2 UX 被跳过，用户看到 state 1 → ~1s gap → state 3，无 visible submitted 反馈，retry button 永不显示。**Latent bug**：若未来 SDK 支持 async response 或 wrapper 被重构为返回 response，230099 立即触发。

**风险分级**：
- P0：无（V2-invalid action 容器是 dead code，不发送）
- P1：无
- P2：① _wrap_handle_card_action_event blanket-suppress 非 clarify action（v0.20.5 安全，未来 hermes 加新 card button 类型会破坏）；② _wrap_feishu_adapter_edit TypeError fallback 丢 finalize=True（实际不触发）
- P3：① build_clarify_submitted_card V2-invalid action 容器（dead code，latent bug）；② reaction → card-status 指示器功能死代码（emoji code 不匹配）；③ wrapper 用 **kwargs 调 orig，若 hermes 未来加新参数 + 用关键字调用会 TypeError（v0.20.5 无新参数）

**待验证项**（静态分析无法确认）：
- v0.19.0→v0.20.5 期间 platform_registry.py 的 commits（浅克隆只有 1 commit 无法 git log）
- standard_icon `color` 字段在 V2 schema 下的合法性（E2E 只测过 "无 color" 变体；生产 clarify state 1 用 color:"blue" 工作正常 → 推测 color 是 V2-OK，state 2/3 用 color:"orange"/"green"，state 2 dead code 不发，state 3 工作 → color 应 OK）

---
Task ID: 2-a
Agent: general-purpose（GatewayRunner/AIAgent/conversation_loop 兼容性对比）
Task: 深度对比插件 patching/gateway.py 的 6 个 wrapper + patching/callbacks.py 的 _maybe_wrap_callbacks + patching/hooks.py 的 10 个 hook 与 hermes v0.20.5 的签名/调用方式/属性可达性兼容性

Work Log:
- 读 worklog.md 全文，确认前置 7 条关键事实（重点：v0.20.5 _run_agent 新增 persist_user_display_kind/message_type）
- 读 patching/gateway.py（736 行）的 6 个 wrapper：_wrap_handle_message(21-67)/_wrap_handle_message_with_agent(69-224)/_wrap_run_agent(226-480)/_wrap_run_conversation(482-526)/_wrap_run_background_task(530-652)/_wrap_cron_deliver(656-736)
- 读 patching/callbacks.py（233 行）的 _maybe_wrap_callbacks：5 个 callback 属性替换（stream_delta_callback / interim_assistant_callback / tool_progress_callback / reasoning_callback / background_review_callback）
- 读 patching/hooks.py（219 行）的 10 个 hook：on_feishu_normalize/on_message_started/on_message_completed/on_tool_updated/on_answer_delta/on_thinking_delta/on_reasoning_delta/on_background_review_message/on_message_aborted/on_message_interrupted/on_cron_deliver
- Grep hermes v0.20.5：gateway/run.py 中 _run_agent@27868 / _handle_message@16461 / _handle_message_with_agent@18798 / _run_background_task@22370 / _run_agent_inner@28044 签名
- Grep self._run_agent( 在 gateway/run.py 的所有调用点：20160（_handle_message_with_agent 内）、29589（_run_agent_inner 内的递归 followup_result），**两处全部用关键字参数**——这是 _wrap_run_agent 兼容性的关键证据
- 读 agent/conversation_loop.py:1762 的 run_conversation 模块级签名：含 v0.20.5 新增 persist_user_display_kind / persist_user_display_metadata / moa_config 共 3 个新参数
- 读 run_agent.py:8482 的 AIAgent.run_conversation 转发器签名（与模块级一致）+ 8856 的实际调用（7 位置 + 4 关键字）
- Grep run_agent.py + agent_init.py 确认 5 个 callback 属性仍在 AIAgent.__init__ 实例上赋值（agent_init.py:838/843/852/853/668）+ gateway/run.py:5924 在线时设置 background_review_callback
- Grep cron/scheduler.py:2652 的 _deliver_result 签名 + 两个调用点（6709/6848，均用 2 位置 + 2 关键字）
- 读 gateway/delivery.py:60-89 的 DeliveryTransport.send：v0.20.5 cron 推送经过 DeliveryRouter → DeliveryTransport → adapter.send(chat_id, content, metadata=metadata)，而非直接调 adapter.send
- Grep + 确认 on_cron_deliver hook（hooks.py:203-219）在插件运行时从未被调用——_wrap_cron_deliver 直接调 ctrl._do_cron_deliver（controller/mixin.py:58），hook 是 dead code

Stage Summary:
- **总体结论：插件 6 个 wrapper + 5 个 callback 包装在 hermes v0.20.5 下兼容性 100% 通过签名/调用方式比对**（前提：Task 2-c 验证 import 路径可达性）
- **_wrap_run_agent 兼容性 ✅**：插件 wrapper 显式参数到 channel_prompt，moa_config/persist_user_message/persist_user_timestamp/persist_user_display_kind/message_type 走 **kwargs 透传；hermes 两处 self._run_agent 调用点（run.py:20160、29589）全部用关键字参数，wrapper 显式参数列表足以兜住
- **_wrap_run_conversation 兼容性 ✅（P2 风险）**：模块级 + AIAgent 方法级双重 patch 都生效；hermes 转发器（run_agent.py:8856）用 7 位置 + 4 关键字调用模块级，wrapper 显式参数到 persist_user_timestamp 恰好覆盖 7 位置 + persist_user_timestamp，剩余 persist_user_display_kind/metadata/moa_config 走 **kwargs 透传给 orig(self, user_message, **call_kwargs)。风险：v0.20.5 新增的 3 个参数没像 persist_user_timestamp 那样做 inspect.signature 探测，若 hermes 未来移除任意一个会导致 TypeError（向后兼容性脆弱）
- **_wrap_handle_message / _wrap_handle_message_with_agent / _wrap_run_background_task / _wrap_cron_deliver 兼容性 ✅**：wrapper 签名（self, event/positional, *args, **kwargs）或（job, content, adapters, loop, **kwargs）与 hermes v0.20.5 完全对齐；所有 hermes 内部调用点用位置 + 关键字混合，wrapper 都能正确捕获
- **5 个 callback 属性包装 ✅**：agent_init.py 在 AIAgent.__init__ 实例上设置 stream_delta_callback/interim_assistant_callback/tool_progress_callback/reasoning_callback/background_review_callback；插件用实例属性替换（agent.stream_delta_callback = _answer_wrapper）会被 hermes 的 self.stream_delta_callback 读取路径正确拾取
- **P1 风险：cron 推送的 RELAY-fronted 场景**：v0.20.5 的 DeliveryTransport.send 在 relay 模式下走 self.adapter.send_for_platform(...)（delivery.py:83），绕过 feishu_adapter.send，插件 _wrap_cron_deliver 的 feishu_adapter.send 替换不会触发——RELAY 前置的飞书部署下 cron 卡片重定向失效（白话：cron 任务回复会变成纯文本而非卡片）。需要 E2E 验证是否影响主用例
- **P2 风险：on_cron_deliver hook 是 dead code**：hooks.py:203-219 定义 + plugin.yaml:18 注册，但 _wrap_cron_deliver（gateway.py:656-736）从未调用它，而是直接 ctrl._do_cron_deliver。用户影响：无运行时影响，但维护成本/未来重构隐患
- **P2 风险：_maybe_wrap_callbacks 在 feishu 上下文里覆盖 cli 的 stream_delta_callback=None**：cli.py:21334 主动 `cli.agent.stream_delta_callback = None` 关流，但若同一进程同时跑 feishu 网关（_msg_ctx 有 eid），_maybe_wrap_callbacks 走 callbacks.py:85-106 的 synthetic 分支，会重新挂上 _answer_wrapper_synthetic。当前 cli 与 feishu gateway 不会同进程运行，低概率但需留意
- **未发现的兼容性问题**：无 P0 级问题。所有 wrapper 签名匹配、所有调用点用关键字参数、所有 callback 属性未改名、_run_agent 新增参数通过 **kwargs 兜住
- **存疑待验证**：
  - 实际 gateway 运行时 `_msg_ctx` 是否在 _wrap_run_agent 调用时已 set（依赖 _wrap_handle_message_with_agent 先于 _wrap_run_agent 执行——需 E2E 跑一条真实飞书消息验证 ctx 链路）
  - _wrap_cron_deliver 在 RELAY-fronted 飞书部署下是否真的失效（需有 RELAY 环境的 E2E）
  - 模块级 run_conversation patch 是否被 hermes 转发器 import 拾取（依赖 sys.modules["agent.conversation_loop"] 单一性——交 Task 2-c 验证 anchor-based discovery 后的 module identity）


---
Task ID: 会话2-汇总
Agent: 主代理（Z.ai Code）
Task: 汇总 3 个子代理结果 + 补充验证空 elements bug 链路 + 形成 final 报告

Work Log:
- 确认 3 个子代理（2-a/2-b/2-c）均已追加 worklog，结论齐全
- 确认本地无生产日志（/home/z/.hermes/ 为沙箱预置，logs/curator 空；生产日志在用户服务器）
- grep 插件全码确认 V2 废弃 action 容器仅 special.py:277 一处（clarify state 2，2-b 已确认 dead code）
- 追踪 build_cron_card 空 content 完整 bug 链路：gateway.py:701-706 → mixin.py:58-63 → special.py:90-93
- 追踪 build_gateway_card 空 content：special.py:105-126 + test_gateway_card.py:60-65 测试断言了非法行为
- 读 gateway.py:688-736 确认 _card_sending_send 有 fallback 但 except Exception: pass 无日志

Stage Summary:
- **兼容性总结论：插件 v1.6.0 与 hermes v0.20.5（tag v2026.8.19）无 P0 级兼容问题**。15 个 wrapper 全兼容、import 路径全可达、v0.20.5 新增 _run_agent 参数（persist_user_display_kind/message_type/persist_user_display_metadata/moa_config）经 **kwargs 透传兼容（hermes 调用点全用关键字参数）。
- **P1-RELAY**：hermes v0.20.5 DeliveryTransport.send 在 relay 模式调 send_for_platform 不走 adapter.send → RELAY 部署下 cron 推送卡片失效退纯文本（需确认用户是否用 RELAY，无人用可降 P2）
- **P2-空卡**：build_cron_card/build_gateway_card 空 content 产生非法空 elements 卡（E2E T7 铁证 230099）；cron 全空白内容触发一次必败 API + 静默降级纯文本（gateway.py:720 except pass 无日志）；test_gateway_card.py:60-65 断言了非法行为（测试与飞书实际脱节）
- **P2-其余**：clarify state2 action 容器（dead code 但 UX 缺 submitted 反馈）/ inspect 探测不全 / blanket-suppress / TypeError fallback 丢 finalize / 集成测试 OPTIONAL 误报 skip（硬编码已删路径，实际 _add_reaction 存在）
- **P3**：on_cron_deliver dead hook / reaction status map 死代码 / synthetic 覆盖 cli 关流（不触发）
- 存疑待 E2E：standard_icon color 显式验证（生产推测 OK）/ RELAY 实际使用 / _msg_ctx 中断时序 / reasoning_callback 双重 wrap
