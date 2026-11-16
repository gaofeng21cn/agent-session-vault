# 架构说明

本文档只负责系统职责、数据流、存储域和不变量，不定义配置字段或操作步骤。

## 所有权

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| OPL Fleet | 已批准节点清单、控制节点身份、路由、准入、任务投放和产物回传 | 投影语义、Tokscale 提交、归档内容 |
| Agent Session Vault | 投影格式和状态、导入的统计历史、受管 Tokscale 环境、stable 恢复副本、Codex 归档生命周期 | Fleet inventory、实时客户端状态、provider 账单真相 |
| Tokscale | 用量计算、官方 preview 和外部提交 | 跨机发现、来源收集、归档生命周期 |
| Codex、Gemini CLI、OpenClaw、Antigravity IDE、ZCode | 各自权威的实时会话目录和 API | 统计投影和 Vault 归档状态 |

每项职责只有一个负责人。Session Vault 只消费 Fleet 和客户端接口，不复制它们的控制状态。

## 应用管理

`opl-package.json` 持有稳定 Package 身份、显示元数据、版本和原生载体声明。Codex
marketplace 解析仓库根目录插件；`.codex-plugin/plugin.json` 与通用 `plugin.json`
描述同一插件。Framework 为源码版安装固定已审阅的 owner commit，并把原生载体回读
投影到统一应用目录。`development_only` 发布状态与软件管理兼容性分开，不表示已有
稳定制品发行。

插件通过 `vault.py` 直接调用同包 CLI owner，不依赖用户 PATH 上的另一版本。
审阅视图继续使用 `app_contribution.py`。安装、更新与卸载由原生插件管理器负责；归档、
裁剪、恢复和提交仍由 Vault 显式操作。卸载软件保留全部外部数据。
运行状态视图只检查 Python、Fleet 和 npx 是否可用，不读取用户配置或执行这些工具。
各功能的依赖状态与节点访问、归档覆盖及提交验收分别展示。

## 运行数据流

```text
控制节点的实时客户端 root
             |
             v
          本机投影 --------+
                           |
已批准 Fleet 节点          |         受管 projection HOME
       |                    |                 +
       v                    v                 |
Fleet 投影任务 -------> 导入投影 + 受管本机 extras
                                           |
                                           v
                                  控制节点的一次 Tokscale 运行
```

`sync fleet` 是唯一跨机路径。Fleet 选择已批准候选节点、执行 fresh admission、投放自包含的
投影任务并回传产物。Session Vault 校验产物后，按稳定 Fleet node identity 导入到 `import_root`。

控制节点本机投影和 Fleet 导入投影都保存在逐机 `.raw/<client>` tree 中。`.raw` 只是存储布局，
不是可选视图；产品只暴露一个受管 Tokscale projection。

## 投影合同

- 支持 Codex、Gemini CLI、OpenClaw、Antigravity IDE 和 ZCode。Gemini 历史在
  Antigravity 更名后仍作为独立 client 保留。
- Antigravity IDE 通过与 submit 相同的当前 Tokscale 包执行官方 `antigravity sync`
  RPC；Vault 只投影生成的 usage cache，IDE language server 不可用时保留旧 cache。
  如果官方 RPC 明确报告 `trajectory not found`，Vault 只处理同一批已确认失效且已陈旧的
  本地 brain、conversation 和索引候选：移动到带 manifest 的可恢复 quarantine，并最多
  重试一次官方同步；近期或无法证明陈旧的会话保持不动。
- OpenClaw 投影支持当前逐 agent SQLite（含 WAL、zstd 压缩事件及数据库内删除/冷归档）、
  旧 JSONL、`.zst`/`.gz` 压缩历史和 agent 自有 Codex rollout。SQLite 通过只读快照读取，
  仅保留模型元数据与用量事件，排除凭据、对话正文和工具输出；统计缓存保留客户端删除的
  历史事件。本机与 Fleet 采集使用同一实现，extra root 的 agent 目录层级满足 Tokscale
  原生 SQLite 发现规则。
- 事件与 fork 去重、Codex rollout 与会话镜像的对齐由原生 Tokscale 负责。
- ZCode 实时 SQLite 数据库通过 SQLite backup 读取；Vault 只把 model、时间、session
  identity 和 token 计数导出为 JSONL，并与旧 `.zcode/projects` JSONL 历史合并。
- Tokscale 用量预览和提交使用 `projection_home` 作为 `HOME`，并移除 `CODEX_HOME`。
  独立的官方 Antigravity RPC cache 刷新使用真实用户 HOME 访问 IDE，并写入专用的
  `TOKSCALE_CONFIG_DIR`；该步骤不执行统计提交。
- `TOKSCALE_EXTRA_DIRS` 包含本机投影、Fleet 导入投影，以及带 `sync-state.json` 的显式本机
  Codex namespace。
- Workspace `.codex` root 和客户端实时 root 不会直接进入 Tokscale。
- 投影导入保留历史；source 删除不会删除已经导入的用量记录。
- Fleet 投影状态支持首次 full 初始化和后续 validated delta；base snapshot 不匹配时拒绝导入，
  不做猜测。
- 只有控制节点执行一次聚合提交；远端节点只构建并回传投影，不执行提交。

投影数据是可从仍存在来源重建的派生数据，不是完整会话备份。

## 存储域

| 域 | 作用 | 日常 Tokscale 输入 | 破坏性权限 |
| --- | --- | --- | --- |
| 实时客户端 root | 客户端权威状态 | 永不 | 仅客户端 owner |
| 投影 imports | 精简、保留历史的统计输入 | 是 | 可重建；自身不能授权删除实时来源 |
| 受管本机 extras | 显式、只增不减的 Codex 导入 | 是 | 仅限 Vault 管理的 namespace |
| Stable analytics | analytics 与控制文件的 packed、verified 恢复副本 | 否 | 只恢复到独立目标 |
| 可选 stable migration profile | 用于换机的显式实时会话副本 | 否 | 需要客户端静止；不合并到 live root |
| 完整 Codex archive | immutable object、snapshot、catalog、verification 和 receipt | 否 | 只有全部证据成立时才能授权 plan-driven prune |

云同步目录或 NAS 可以承载 stable 或 archive 数据，但存储位置不会因此成为客户端实时 root。

## Stable Analytics

stable 层把投影 imports 和受管 extras 打包成有索引的 zstd shard，并复制 Vault 配置和 Tokscale
custom pricing。每次成功 mirror 都生成 verified manifest；restore 会拒绝缺失或未验证的 manifest，
并且只写到独立目标。

该层保护统计连续性。可选 live-session profile 只是显式迁移副本，仍与日常 Tokscale 输入和
可检索 Codex archive 分离。

## 完整归档

archive 只扫描已配置的 Codex source 和允许的 session/index 文件。它用稳定 machine identity
与 source root 标识来源，保存 immutable content-addressed object，发布 snapshot manifest，
并构建用于查询和恢复的 catalog segment。

文件内容身份是“字节长度＋SHA-256”，与来源、会话 ID 和逻辑路径分离。每轮从全库已提交
快照的已校验元数据重建一个内存内容索引；换路径、跨来源和回到旧版本都复用已有 bundle/member，
本轮相同的新文件也只保存一份，不维护另一套可变索引数据库。新 manifest 只列实际引用的包，
历史快照保留自己的引用；本轮全部新对象发布后，才提交任何可能引用它们的来源快照。

这是精确文件字节去重。变化版本和不同编码表示仍分别保留原字节，不是内容块或语义对话去重。
普通归档不重写或删除已有历史，深度对象/成员验证仍是裁剪前提。cycle 回执分别报告新增内容、
新增压缩包和复用逻辑文件字节。

显式存量整理由 `archive compact-plan/compact-apply` 持有。它是一次存储表示迁移：同一 vault
身份、snapshot ID、来源、时间、父子关系、警告、路径、版本和全部原文件字节保持不变，只更新
bundle/member 引用及相应 checksum。候选代际使用同一 archive-v1 格式，按默认 512 MiB
逻辑字节分包（超大单文件独立成包），每份相同文件内容只保存一次。源对象流式读入，每个候选
对象仅做一次完整深检；旧 TAR 内部硬链接按目标内容验证，输出只写明确的普通成员，不含链接、
扩展属性或 AppleDouble。所有历史逻辑摘要、成员集合、SHA-256、逻辑字节和 staging 恢复样本
通过后，同 NAS rename 切换整个代际。原元数据以核验过的 TAR.zst 保留；原容器仅在切换后核对
覆盖并再次通过 NAS 预检后，按精确清单删除。净减量包含新包及元数据备份开销。

本机 archive-cycle 锁和库根外的持久 maintenance fence 防止同时维护。fence 存在时，归档
写入、裁剪和恢复拒绝执行；失败时保留 fence、候选、未删除的原数据和进度证据，只读对账。
该迁移不改变调度，不自动续跑，不以整理存量冒充新增 live 快照。

恢复计划带 digest，并且始终使用 staging mode。本机裁剪是另一份带 digest 的计划；生成和执行
时都必须同时满足：

- 当前 source identity、metadata 和 checksum 一致；
- 引用的 archive snapshot 已通过 deep verification；
- 每个候选文件都有当前 projection coverage；
- verified stable mirror 覆盖 projection imports；
- 不与 live session 或外部 hard link 共享；
- 删除前后的 Tokscale 官方 preview 保持一致。

任何单独的 snapshot、stable mirror、projection 或成功测试都不能授权删除。

## 非目标

- 维护机器 inventory、route 或 artifact transport
- 提供多个可选 Tokscale 视图
- 修改 Tokscale 或客户端上游
- 在当前正式 Tokscale 包提供并经本仓批准来源格式前启用 Antigravity CLI 或 DeepSeek Harness
- 把 provider 账单作为 Vault 计算结果
- 直接恢复到实时 Codex home
- 为已退役流程保留兼容命令
