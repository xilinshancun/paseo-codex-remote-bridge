# 已有 Paseo 安装可以复用哪些改动

多数场景只需[选一个桥接方式](../README.md)。下面按实际遇到的问题列出可选的宿主补丁、插件和已有上游工作。自定义 provider 决定启动哪个程序，Paseo daemon 管理会话，Paseo 客户端负责聊天界面。这三个位置决定了改动要装在哪里。

## 远端 Codex 能读 Mac 文件，但调用不了 Paseo 工具

Codex 执行项目命令和调用 Paseo MCP 是两条路径。前者由 `environments.toml` 中的 Mac exec-server 承接。后者还需要 daemon 为 `paseo` 这个 MCP 服务选择可访问 Mac 的执行环境；每次会话的地址和认证头由 daemon 提供。

在支持具名 MCP 环境的 daemon 上，新建 Codex 实例时给[原安装命令](setup.md#2-生成本机实例)加 `--route-paseo-mcp`。它只在生成的 provider 片段中增加以下选项。已有实例可以把同一字段合入其 Codex provider 的现有 `options`，然后运行 `paseo reload`。

```json
{
  "mcp_servers": {
    "paseo": { "environment_id": "mac" }
  }
}
```

在新会话中让 Codex 通过 Paseo MCP 列出当前可见的 agents。等这个测试会话空闲后，用同一 daemon 的 CLI 执行 `paseo agent reload <测试会话ID>`，再回到原聊天重复一次。两次都应得到 daemon 的结果。只测 `pwd` 或模型列表无法确认 MCP 路由。如果 daemon 拒绝 `mcp_servers` 字段，移除此选项可继续使用常规的 Codex 文件和图片桥。这时可按下一节评估宿主补丁。

## 为 Paseo v0.15.1 构建可选补丁

这四份宿主补丁从本地 Paseo 开发工作中提取，可以分别应用在干净的 **v0.15.1** 源码上，基线为 `c4dc36272cda1bb07a8c11b7beee41f247ee7952`。其它版本应先检查是否已有同等功能，再移植所需改动。补丁不会随 bridge 安装器自动应用。[Paseo 基线](https://github.com/getpaseo/paseo/tree/c4dc36272cda1bb07a8c11b7beee41f247ee7952) · [源文件与许可证](../integrations/paseo-0.15.1/)

| 何时用 | 选择的补丁 | 在 Paseo 源码中的回归测试 |
| --- | --- | --- |
| 需要把远端 Codex 的 Paseo MCP 请求路由到 Mac | `codex-mcp-routing.patch`，保留 daemon 发给该会话的 MCP 地址、认证头和工具授权 | `packages/server/src/server/agent/providers/codex-mcp-routing.test.ts` |
| Codex 子进程在 daemon 写入请求期间关闭了标准输入 | `codex-stdin-epipe.patch`，结束失去管道的会话并通知请求方 | `packages/server/src/server/agent/providers/codex/app-server-transport.test.ts` |
| 打开已有 Antigravity 本机会话时需要读取磁盘上的历史 | `antigravity-local-history.patch`，读取该用户已保存的 transcript。较新代码也可参考[上游 Antigravity 历史支持](https://github.com/getpaseo/paseo/pull/10193) | `plugins/antigravity-provider/server/provider.test.ts` |
| 在现有聊天的模型菜单里选择账号，忙时排队、空闲时才重载 | `model-account-menu.patch`，提供模型菜单插件接口和 daemon 的空闲重载 RPC。还需下文的账号池插件提供实际账号 | `packages/server/src/server/session.test.ts`、`packages/app/src/plugins/evaluate.test.ts`、`packages/app/e2e/browser/plugin-buttons.spec.ts` |

例如，先在此 bridge 仓库根目录记下补丁路径，然后创建独立的 Paseo 源码目录。下面只选择 MCP 路由。使用其它补丁时替换文件名和表中测试路径。

```sh
PATCH_DIR="$PWD/integrations/paseo-0.15.1"
git clone --branch v0.15.1 --depth 1 https://github.com/getpaseo/paseo.git paseo-0.15.1
cd paseo-0.15.1
git apply --check "$PATCH_DIR/codex-mcp-routing.patch"
git apply "$PATCH_DIR/codex-mcp-routing.patch"
npm ci
npm run build:server
npx vitest run packages/server/src/server/agent/providers/codex-mcp-routing.test.ts --bail=1
```

在刚才的 Paseo 源码目录运行 `npm run dev`。该版本默认使用 `127.0.0.1:6768` 和当前 checkout 的 `.dev/paseo-home`。将 bridge 生成的 provider 项合入 **该目录**的 `config.json`。在同一 checkout 的另一终端运行 `npm run cli -- reload` 和 `npm run cli -- provider models codex-remote --json`，再用 `npm run dev:app` 打开连接这个开发 daemon 的客户端。按前一节测试 MCP，重载测试会话时也使用仓库 CLI，即 `npm run cli -- agent reload <测试会话ID>`。已安装的桌面版使用自己的 daemon，与这个开发实例分开。[该版本的开发说明](https://github.com/getpaseo/paseo/blob/c4dc36272cda1bb07a8c11b7beee41f247ee7952/docs/development.md)

补丁中的回归测试使用测试会话与临时数据。原始 Paseo 源码、补丁上下文和出处随目录中的 LICENSE 一起保留。

## 让已配置的 Codex provider 共用账号池

当两个账号各自已经通过[文件和图片验收](setup.md)，可以给 Account Switcher 添加账号池支持。插件在额度耗尽时选择允许继续运行同一模型的账号；每个 provider 的原始 bridge 命令仍负责 SSH、工作区和隧道。跨账号继续会话会把该线程的历史交给目标账号，只将允许共享此历史的账号放入同一池。

在本 bridge 仓库根目录运行以下命令。补丁针对插件 fork 的固定提交，与上面的 Paseo 补丁应用到不同目录。使用插件仓库指定的 Node 版本（见其 `.node-version`）。

```sh
POOL_PATCH="$PWD/integrations/account-switcher/configured-codex-pool.patch"
git clone --branch feat/continuous-failover https://github.com/xilinshancun/paseo-plugin-account-switcher.git account-switcher-pool
cd account-switcher-pool
git switch --detach ce0ff877086f9f2446424ea4b48afe41ff00ac43
git apply --check "$POOL_PATCH"
git apply "$POOL_PATCH"
npm ci
npm run build
npm run typecheck
npm test -- tests/codex-pool.test.ts tests/failover-row.test.ts tests/ui-failover-row.test.ts
```

第一次验证时将这个目录安装到独立开发 daemon，例如 `paseo plugin install "$PWD" --host 127.0.0.1:6768`。检查该 daemon 的插件已启用。在打过补丁的插件源码中打开 `docs/installation.md` 的 **Configured Codex pools**，按其中的配置片段逐个接入 provider。每个远端账号的 `command` 指向本机生成的 bridge 启动器，`ssh` 取开发机别名，`home` 填开发机上该账号 Codex 配置目录的绝对路径。保留原配置备份，按插件说明启用 Codex 自动续跑。

模型菜单的账号选择还需在同一个 Paseo 源码目录应用 `model-account-menu.patch`，重新构建服务端并运行这份客户端。先用一个测试聊天确认手动切换后能继续同一原生线程，再在正在回复时排队并取消一次切换，确认正在运行的回复完整。账号池等待、额度失败和恢复的离线回归在 `tests/codex-pool.test.ts` 中。[插件基线](https://github.com/xilinshancun/paseo-plugin-account-switcher/tree/ce0ff877086f9f2446424ea4b48afe41ff00ac43) · [补丁及许可证](../integrations/account-switcher/)

## 多账号、会话恢复和界面

| 想要的行为 | 对应实现与检查方法 |
| --- | --- |
| 手动切换会话账号、查看额度、额度耗尽后续跑 | 安装 [Account Switcher](https://github.com/dutchakdev/paseo-plugin-account-switcher)。固定的[连续切换开发版本](https://github.com/xilinshancun/paseo-plugin-account-switcher/tree/ce0ff877086f9f2446424ea4b48afe41ff00ac43)记录了额度恢复后的处理。用测试聊天检查切换前后的原生会话 ID 与所选模型。 |
| 网络重连后先显示当前聊天，大历史恢复时仍可继续使用 | 本地 Paseo 客户端优先同步可见标签页，限制后台初始化并发；daemon 复用历史读取。在同一工作区打开多个有历史的聊天，重连时先检查当前聊天，再逐个查看其它标签。 |
| 子任务的后代仍在运行时，父行保持工作中，批量归档保留活跃分支 | 由 Paseo 客户端与 daemon 提供。在子任务下再启动一层任务，执行 **Archive finished**，核对仍工作的分支。[上游子任务状态 PR](https://github.com/getpaseo/paseo/pull/8279) |
| 导入原项目目录已经消失的会话 | Paseo 导入界面负责提示与选择目录。可用一个已迁移的测试项目确认行为。[上游导入 PR](https://github.com/getpaseo/paseo/pull/7372) |
| 启动远端会话时取消发送，仍保留文字和附件草稿 | 依赖 Paseo composer、daemon 的取消状态与消息回执。本地宿主修复还处理了原生子任务历史。验收时在启动阶段取消一条带测试图片的消息，再从原草稿重发一次。 |
| 远端 agy 启动时减少对 Mac 的 Git 和 skills 元数据读取 | 原部署会预读一小批 Git 元数据和 `SKILL.md`，用 Linux 私有挂载命名空间临时映射，首条输出时恢复真实文件。该实现依赖 Linux 命名空间及相应 Python 接口。分发的[工作区启动器](antigravity.md#用远端-agy-打开-mac-工作区)从会话开始就读取实时文件。 |

表中标注“本地”的能力记录了已观察到的实现位置和验收条件。它们是否可用，取决于你安装的 Paseo 或插件版本。仅安装本桥即可使用的功能列在[首页](../README.md)。
