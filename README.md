# TaxonForge · 版本化分类与历史重分类

管理分类树及规则的不可变版本，根据记录发生时刻应用有效规则，解释分类结果，并比较分类体系变化对历史记录的影响。

## 已实现

- 分类树父节点验证、环检测、编码和别名全局唯一。
- 分类节点和规则有效期为 `[from, until)`；整个祖先链都有效时目标分类才可用。
- 声明式 `all/any/not` 和 `eq/ne/in/contains/gte/lt/prefix` 规则，支持嵌套字段。
- 规则节点数和深度限制；缺字段和类型不兼容记录在解释中。
- 优先级决胜，同级规则命中不同分类返回明确 `conflict`，不会任意选一个。
- SQLite 保存不可变分类版本、活跃版本切换历史、输入及分类运行。
- 以原始输入回放新版本、只读影响比较、切回历史版本。
- 独立的术语提及标注接口（不参与规则分类），固定 Unicode 14.0.0 分段与匹配协议。

## 术语提及标注

`MentionAnnotator` / `TaxonomyService.annotate_mentions` 在长文本中标出已有分类节点的别名提及，只返回候选，不选择类别、不评估谓词、不写分类 run。

- **固定版本管线**：先按 UAX #29（Unicode 14.0.0，属性表固化在 `_ucd14_data.py`，不依赖运行时 `unicodedata` 版本）把**原文**切成扩展字素簇；对每个完整簇执行 `NFKC` 后 `casefold`，再拼接成检索视图；别名用同一管线生成检索键。解释器的 Unicode 版本不是 14.0.0 时直接拒绝。
- **原文位置**：所有 `start/end` 都是 Python 字符串码点半开区间，命中只能落在完整原文字素边界上，视图坐标单独保留为 `view_start/view_end` 供回查。词典 `é` 命中原文 `e`+组合尖音符的 `[0,2)`；`ffi` 命中单个连字 `ﬃ` 的 `[0,1)`；只有 `f` 的词条不能从该连字的展开结果截出伪区间。
- **候选与歧义**：不同字面别名规范化为同一键时合并为一次提及，候选节点按编码稳定排序并标记 `ambiguous`，不依赖词典输入顺序，不做语境消歧。
- **完整结果与不重叠视图**：`mentions` 给出全部重叠候选；`non_overlapping_view` 按“最大化覆盖码点数 → 最少提及数 → 区间与候选序列稳定裁决”全局求解（O(n log n)），不是左起最长。`abcdef` 上 `abcd/abc/def` 的视图选择 `abc`+`def`。
- **任意切分点**：文本按一次性字符串分块流喂入，切点可以在基本字符与组合符之间、连字或代理对附近。分块绝不独立规范化；字素边界未确定前不输出最终区间。任意双块/三块切分的结果与一次性输入逐字段一致。
- **显式预算**：声明单字素码点、每提及候选数、提及数、驻留文本码点与暂存字节上限，超限抛 `MentionBudgetError`，不截断、不丢候选、不静默拼接全文；索引内存（`index_bytes`）与自动机内存（`automaton_bytes`）单独报告。长文本在窗口可裁剪时可超过驻留上限继续处理。
- **版本绑定**：输出信封绑定原文摘要、taxonomy 摘要、词典摘要及 Unicode/匹配协议版本；标注只读取显式传入的版本，`activate` 切换不影响已绑定的候选集合。

```python
from taxonforge import MentionAnnotator

annotator = MentionAnnotator(document, strategy="aho")  # 或 strategy="scan"
for chunk in stream:                 # 切点任意，包括组合符/连字中间
    partial = annotator.add(chunk)   # 只返回边界已确定的提及
envelope = annotator.finish()        # mentions + non_overlapping_view + stats/cost
```

`cost` 同时报告 Aho-Corasick 多模式自动机的转移数/状态数和逐词扫描的探针数，结果在两种策略下逐字段一致；自动机随视图码点线性增长，扫描成本与词典规模成正比。CLI 提供 `annotate` 子命令，`--chunk-codepoints` 用于验证分块不变性，`--strategy scan` 切换参考实现。该管线仅属于标注接口，`Taxonomy.aliases` 的精确别名解析保持不变。

## 运行

Python 3.11+。在项目目录执行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m taxonforge.cli --db /tmp/taxonforge-demo.sqlite register examples/taxonomy.json
python3 -m taxonforge.cli --db /tmp/taxonforge-demo.sqlite classify examples/records.json
python3 -m unittest discover -s tests -v
python3 examples/smoke.py
```

样例中的相同业务属性，在 2026 年归入 `hightech`，在该节点尚未生效的 2025 年归入 `manufacturing`；另一个行业无匹配。端到端还验证新版本影响范围和切回旧版本后结果一致。

分类输出包含 `status/category/path/winners/explanations/version`。所有业务时间必须带时区。`name` 是不可变版本名；同名同内容可重复注册，同名不同内容会拒绝。内容哈希可作为精确版本引用。

## 模块与扩展

`rules.py` 是有界谓词解释器；`taxonomy.py` 管理分类结构和决胜；`service.py` 管理持久化、版本和回放；`cli.py` 承载操作。`_grapheme.py` 是固定 14.0.0 的 UAX #29 分段状态机（一次性与流式同机），`_ucd14_data.py` 是生成器固化的属性表，`mentions.py` 承载词典索引、Aho-Corasick/逐词扫描、候选合并与不重叠求解；`tools/generate_ucd14.py` 仅在升级 Unicode 数据时离线运行。

```python
from taxonforge import TaxonomyService
from taxonforge.rules import PredicateEngine

engine = PredicateEngine(operators={
    "suffix": lambda actual, expected: isinstance(actual, str) and actual.endswith(expected)
})
service = TaxonomyService("taxonomy.sqlite", engine=engine)
try:
    # doc 是声明 categories 和 rules 的 JSON 对象。
    version = service.register(doc)
    result = service.classify(records, version=version)
    replay = service.replay(result["run"], version=version)
finally:
    service.close()
```

增加自定义运算符后，所有读取版本的进程都需提供一致的运算符实现。也可替换存储适配，或在领域服务前添加审核接口。

## 语义和当前边界

一条记录最终最多有一个分类；同优先级、同目标的多条规则可以共同胜出。缺字段的叶谓词为 false，`not` 按布尔语义反转；需要区分缺失状态时应新增显式存在性运算符。影响比较关注状态、分类、路径和获胜目标，规则解释文字变化不会自动计作分类影响。

`activate` 切换后续运行的默认版本，不修改已有运行；历史重分类会创建新运行。当前不包含节点拆分迁移、审批流、规则编译索引或分布式批处理，回放输入与结果均以完整 JSON 保存，适合受控本地数据规模。
