# 安装、迁移与维护

以下使用 `devbox`、`codex-remote`、端口 `18733` 作为示例。可以自选 SSH 别名、Codex 配置目录、provider 名称和端口，每个实例保持一致即可。

## 1. 准备两端程序

Mac 需要 Paseo、Python 3.9+、OpenSSH 和 Codex CLI；开发机需要 SSH 服务和 Codex CLI。分别检查：

```sh
python3 --version
"$HOME/.local/bin/codex" --version
"$HOME/.local/bin/codex" exec-server --help
ssh -T -o BatchMode=yes devbox '"$HOME/.local/bin/codex" --version'
```

命令中的 Codex 路径按实际安装位置调整。SSH 别名需事先配置好密钥登录和主机信任；服务使用 `BatchMode=yes`，不会弹出密码输入框。

在开发机使用所选的 `CODEX_HOME` 按 Codex 正常流程登录：

```sh
ssh -t devbox 'CODEX_HOME="$HOME/.codex-remote" "$HOME/.local/bin/codex" login'
```

已有该配置目录的有效登录可直接复用。桥接程序不复制、替换或切换账号登录文件。配置目录也可以用已存在的其它绝对路径。

## 2. 生成本机实例

在仓库目录执行：

```sh
python3 scripts/install.py \
  --ssh-host devbox \
  --profile codex-remote \
  --remote-codex-home '~/.codex-remote' \
  --remote-codex-bin '~/.local/bin/codex' \
  --local-codex-bin "$HOME/.local/bin/codex" \
  --port 18733

BRIDGE_DIR="$HOME/.local/share/paseo-codex-remote-bridge/codex-remote"
```

远端路径接受绝对路径或以 `~/` 开头的路径；单引号使示例中的 `~` 留给远端解析。安装目录不能已经存在，也不能在源码仓库内。

生成文件：

| 文件 | 用途 |
| --- | --- |
| `codex-remote`、`launcher.py`、`image_bridge.py` | Paseo 启动远端 Codex 的入口和运行时代码 |
| `bridge.json` | 此电脑的 SSH、路径和端口配置 |
| `provider.json` | 要合入 Paseo 配置的片段 |
| `exec-server.plist` | Mac 工具执行服务 |
| `ssh-tunnel.plist` | 让开发机回连 Mac 的反向 SSH 隧道 |
| `exec-server.token` | 新生成的工具服务连接凭证 |
| `remote-environments.toml` | 要安装到远端 Codex 配置目录的工具环境配置，包含同一连接凭证 |

后两份文件权限为 `0600`，实例目录为 `0700`。连接凭证用于访问 Mac 上的命令执行服务，与 Codex 账号登录 token 是两回事，不应提交到 Git 或粘贴到聊天。

## 3. 配置远端工具环境

下面的命令只适用于新的远端配置，已有 `environments.toml` 时会拒绝覆盖。已有配置时先保存副本，再按本实例的端口和 token 合并所选环境，不要覆盖其它正在使用的环境。

```sh
ssh devbox 'umask 077; mkdir -p "$HOME/.codex-remote"; test ! -e "$HOME/.codex-remote/environments.toml" && (set -C; cat > "$HOME/.codex-remote/environments.toml")' \
  < "$BRIDGE_DIR/remote-environments.toml"
```

这是上述实测 Codex CLI 使用的配置格式：默认工具环境是 `mac`，关闭开发机本地环境，并通过 `ws://127.0.0.1:18733` 访问 Mac 工具服务。文件位于本实例 `CODEX_HOME/environments.toml` 下。

同一开发机上若同时连接两台 Mac，请为它们分别使用不同端口、provider 名称和远端 `CODEX_HOME`，避免工具被路由到另一台电脑。迁移同一实例时，应等旧会话结束、停止旧电脑对应隧道后再接入新电脑。

## 4. 启动 Mac 服务

先确认没有其它服务占用所选端口。以下为当前登录用户注册服务，不需要 `sudo`：

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp -n "$BRIDGE_DIR/exec-server.plist" \
  "$HOME/Library/LaunchAgents/local.paseo-codex-remote-bridge.codex-remote.exec-server.plist"
cp -n "$BRIDGE_DIR/ssh-tunnel.plist" \
  "$HOME/Library/LaunchAgents/local.paseo-codex-remote-bridge.codex-remote.ssh-tunnel.plist"

launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/local.paseo-codex-remote-bridge.codex-remote.exec-server.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/local.paseo-codex-remote-bridge.codex-remote.ssh-tunnel.plist"
```

使用其它 `--profile` 时，将文件名和后续 label 中的 `codex-remote` 同步替换。服务的实际 `Label` 已由安装器写入 plist。

两个服务启用 `RunAtLoad` 和 `KeepAlive`，随用户登录加载。Mac 工具服务只监听本机 loopback；反向隧道也只请求绑定开发机 loopback。SSH 服务端必须允许这种端口转发，不应配置 `GatewayPorts yes` 强制把远端监听暴露给所有网卡。

## 5. 接入 Paseo

打开生成的 `provider.json`，把其中 `agents.providers.codex-remote` 这一项合入 Mac 的 `~/.paseo/config.json`。保留原文件中的其它设置与 providers；不要用片段覆盖整个文件。生成结构如下：

```json
{
  "agents": {
    "providers": {
      "codex-remote": {
        "extends": "codex",
        "label": "codex-remote",
        "command": ["/absolute/installation/path/codex-remote"]
      }
    }
  }
}
```

应用配置后，在 Paseo 中选择该 provider。可以用其 CLI 检查：

```sh
paseo reload
paseo provider diagnostic codex-remote --json
paseo provider models codex-remote --json
```

若 `paseo` 不在 PATH，可用应用内 CLI 的实际路径。已有会话需在空闲时重连才会采用新入口；不要为更新一个实例批量中断其它会话。

## 接入检查

```sh
"$BRIDGE_DIR/codex-remote" --version
launchctl print "gui/$(id -u)/local.paseo-codex-remote-bridge.codex-remote.exec-server"
launchctl print "gui/$(id -u)/local.paseo-codex-remote-bridge.codex-remote.ssh-tunnel"
```

然后在 Paseo 新建一个使用该 provider 的测试会话：

1. 让 Codex 执行 `pwd`、读取一个你指定的无敏感内容的 Mac 文件，确认工具落在预期电脑。
2. 上传一张有文字的测试图片，要求直接读图且不调用工具。确认不会出现 Mac 临时路径在开发机不存在的错误。
3. 结束测试会话，确认没有遗留的测试进程。

`--version` 只验证 SSH 启动链；provider discovery 只验证模型配置；它们都不能替代工具和图片检查。

## 日常更新

在源码仓库执行：

```sh
git pull --ff-only
python3 -m unittest discover -s tests -v
python3 scripts/install.py --update --install-dir "$BRIDGE_DIR"
```

`--update` 保留所有实例配置和连接凭证，只更新运行时代码。会话空闲后重连，新的 app-server 进程才会加载更新。若要更换主机、端口或 Codex 路径，使用新的实例目录生成一致的配置，再按上述接入步骤切换。

安装器把执行它的 Python 路径写入启动脚本。以后移除了该 Python 时，使用仍存在的 Python 重新执行 `--update`。跨电脑应重新安装实例，不直接搬运带有旧电脑绝对路径的生成目录。

## 常见问题与停止服务

| 现象 | 检查位置 |
| --- | --- |
| SSH 无法连接 | 本机 SSH 别名、主机信任、密钥、`BatchMode=yes` 登录 |
| 模型认证失败 | 开发机对应 `CODEX_HOME` 的正常 Codex 登录状态 |
| 工具无法回到 Mac | 两个 launchd 服务、端口是否冲突、远端 `environments.toml` 的端口与新 token 是否匹配 |
| 工具运行到了旧电脑 | 旧隧道是否仍占用同一远端端口；两个会话是否共用远端 `CODEX_HOME` |
| 图片仍提示找不到 Mac 路径 | 会话是否已重连、provider 是否指向新入口、图片在 Mac 上是否仍存在且可读 |
| 出现 `codex-e image bridge` 提示 | 这是原桥接保留的诊断前缀，与本实例的账号选择无关 |

本地图片无法读取或格式无法识别时，桥接保留原输入并给出不含路径、图片内容的 stderr 提示。不要将这个提示理解为已成功读图；可在 Mac 上用 `view_image` 单独确认文件。

停止这个实例的服务前，先结束使用它的会话：

```sh
launchctl bootout "gui/$(id -u)/local.paseo-codex-remote-bridge.codex-remote.ssh-tunnel"
launchctl bootout "gui/$(id -u)/local.paseo-codex-remote-bridge.codex-remote.exec-server"
```

如要卸载，再移除对应的两份 LaunchAgent plist 和 Paseo provider 项，最后删除本实例目录。仅停用本实例，不影响其它账号或服务。
