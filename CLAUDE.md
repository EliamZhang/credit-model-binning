# CLAUDE.md — 本仓库的 AI 操作手册

> 面向 AI（Claude Code）的维护说明。人读 [README.md](README.md)（方法论），AI 读本文件（操作规范）。
> 所有操作完成后必须走"验证纪律"（见第 6 节），数值不一致不允许提交。

## 1. 项目是什么

信贷风控的**模型分分箱与策略阈值分析**仓库，配置驱动：

- 把模型分（高分高风险方向）在 Train 上 20 等频初分 → 自动合箱为 6~8 档（目标 7 档）→ OOT 验证 → 阈值策略 → 输出 Excel 报告；
- 支持笔数口径（cnt，主口径）与金额口径（amt）；
- 支持两模型交叉分析（matrix 全局交叉 / cond 条件子箱）；
- **新样本/新模型/新交叉 = 只改 configs/，不改 pipeline/**（除非用户明确要求改管线逻辑）。

## 2. 目录与数据位置

```
res/        # 输入数据（gitignored）：用户把 CSV 放这里，文件名必须与 configs 一致
out/        # 输出 Excel 与临时文件（gitignored）
configs/    # datasets.py（样本集）、models.py（模型）——扩展点
pipeline/   # 核心管线：settings（常量层）/ data_loading / risk_metrics / binning_cnt /
            #   strategy / monthly / reporting / orchestration / bin_amt / cross_analysis
scripts/    # 入口：bin_model.py、cross_models.py + 4 个快捷壳
docs/       # 全部报告 md 与参考文档
scr/        # 数据准备与核对工具
tests/      # 单元测试（19 例）
```

标准输入三件套（以老客为例，见 configs/datasets.py）：

1. `sample.csv`：样本底表（已完成申请；含 application_id/user_id/sample_datetime）；
2. `application_info.csv`：申请信息（申请时间、月份、duedate 标签、principal、审批状态）；
3. 模型分文件：每行一个申请一条分，列名即模型分数列（如 `aus_old_risk_bid_mltmodel_v1_2_v20260325_lgb_score`）。

## 3. 运行入口速查

```bash
# 单模型分箱（.venv 为项目虚拟环境，Windows 下用 .venv/Scripts/python.exe）
python scripts/bin_model.py --dataset laoke --model mlt --metric cnt     # mlt 笔数口径
python scripts/bin_model.py --dataset laoke --model mlt --metric amt     # mlt 金额口径
python scripts/bin_model.py --dataset laoke --model worthiness --metric cnt  # 价值模型
# 快捷壳（等价命令）
python scripts/bin_mlt_cnt.py
python scripts/bin_mlt_amt.py
python scripts/bin_worthiness_cnt.py
# 交叉分析
python scripts/cross_models.py --dataset laoke --model-a mlt --model-b worthiness --mode matrix
python scripts/cross_models.py --dataset laoke --model-a mlt --model-b worthiness --mode cond
python scripts/cross_mlt_wth.py   # 快捷壳（matrix）
# 测试与核对
python -m unittest discover tests
python scr/_verify_report_sync_mlt_cnt.py   # 重跑 mlt cnt 后必跑
python scr/_verify_report_sync_mlt_amt.py   # 重跑 mlt amt 后必跑
```

输出文件名规则：`out/<model.report_prefix>_YYYYMMDD.xlsx`；交叉用 `REPORT_PREFIXES`（scripts/cross_models.py）登记的历史前缀，新组合默认 `binning_cross_<a>_<b>_strategy_report`。

## 4. 新增样本集（如新客）操作步骤

1. **要数据**：向用户确认三张表（sample、application_info、模型分文件）的路径与字段口径，特别是 duedate 标签列、principal、审批状态字段是否同名；若无对应字段，先与用户确认口径映射，不要自行假设；
2. **放数据**：让用户把 CSV 放入 `res/`（或指定的 data_dir）；文件很大时提醒用户 res/ 已被 gitignore；
3. **写配置**：在 configs/datasets.py 复制一份（参照 xinke 模板）填 data_dir/sample_file/application_file/train_end_month/oot_start_month/incomplete_statuses，取消 TODO 注释；
4. **验方向**：用样本数据做十分位违约率检查（分数 decile → 3M30+ 率），确认 high_score_high_risk 取值；价值类模型把"低分=高价值"语义记入 value_semantics；
5. **试跑**：`python scripts/bin_model.py --dataset <key> --model <model> --metric cnt`，检查日志（样本量、月份切分、初始箱数、缺失量）与 Excel 01_总览；
6. **写报告**：参照第 7 节规范在 docs/ 写 md，数值一律从 Excel 取。

## 5. 新增模型操作步骤

1. **要数据**：向用户确认模型分文件的文件名、分数列名、方向（高分=高风险？）、策略约束是否需要调整；价值类模型确认"低分=高价值"语义；
2. **放数据**：模型分文件放入 `res/`；
3. **写配置**：在 configs/models.py 复制一份（参照 mlt），填 score_file/raw_score_col/score_col/initial_bin_col/final_bin_col/high_score_high_risk/strategy_config/report_prefix/cross_tag/display_short；列名保持唯一（不同模型不要共用 score_col）；
4. **验方向**：十分位违约率检查（最高分位 vs 最低分位的 3M30+），不一致要停下来问用户；
5. **跑分箱**：`python scripts/bin_model.py --dataset <d> --model <m> --metric cnt`；评审方案与阈值后把最终方案记入模型配置注释（供交叉分析 `_current_thresholds` 登记）；
6. **交叉**：需要与其它模型交叉时按第 6 节。

## 6. 新增交叉组合操作步骤

1. `python scripts/cross_models.py --dataset <d> --model-a <a> --model-b <b> --mode matrix|cond`；
2. 现行阈值档位在 `pipeline/cross_analysis.py` 的 `_current_thresholds` 登记（老客 mlt×worthiness 已登记）；
3. 输出前缀按第 3 节规则登记到 `scripts/cross_models.py` 的 `REPORT_PREFIXES`；
4. 数值解读前先确认两模型方向一致（都按高分高风险参与交叉）。

## 7. 验证纪律（最重要）

1. **任何改动后必跑**：`python -m unittest discover tests`（19 例全绿）；
2. **碰了 pipeline 或 configs 后必回归**：重跑 `scripts/bin_mlt_cnt.py` + `scr/_verify_report_sync_mlt_cnt.py`（961 个数值单元）、`scripts/bin_mlt_amt.py` + 金额核对（940 个单元）；价值模型/交叉场景对比冻结关键值（见第 8 节）；
3. **报告数值禁止手抄**：md 报告里的数字必须来自 Excel（用 openpyxl 读值或脚本生成），写完与 Excel 逐项核对；
4. **OOT 纪律**：OOT 不参与任何分箱/合箱/阈值选择；所有方案只在 Train 上定；
5. **提交纪律**：验证全绿才提交；提交信息用中文、说明改动与验证结果；用户未要求不提交。

## 8. 冻结回归基准（老客，改动后必须与此一致）

| 场景 | 关键值 |
| --- | --- |
| mlt cnt | 7 档 `[(1,1),(2,4),(5,8),(9,11),(12,15),(16,19),(20,20)]`，自动 0.0803750459943264，接纳 0.1821580944836785，PSI 0.0054 |
| mlt amt | 7 档 `[(1,1),(2,4),(5,10),(11,13),(14,17),(18,19),(20,20)]`，自动 0.0494555109039948，接纳 0.1411377275703105，PSI 0.0061 |
| 价值模型 cnt | 7 档 `[(1,1),(2,4),(5,8),(9,13),(14,16),(17,19),(20,20)]`，自动 0.1362170673263007，接纳 0.1863252117841281，PSI 0.0084，缺失 21,914 笔（6.68%） |
| 交叉 matrix | Pearson 0.5938；AND（mlt ≤ E 且 wth ≤ C）接纳 37.54% / 风险 5.74%；四象限：双低 37.54%、仅 mlt 低 37.86%、仅价值低 2.46%（22.74%）、双高 22.14% |
| 交叉 cond | 21 格 A1–G3；G 档内子箱 35.09% / 39.60% / 45.87%；组合分布 PSI 0.0098；IV 0.6993 → 0.7017 |

## 9. 关键业务口径提醒

- **风险方向**：两模型均为高分高风险（`high_score_high_risk=True`）；低分=低风险，A 档 = 最安全；
- **价值语义**：价值模型"低分 = 高价值"（低分 = 高收入/高利息贡献，老客验证 corr 约 −0.42/−0.37）；A 档同时是"低风险 + 高价值"；价值 ≤ C 但 mlt > E 的 2.46% 人群是"价值好 & 风险差"错配客群（风险 22.74%），价值模型不可单独上线；
- **缺失分**：价值模型缺失 21,914 笔（无银行交易数据），按拒绝处理；mlt 缺失 0 笔；
- **提额场景**：走双低象限（mlt ≤ E 且 wth ≤ C），风险由 mlt 把关、额度由价值（收入代理）支撑；头部子箱风险拉不开但价值维度显著；
- **条件子箱**：分层能力只在 mlt 高风险档/拒绝段；沿子箱轴收紧接纳只丢流量不降险（7.26%→7.24%→7.23%），正确用途是拒绝段处置排序；
- **区间规则**：(left, right]，阈值不取整，线上缺失分按拒绝。

## 10. 常见坑

- **Windows 控制台 GBK 乱码**：运行脚本的输出在日志里可能乱码，不影响结果；核对数值用 openpyxl 读 Excel，不要解析控制台输出；
- **Python 路径**：项目用 `.venv/Scripts/python.exe`；scripts/ 下的脚本入口会自行把项目根目录加入 sys.path；
- **pipeline 的 settings 注入机制**：动态常量（SCORE_COL 等）由 `settings.sync()` 刷入各模块全局；新增函数若使用这些常量，写裸引用即可，但**不要**在函数默认参数里引用动态常量（import 时会被冻结），需要时用 `None + 函数体内解析` 模式（参考 risk_metrics.calc_bin_stats）；
- **等频初分可能不足 20 箱**：分数唯一值不足时 qcut duplicates=drop，少于 6 箱会报错——遇此情况先与用户确认分数分布；
- **金额口径与笔数口径不可混用**：bin_amt 的 21 个差异函数按调用传递闭包保留，新增差异函数时注意其内部裸调用的归属模块；
- **报告里不写未经验证的结论**：所有结论必须有对应数值支撑，来源注明（Train/OOT、笔数/金额口径）。
