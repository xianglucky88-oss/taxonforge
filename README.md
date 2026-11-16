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

`rules.py` 是有界谓词解释器；`taxonomy.py` 管理分类结构和决胜；`service.py` 管理持久化、版本和回放；`cli.py` 承载操作。

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
