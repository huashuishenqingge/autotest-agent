# 项目十一 AutoTest Agent：两人共享接口 v1.0

## 团队协作与进度入口

采用Spec驱动协作，先阅读 [Spec入口](specs/README.md)、[需求规格](specs/需求规格.md)、[接口契约](specs/接口契约.md)和当前阶段 [P03规格](specs/P03-隔离执行.md)。v0.1已获用户确认，不表示已有实现均通过验收。

请先阅读 [项目计划与进度](项目计划与进度.md)，其中包含阶段计划、两人分工、任务依赖、验收标准、当前状态和交接记录。每次领取或完成任务都必须同步更新该清单；每阶段交付后等待用户确认。智能体接手同时遵守 [AGENTS.md](AGENTS.md)。环境与方案依据见 [阶段1_技术方案与验收](阶段1_技术方案与验收.md)。

Python 3.11+，仅标准库，无安装依赖。使用 frozen dataclass + Protocol；不依赖 Pydantic 或 LangGraph。本包交付可运行的共享协议与校验逻辑，不包含真实 LLM、仓库解析器、测试执行器和自动修复实现。老师的完整题目未提供，按已知要求覆盖“仓库解析、测试生成、缺陷定位与修复、五个项目评估”。

## 快速开始

### 隔离执行与补丁应用（阶段3，真实容器验收待完成）

`execution.proposals.apply_proposal`在调度器独占临时副本中校验并应用提案，返回含diff及原始字节的回滚凭据；`rollback`会先检查文件未被后续修改。禁止在共享、并发写入的目标目录使用。

`execution.docker_runner.run_tests`只使用审核后的不可变本地镜像ID运行固定pytest命令，缺少Docker时返回环境失败，绝不在宿主执行测试。当前仅支持清单内Python文件；不自动安装目标依赖。构建、运行步骤及限制见 [阶段3验证与交接](阶段3_验证与交接.md)。本机WSL尚未安装，当前不能宣称容器执行已验证。

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

写入时重新执行路径与哈希检查，拒绝符号链接及 Windows junction/reparse point；使用禁止跟随链接的文件打开方式或受控文件系统服务，防止检查与写入之间路径被替换。检查源文件白名单和所有提案后再事务式应用；失败全部回滚。禁止提交仓库、推送、读取宿主目录。禁止输出秘密信息。默认策略是预算配置，不是操作系统沙箱实现。本包不提供执行器，接入前必须实现上述安全边界。

## 五个项目评估

每个项目记录 ProjectEvaluation：唯一 project_id、固定 revision、新增用例数、原有测试 baseline、最终 full-suite TestRun、人工确认缺陷数、确认修复数和总耗时。validate_five_projects 要求恰好五个不同项目，不允许以五次运行冒充五个项目。项目选择需符合老师指定范围；未提供范围时，由双方明确语言、规模、许可证和依赖后确定。

每个项目固定环境、随机种子、模型与提示版本、预算；保存生成前后测试、补丁、完整执行命令参数、脱敏日志和快照摘要。新增用例数按收集到的测试项计数，不能按文件数。修复数按已确认缺陷计数，不能按通过断言数。覆盖率若采集，应统一工具、配置和源文件范围，分别报告前后值；本契约不要求覆盖率工具。汇总测试通过率、确认修复率、耗时和失败原因，分母为零时写 N/A。未完成的项目也记录实际失败结果，不制造成功数据。

## 与 LangGraph / JSON 对接

可用包含 `snapshot: WorkflowState` 的 TypedDict 作为图状态，每个串行节点返回 `{"snapshot": new_snapshot}`；只允许调度节点调用 advance，避免两个 Agent 并行覆盖同一快照。不要把本 reducer 当作 LangGraph 的列表拼接 reducer。

当前是同进程 Python 契约，不提供 JSON 自动反序列化。跨进程接入时用显式 schema（例如 Pydantic v2）重建对象、枚举和 tuple，拒绝未知字段，检查 CONTRACT_VERSION 与输入内容路径/大小/完整性；不要直接用未经验证的 dict 或 pickle。dataclasses.asdict 可用于导出快照，但不等于安全的输入解析器。
