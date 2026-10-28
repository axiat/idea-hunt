# idea-hunt

用于分层科研选题的 agent skill，默认面向机器人与具身智能研究。总 Leader 按研究主题分组，各组依次完成问题归纳、竞争构想、独立初筛、集中核验、三席审稿与独立达标审计。候选、来源、版本与评审意见保存在本地工作区，支持显式续跑。

校验脚本检查任务引用、权限、暂停、来源额度与生命周期；任务变更工具通过预览、owner 检查及 compare-and-swap 登记已有事实。完整原生消息与 Markdown 文件均为正式科学交付。派发前核对作者和实际接收组长的原生会话约束确认；消息验收绑定完整正文、原生发送者/接收者、版本、定位、指纹与独立实读收据。消息归档工具为可选文件保全，不生成实读或完成事实，也不解除文件创建限制。科学价值由研究与审稿角色判断，脚本不能证明真实事件、阅读理解或结论正确。

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

会话恢复后，原生角色续接按[原生续接](参考/原生续接.md)核对当前工具 schema。支持 `SendMessage` 的环境使用原 agent ID 联系既有上下文；`Agent` 缺少 `resume` 参数时，仅用于明确批准的新角色。`inspect` 与同 actor 占用诊断返回仅限身份确认和 checkpoint 的 `native_context_followup`，该提示不授予科研权限。工具路由回执、角色实际回应、科研启动及完整交付分别核验。

运行根的 `running/active` 仅表示运行登记为活跃，动作许可仍须核对当前暂停、权限及具体任务；实际进展由任务启动事实、组内实读的科学原文及具名阶段判断说明。各组围绕不同未决问题选材，可以共享论文与已核事实。早期条件构想将科学未知明确列为假设或后续核验；来源运输的未知条件仍按原合同处理。阶段简报保留对象版本及原文引用，正式评级形成前注明未评级，ACK 或来源任务完成不产生 A 类或 SA。

GPU 训练、模型推理实验、仿真、实机、独立后台服务和对外发布分别取得明确授权。研究仅在活跃 CLI 会话内运行；关闭后的继续通过 `resume` 显式发起。AI 评审是研究建议，预期效果及论文录用均无保证。

## 维护与更新

技能代码以本仓库为维护源。更新已有安装前，先比较并备份本地修改、个人配置及任务引用的规范原字节与 hash，再同步已核验版本；当前模型、provider、权限与并发设置保持。`配置.yaml` 的个人设置、研究工作区和运行档案分别保留，模板用于新工作区，已有工作区按实际需要逐项合并规范变化。

续跑前按 `SKILL.md` 核对当前授权、真实在途任务及规范变化；旧任务、norm hash、作者身份和原裁决保持原文。epoch 调整以有效规范、角色或授权映射的实际变化为依据，普通代码更新沿用现有 epoch；需要迁移任务时，在暂停下记录明确映射，研究恢复另按当前用户授权确认。更新后执行离线校验；测试通过仅证明程序合同，不构成研究恢复或科学达标。

## 离线校验

在仓库根目录执行：

```bash
python3 -I -B 测试/任务校验测试.py
python3 -I -B 测试/任务变更_测试.py
python3 -I -B 测试/阶段合同测试.py
python3 -I -B 测试/消息归档测试.py
python3 -I -B 测试/消息交付测试.py
python3 -I -B 测试/任务校验测试.py --example
python3 -I -B 脚本/任务校验.py --help
python3 -I -B 脚本/任务变更.py --help
python3 -I -B 脚本/消息归档.py --help
```

测试使用临时目录与合成数据，`--example` 输出通过 dispatch 检查的完整合成 JSON 示例。运行档案接口见 [`参考/任务接口.md`](参考/任务接口.md)。历史规范归档回退目前仅支持默认工作区的白名单路径，具体要求见接口说明；新运行和普通检查支持 `workspace` 覆盖。

RTK 可作为命令包装，例如 `rtk proxy python3 -I -B 测试/任务校验测试.py`，安装及校验均无 RTK 强制依赖。

## 文件

| 路径 | 内容 |
| --- | --- |
| `SKILL.md` | 显式调用、分层调度、审稿、交付与恢复流程。 |
| `配置.yaml` | 默认工作区、小组数与本次覆盖的配置入口。 |
| `工作区模板/` | 可复制的研究条件、角色和评审规范。 |
| `脚本/` | 只读任务校验、有界查询、任务生命周期登记与消息原文归档。 |
| `测试/` | 离线合成数据回归测试。 |
| `参考/任务接口.md` | 任务结构、收据、来源合同与 Python/CLI 接口。 |

## License

[MIT](LICENSE) · Copyright (c) 2026 axiat
