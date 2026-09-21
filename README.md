# 《未来战争》Python 参赛智能体

依据 `czb1/win` 的 v1.0 任务书、接口文档和示例开发。实现 HTTP 回合决策服务，包含中文设计资料、独立模块、样例与自动化测试。

**交付状态：可启动和测试的策略基线。** 建筑区域及部分建筑参数在任务书的内网图片中，当前无法读取。默认布局、建造费用沿用或推导自仓库 demo，正式比赛前需核对。未获得完整判题器，尚未验证比赛胜率、连续生存十天或完整计分表现。

最新工人策略：默认十二格城墙（迎敌面六格、两翼各三格）保持原建设逻辑，三座火箭集中在基地后角，由一名工人在公共操作格按实际冷却轮射；另一名工人夜间安全采矿，炮手死亡后停止采矿并回来补位。夜前优先选择未携带施工石料的工人回防，避免打断补墙。每日批量出售、采购升级和自进化方法学习保持原逻辑；上述炮手调度均由确定性代码执行，不依赖弱模型。具体规则与验证见 [经济模块设计](docs/经济.md)、[防御模块设计](docs/防御.md) 和 [战斗模块设计](docs/战斗.md)。

持续采矿、批量购物和夜前调度统一维护在 [经济模块设计](docs/经济.md)。

当前补充修复：被建筑阻断的炮台不再占用工人，队友临时堵路仍沿用主分支的协同让行；全部炮台不可达时可改以基地规划采矿返程。正常采矿、出售和建设无法继续时，工人可尝试实际步行两步以内的矿点；按实际采集位置到已分配操控站位的路程核对回防，保留默认 5 回合余量，矿石允许留到次日出售。建设采购阶段切换前不额外启动顺手采矿，避免打乱工人运输顺序。施工石料保留、固定卖矿目标和移动失败退避均沿用主分支。

此前同步 `main` 的 `0bd97af` 后，Python 3.11 虚拟环境下全部 188 项测试通过。相同八组首日受控回放中，一组附近铁矿场景采集由 53 次增至 56 次（增加 3 块未售石头），其余七组夜前状态一致；所有场景均保留主分支的首夜火力、围墙和三名操控者到位，非法动作均为 0。相关策略、设定原因与经验统一维护在 [经济模块设计](docs/经济.md) 和 [防御模块设计](docs/防御.md)。

机器人阵营隔离见 [战斗模块设计](docs/战斗.md)；固定 `READ` / `LIST` 工具和任务可靠性见 [自进化模块设计](docs/自进化.md)。

连续防线和布局取舍统一维护在 [防御模块设计](docs/防御.md)。

默认左侧基地先建右侧迎敌墙，右侧基地先建左侧迎敌墙，再建设两翼，共十二格；后方保持开放，供采矿、购物和回防使用。显式 `wall_cells` 布局仍按配置执行。自定义直射武器请配套显式布局，否则连续墙会挡住其弹道。

默认十二格墙按共享边连续施工：第一格确定起点，后续必须紧贴已有墙（含当回合已提交的建墙），不能跳格或只对角接触。迎敌面六格完成后再从端点延伸两翼；正在前往施工点的工人不算已建墙。显式布局仍尊重用户配置。

上一轮策略修正：矿石批量出售、造墙石料批量采集、失败建造点退避期间切换经济活动、按当回合可输出火力分配操控者、火箭空地溅射选点，以及电磁炮回合末伤害结算修正。策略与设定原因按领域统一维护在战斗、经济、防御、自进化四份模块文档中。

## 快速启动

推荐与比赛一致的 **Python 3.11.10**，仅使用标准库，无需安装第三方包。交付环境实际测试版本见 `docs/测试报告.md`。

在项目根目录运行：

```bash
bash run.sh 8080
```

或直接启动图片中指定的 Python 入口：

```bash
python3 SDK/SDK_Python/CoreGeek/main3.py 8080
```

Windows：

```bat
run.bat 8080
```

服务默认监听 `0.0.0.0`，判题入口为 `POST /`，健康检查为 `GET /healthz`。

排查找矿时，可开启诊断日志：

```bash
.venv/bin/python SDK/SDK_Python/CoreGeek/main3.py 8080 --log-level DEBUG 2> mining.log
.venv/bin/python tools/replay.py examples/request.json --log-level DEBUG
```

首次请求和矿区变化时，`source=mapInfo.zones` 日志记录实际收到的矿区数量、类型和坐标。`mining_mode` 日志记录工人选中的矿区、剩余步数和移动／采集指令；DEBUG 级别另外记录空背包空间、回防、不可达、出售时间不足等诊断。日志写入 stderr，不改变响应 JSON。未收到矿区时会显示 `mines_received=0`，不会臆造矿点。仓库样例是第 85 回合的夜间状态，原样回放执行回防；不能把夜间不采矿当成未收到矿区。

本地 HTTP 连续请求测试覆盖了视野外坐标选矿、7 次移动后连续采集 3 次、矿区刷新后切换目标以及矿区消失后停止采集。这里只验证收到的坐标如何驱动策略，不代表官方服务是否下发完整地图，实战仍以日志中的请求信息为准。

```bash
curl http://127.0.0.1:8080/healthz
curl -X POST http://127.0.0.1:8080/ -H 'Content-Type: application/json' --data-binary @examples/request.json
```

端口由判题系统启动时传入。`CoreGeek` 目录中另有独立 `run.sh`，可在平台只接收该目录时使用。

## 离线验证

默认使用统一入口：

```bash
# 本地 Python 3.11 虚拟环境；无需安装额外依赖
.venv/bin/python tools/run_checks.py --quick  # 中间迭代，省略标记的连续模拟测试
.venv/bin/python tools/run_checks.py          # 最终验收：全部测试 + 独立策略基准
```

完整模式保留左右镜像首日建墙、三日重建、五日升级等现有回归，默认只显示通过/失败、数量和耗时；完整日志写入 `artifacts/validation/full.log`，失败时显示有限长度的日志尾部。快速模式会明确显示省略数量，不可替代最终验收。CI 运行完整模式；不再重复执行单元测试已覆盖的建墙和升级脚本，也不再每次回放固定的历史版本。

这些离线模拟不请求真实模型，因此不消耗比赛模型额度；节省的是开发过程中阅读长日志、重复分析和重复执行的开销，不承诺固定 token 节省比例。

需要排查具体问题时再单独运行（将输出保存到文件后读取所需指标）：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_front_night_sales.py' -q
.venv/bin/python tools/replay.py examples/request.json --output artifacts/local-response.json
# 默认按接口样例的矿点、商店和售价重建开局；耗尽后跨地图随机刷新
.venv/bin/python tools/day_economy_benchmark.py --trace --output artifacts/economy.json
# 随机地图、多种子、左右镜像；记录首夜分布
.venv/bin/python tools/day_economy_benchmark.py --profile random --seeds 0 7 19 --both-sides --output artifacts/random-economy.json
# 可调整密度，例如每种矿 4 个（合计 12 个）；截图无法确认的数量不可视为官方规则
.venv/bin/python tools/day_economy_benchmark.py --profile random --mines-per-kind 4 --output artifacts/dense-economy.json
# 历史理想场景仍用于策略回归
.venv/bin/python tools/day_economy_benchmark.py --profile controlled --case near --days 3 --output artifacts/controlled-economy.json
.venv/bin/python tools/fortification_benchmark.py --mirror > artifacts/construction.json
```

经济回放默认 `sample` 档使用接口样例中的石/铁/铜各 2 个矿点、售价 1/3/5，初始金币 75、无炮塔、空背包。样例本身是夜间快照，只复用地图与价格，不声称还原真实首日。`random` 档按相同密度随机生成初始矿点；两档均每矿采集 10 次后于下一回合换位，避开双方推导建造区、角色和中立单位。同回合争抢最后一份矿石时，每人仍获得一份。`controlled` 保留旧的近距离循环矿与 1/6/10 高售价，首夜三座二级炮塔仅是这个理想夹具的断言。

比较入夜状态请使用 `checkpoints["69"]`（第 69 回合结算后、首夜第 70 回合之前），而不是无战斗夜晚结束后的余额。报告区分初始金币、卖矿收入、任务收入（当前未模拟，为 0）、支出、库存、矿点数量、炮塔等级和操控者到位数。完整验收自动生成 `artifacts/calibration/first-night.json` 的八组样例/随机镜像结果，无需重复跑矩阵。多日模式没有机器人战斗、敌方抢矿、任务奖励或新闻价格变化，不能据此推断真实多日财富和胜率。

回放工具还支持由多个连续回合请求组成的 JSON 数组。它只调用决策器，不模拟机器人、经济结算或任务判分。

`strategy_benchmark.py` 单独提供受控采矿回放和静态密集机器人耗时测试。可传入 `--agent-root /path/to/old-checkout` 对比旧版；该脚本不是完整比赛模拟器，不能用于计算胜率。

## 文件导航

| 路径 | 内容 |
|---|---|
| `docs/战斗.md` | 战斗策略、武器设定、原因、经验教训与验证边界 |
| `docs/经济.md` | 采矿、交易、采购、升级和资源调度 |
| `docs/防御.md` | 基地布局、建墙、炮塔位置、通路和回防 |
| `docs/自进化.md` | 任务状态机、模型交互、沙盒、记忆与经验复用 |
| `docs/测试报告.md` | 测试命令、真实结果、覆盖范围与未验证项 |
| `SDK/SDK_Python/CoreGeek/main3.py` | 比赛入口与 `callback(json_data)` |
| `SDK/SDK_Python/CoreGeek/agent/` | 协议模型、指令校验、策略、寻路、记忆和 HTTP 服务 |
| `config/default.json` | 默认策略参数 |
| `config/explicit-layout.example.json` | 手工填写合法建造坐标的配置模板 |
| `examples/request.json` | 修正末尾逗号后的仓库请求样例 |
| `examples/response.json` | 本项目运行样例得到的响应 |
| `tests/test_agent.py` | 标准库 unittest 自动化测试 |
| `tools/replay.py` | 单回合及连续请求回放 |

仅实现 Python 分支，不创建图片中的 C++、Java、Go、Rust 空壳工程。

## 配置与正式接入

```bash
bash run.sh 8080 --config config/default.json
```

也可设置 `FUTURE_WAR_CONFIG` 为配置文件路径。`PYTHON` 环境变量可指定 `run.sh` 使用的 Python 解释器。

1. 从官方图或判题器确认武器格、围墙格与费用。
2. 编辑 `config/explicit-layout.example.json`，填入己方地图的 `weapon_cells`、`wall_cells` 绝对坐标，核对 `weapon_cost` 与 `wall_stones`。空列表表示不建造相应建筑。
3. 显式坐标需按上下半场的己方阵地分别配置；默认推导模式会根据当前基地位置计算，但未经官方建造区域验证。
4. 默认 `round_origin: 0`，对应第 70 回合首次进攻、第 130 回合进入次日。仅当平台明确采用第 71 回合首夜时，改为 `round_origin: 1`。
5. 接入官方判题器，验证弹道起点、边界判定、建造区域、升级后多目标、夜间第一回合和任务点占用规则。

LLM 由判题器通过响应中的 `prompt` 调用，程序本身不需要 API Key，也不访问外部模型服务。`executeCmd` 中的 Python 交给官方任务沙盒执行；本地 HTTP 服务只做字符串构造与语法检查。

任务模型可返回 `READ 路径`、`LIST 路径`、`ANSWER` 加答案，或 `PYTHON` 加代码。文件分页可用 `READ 路径 字符偏移`。代码成功执行且输出第一行 `FINAL_ANSWER`、后续行只包含最终答案时，程序下一回合直接提交；失败、超时和截断的输出会进入修复流程。历史解法仅在合法提交后任务正常消失等条件成立时保存为有完成迹象的参考，不把动作合法性当成判题正确率。

`SDK/main3.py` 已转接正式实现，与 `SDK/SDK_Python/CoreGeek/main3.py` 共用同一智能体；`demo/` 仍是历史示例，请使用上述正式入口。模型工具代码只在官方沙盒执行。

使用旧配置时请同步更新 `round_origin: 0`、`sell_batch: 40`、`sell_batch_max: 80`、`stone_batch: 10`，否则旧配置会覆盖新默认值。其他默认策略：默认 `loadout` 现为三座 `rocket`，`task_max_rounds` 改为 1300（整局上限）。实际任务仍受官方 `timeoutRounds` 和回防时间约束；手动保留的 40 回合配置仍会提前限制长任务。

如平台暂不提供 LLM，将 `llm_enabled` 设为 `false`，经济和防御仍可运行，自进化任务与新闻推理停止发起。

## 运行约定

- 每个角色至多一个动作；空闲时省略该角色，不发接口未定义的 `wait`。
- 日志写入 stderr，不把调试信息混入 HTTP JSON。
- 常规决策内置 3.5 秒预算；超出时返回已校验动作。没有实测的极端状态不作硬实时保证。
- 单进程保留至多 8 组队伍／阵营／基地记忆；回合回退重置。进程重启会丢失新闻、任务上下文和提示缓存。
- 相同状态的重复请求返回缓存响应，不重复计入内部 LLM 预算。
- 源码生成与测试完成后没有执行任何比赛平台发布或远程部署操作。

