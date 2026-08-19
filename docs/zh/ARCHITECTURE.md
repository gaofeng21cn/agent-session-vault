# 架构说明

本文档只负责系统职责、数据流、存储域和不变量，不定义配置字段或操作步骤。

## 所有权

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| OPL Fleet | 已批准节点清单、控制节点身份、路由、准入、任务投放和产物回传 | 投影语义、Tokscale 提交、归档内容 |
| Agent Session Vault | 投影格式和状态、导入的统计历史、受管 Tokscale 环境、stable 恢复副本、Codex 归档生命周期 | Fleet inventory、实时客户端状态、provider 账单真相 |
| Tokscale | 用量计算、官方 preview 和外部提交 | 跨机发现、来源收集、归档生命周期 |
| Codex、Gemini CLI、OpenClaw | 各自权威的实时会话目录 | 统计投影和 Vault 归档状态 |

每项职责只有一个负责人。Session Vault 只消费 Fleet 和客户端接口，不复制它们的控制状态。

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

- 支持 Codex、Gemini CLI 和 OpenClaw。
- Tokscale 使用 `projection_home` 作为 `HOME`，永远不会收到真实用户 HOME 或 `CODEX_HOME`。
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
- 把 provider 账单作为 Vault 计算结果
- 直接恢复到实时 Codex home
- 为已退役流程保留兼容命令
