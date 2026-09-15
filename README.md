# 《未来战争》Python 参赛智能体

依据 `czb1/win` 的 v1.0 任务书、接口文档和示例开发。实现 HTTP 回合决策服务，包含中文设计资料、独立模块、样例与自动化测试。

**交付状态：可启动和测试的策略基线。** 建筑区域及部分建筑参数在任务书的内网图片中，当前无法读取。默认布局、建造费用沿用或推导自仓库 demo，正式比赛前需核对。未获得完整判题器，尚未验证比赛胜率、连续生存十天或完整计分表现。

最新经济策略：默认前 40 回合双工人挣钱，按卖矿、购物和交付路程提前准备；临时分配建造采购任务，批量买券，优先三座武器全部二级、随后三级。满血角色不再专程买备用药，健康围墙不自动逐座升级。经济地图先安排迎敌侧墙段，后侧延后，因此不保证首日建齐 18 面墙。策略、首夜火力与多日收入对比见 [按夜晚截止时间安排经济和升级](docs/deadline-economy.md)。

此前机器人阵营隔离、固定 `READ` / `LIST` 工具和自进化可靠性改进见 [实战采购与自进化改进](docs/实战采购与自进化改进.md)。

此前连续防线和布局修复见 [自进化与连续防线修复](docs/自进化与连续防线修复.md)。

默认左侧基地先建右墙，右侧基地先建左墙。外围 20 格只保留背敌侧两格运输通道，共 18 个墙位；迎敌面不再挖射击口。通道供采矿、购物和回防使用，因此这不是完全封闭的城墙。自定义直射武器请配套显式布局，否则连续墙会挡住其弹道。

上一轮策略修正：矿石批量出售、造墙石料批量采集、失败建造点退避期间切换经济活动、按当回合可输出火力分配操控者、火箭空地溅射选点，以及电磁炮回合末伤害结算修正。具体对比、复现方法和限制见 [策略优化验证](docs/策略优化验证.md)。

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

```bash
curl http://127.0.0.1:8080/healthz
curl -X POST http://127.0.0.1:8080/ -H 'Content-Type: application/json' --data-binary @examples/request.json
```

端口由判题系统启动时传入。`CoreGeek` 目录中另有独立 `run.sh`，可在平台只接收该目录时使用。

## 离线验证

```bash
python3 -m unittest discover -s tests -v
python3 tools/replay.py examples/request.json
python3 tools/replay.py examples/request.json --output examples/local-response.json
python3 tools/strategy_benchmark.py
python3 tools/progression_benchmark.py
python3 tools/day_economy_benchmark.py --days 3
python3 tools/day_economy_benchmark.py --case far_shop
python3 tools/fortification_benchmark.py
python3 tools/fortification_benchmark.py --mirror
```

回放工具还支持由多个连续回合请求组成的 JSON 数组。它只调用决策器，不模拟机器人、经济结算或任务判分。

`strategy_benchmark.py` 单独提供受控采矿回放和静态密集机器人耗时测试。可传入 `--agent-root /path/to/old-checkout` 对比旧版；该脚本不是完整比赛模拟器，不能用于计算胜率。

## 文件导航

| 路径 | 内容 |
|---|---|
| `docs/设计思路.md` | 目标拆解、策略优先级、路线取舍与优化计划 |
| `docs/设计文档.md` | 需求映射、模块设计、数据与协议、算法、配置及联调清单 |
| `docs/测试报告.md` | 测试命令、真实结果、覆盖范围与未验证项 |
| `docs/自进化与连续防线修复.md` | 最新故障原因、策略取舍、92 项测试及镜像建墙回放 |
| `docs/来源与差异.md` | 上游固定版本、样例修复与未确认规则 |
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
4. 确认首回合为 1；若为 0，配置 `round_origin: 0`。
5. 接入官方判题器，验证弹道起点、边界判定、建造区域、升级后多目标、夜间第一回合和任务点占用规则。

LLM 由判题器通过响应中的 `prompt` 调用，程序本身不需要 API Key，也不访问外部模型服务。`executeCmd` 中的 Python 交给官方任务沙盒执行；本地 HTTP 服务只做字符串构造与语法检查。

任务模型可返回 `READ 路径`、`LIST 路径`、`ANSWER` 加答案，或 `PYTHON` 加代码。文件分页可用 `READ 路径 字符偏移`。代码成功执行且输出第一行 `FINAL_ANSWER`、后续行只包含最终答案时，程序下一回合直接提交；失败、超时和截断的输出会进入修复流程。历史解法仅在合法提交后任务正常消失等条件成立时保存为有完成迹象的参考，不把动作合法性当成判题正确率。

`SDK/main3.py` 已转接正式实现，与 `SDK/SDK_Python/CoreGeek/main3.py` 共用同一智能体；`demo/` 仍是历史示例，请使用上述正式入口。模型工具代码只在官方沙盒执行。

使用旧配置时请同步更新：默认 `loadout` 现为三座 `rocket`，`task_max_rounds` 改为 1300（整局上限）。实际任务仍受官方 `timeoutRounds` 和回防时间约束；手动保留的 40 回合配置仍会提前限制长任务。

如平台暂不提供 LLM，将 `llm_enabled` 设为 `false`，经济和防御仍可运行，自进化任务与新闻推理停止发起。

## 运行约定

- 每个角色至多一个动作；空闲时省略该角色，不发接口未定义的 `wait`。
- 日志写入 stderr，不把调试信息混入 HTTP JSON。
- 常规决策内置 3.5 秒预算；超出时返回已校验动作。没有实测的极端状态不作硬实时保证。
- 单进程保留至多 8 组队伍／阵营／基地记忆；回合回退重置。进程重启会丢失新闻、任务上下文和提示缓存。
- 相同状态的重复请求返回缓存响应，不重复计入内部 LLM 预算。
- 源码生成与测试完成后没有执行任何比赛平台发布或远程部署操作。
