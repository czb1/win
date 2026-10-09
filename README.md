# 《未来战争》Python 参赛智能体

当前比赛阶段为 **v2.0 / 32进16**。后续规则与接口从 [v2.0 比赛资料](比赛文档/v2.0-32进16/README.md) 阅读，后续设计与验证维护在 [2.0 版本设计文档](2.0版本设计文档/README.md)。

Python 参赛实现提供 HTTP 回合决策服务，已逐项适配部分 v2.0 规则；具体实现、待确认规则和验证范围以 2.0 设计文档的最新变更记录为准。

## 当前阶段文档

| 入口 | 适用范围 |
|---|---|
| [v2.0 / 32进16 比赛资料](比赛文档/v2.0-32进16/README.md) | 当前任务书、接口文档、变更摘要与原始示例 |
| [2.0 版本设计文档](2.0版本设计文档/README.md) | 当前四模块设计基线与待适配、待验证事项 |
| [v1.0 初赛比赛资料](比赛文档/v1.0-初赛/README.md) | 历史任务书、接口文档和原始示例 |
| [1.0 版本设计文档](1.0版本设计文档/README.md) | 原 `docs/` 的历史设计、测试报告与验证数据 |

## v2.0 当前围墙策略

默认围墙围住整个基地，与基地占地保持两格距离，共十九格；先建六格迎敌墙，再延伸两翼并围住后方。侧后方靠近原炮手通路的位置保留一格人员出入口，该格不进入建墙或补洞计划。默认建两座火箭和一座加特林：开拓者每晚在公共格按冷却轮流操控火箭，工人在入口旁的独立站位操控加特林，不再派开拓者去敌方卡位。加特林优先射击可实际命中的最高血量敌人；附近威胁清除后工人安全采矿，两座三级火箭继续攻击射程内的敌方墙。左右阵营自动镜像；显式 `wall_cells` 仍按配置执行。

加特林按当前目标的有效弹道选择操控格，原站位无可命中目标时允许换位，已有有效站位则保持稳定。默认混合布局在原三个推导武器格内比较入口射界与独立站位，保留已建武器；完整围墙下主要守入口，不能隔着基地或围墙射击正面敌人。完整周界的唯一入口被炮手占用且相邻队友需要通行时，炮手向外逐格退让，并留出后续两个回合；开拓者回防通路先于工人回位预约。自定义非公共格布局同样执行选定站位。`unit_decision.targeting` 记录当前操控者、射程内目标数、畅通弹道数及建筑／人物／中立物阻挡样本，可区分换位、待命、冷却、无人操控和弹道受阻。

普通墙体升级依次为：迎敌侧一级升二级、其他侧一级升二级；整圈墙建成且都达到二级后，迎敌侧按原有中心向两端的顺序升三级，最后其他侧升三级。重建墙继续按已有恢复优先级处理，所有侧墙均可恢复到三级。当前策略与验证维护在 [2.0 防御模块设计](2.0版本设计文档/防御.md)。

## 1.0 实现与历史验证记录

以下内容记录已有实现及此前验证，不构成 v2.0 新赛制的适配完成或实战验证结论。

**交付状态：可启动和测试的策略基线。** 建筑区域及部分建筑参数在任务书的内网图片中，当前无法读取。默认布局、建造费用沿用或推导自仓库 demo，正式比赛前需核对。未获得完整判题器，尚未验证比赛胜率、连续生存十天或完整计分表现。

1.0 已记录的工人策略：默认十二格城墙（迎敌面六格、两翼各三格）保持原建设逻辑，三座火箭集中在基地后角，由一名工人在公共操作格按实际冷却轮射；另一名工人夜间安全采矿，炮手死亡后停止采矿并回来补位。夜前优先选择未携带施工石料的工人回防，避免打断补墙。每日批量出售、采购升级和自进化方法学习保持原逻辑；上述炮手调度均由确定性代码执行，不依赖弱模型。具体规则与验证见 [1.0 经济模块设计](1.0版本设计文档/经济.md)、[1.0 防御模块设计](1.0版本设计文档/防御.md) 和 [1.0 战斗模块设计](1.0版本设计文档/战斗.md)。

持续采矿、批量购物和夜前调度统一维护在 [1.0 经济模块设计](1.0版本设计文档/经济.md)。

当前补充修复：被建筑阻断的炮台不再占用工人，队友临时堵路仍沿用主分支的协同让行；全部炮台不可达时可改以基地规划采矿返程。正常采矿、出售和建设无法继续时，工人可尝试实际步行两步以内的矿点；按实际采集位置到已分配操控站位的路程核对回防，保留默认 5 回合余量，矿石允许留到次日出售。建设采购阶段切换前不额外启动顺手采矿，避免打乱工人运输顺序。施工石料保留、固定卖矿目标和移动失败退避均沿用主分支。

此前同步 `main` 的 `0bd97af` 后，Python 3.11 虚拟环境下全部 188 项测试通过。相同八组首日受控回放中，一组附近铁矿场景采集由 53 次增至 56 次（增加 3 块未售石头），其余七组夜前状态一致；所有场景均保留主分支的首夜火力、围墙和三名操控者到位，非法动作均为 0。相关策略、设定原因与经验统一维护在 [1.0 经济模块设计](1.0版本设计文档/经济.md) 和 [1.0 防御模块设计](1.0版本设计文档/防御.md)。

机器人阵营隔离见 [1.0 战斗模块设计](1.0版本设计文档/战斗.md)；固定 `READ` / `LIST` 工具和任务可靠性见 [1.0 自进化模块设计](1.0版本设计文档/自进化.md)。

连续防线和布局取舍统一维护在 [1.0 防御模块设计](1.0版本设计文档/防御.md)。

默认左侧基地先建右侧迎敌墙，右侧基地先建左侧迎敌墙，再建设两翼，共十二格；后方保持开放，供采矿、购物和回防使用。显式 `wall_cells` 布局仍按配置执行。自定义直射武器请配套显式布局，否则连续墙会挡住其弹道。

默认十二格墙按共享边连续施工：第一格确定起点，后续必须紧贴已有墙（含当回合已提交的建墙），不能跳格或只对角接触。迎敌面六格完成后再从端点延伸两翼；正在前往施工点的工人不算已建墙。显式布局仍尊重用户配置。

上一轮策略修正：矿石批量出售、造墙石料批量采集、失败建造点退避期间切换经济活动、按当回合可输出火力分配操控者、火箭空地溅射选点，以及电磁炮回合末伤害结算修正。策略与设定原因按领域统一维护在战斗、经济、防御、自进化四份模块文档中。

## 快速启动

推荐与比赛一致的 **Python 3.11.10**，仅使用标准库，无需安装第三方包。交付环境实际测试版本见 `1.0版本设计文档/测试报告.md`。

日志默认加密，仓库已配置正式公钥 `SDK/SDK_Python/CoreGeek/log-public.json`。**自动发布的 Release 包会携带公钥，可以直接上传比赛平台**，不需要每次生成密钥或重新打包。

对应私钥通过独立文件交付，仅在本地保存和备份，不进入 Git 或比赛包。可将交付文件在自己的电脑上保存为 `keys/log-private.json`，供下面的本地解密命令使用。私钥丢失后已有日志无法恢复；发布工作流会拒绝打包包含 FWLOG 私钥 JSON 的目录。

仅在主动更换密钥时生成新的一对密钥（默认 RSA 3072 位）：

```bash
python tools/extract_logs.py --generate-keys keys/log-private-new.json --public-key-out keys/log-public-new.json
```

保留并备份新的私钥，将新的公钥替换 `SDK/SDK_Python/CoreGeek/log-public.json` 并提交仓库，之后的发布包会使用新公钥；旧日志仍需对应旧私钥。密钥生成不会覆盖已有文件，私钥默认使用权限 `0600`；Windows 请通过文件权限限制访问。`keys/` 和私钥文件默认不进入 Git。

也可通过 `--log-public-key /path/to/log-public.json` 或环境变量 `FUTURE_WAR_LOG_PUBLIC_KEY` 指定公钥。缺失或无效时启动失败，不自动退回明文；仅本地调试可以显式传 `--plaintext-logs`。加密运行及密钥生成都只用 Python 标准库，不需要系统 OpenSSL、第三方包或网络。

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

### 按天数、昼夜和任务查日志

日志面向“看录像找到回合 → 下载一个 `.log` 文件 → 本地解密并提取问题区间 → 交给智能体分析修改”的流程。比赛端统一向 stderr 输出 `FWENC {JSON}` 加密封装，无需 HTML 或平台支持多个文件。解密后的记录仍带格式版本、运行编号、请求编号、队伍、场次、全局回合、天数／昼夜及任务／单位编号；时间戳为 UTC，游戏时间按 `round_origin` 计算。回合倒退会新建场次，相同请求的缓存命中也单独记录。

自行实现 RFC 8439 的 ChaCha20-Poly1305 和 RFC 8017 的 RSA-OAEP／SHA-256。每次日志配置生成随机 256 位运行密钥，用公钥封装；每条记录压缩后完整加密，计数 nonce 在该运行密钥下不重复。外层只含协议、密钥指纹、封装密钥、nonce、密文及可选分段信息，地图、题目、回复、异常和业务身份均在密文内。每条／每段携带恢复所需的封装密钥，截取文件不依赖文件头。同一条密文可复制到 stderr 和本地文件，切换进程／重新配置会更换运行密钥。认证校验保护记录内容与外层协议字段，但公钥加密不证明日志发送者身份，也不能证明整份文件未被删除尾部；缺段与序号缺口仍按输入文件报告。

每回合的 `turn_snapshot` 保留原始地图、所有下发单位、背包、金币、积分、任务点、价格，以及 v2.0 的矿石 `remain`、驾驶状态、小车和 `summonRobotList`。敌方单位仅记录实际收到的观测；`enemy_last_seen` 是历史位置，不是当前真实位置。机器人攻击目标不代表归属。快照包含完整当前状态，不依赖之前回合的地图增量。

`unit_decision` 记录最终动作／未行动原因、决策代码位置、采矿候选淘汰汇总、路程、时间预算、回防条件、资源和校验拒绝原因。`previous_feedback` 同时记录前一回合指令、合法性与实际位置／血量／背包／金币／积分变化；合法不等于生效，跳过回合时不关联旧指令。`turn_response` 与 HTTP 日志记录预算耗尽、缓存、空响应降级、锁竞争、断开及耗时。

长题目、prompt、模型回复和执行结果完整保存，重复内容通过 `payload_id` 引用。大记录加密后按编号拆成可校验的 base64 分段，物理行小于8KB；分段只影响日志传输，不改 HTTP 响应。单条解密记录上限16MiB，分段缓存上限32MiB。日志失败只输出固定的 `FWLOG-ERROR logging_failed` 提示，不打印原文、异常值或密钥，也不使已校验动作失败。核心快照、原因、反馈及任务原文在 WARNING／ERROR 下仍保存，`--log-level` 主要控制既有普通／DEBUG 诊断。

`--log-dir` 是可选的本地副本，保存同样加密的 `game.log` 与 `events.jsonl`，每文件50MB轮转并保留5份备份；下载的单个 `.log` 文件已包含分析所需记录。轮转删除的旧副本、平台截断或手工剪切造成的缺失不能恢复，提取结果会报告缺少元数据、分段、引用或中间序号。

```bash
.venv/bin/python SDK/SDK_Python/CoreGeek/main3.py 8080 --log-level INFO --log-dir artifacts/game
# 离线回放同样支持；响应 JSON 仍单独输出
.venv/bin/python tools/replay.py examples/request.json --log-level INFO --log-dir artifacts/replay
# 先看天数、昼夜、任务编号及日志数量目录
.venv/bin/python tools/query_logs.py artifacts/game/events.jsonl --private-key keys/log-private.json --list
# 第2天黑夜；phase=day 表示白天
.venv/bin/python tools/query_logs.py artifacts/game/events.jsonl --private-key keys/log-private.json --day 2 --phase night
# 自进化：领取、题目、模型回复、沙盒执行、提交、结束及结果
.venv/bin/python tools/query_logs.py artifacts/game/events.jsonl --private-key keys/log-private.json --category evolution
# 长上下文：累计新闻、民间传闻、线索分析、用品准备与宝藏召唤
.venv/bin/python tools/query_logs.py artifacts/game/events.jsonl --private-key keys/log-private.json --category long_context
```

下载 `.log` 文件后，只需下载 [extract_logs.py](tools/extract_logs.py)，准备对应的本地私钥，用 Python 3.11+ 执行（单文件、只用标准库，无需安装依赖）。以下 `keys/log-private.json` 请替换为自己的私钥路径：

```bash
# 同一 .log 文件有多个场次时先确认编号；也会列出任务编号
python extract_logs.py match.log --private-key keys/log-private.json --list
# 提取录像对应的120~140回合，并附带前后5回合
python extract_logs.py match.log --private-key keys/log-private.json --from-round 120 --to-round 140 --context 5 --out issue
# 可附加 --session 场次编号、--team 队伍编号、--unit-id 人物编号
python extract_logs.py match.log --private-key keys/log-private.json --task-id "场次/r领取回合" --context 0 --out task_issue
# 需要进一步分文件时使用 --split
python extract_logs.py match.log --private-key keys/log-private.json --from-round 120 --to-round 140 --out issue --split
```

四种常用筛选方式（天数以第2天为例，替换 `--day 2` 即可）：

| 提取内容 | 命令 |
|---|---|
| 只提取自进化任务 | `python extract_logs.py match.log --private-key keys/log-private.json --mode evolution --out evolution` |
| 只提取长上下文任务 | `python extract_logs.py match.log --private-key keys/log-private.json --mode long-context --out long_context` |
| 第2天白天的普通日志 | `python extract_logs.py match.log --private-key keys/log-private.json --mode non-task --day 2 --phase day --out day2_day` |
| 第2天黑夜的普通日志 | `python extract_logs.py match.log --private-key keys/log-private.json --mode non-task --day 2 --phase night --out day2_night` |

筛选模式的 `issue.txt` 只包含目标记录；场次元数据和完整性报告保存在 `meta.json`。任务模式按日志类别区分自进化和长上下文，并自动补齐所引用的题目／传闻原文；明确标为长上下文的记录不会因事件名以 `task_` 开头而归入自进化。普通模式排除自进化、长上下文和推理类别、模型／沙盒原文、开拓者决策、带任务上下文／任务动作／宝藏结果的反馈，以及含任务指令／模型或沙盒调用的响应，保留同回合的工人、武器等普通记录。`--context` 不会将指定天数／昼夜外的普通记录带入输出。`--list` 同样遵守模式、天数、昼夜和身份筛选；不加 `--mode` 时使用 `all`，导出完整问题区间。

解密、校验后默认输出明文 `issue/issue.txt`（全部相关诊断）、独立的 `issue/tasks.txt`（自进化、新闻推理、宝藏、相关开拓者动作与反馈）和 `issue/meta.json`（区间及完整性报告）。可以直接发送 `issue.txt`，任务问题可单独发送 `tasks.txt`。`--split` 额外输出 `turns.jsonl`、`decisions.jsonl`、`feedback.jsonl`、`errors.jsonl`。提取器分多遍流式读取，不将整份 `.log` 文件载入内存；自动补入所选场次的版本配置和区间外被引用的长文本，原始回合与任务编号不改写。`included_as` 标明补入的上下文；缺失数据明确报告，不据此补造执行成功。支持平台时间戳前缀、混杂启动信息、UTF-8／UTF-16 `.log` 和旧 JSONL，校验分段完整性、哈希及加密认证。错误／缺失私钥退出码为2且不会开始导出；损坏密文会跳过、记录 `read_report.crypto_errors` 并以退出码2导出可恢复部分。缺段记录进入 `incomplete_records`。历史明文日志无需私钥，同文件可混合旧／新格式；公钥轮换后可以重复传入 `--private-key` 指定多个旧私钥。导出的明文文件与临时提取文件也需要按本地敏感文件保存。

从 `--list` 复制任务编号后，可用 `--task-id "编号"` 查看同一次任务跨白天／黑夜的记录；自进化编号为 `session/r领取回合`（没有领取记录时用首次看到题目的回合），长上下文编号为 `session/long-context`，新闻推理为 `session/reasoning`。查询工具支持 `.log` 和 JSONL，可组合 `--from-round`、`--to-round`、`--unit-id`、`--event`、`--team`、`--session`、`--contains`、`--level` 和 `--limit`；`--json` 输出恢复后的 JSON Lines。白天70回合、黑夜60回合，阶段内回合从1计数；任务结束和 outcome 保留旧任务编号，新任务单独编号。领取失败、合法提交、任务结束和有证据的完成分别记录。

工具不能给历史无结构的纯文本日志补充地图和任务编号。使用 `callback()` 的外部宿主需调用 `agent.logging_system.configure_logging("INFO")` 启用同样的 stderr 输出，也可传入日志目录。场次编号基于代理内存生命周期，不代表官方比赛 ID。运行配置和源码指纹可用于核对程序版本；打包方可通过 `FUTURE_WAR_VERSION` 附加包版本。

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
.venv/bin/python tools/run_checks.py          # 日常开发、交付和 CI：仅快速检查
.venv/bin/python tools/run_checks.py --quick  # 与默认命令相同
.venv/bin/python tools/run_checks.py --full   # 手动按需开启：全部仿真 + 独立策略基准
```

默认禁用标记的仿真测试（首日镜像、多地图校准、三日重建、五日升级等）和独立策略基准，保留协议、动作合法性、状态转换及边界条件等快速回归。CI 的 push / PR 检查也只运行快速模式；开发中间迭代和交付均不再要求全量仿真。

默认日志为 `artifacts/validation/quick.log`，摘要显示执行数量、排除的仿真数量和耗时。仿真测试及断言仍保留，需要时显式运行 `--full`，日志为 `artifacts/validation/full.log`；也可在 GitHub Actions 手动触发并勾选 `full`。快速检查通过不代表多日策略已验证。直接执行原始 `unittest discover` 仍会包含仿真，日常请使用统一入口。

这些离线模拟不请求真实模型，因此不消耗比赛模型额度；节省的是开发过程中阅读长日志、重复分析和重复执行的开销，不承诺固定 token 节省比例。

以下命令仅供明确需要时手动排查，不属于默认验证流程（将输出保存到文件后读取所需指标）：

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

比较入夜状态请使用 `checkpoints["69"]`（第 69 回合结算后、首夜第 70 回合之前），而不是无战斗夜晚结束后的余额。报告区分初始金币、卖矿收入、任务收入（当前未模拟，为 0）、支出、库存、矿点数量、炮塔等级和操控者到位数。手动 `--full` 验证会生成 `artifacts/calibration/first-night.json` 的八组样例/随机镜像结果，无需重复跑矩阵。多日模式没有机器人战斗、敌方抢矿、任务奖励或新闻价格变化，不能据此推断真实多日财富和胜率。

回放工具还支持由多个连续回合请求组成的 JSON 数组。它只调用决策器，不模拟机器人、经济结算或任务判分。

`strategy_benchmark.py` 单独提供受控采矿回放和静态密集机器人耗时测试。可传入 `--agent-root /path/to/old-checkout` 对比旧版；该脚本不是完整比赛模拟器，不能用于计算胜率。

## 文件导航

| 路径 | 内容 |
|---|---|
| `1.0版本设计文档/战斗.md` | 历史战斗策略、武器设定、原因、经验教训与验证边界 |
| `1.0版本设计文档/经济.md` | 历史采矿、交易、采购、升级和资源调度 |
| `1.0版本设计文档/防御.md` | 历史基地布局、建墙、炮塔位置、通路和回防 |
| `1.0版本设计文档/自进化.md` | 历史任务状态机、模型交互、沙盒、记忆与经验复用 |
| `1.0版本设计文档/测试报告.md` | 2026-09-14 的历史测试报告，不能作为当前阶段验收 |
| `SDK/SDK_Python/CoreGeek/main3.py` | 比赛入口与 `callback(json_data)` |
| `SDK/SDK_Python/CoreGeek/agent/` | 协议模型、指令校验、策略、寻路、记忆和 HTTP 服务 |
| `config/default.json` | 默认策略参数 |
| `config/explicit-layout.example.json` | 手工填写合法建造坐标的配置模板 |
| `examples/request.json` | v1.0 派生回放与测试样例，保持原位置和内容 |
| `examples/response.json` | 原 v1.0 样例回放得到的响应，保留为历史夹具 |
| `tests/test_agent.py` | 标准库 unittest 自动化测试 |
| `tools/replay.py` | 单回合及连续请求回放 |
| `tools/extract_logs.py` | 可单独下载运行，从比赛 `.log` 文件提取问题区间和独立任务日志，内置零依赖密钥生成、解密、流式解析和分段校验 |
| `tools/log_records.py` | 兼容查询工具的日志读取导入入口 |

仅实现 Python 分支，不创建图片中的 C++、Java、Go、Rust 空壳工程。

## 打包与版本

GitHub Actions 在 `main`、`develop` 和 `personal/cyf-develop` 推送时继续打包 `SDK/SDK_Python/CoreGeek`，打包范围及格式不变。

- 正式版从 `v2.0.0` 开始，按已有 `v2.0.<补丁号>` 正式标签递增，例如 `v2.0.1`、`v2.0.2`，不再使用 Actions 运行编号作为正式补丁号。
- 开发分支使用 `v2.0.<下一正式补丁号>-dev.<运行编号>`，保持预发布标记，不占用正式版本号。
- 产物名为 `CoreGeek-<版本号>.tar.gz`；首次正式包为 `CoreGeek-v2.0.0.tar.gz`。
- 发布按分支排队。同一正式提交或同一次开发工作流重跑会复用已有版本；版本指向不同提交时保持原有拒绝覆盖检查。
- 历史 v1.0 标签与发布包保留。包版本切换不代表新赛制功能已实现或经过实战验证。

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

直射弹道与人物移动占位分开计算，以下选项默认保留原实现假设，须经比赛方或官方判题器确认后再改：

| 选项 | 默认值 | 可选行为 |
|---|---|---|
| `projectile_origin` | `"weapon"` | `"controller"` 使用操控者位置作为直射弹道起点；射程和加特林扇形仍以武器位置校验 |
| `projectile_characters_block` | `true` | `false` 仅取消人物对直射弹道的阻挡，移动占位、建筑／中立物阻挡、机器人首个命中与阵营保护仍保留 |

火箭继续按落点与溅射计算，不使用直射阻挡过滤。默认直线擦边格判定沿用旧实现，尚未由官方判题器确认；待确认项见 [比赛资料说明](比赛文档/v2.0-32进16/README.md)。

LLM 由判题器通过响应中的 `prompt` 调用，程序本身不需要 API Key，也不访问外部模型服务。`executeCmd` 中的 Python 交给官方任务沙盒执行；本地 HTTP 服务只做字符串构造与语法检查。

任务模型可返回 `READ 路径`、`LIST 路径`、`ANSWER` 加答案，或 `PYTHON` 加代码。文件分页可用 `READ 路径 字符偏移`。代码成功执行且输出第一行 `FINAL_ANSWER`、后续行只包含最终答案时，程序下一回合直接提交；失败、超时和截断的输出会进入修复流程。历史解法仅在合法提交后任务正常消失等条件成立时保存为有完成迹象的参考，不把动作合法性当成判题正确率。

`SDK/main3.py` 已转接正式实现，与 `SDK/SDK_Python/CoreGeek/main3.py` 共用同一智能体；`demo/` 仍是历史示例，请使用上述正式入口。模型工具代码只在官方沙盒执行。

使用旧配置时请同步更新 `round_origin: 0`、`sell_batch: 40`、`sell_batch_max: 80`、`stone_batch: 10`，否则旧配置会覆盖新默认值。默认 `loadout` 为 `["rocket", "rocket", "gatling"]`，使用旧的三火箭配置时也须同步更新；`task_max_rounds` 为 1300（整局上限）。实际任务仍受官方 `timeoutRounds` 和回防时间约束；手动保留的 40 回合配置仍会提前限制长任务。

如平台暂不提供 LLM，将 `llm_enabled` 设为 `false`，经济和防御仍可运行，自进化任务与新闻推理停止发起。

## 运行约定

- 每个角色至多一个动作；空闲时省略该角色，不发接口未定义的 `wait`。
- 日志写入 stderr，不把调试信息混入 HTTP JSON。
- 常规决策内置 3.5 秒预算；超出时返回已校验动作。没有实测的极端状态不作硬实时保证。
- 单进程保留至多 8 组队伍／阵营／基地记忆；回合回退重置。进程重启会丢失新闻、任务上下文和提示缓存。
- 相同状态的重复请求返回缓存响应，不重复计入内部 LLM 预算。
- GitHub 发布由上述打包工作流执行；比赛平台的上传和部署需按平台流程另行完成。


日志加密实现没有独立安全审计，Python 整数运算不保证恒定时间；私钥解密仅用于本地离线工具，不作为对外网络服务。加密核心修改后运行 `python tools/sync_log_crypto.py` 同步单文件提取器，快速测试会核对内嵌代码与核心完全一致。
