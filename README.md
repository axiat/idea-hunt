# idea-hunt

用于分层科研选题的 agent skill，默认面向机器人与具身智能研究。总 Leader 按研究主题分组，各组依次完成问题归纳、竞争构想、独立初筛、集中核验、三席审稿与独立达标审计。候选、来源、版本与评审意见保存在本地工作区，支持显式续跑。

校验脚本检查任务引用、权限、暂停、来源额度与生命周期；任务变更工具通过预览、owner 检查及 compare-and-swap 登记已有事实。科学价值由研究与审稿角色判断，脚本不能证明真实事件、阅读理解或结论正确。

## 环境

- macOS 或 Linux，Python 3.9+；脚本仅使用标准库，任务变更工具依赖 POSIX `fcntl`。Windows 可在 WSL 中运行。
- 支持 skill 的 agent CLI，并能实际创建独立 agent、定向发送消息、读写本地文件及检索公开资料。Claude Code 使用 `/idea-hunt`；其他 CLI 须按其能力和调用语法适配。
- 模型与工具权限沿用当前 CLI。研究会消耗模型及检索额度；`groups` 是研究小组数，实际 agent 数随阶段增加。默认 `groups: 7`，首次运行建议显式使用 `groups=1`。

## 安装

仓库根目录就是 skill 目录。以下命令用于首次安装，目标目录须尚未被占用：

```bash
git clone https://github.com/axiat/idea-hunt.git "$HOME/.agents/skills/idea-hunt"
```

Claude Code 通过个人 skill 软链接加载：

```bash
target="$HOME/.claude/skills/idea-hunt"
mkdir -p "$(dirname "$target")"
test ! -e "$target" && test ! -L "$target" &&
  ln -s "$HOME/.agents/skills/idea-hunt" "$target"
```

启动新 CLI 会话，使 skill 被发现。原生扫描 `~/.agents/skills` 的 CLI 可以直接加载该目录；模型及工具能力仍以当前 CLI 为准。

## 工作区与配置

将模板复制到一个新工作区，填写其中 `AGENTS.md` 的团队、算力、设备、数据条件及本次授权。模板目录与真实运行档案分开保存；已有工作区继续使用原位置及内容。

```bash
workspace="$HOME/research/机器人选题"
mkdir -p "$(dirname "$workspace")"
mkdir "$workspace" &&
  cp -R "$HOME/.agents/skills/idea-hunt/工作区模板/." "$workspace/"
```

默认配置位于 skill 根目录的 [`配置.yaml`](配置.yaml)：

```yaml
workspace: "~/research/机器人选题"
groups: 7
extra_directions: ""
resources_override: ""
```

`判定标准.md` 定义研究类别及正式评级要求，`评审校准.md` 提供完整评审条款，`角色/` 定义职责。研究条件未知时保持未知。研究方向和标准可在独立工作区内调整，实际判据与 hash 随运行记录。

## 使用

```text
/idea-hunt groups=1
/idea-hunt groups=3 extra_directions="接触感知与操作"
/idea-hunt groups=1 resources_override="本次仅有 RTX 4090 24 GiB，机器人接口未知"
/idea-hunt groups=1 workspace="~/research/机器人选题"
/idea-hunt resume="~/research/机器人选题/运行/YYYY-MM-DD_HHMMSS_探索"
```

本次明确参数优先于永久配置；续跑以原运行快照为基准。各次运行保存在 `<workspace>/运行/`，包含有效配置、状态及组档案。暂停或停止后先保全真实在途任务及材料，恢复须取得新的明确授权。

GPU 训练、模型推理实验、仿真、实机、独立后台服务和对外发布分别取得明确授权。研究仅在活跃 CLI 会话内运行；关闭后的继续通过 `resume` 显式发起。AI 评审是研究建议，预期效果及论文录用均无保证。

## 离线校验

在仓库根目录执行：

```bash
python3 -I -B 测试/任务校验测试.py
python3 -I -B 测试/任务变更_测试.py
python3 -I -B 测试/任务校验测试.py --example
python3 -I -B 脚本/任务校验.py --help
python3 -I -B 脚本/任务变更.py --help
```

测试使用临时目录与合成数据，`--example` 输出通过 dispatch 检查的完整合成 JSON 示例。运行档案接口见 [`参考/任务接口.md`](参考/任务接口.md)。历史规范归档回退目前仅支持默认工作区的白名单路径，具体要求见接口说明；新运行和普通检查支持 `workspace` 覆盖。

RTK 可作为命令包装，例如 `rtk proxy python3 -I -B 测试/任务校验测试.py`，安装及校验均无 RTK 强制依赖。

## 文件

| 路径 | 内容 |
| --- | --- |
| `SKILL.md` | 显式调用、分层调度、审稿、交付与恢复流程。 |
| `配置.yaml` | 默认工作区、小组数与本次覆盖的配置入口。 |
| `工作区模板/` | 可复制的研究条件、角色和评审规范。 |
| `脚本/` | 只读任务校验、有界查询与任务生命周期登记。 |
| `测试/` | 离线合成数据回归测试。 |
| `参考/任务接口.md` | 任务结构、收据、来源合同与 Python/CLI 接口。 |

## License

[MIT](LICENSE) · Copyright (c) 2026 axiat
