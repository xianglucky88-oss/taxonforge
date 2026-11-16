# TaxonForge · 版本化分类与历史重分类

管理分类树及规则的不可变版本，根据记录发生时刻应用有效规则，解释分类结果，并比较分类体系变化对历史记录的影响。审核与激活以**不可变执行制品**为对象：批准的不再是版本名称，而是同时固定 taxonomy、解释协议与 operator 内容的制品身份。

## 已实现

- 分类树父节点验证、环检测、编码和别名全局唯一。
- 分类节点和规则有效期为 `[from, until)`；整个祖先链都有效时目标分类才可用。
- 声明式 `all/any/not` 和 `eq/ne/in/contains/gte/lt/prefix` 规则，支持嵌套字段。
- 规则节点数和深度限制；缺字段和类型不兼容记录在解释中。
- 优先级决胜，同级规则命中不同分类返回明确 `conflict`，不会任意选一个。
- **制品身份**：规范化 taxonomy 文档 + 规则解释协议 + operator 受控版本/配置 + 内容 digest 的规范化 SHA-256；审核意见与批准事件只绑定该身份。
- **双人审核**：草稿规范化后需两名不同且非作者的审核者批准；撤回、拒绝、重复批准、批准过期都有明确结果。
- **激活门槛**：所有激活入口（`register` 默认激活、`activate`、CLI）经过同一事务门槛，携带 expected active 身份，失败不留下伪激活历史。
- **运行固定**：一次 run 的规则、解释和 operator 完整来自 run 开始时固定的制品，激活并发切换不会混入。
- SQLite 保存不可变制品、审核事件、活跃版本切换历史、输入及分类运行。
- 以原始输入回放新版本、只读影响比较、切回历史版本；历史（制品化之前）版本仍可读。

## 制品身份与审核

制品（`Artifact`）是审核与激活的唯一对象，其身份 digest 覆盖：

1. **规范化 taxonomy 文档**（canonical JSON，键排序、无空白、拒绝 NaN）；
2. **规则解释协议**（`PredicateEngine.PROTOCOL`，解释语义变更时升版）；
3. **operator 的受控版本与配置**（`OperatorRegistry` 中每个 operator 的 `version` 与 JSON `config`）；
4. **operator 内容 digest**（函数字节码、常量、默认值与闭包捕获的 JSON 内容）。

身份不取 Python 对象地址，也不只取版本名称。已发布的 `(name, version)` operator 内容不可变：以不同内容重复注册同一版本会被拒绝，调用方无法在审核后偷换实现。修改 taxonomy 内容**或** operator 版本/配置都会产生新身份，旧批准不会转移到新制品。

审核事件（`approve`/`withdraw`/`reject`/`retract`）只绑定制品 digest， append-only 存储：

- 两名**不同且非作者**的审核者批准后，制品才处于 `approved`；
- 作者不能批准自己的草稿；重复批准报 `DuplicateApproval`（撤回或过期后可重新批准）；
- 批准带有效期（默认 7 天，可按 TTL 或截止时间指定），过期批准不计入门槛；
- `withdraw` 撤回自己的批准；`reject`（非作者）与 `retract`（作者）使制品进入终态，审核关闭；
- 身份认证由受信调用环境提供（`identity` 参数 / `--identity` / `TAXONFORGE_IDENTITY`），服务只记录与校验身份，不实现通用权限后台。

## 激活门槛

`activate(digest, expected=...)` 在一个提交边界内完成：读取当前 active 并与 `expected` 比较（CAS）、检查制品当前持有有效批准、切换 active、追加激活历史。任何一步失败整体回滚：

- 两个进程从相同 expected 出发，最多一个成功（`BEGIN IMMEDIATE` 串行化写者，败者得到 `ActivationConflict`，且不追加伪激活历史）；
- 进程在提交前退出不会留下只有 active 或只有审批成功的半状态；
- `register` 的默认激活与 CLI 的 `activate` 都经过同一门槛；`expected` 缺省时取当前 active，CLI 可用 `--expected <digest|none>` 显式指定。

## 运行固定与兼容

`classify`/`replay` 在 run 开始时把制品固定下来（含冻结的 operator 函数表），整个 run 的规则、解释与 operator 都来自该制品；即使另一进程在 run 进行中途切换了 active，本次 run 的结果与记录仍完全属于开始时的制品。run 记录与每条结果的 `version` 字段都是制品 digest，可用存储的输入重放验证解释一致性。

历史兼容：制品化之前注册的 `versions` 行仍可按原 digest 读取、分类与回放（`load` 自动回退），但不能被激活——兼容模式不会把历史版本或新草稿自动视为获批；新草稿必须走完整审核。历史版本名保持不可变。

## 设计权衡

- **保存完整制品 vs 引用不可变对象**：数据库保存完整的制品描述（规范化文档、协议、operator 版本/配置/digest），可独立审计、可跨进程重建；operator **实现**（代码）无法序列化进 SQLite，因此按 `(name, version)` 从受控注册表解析并校验内容 digest——执行环境必须显式提供被审版本，提供不出或被篡改则拒绝加载（`OperatorMismatch`），而不是静默运行调用方改过的注册表。
- **长期冻结执行器 vs 按运行加载**：制品对象在构建时快照 operator 函数表，进程内 run 不回头读注册表；跨进程 `load` 每次按记录解析并校验，而不是长期缓存执行器。代价是每次 load 重建小规模执行器与重新规范化文档，换来的是调用方修改注册表后不会静默漂移——新草稿得到新身份，旧制品在匹配环境中行为不变。

## 运行

Python 3.11+。在项目目录执行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m taxonforge.cli --db /tmp/taxonforge-demo.sqlite register examples/taxonomy.json --no-activate --author release-author
python3 -m taxonforge.cli --db /tmp/taxonforge-demo.sqlite approve <digest> --by reviewer-a
python3 -m taxonforge.cli --db /tmp/taxonforge-demo.sqlite approve <digest> --by reviewer-b
python3 -m taxonforge.cli --db /tmp/taxonforge-demo.sqlite activate <digest> --expected none
python3 -m taxonforge.cli --db /tmp/taxonforge-demo.sqlite classify examples/records.json
python3 -m unittest discover -s tests -v
python3 examples/smoke.py
```

样例中的相同业务属性，在 2026 年归入 `hightech`，在该节点尚未生效的 2025 年归入 `manufacturing`；另一个行业无匹配。端到端还验证未获批草稿无法激活、新版本影响范围、过期 expected 冲突以及切回旧版本后结果一致。

分类输出包含 `status/category/path/winners/explanations/version`（`version` 为制品 digest）。所有业务时间必须带时区。`name` 是不可变版本名；同名同内容可重复注册，同名不同内容会拒绝。制品 digest 可作为精确版本引用。

## 模块与扩展

`rules.py` 是有界谓词解释器与受控 operator 注册表；`taxonomy.py` 管理分类结构和决胜；`artifact.py` 定义制品封装与校验；`service.py` 管理持久化、审核状态机、激活门槛和回放；`cli.py` 承载操作。

```python
from taxonforge import OperatorRegistry, PredicateEngine, TaxonomyService

registry = OperatorRegistry.builtins()
registry.register("suffix", lambda a, b: isinstance(a, str) and a.endswith(b),
                  version="1", config={})
service = TaxonomyService("taxonomy.sqlite",
                          engine=PredicateEngine(registry=registry),
                          identity="release-author")
try:
    digest = service.register(doc, activate=False)   # doc 是声明 categories 和 rules 的 JSON 对象
    service.approve(digest, "reviewer-a")
    service.approve(digest, "reviewer-b", ttl_seconds=3600)
    service.activate(digest, expected=None)          # 期望当前无 active
    result = service.classify(records)
    replay = service.replay(result["run"])
finally:
    service.close()
```

增加自定义运算符后，所有读取制品的进程都需在注册表中提供同一 `(name, version)` 的相同内容；内容 digest 不匹配会拒绝执行。也可替换存储适配，或在领域服务前添加审核接口。

## 语义和当前边界

一条记录最终最多有一个分类；同优先级、同目标的多条规则可以共同胜出。缺字段的叶谓词为 false，`not` 按布尔语义反转；需要区分缺失状态时应新增显式存在性运算符。影响比较关注状态、分类、路径和获胜目标，规则解释文字变化不会自动计作分类影响。

`activate` 切换后续运行的默认版本，不修改已有运行；历史重分类会创建新运行。operator 内容 digest 覆盖字节码、常量、默认值与 JSON 可序列化的闭包捕获；函数引用的可变全局状态不在覆盖范围内，受控注册表由受信环境保证纪律。当前不包含节点拆分迁移、规则编译索引或分布式批处理，回放输入与结果均以完整 JSON 保存，适合受控本地数据规模。
