# 项目十一 AutoTest Agent：两人共享接口 v1.0

## 团队协作与进度入口

当前（2026-11-13）：P07、P08A及P08B T1—T4已获用户接受；T5已收齐360条正式执行记录，存在测试收集入口及覆盖率完整性门槛问题，尚不能直接给最终模型结论。56条程序技术verified仍待T6语义确认，下一阶段未开始。见[正式执行记录与交接](阶段8_正式执行记录与交接.md)及唯一[项目计划与进度](项目计划与进度.md)。P06独立确认仍待用户。

P06已完成真实单命令闭环及覆盖率反馈技术验收，待用户确认：三个任务全部重新生成测试，修复后含保留130/61/110项通过；独立语义与重放通过。正确反馈子任务继承父全部测试，19项可见/141项完整通过，覆盖率保持满覆盖，无增量。命令与限制见[阶段6记录](阶段6_验证与交接.md)，真实证据见[evidence/p06-real-workflow.json](evidence/p06-real-workflow.json)。此段为P06历史交付记录，当前进度见上方入口。

P05已获用户验收：三个冻结函数完整验证132/55/108项通过。用 `python -m examples.repair_boundary --replay evidence/p05-real-repairs.json --image-id <本机执行镜像sha256 ID>` 可重放历史补丁；P05后两例使用人工可见测试，P06已补齐三个任务的真实生成。

P04已获用户验收：学校DeepSeek生成9例，完整测试9通过1预期边界失败，独立复核及重放通过。真实提案和输入见[evidence/p04-deepseek-generation.json](evidence/p04-deepseek-generation.json)；`python -m examples.replay_p04 --image-id <本机执行镜像sha256 ID>` 可无模型调用复现。

模型配置见[阶段4记录](阶段4_验证与交接.md)：复制.env.example到本机.env，填写学校密钥及授权DeepSeek模型ID；本机.env忽略提交。用户已取消固定调用次数门槛，仍记录usage和次数；单次输出2048、等待300秒，无HTTP自动重试。修复客户端显式禁用思考模式以获得简洁完整答复。

P03已获用户验收。先按[阶段3记录](阶段3_验证与交接.md)构建镜像并取本地sha256 ID，再执行 `python -m examples.verify_isolation --image-id <镜像ID>`，可复跑9个固定隔离场景。

最新开发范围见 [任务书](任务书.md)：先完成边界条件缺陷的真实测试—修复闭环，用户验收后再扩展空值/类型及异常处理。最终三类缺陷、Web与五项目目标保留。

采用Spec驱动协作，先阅读 [Spec入口](specs/README.md)、[需求规格](specs/需求规格.md)、[接口契约](specs/接口契约.md)和当前阶段 [P08B实施计划](specs/P08B-实施计划.md)。规格批准、实现自测、独立复核及用户验收分别记录。

请先阅读 [项目计划与进度](项目计划与进度.md)，其中包含阶段计划、两人分工、任务依赖、验收标准、当前状态和交接记录。每次领取或完成任务都必须同步更新该清单；每阶段交付后等待用户确认。智能体接手同时遵守 [AGENTS.md](AGENTS.md)。环境与方案依据见 [阶段1_技术方案与验收](阶段1_技术方案与验收.md)。

宿主代码使用Python 3.11+标准库与frozen dataclass/Protocol，不依赖Pydantic或LangGraph；容器镜像固定Python、pytest及coverage。已实现解析、真实模型生成、隔离执行、边界修复、统一调度及受控覆盖率反馈；Web与五项目实验仍按进度表推进。

## 快速开始

### 单命令闭环及覆盖率（P06）

```powershell
docker build -t autotest-executor:phase6 execution
$executionImage = docker image inspect autotest-executor:phase6 --format '{{.Id}}'
# 无需密钥，重放真实测试和补丁：
python -m workflow.orchestrator --replay evidence/p06-real-workflow.json --feedback --image-id $executionImage
# 配置学校密钥后，重新真实生成与修复（模型须在自己授权列表内）：
python -m workflow.orchestrator --call --model deepseek-v4-flash-ascend --feedback --image-id $executionImage
```

不加--call/--replay只预览；--case可选固定任务；--cancel-file指定本机取消标记。重放不调用模型，输入/独立摘要须一致。固定样例以外的复杂项目、依赖及业务数据仍需另行适配。

### 隔离执行与补丁应用（阶段3已验收）

`execution.proposals.apply_proposal`在调度器独占临时副本中校验并应用提案，返回含diff及原始字节的回滚凭据；`rollback`会先检查文件未被后续修改。禁止在共享、并发写入的目标目录使用。

`execution.docker_runner.run_tests`使用审核后的不可变本地镜像ID运行固定pytest命令，缺少Docker时返回环境失败。当前支持显式Python清单，不自动安装目标依赖。构建、运行步骤及边界见 [阶段3验证与交接](阶段3_验证与交接.md)；本机WSL及Docker已实际运行验证，其他电脑须独立准备环境。

### 仓库解析（阶段2新增）

Windows首次创建独立环境：`python -m venv .venv`。本阶段仅使用标准库，无需安装额外依赖。

在项目根运行：

```console
.venv\Scripts\python.exe -m examples.inspect_repository . --target workflow/state.py --requirement "Validate state transitions and proposal scope"
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

解析器不执行目标代码。`repository.parser.parse_repository`返回冻结的源码、测试、符号、近似导入/调用及问题清单；`repository.context.build_context`返回现有`GenerateInput`和额外的只读测试上下文、行范围、被省略文件。目标文件超预算明确失败，不静默截断。模型适配器后续须显式读取`related_tests`，该字段不在现有协议内。

当前只支持UTF-8（含BOM）的Python文件；语法错误、超大文件和非UTF-8文件记录并排除，不加入可写源码。隐藏目录、依赖、构建产物和常见配置脚本被排除；这不是完整业务源码识别器，后续执行器必须再次审核可写白名单。输入应为稳定受控副本；AST调用仅为语法线索，不保证动态解析完整。`analysis-sha256:`是分析文件集合摘要，不是Git提交或执行器工作区摘要。README与配置文件尚不自动加入模型上下文，需求需明确传入；相关测试匹配采用导入及文件名启发式。

在解压后的 autotest-agent 目录运行：

```console
python -m unittest discover -s tests -v
python -m examples.minimal
```

示例用虚构执行结果演示协议流转，不执行生成的代码。真实运行证据只能由调度器的隔离执行器产生。

## 两人分工和接入

- 同学 A：实现 `TestGenerationAgent.generate(GenerateInput) -> GenerateOutput`，负责仓库解析适配、需求到测试设计、边界用例与测试生成。
- 同学 B：实现 `DefectRepairAgent.repair(RepairInput) -> RepairOutput`，负责失败证据分析、缺陷定位、最小源文件修复。
- 双方共同维护本协议、调度器和五个项目评估；接口变更先更新 CONTRACT_VERSION，再同步调用方。这里的 Protocol 是静态类型接口；运行时必须调用 validate_proposal，不能仅依赖类型标注。

调度器负责读取代码、构建请求、校验响应、应用文件和运行完整测试集。两个 Agent 都不直接写仓库或执行命令。仓库内容和测试日志均作为不可信数据处理，不允许其中的文字改变系统权限或调度策略。

## 数据和输入输出契约

所有集合使用 tuple，快照不可变；调用 `dataclasses.replace` 创建新快照。Repository 保存 project_id、不可变基线 revision、源文件/测试清单和限定名称的符号索引。解析器必须先确定源文件可写白名单；只纳入业务源码，排除配置、依赖清单、CI、生成器和凭据文件。revision 在一次任务内固定；TestRun.snapshot_digest 标记每次实际应用后的工作区快照。

GenerateInput 包含 task_id、repository、policy、requirements 与 source_texts（路径、代码文本的二元组）。生成器必须基于需求或独立行为规范生成测试，不能把当前有缺陷的实现当作预期结果。GenerateOutput 回传同一 task_id 和 base_revision、files、rationale，或 failure。只能在 policy.test_prefix 内创建 `test_*.py`，不能覆盖既有文件。

RepairInput 另外包含 failed_run、1 起始的 attempt、当前源码 source_texts 和经过脱敏、截断的 sanitized_log。修复器应拿到相关失败测试内容和堆栈作为诊断上下文（可放 sanitized_log；不要截掉关键断言）。仅 TEST_ASSERTION 进入修复；调度器应先复现并排除错误测试和不稳定测试。RepairOutput 回传相同任务、基线和 based_on_run_id，包含 diagnosis 和源码文件提案。只能替换 source_files 白名单内的文件，不能改测试、删除测试或降低断言。

FileProposal 表达完整 UTF-8 新文本，不是 shell 命令或统一 diff。expected_sha256 为 None 表示仅新建；否则必须是当前磁盘文件字节的 SHA-256，调度器应在写入前比较。即便第二次修复仍使用最初 base_revision，哈希也必须对应最新源码，based_on_run_id 必须对应最新失败运行。文件不存在、已存在或哈希不符时，以 PATCH_CONFLICT 结束该次应用，不得覆盖。

成功响应必须有非空且路径唯一的 files；失败响应必须只有 failure、没有 files。Failure 提供 kind、脱敏 message 和 evidence（产物 ID）。validate_proposal 对跨任务、过期基线、路径、文件预算、修复次数和运行 ID 进行检查；它不验证代码正确性，也不负责文件写入。返回失败响应后，调度器显式转入 FAILED。

## 状态机

```text
CREATED → PARSED → TESTS_PROPOSED → TESTED → VERIFIED
                                      ↓       ↑
                                FIX_PROPOSED ──┘
```

FIX_PROPOSED 后必须再次进入 TESTED，不能直接 VERIFIED。TESTED 有已复现的断言失败才可进入 FIX_PROPOSED；每次进入消耗一次修复预算。任一非终态可携带 Failure 转 FAILED。VERIFIED 和 FAILED 为终态。新任务用于人工调整后的重跑。

advance 是调度器专用 reducer，不替代提案校验。TESTS_PROPOSED/FIX_PROPOSED 必须在成功校验并接受相应输出后才能进入。TESTED 必须提交 TestRun；VERIFIED 要求最后一次完整测试集退出码为零、无失败或错误且至少一项通过。执行器负责证明它确实运行了完整原有测试与新增测试；状态类型本身无法证明执行真实性。

推荐流程：解析并固定基线 → 在干净副本运行原有测试记录基线 → 生成并验证测试 → 应用新测试 → 运行完整测试 → 成功则验证，否则复现和分类 → 调用修复器 → 校验并应用源码 → 再运行完整测试 → 预算内重试或终止。保留所有历史 TestRun。新测试用例不可靠时转人工审查或新任务重新生成，禁止修复器修改它以获得通过。

## 失败分类与处理

- PARSE_ERROR / TEST_GENERATION / INVALID_OUTPUT：解析、生成或协议失败，记录证据并终止；协议不匹配不要进入执行器。
- TEST_ASSERTION：断言失败，复现并确认规范正确后可修复。
- TEST_COLLECTION / ENVIRONMENT：导入、收集或环境错误，修复环境后新任务重跑，禁止冒充产品缺陷。
- TIMEOUT / SECURITY：超时或策略拒绝，终止执行并保存截断证据。
- PATCH_CONFLICT：源码哈希不符，重新读取和重新诊断，禁止盲目应用旧输出。
- REGRESSION：修复引入原有测试失败，应回滚本次补丁；保留证据后人工审查或新任务。
- BUDGET / INTERNAL：预算耗尽或内部异常，明确终止，不把空测试集或未运行状态标记为成功。

## 路径与执行安全边界

relative_path 拒绝绝对路径、盘符、反斜线、空段、点段、父目录、控制字符、Windows 保留名以及部分敏感路径。resolve_inside 进一步检查解析符号链接后的路径是否仍在仓库根目录。大小写不敏感的重复提案也被拒绝。这些只是必要检查，不能替代执行隔离。

执行器必须在独立临时工作区、低权限、禁用网络、无宿主凭据的沙箱中运行整个目标仓库和测试，限制 CPU、内存、进程、磁盘及 policy 超时/日志大小。使用预先批准的测试命令参数列表和 shell=False；不执行 Agent 生成的命令，不自动安装仓库要求的任意依赖或执行安装钩子。依赖预先审核并固定版本。

写入前检查路径、白名单、全部文件当前哈希及链接，再事务应用；拒绝符号链接和Windows junction/reparse point。当前实现要求调度器独占、无并发写者的临时副本，不能把路径复查当作恶意并发替换下的文件系统保证。失败恢复字节，回滚冲突明确终止。模型不能要求提交仓库、推送或读取宿主目录；执行器只挂载独立只读快照，详细权限与资源边界以P03/P05验收记录为准。

## 五个项目评估

每个项目记录 ProjectEvaluation：唯一 project_id、固定 revision、新增用例数、原有测试 baseline、最终 full-suite TestRun、人工确认缺陷数、确认修复数和总耗时。validate_five_projects 要求恰好五个不同项目，不允许以五次运行冒充五个项目。项目选择需符合老师指定范围；未提供范围时，由双方明确语言、规模、许可证和依赖后确定。

每个项目固定环境、随机种子、模型与提示版本、预算；保存生成前后测试、补丁、完整执行命令参数、脱敏日志和快照摘要。新增用例数按收集到的测试项计数，不能按文件数。修复数按已确认缺陷计数，不能按通过断言数。覆盖率若采集，应统一工具、配置和源文件范围，分别报告前后值；本契约不要求覆盖率工具。汇总测试通过率、确认修复率、耗时和失败原因，分母为零时写 N/A。未完成的项目也记录实际失败结果，不制造成功数据。

## 与 LangGraph / JSON 对接

可用包含 `snapshot: WorkflowState` 的 TypedDict 作为图状态，每个串行节点返回 `{"snapshot": new_snapshot}`；只允许调度节点调用 advance，避免两个 Agent 并行覆盖同一快照。不要把本 reducer 当作 LangGraph 的列表拼接 reducer。

当前是同进程 Python 契约，不提供 JSON 自动反序列化。跨进程接入时用显式 schema（例如 Pydantic v2）重建对象、枚举和 tuple，拒绝未知字段，检查 CONTRACT_VERSION 与输入内容路径/大小/完整性；不要直接用未经验证的 dict 或 pickle。dataclasses.asdict 可用于导出快照，但不等于安全的输入解析器。

## P07 本机任务中心

在项目根目录启动（Python 3.11+，无额外Web依赖）：

```console
python -m web.server --port 8765
```

打开 `http://127.0.0.1:8765`。默认演示模式无需密钥及Docker，页面、下载和结果都标注模拟证据。可提交三种冻结边界任务、查询状态、取消、查看测试/日志/诊断/补丁差异、下载JSON。SQLite和产物保存在忽略的 `artifacts/web`。一次一个任务，排队上限10。取消在阶段边界生效，不保证中断在途模型请求；任务重启后显示interrupted，不自动重试收费调用。当前轮询状态为任务级，详细执行日志在任务返回后展示，不提供实时容器日志流。

真实模式使用本机 `.env` 和已审核的不可变本地Docker镜像：

```console
python -m web.server --port 8765 --enable-real --image-id sha256:<64位实际镜像ID>
```

`<64位实际镜像ID>`须替换。真实模式复用P06 `run_case` 和既有调用账本，会产生模型费用。首期只支持三个冻结任务，不支持浏览器上传任意仓库；覆盖率在P06B完成前显示无数据。仅监听127.0.0.1，必须使用该地址而非localhost访问。用户身份认证、远程部署、多用户权限不在本期范围。不要将本服务代理到公网。

同一数据目录一次只允许一个服务。异常退出留下server.lock时，先确认旧进程已停止再删除该锁。测试：`python -m unittest discover -s tests -v`，HTTP测试需要允许本机回环监听。P07实际验收证据和限制见 `阶段7_验证与交接.md`。

### P06完成后的P07补充

Web已同步P06最终接口，支持 `--model deepseek-v4-flash-ascend` 显式覆盖授权模型、`--feedback` 执行覆盖率反馈子任务（参数与 `--enable-real` 同用）。不修改 `.env`。模型授权列表校验和阶段边界取消沿用P06，父任务成功后才创建反馈子任务。

无Docker或密钥时可查看仓库内已保存的P06真实证据：

```console
python -m web.server --port 8766 --evidence --data artifacts/web-evidence
```

打开 `http://127.0.0.1:8766`，可查看三项历史真实任务及一个反馈子任务、实际生成测试、诊断与diff、覆盖率分子/分母和百分点、父子任务关联，下载含证据包SHA256的JSON。该模式只读，不启动模型/容器、不接受提交，不声称本机重新运行。历史日志留在原开发机，页面不会据其绝对路径读取本机文件。原有“覆盖率待P06B”的首版说明已由本段取代，缺失覆盖率仍显示无数据，零分母N/A。反馈前后均满覆盖时显示0个百分点。

真实运行示例（先准备本机审核镜像及 `.env`）：

```console
python -m web.server --port 8765 --enable-real --model deepseek-v4-flash-ascend --feedback --image-id sha256:<本机64位实际镜像ID>
```

若已有首版服务，请先Ctrl+C停止后再用新代码启动；不要将另一个电脑上的镜像ID误认为本机已有镜像。


P07真实Web验收已获用户接受。当前分支feature/experiments-p08，P08A已于2026-11-10获用户“接受”验收；P08B正式30任务尚未开始。总进度见`项目计划与进度.md`，真实结果、排除和失败历史见`阶段8_验证与交接.md`。

五项目审核回归及来源/固定依赖校验通过；资源库存、确定性函数上下文与可信回填已实现。非正式MarkupSafe B1/B2/A0真实修复含保留22/35/39通过，第二轮覆盖率无增量。不能将单函数预跑写成五项目正式实验结论。Windows132项127通过5跳过；Linux核心116通过1跳过，Web由Windows完整回归覆盖。

无需模型的入口：

```powershell
python -m experiments.preflight --export artifacts/p08a-preflight-new.json
python -m experiments.pilot
python -m experiments.admission
```

`admission`默认只核对冻结工作副本并预览来源。真实项目准入公共清单在`fixtures/p08a-project-admission.json`，评估侧清单在`validation/p08a-project-admission.json`；评估内容不进入模型。仅支持P08A准入，不是正式任务冻结器。

保存提案免费重放（需本机拥有下列已审镜像和固定reference副本）：

```powershell
python -m experiments.admission --replay evidence/p08a-markupsafe-admission.json --group B1 --image-id sha256:a150efa0a707a0bac2ed4ca74df6f5dad4fa9a8cfdf6b9d50ce60c6281d20df2 --artifacts artifacts/p08a/my-replay --export artifacts/p08a/my-replay.json
```

导出路径必须全新，已有campaign拒绝重用；不会自动收费重跑。新真实调用须显式`--call`，使用本机.env和累计账本。Git不会同步.env、参考克隆、下载wheel、Docker镜像或本地artifacts；搭档需按`experiments/projects.json`取得固定tag/SHA。当前证据按Windows CRLF字节冻结，复现克隆使用仓库局部`--config core.autocrlf=true`并核对盘点摘要。镜像可由持有者导出/导入同一审查镜像；依赖锁及离线构建说明见阶段8交接，另一机器是否可直接复现由P09干净环境验证，尚未宣称通过。


2026-11-10最新：P08B实施计划已确认；T1正式任务清单与统一审核库存已于2026-11-10获用户“接受”验收。接口为`experiments.formal_tasks.load_formal_tasks(...)`与`experiments.upstream.build_reviewed_inventory(...)`，格式见`specs/P08B-任务契约.md`。加载只校验输入，不调用模型、不执行目标代码，也不证明候选缺陷成立。Windows135通过5跳过、Linux核心124通过1跳过；Jinja/dateutil新执行摘要与P08A相同。正式清单尚未冻结，无正式任务JSON可直接批量运行；不要把P08A准入清单传入正式加载器。详见`evidence/p08b-t1-verification.json`。


P08B T2首池结果：固定30候选中10合格、20被原有测试检出；140次无模型候选容器完成，正式配额还差20，尚未冻结或启动正式实验。见[候选筛选与缺口](阶段8_候选筛选与缺口.md)。筛选入口`python -m experiments.screening --export <全新导出路径> --artifacts <全新产物目录>`需要本机固定reference副本、已审wheel数据与722765…镜像；本次已有目录拒绝复用。重跑不增加独立函数数，扩池应先做新版本语义审核并提交，保留原排除历史。


P08B T2第二池已完成：累计24个不同合格函数，按五项目三类配额可填22/30槽，还差8；正式冻结/模型实验尚未开始。见[第二批筛选与累计缺口](阶段8_第二批筛选与累计缺口.md)。第二池入口为`python -m experiments.screening --pool validation/p08b-candidates-v2.json --export <全新路径> --artifacts <全新目录>`，已有运行禁止覆盖。语义否决仅记档不执行；原池和全部排除保留，不能靠重跑或改类别凑数。累计证据为`evidence/p08b-screening-cumulative-v2.json`；Windows143通过5跳过、Linux核心132通过1跳过。


P08B T2第三池完成：累计29个不同合格函数，但按固定项目/类别只可填25/30槽，还差ItsDangerous3、dateutil2。见[第三批筛选与累计缺口](阶段8_第三批筛选与累计缺口.md)及`evidence/p08b-screening-cumulative-v3.json`。候选代码入口不变，第三池索引为`validation/p08b-candidates-v3.json`；原输入/排除保留，复验版本不重复算函数，仍未正式冻结或调用模型。


2026-11-11第四池：先提交7828857，独立静态批准2项。首次两个reference容器启动exit125且0测试，故障原因未确证；引擎/镜像/空挂载随后检查通过，保留初始记录，新目录明确补跑。dateutil.ParserError.__str__完整8阶段合格，Signer.derive_key原回归检出3阶段，11次实际测试全部清理；初始两个失败容器后续无遗留。累计30不同合格函数，按配额26/30、还缺ItsDangerous边界2/异常1及dateutil异常1。旧记录/重复函数不双计，数据-only不冒称新跑旧单元；模型0、正式冻结0。证据evidence/p08b-screening-v4.json、p08b-screening-v4-initial-failure.json、p08b-screening-cumulative-v4.json。待用户确认继续补4槽，T3/P09未开始。


## 2026-11-11 第五池交付（待确认，缺2槽）

用户接受第四池并授权补缺。69b1bad预先固定4候选，review_p08b_t2_v5静态批准后运行，代码未改。两个空消息边界（HMACAlgorithm.get_signature及Signer.get_signature）各完整8阶段合格；tzstr保留测试在正确版本未抛错，reference_failed排除，不认定库违规；Signer.unsign变异在原回归产生29项混合失败，按保守environment_failed标签排除，不声称Docker故障。总21次真实测试，全部cleanup；21实际manifest逐文件核对，2合格双轮私有断言一致。没有重跑旧148/132代码测试，不冒记新跑。

累计75声明、71提议函数、32不同合格函数，但配额仅28/30；仍缺ItsDangerous异常处理1、dateutil异常处理1。所有旧声明/失败及第四池2次启动失败保留；累计389次完成的候选测试，另2次失败启动，模型0、正式冻结/运行0。证据evidence/p08b-screening-v5.json、p08b-screening-cumulative-v5.json及p08b-candidate-v5-review.json。下一步只补2槽，等待用户确认；T3未启动。


## 2026-11-11 第六池交付：T2配额30/30（待用户确认）

用户接受第五池并授权继续。50a6943预声明两异常候选，经review_p08b_t2_v6独立语义批准后执行：BadHeader.__init__丢original_error、_validate_fromutc_inputs错异常类型，均完整双轮8阶段合格。原回归每轮分别298通过、2032通过64跳过，缺陷版本仍通过；保留测试正确版通过、缺陷版稳定断言失败。16次真实容器全部清理，逐一核验实际库存/镜像/快照，两项完整私有失败身份双轮一致。代码未改，未重跑历史实现测试。

累计77声明/73提议函数/34不同合格函数，五项目每类2、各6，共30/30槽足额，4个同格备选。累计405次完成测试，另保留第四池2次启动失败；所有旧排除保留。模型调用0，正式冻结0/30、运行0/360，真实历史0。新证据evidence/p08b-screening-v6.json、p08b-screening-cumulative-v6.json、p08b-candidate-v6-review.json；完整命令、结果与限制见阶段8_第六批筛选与配额足额.md。

T2足额待用户确认；下一步T3选定30函数并互审冻结，当前未开始。P06独立确认待用户，P09未开始。


## 2026-11-12 T3冻结交付（待用户确认）

用户接受T2后进入T3。当前助手实施experiments/freeze.py及9项失败/拒绝测试；固定排序从34合格不同函数选30项，15格各2，4备选保留。公共/评估/协议/摘要分别为p08b-tasks.json、p08b-evaluation.json、p08b-protocol.json、evidence/p08b-freeze.json；冻结提交单独回执记录，无SHA自引用。独立review_p08b_freeze审核代码和30项语义无阻断，API入口证据局限及原docstring补充保留。

最终Windows157项152通过5跳过，Linux核心141通过1跳过（Web由Windows覆盖）；主助手静态复验240原始manifest、30对完整故障身份，60离线实际消息隔离通过。没有重跑240容器或产生模型调用；正式运行0/360、真实历史0。Python实现哈希仅CRLF归一LF，JSON/源库存仍原字节。详细命令/限制/变更/证据见阶段8_正式任务冻结.md及evidence/p08b-t3-verification.json。

T3已交付待确认；T4批量调度未开始。P06独立确认仍待用户，P09未开始。


## 2026-11-12 T4调度交付（待用户确认）

用户接受T3后实施T4。新增campaign.py及调度/故障测试，runner只新增可选scope（旧默认不变）。360固定槽计划已预声明，正式目录未创建；核验T3的36份不变文件及明确的runner运行时增量，T3数据/协议/评估证据不改。防重复claim、内核锁、返回usage/未知、原子结果/摘要、取消/恢复/导出失败及独立supplemental attempt已实现；补跑不替换原三次主重复。

最终Windows177项172通过5跳过，Linux核心161通过1跳过（Web由Windows覆盖）；25联合专项+1敏感写入保护测试及完整回归通过。12槽非正式合成集成使用真实流程但模型/Docker均替身，24替身completion/105替身执行，恢复新增0/0，不算真实修复。review_p08b_campaign只读复核及运行时SHA核验无阻断。证据evidence/p08b-t4-verification.json、p08b-t4-review.json、p08b-t4-synthetic-integration.json、p08b-schedule.json。

T4待用户确认，正式运行0/360、真实模型调用0，T5未开始。T4检查入口`.venv/Scripts/python.exe -m experiments.campaign --verify`；T3原CLI保留严格冻结校验，当前runner增量应被其拒绝，不能冒称T3冻结被改写。详细命令、文件及限制见阶段8_批量调度与恢复.md。P06独立确认及P09仍保留原状态。


## T5正式360记录交付（待用户确认，有有效性问题）

固定360条主记录全部落盘（pending0/unknown0），启动2026-11-12 11:02:29 +08:00，最后落盘2026-11-12 21:18:47 +08:00；没有补跑、改变任务/提示/参数或重置账本。包装层请求284次，已知tokens 667155，未知4次保持null。原始成功/失败全部保留，技术verified不是语义确认。

已经独立核验两项需T6优先审查的执行问题：Jinja相同字节/镜像但收集入口不同导致结果并存差异；一例dateutil generated-only coverage缺14项、与全量门槛ValueError控制流强一致。未改变本批或泛化所有失败根因，详见p08b-t5-protocol-anomalies.json。不能直接把本批状态解释成可靠模型成功率。

导出360对齐JSON/CSV、实际执行索引/摘要、生成测试及全部patch，独立只读审查见evidence/p08b-t5-review.json；完整统计口径/命令/限制见阶段8_正式执行记录与交接.md。T5记录交付待确认，T6未开始；P06独立确认及P09维持原状态。
