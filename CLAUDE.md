# CLAUDE.md — 本仓库的 AI 操作手册

> 面向 AI（Claude Code）的维护说明。人读 [README.md](README.md)（方法论），AI 读本文件（操作规范）。
> 所有操作完成后必须走"验证纪律"（见第 6 节），数值不一致不允许提交。

## 1. 项目是什么

信贷风控的**模型分分箱与策略阈值分析**仓库，配置驱动：

- 把模型分（高分高风险方向）在 Train 上 20 等频初分 → 自动合箱为 6~8 档（目标 7 档）→ OOT 验证 → 阈值策略 → 输出 Excel 报告；
- 支持笔数口径（cnt）分箱与策略阈值分析（1M30+/3M30+ 金额逾期率为报告参考列）；
- 支持两模型交叉分析（matrix 全局交叉）；
- **新样本/新模型/新交叉 = 只改 configs/，不改 pipeline/**（除非用户明确要求改管线逻辑）。

## 2. 目录与数据位置

```
res/        # 输入数据（gitignored）：用户把 CSV 放这里，文件名必须与 configs 一致；
            #   命名前缀约定：老客 old_*、新客 new_*，新数据文件不要与既有文件重名
out/        # 输出 Excel 与临时文件（gitignored）
configs/    # datasets.py（样本集）、models.py（模型）——扩展点
pipeline/   # 核心管线：settings（常量层）/ common / data_loading / risk_metrics / binning_cnt /
            #   strategy / monthly / reporting / orchestration / cross_analysis
scripts/    # 入口：bin_model.py、cross_models.py、check_data.py + 3 个快捷壳
docs/       # 全部报告 md 与参考文档
scr/        # 数据准备、报告生成与核对工具
tests/      # 单元测试（28 例）
单变量分析/ # 拒付规则（BR05）策略迭代与收益/损失回测文档（dishonour_rule_report + BR05_gain_loss_analysis）
```

标准输入三件套（以老客为例，见 configs/datasets.py）：

1. `old_sample.csv`：老客样本底表（已完成申请；含 application_id/user_id/sample_datetime）；
2. `old_application_info.csv`：老客申请信息（申请时间、月份、duedate 标签、principal、审批状态）；
3. 模型分文件：每行一个申请一条分，列名即模型分数列（老客 mlt 为 `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score`，文件 `old_mlt_score.csv`；价值模型文件 `old_worthiness_score.csv`）。

运行环境：Python 3.11（Windows），虚拟环境 `.venv`（解释器 `.venv/Scripts/python.exe`）；依赖首次安装用 `.venv/Scripts/python.exe -m pip install -r requirements.txt`。

✅ **new 是新客已填实配置**：configs/datasets.py 中 `new` 配置与数据（`res/new_*`）已入库、可直接运行；新增样本集时参照 `new` 配置复制一份，按第 4 节流程操作。

## 2.1 数据字段口径清单（拿到新数据先核对）

宽表必须存在以下字段（缺哪个先问用户怎么补，不要自行造口径）：

| 字段 | 口径 |
| --- | --- |
| application_id / user_id | 主键（分箱按 application_id 行数计 n） |
| application_time / application_month | 申请时间；month 为 YYYY-MM 字符串，Train/OOT 按字符串比较切分 |
| score 列（模型分） | 数值型；缺失行剔除并计入总览"缺失量" |
| duedate_1m_30 / duedate_3m_30 | 标签：∈{0,1} 为成熟，=1 为坏（30+ 逾期）；成熟定义=在库满 1/3 个月 |
| principal | 本金，金额逾期率敞口分母 |
| estimate_principal_remaining_mob1 / mob3 | 逾期剩余本金（金额逾期率坏样本分子） |
| dpd_days_ever_mob1 / mob3 | ≥30 判定金额逾期（金额逾期率口径） |
| status / application_status / assessment_status | 历史实际审批漏斗（通过=首字符 3/4；Auto/Manual Approved；成交=Active_Account/Closed/Blocked） |

新数据若字段名不同，先在 configs 或与用户确认映射；若连标签都没有，先停下说明"无法做有监督分箱评估"。

## 2.2 Excel 取数地图（写报告时按此找数）

- **单模型分箱 Excel**（6 sheets）：`01_总览`（方案/阈值/PSI/AUC/KS/漏斗速览）→ `02_分箱详情`（合箱过程/候选/步骤）→ `03_最终分箱统计`（Train/OOT 分箱大表，含 CI 与 Lift）→ `04_策略方案`（历史漏斗/测算流量/阈值选择/敏感性/分段）→ `05_模型验证`（对比/AUC/KS/PSI/单调性/月度）→ `06_附录`（配置/上线规则/指标字典）；
- **交叉 matrix Excel**（7 sheets）：`01_总览`（相关性与方案）→ `02/03_交叉矩阵_Train/OOT`（7×7 每格 14 指标，含历史实际自动审批通过率列——老客自 20260904 重跑版起才有）→ `04_条件增量分析` → `05_组合评分效果` → `06_二维策略模拟`（策略对照/AND 网格/四象限）→ `07_附录`。收入矩阵只存在于报告 md（Excel 无收入列），写数时按脚本重算口径（见下）。

取数一律用 openpyxl 读值（`data_only=True`），不要从控制台输出抄数。

**docs/ 报告地图**（改动/核对前先定位数值来源与维护方式，完整清单见 README.md 十.6）：

| 报告（docs/） | 数值锚 | 维护方式 |
| --- | --- | --- |
| 分箱_老客_mlt_笔数.md / 分箱_老客_价值_笔数.md | 老客单模型 Excel（页头锚定日期版本） | `scr/_gen_new_reports.py` 生成（2026-09-07 起由手工迁移至生成器，改文案改生成器重跑）；mlt cnt 用 `scr/_verify_report_sync_mlt_cnt.py`（980 单元）复核 |
| 交叉_老客_mlt_价值.md | 老客交叉 Excel + res/old_*.csv 重算 | 同上（收入/自动审批矩阵由生成器内建断言每次渲染现算核对） |
| 分箱_新客_mlt_笔数.md / 分箱_新客_价值_笔数.md | binning_new_*_strategy_report | 同上（模板与老客同构，生成器内建断言含新客分档边界） |
| 交叉_新客_mlt_价值.md | binning_new_cross_strategy_report | 同上 |

口径提醒：交叉报告（老/新客）收入矩阵为**平均数**三口径。

## 2.3 新数据质量检查与交互协议（数据入库后的第一道工序）

用户放入新样本/新模型数据后，**先跑检查脚本，不要直接跑分箱**：

```bash
python scripts/check_data.py --dataset <d> --model <m>
```

检查项：文件可读性（行/列数）、必需字段存在性（对照 2.1 清单）、主键唯一性、标签/敞口类字段缺失率（缺失 = 未成熟样本属正常，>90% 才报警）、**样本口径**模型分覆盖率与缺失率（与管线 01_总览口径一致）、分数取值范围与十分位 3M30+ 方向、Train/OOT 样本量与 Train 标签成熟率。报告同时写入 `out/data_check_<d>_<m>_YYYYMMDD.txt` 供追溯。

**分级处理协议（必须遵守）**：

| 级别 | 触发条件（示例） | AI 必须做什么 |
| --- | --- | --- |
| BLOCK | 缺必需字段；覆盖率 <80%；样本口径缺失率 >20%；方向与预期相反 | **停下**，向用户列出问题清单，等用户补数据/确认口径后重跑检查 |
| WARN | 覆盖率 80%–95%；缺失率 5%–20%；方向倒挂 ≥3 处；Train 成熟率 <35%；主键重复率偏高 | 报告数值与影响，问用户"继续（记录在案）还是补数据"，按用户决定执行 |
| PASS | 全部通过 | 一行摘要报告即可，继续后续流程 |

已知结构性 WARN 直接引用说明、不重复追问：老客价值模型覆盖率 93.32%、缺失 6.68%（无银行交易数据人群）；Train 3M30+ 成熟率基线约 42%。

**交互格式**：报告问题用"问题 → 影响 → 需要你做什么"三段式；需要用户补数据时给出明确清单（文件名、字段、格式、放哪）。检查通过或用户确认继续后，再进入第 4/5 节的配置与分箱流程。

## 3. 运行入口速查

```bash
# 单模型分箱（.venv 为项目虚拟环境，Windows 下用 .venv/Scripts/python.exe）
python scripts/bin_model.py --dataset laoke --model mlt     # mlt 笔数口径
python scripts/bin_model.py --dataset laoke --model worthiness  # 价值模型
python scripts/bin_model.py --dataset new --model new_mlt       # 新客（新样本集参照此例）
# 快捷壳（等价命令）
python scripts/bin_mlt_cnt.py
python scripts/bin_worthiness_cnt.py
# 交叉分析
python scripts/cross_models.py --dataset laoke --model-a mlt --model-b worthiness --mode matrix
python scripts/cross_mlt_wth.py   # 快捷壳（matrix）
# 测试与核对
python -m unittest discover tests
python scripts/check_data.py --dataset <d> --model <m>   # 新数据质量检查（2.3 节协议）
python scr/_verify_report_sync_mlt_cnt.py   # 重跑 mlt cnt 或改老客笔数 md 后必跑（980 单元）
python scr/_gen_new_reports.py                          # 报告生成器：渲染 --dataset 登记的全部组合
                                                        #   （默认 new；老客加 --dataset laoke，两者覆盖 6 份 md）
                                                        #   [--date YYYYMMDD] [--out-dir]
                                                        #   数值从 Excel/res 现算；跑管线/改模板文案后重跑
```

输出文件名规则：`out/<model.report_prefix>_YYYYMMDD.xlsx`；交叉用 `REPORT_PREFIXES`（scripts/cross_models.py）登记的历史前缀，新组合默认 `binning_cross_<a>_<b>_strategy_report`。

## 4. 新增样本集（如新客）操作步骤

1. **要数据**：向用户确认三张表（sample、application_info、模型分文件）的路径与字段口径，特别是 duedate 标签列、principal、审批状态字段是否同名；若无对应字段，先与用户确认口径映射，不要自行假设；
2. **放数据**：让用户把 CSV 放入 `res/`（或指定的 data_dir）；文件很大时提醒用户 res/ 已被 gitignore；
3. **跑检查**：数据到位后先写临时配置，跑 `python scripts/check_data.py --dataset <key> --model <model>`，按 2.3 节协议处理 BLOCK/WARN，向用户报告并等确认；
4. **写配置**：在 configs/datasets.py 复制一份（参照 new 模板）填 data_dir/sample_file/application_file/train_end_month/oot_start_month/incomplete_statuses；
5. **验方向**：检查脚本已含十分位方向验证；价值类模型把"低分=高价值"语义记入 value_semantics；
6. **试跑**：`python scripts/bin_model.py --dataset <key> --model <model>`，检查日志（样本量、月份切分、初始箱数、缺失量）与 Excel 01_总览；
7. **登记并生成报告**：在 datasets.py/models.py 对应配置补 report_meta（报告名/md 名/单模型与交叉清单）与 report_notes（本次运行的叙事事实），跑 `python scr/_gen_new_reports.py --dataset <key>`（未登记组合先 `--out-dir` 临时目录评审）——md 结构与格式按第 7.1 节模板，数值一律从 Excel/res 现算（生成器内建断言核对）。

## 5. 新增模型操作步骤

1. **要数据**：向用户确认模型分文件的文件名、分数列名、方向（高分=高风险？）、策略约束是否需要调整；价值类模型确认"低分=高价值"语义；
2. **放数据**：模型分文件放入 `res/`；
3. **跑检查**：`python scripts/check_data.py --dataset <d> --model <m>`（分数文件到位后立即跑），按 2.3 节协议处理 BLOCK/WARN；
4. **写配置**：在 configs/models.py 复制一份（参照 mlt），填 score_file/raw_score_col/score_col/initial_bin_col/final_bin_col/high_score_high_risk/strategy_config/report_prefix/cross_tag/display_short；列名保持唯一（不同模型不要共用 score_col）；
5. **验方向**：检查脚本的十分位方向验证结果为准，不一致要停下来问用户；
6. **跑分箱**：`python scripts/bin_model.py --dataset <d> --model <m>`；评审方案与阈值后把最终方案记入模型配置注释（供交叉分析 `_current_thresholds` 登记），并在该模型 `report_meta`/`report_notes` 登记本次运行叙事（decile/merge/dist/steps/cand_note，逐字对应最终评审结论）；
7. **交叉**：需要与其它模型交叉时按第 6 节。

## 6. 新增交叉组合操作步骤

1. `python scripts/cross_models.py --dataset <d> --model-a <a> --model-b <b> --mode matrix`；
2. 现行阈值档位在 `pipeline/cross_analysis.py` 的 `_current_thresholds` 登记（老客 mlt×worthiness 已登记）；
3. 输出前缀按第 3 节规则登记到 `scripts/cross_models.py` 的 `REPORT_PREFIXES`；
4. 数值解读前先确认两模型方向一致（都按高分高风险参与交叉）。

## 7. 验证纪律（最重要）

1. **任何改动后必跑**：`python -m unittest discover tests`（28 例全绿）；
2. **碰了 pipeline 或 configs 后必回归**：重跑 `scripts/bin_mlt_cnt.py` + `scr/_verify_report_sync_mlt_cnt.py`（980 个数值单元）；价值模型/交叉场景对比冻结关键值（见第 8 节）；
3. **报告数值禁止手抄**：md 报告里的数字必须来自 Excel（用 openpyxl 读值或脚本生成），写完与 Excel 逐项核对；
4. **OOT 纪律**：OOT 不参与任何分箱/合箱/阈值选择；所有方案只在 Train 上定；
5. **提交纪律**：验证全绿才提交；提交信息用中文、说明改动与验证结果；用户未要求不提交。

**提交前 DoD 清单**：

- [ ] `python -m unittest discover tests` 全绿
- [ ] 动过 mlt 管线 → cnt 核对 980 单元通过
- [ ] 动过价值模型/交叉 → 关键值与第 8 节基准一致（不一致要说明原因，且经用户确认）
- [ ] 动过生成器（scr/_gen_new_reports.py）或管线 → 重跑 `_gen_new_reports.py`（new + laoke 共 6 份）后 git diff 仅目标行；老客笔数 md 另跑 cnt verify 复核
- [ ] 报告 md 数值与 Excel 逐项一致（生成器内建断言全过；新报告按 7.1 模板与格式）
- [ ] `git status` 无遗漏文件；提交信息中文、含验证结果
- [ ] 推送前已征得用户明确同意（推送纪律）
- [ ] 分支工作流：改动在功能分支上做；提交后按既有惯例同步 staging / master（用户确认后执行）

## 7.1 报告模板与数值格式（docs/ 下新报告的规范）

报告模板由 `scr/_gen_new_reports.py` 实现（新模型/新样本沿用同结构，docs/ 现 7 份即其输出）；骨架为：

```text
# <报告名>（<模型><口径>）
> 数据范围/口径说明 + 一句话结论
## 一、结论摘要（6 条发现 + 核心指标总览表）
## 二、样本设计与指标定义（含方向验证）
## 三、分箱方案设计与结果（初分 → 合箱 → 候选 → 最终统计；三（二）含"约束条件与自动/人工对照"表与"自动与人工的差异"段，自动约束/策略上限/人工指定档位全部由配置与 settings 现算）
## 四、历史实际审批与模型策略测算结果（阈值 → 流量 → 敏感性 → 上线规范）
## 五、方案稳健性验证（单调性/PSI/AUC/KS/月度/分段）
## 六、附录：核心配置参数
```

交叉报告章三为 **22 组矩阵 × Train/OOT**：14 个业务指标矩阵（7×7 每格 n 精确一致；自动审批通过率 n<100 显示 "—"；数值读 Excel 02/03 sheet）+ 8 张收入口径矩阵（**平均数**口径：全样本 4 字段 + gross/net 剔除 <0 样本 + gross/net 成交样本，成交 = status 属 Active_Account/Closed/Blocked；收入无 Excel 列，数值由 res 重算并断言分档计数与 Excel 每格 n 一致）。

数值格式约定：逾期率 2 位小数百分比（含 CI 写 `1.73% [1.48%, 2.01%]`）；阈值全精度原文；AUC/KS/PSI/相关性 4 位小数；样本量千分位；Lift 2~4 位（与同表其余列一致）；pp 差写 `+0.0014` 式四位数。所有表格列名与 Excel 一致，不缩写含义。

## 7.2 协作协议（与用户交互的行为规范）

- **要数据时问具体问题**：列名/文件名/时间窗口/方向语义/是否剔除未完成申请，给出需要用户提供的清单，不要只说"给我数据"；
- **新数据先检查再动手**：数据到位后先跑 `scripts/check_data.py` 并按 2.3 节协议交互，BLOCK 必须停下等用户补数据，WARN 必须报告并取得用户决定；
- **遇异常停下来**：分数方向与预期相反、覆盖率异常低、方案与基准不一致、字段缺失——先向用户说明发现与影响，等确认再继续，不自行改口径；
- **修改既有报告数值时**：说明改动原因与差异来源（如实现统一导致的 0.0003 级差异），在提交信息中留痕；
- **不要主动改 configs 里的冻结值**（老客模型配置与第 8 节基准对应），除非用户要求；
- **推送纪律（最重要）**：任何 `git push` 之前必须先征得用户明确同意。项目 `.claude/settings.json` 已配置 PreToolUse hook 强制拦截 push 并弹确认框（双重保险），但 AI 本身也必须在执行 push 前主动说明"将要推送什么、推到哪里"并等待用户许可，不得因为 hook 弹出确认框就默认用户会点同意。

## 8. 冻结回归基准（老客，改动后必须与此一致）

| 场景 | 关键值 |
| --- | --- |
| mlt cnt | 7 档（手动 final_bin_ranges，2026-09-08 用户确认）`[(1,1),(2,4),(5,8),(9,11),(12,14),(15,18),(19,20)]`，自动 0.0803750459943264，接纳 0.161821271383099，PSI 0.0062 |
| 价值模型 cnt | 7 档 `[(1,1),(2,4),(5,8),(9,13),(14,16),(17,19),(20,20)]`，自动 0.1362170673263007，接纳 0.1863252117841281，PSI 0.0084，缺失 21,914 笔（6.68%） |
| 交叉 matrix | Pearson 0.5938；AND（mlt ≤ E 且 wth ≤ C）接纳 36.47% / 风险 5.53%；四象限：双低 36.47%、仅 mlt 低 33.82%、仅价值低 3.52%（20.59%）、双高 26.18%（24.51%）（20260908 交叉 Excel，mlt 新分箱口径） |

> 新客（new）关键值不冻结在本表，由生成器内建断言保证：分档边界渲染时读单模型 Excel 03 各档右边界（交叉报告附录 C 行即两模型 C 档右边界 mlt 0.1389779549508124 / 价值 0.1933179021763764，与 Excel 逐值断言一致）、双分样本量 Train 407,134 / OOT 129,382（总 536,516）。新客 mlt 2026-09-08 起为手动 7 档 `[(1,1),(2,4),(5,8),(9,10),(11,13),(14,17),(18,20)]`（E/F/G 尾部重组，PSI 0.0074；自动 0.08716503179896717 / 接纳 0.1389779549508124 阈值不变）；新客交叉报告 2026-09-08 起锚定重跑后的 20260908 Excel（新分箱口径）。

## 9. 关键业务口径提醒

- **风险方向**：两模型均为高分高风险（`high_score_high_risk=True`）；低分=低风险，A 档 = 最安全；
- **价值语义**：价值模型"低分 = 高价值"（低分 = 高收入/高利息贡献，老客验证 corr 约 −0.42/−0.37）；A 档同时是"低风险 + 高价值"；价值 ≤ C 但 mlt > E 的 3.52% 人群是"价值好 & 风险差"错配客群（风险 20.59%），价值模型不可单独上线；
- **缺失分**：价值模型缺失 21,914 笔（无银行交易数据），按拒绝处理；mlt 缺失 0 笔；
- **提额场景**：走双低象限（mlt ≤ E 且 wth ≤ C），风险由 mlt 把关、额度由价值（收入代理）支撑；
- **区间规则**：(left, right]，阈值不取整，线上缺失分按拒绝。

## 10. 常见坑

- **Windows 控制台 GBK 乱码**：运行脚本的输出在日志里可能乱码，不影响结果；核对数值用 openpyxl 读 Excel，不要解析控制台输出；
- **Python 路径**：项目用 `.venv/Scripts/python.exe`；scripts/ 下的脚本入口会自行把项目根目录加入 sys.path；
- **pipeline 的 settings 注入机制**：动态常量（SCORE_COL 等）由 `settings.sync()` 刷入各模块全局；新增函数若使用这些常量，写裸引用即可，但**不要**在函数默认参数里引用动态常量（import 时会被冻结），需要时用 `None + 函数体内解析` 模式（参考 risk_metrics.calc_bin_stats）；
- **等频初分可能不足 20 箱**：分数唯一值不足时 qcut duplicates=drop，少于 6 箱会报错——遇此情况先与用户确认分数分布；
- **收入口径跨文档区分**：老/新客交叉报告收入矩阵为**平均数**三口径（res 重算，无 Excel 列）；
- **全部报告 md 由生成器维护（老/新客 7 份同模板）**：scr/_gen_new_reports.py 输出的 md 不要手工改数值/格式——改生成器再重跑（老客 4 份自 2026-09-07 由手工迁移至生成器），提交前确认 git diff 仅目标行（生成器对未改动段落 byte 稳定）；
- **Excel 日期锚**：md 页头锚定具体日期版本 Excel（如 20260901）；重跑管线产出新日期文件后，需先做新旧两版逐格零漂移证明（openpyxl data_only 逐格比较）再更新 md 锚，不能直接换锚；
- **报告里不写未经验证的结论**：所有结论必须有对应数值支撑，来源注明（Train/OOT 与所用指标口径）。
