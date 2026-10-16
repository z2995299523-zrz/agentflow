# NL2SQL SQL 作用域解析：别名指向、按查询层补结构、字段追溯、表达式展开、等值关联键

> 日期: 2026-10-15 | 类别: tech | 关联: [[2026-10-13_design_nl2sql专题库改造]]

---

## 📦 目标

第二份计划里有两个模块都要回答同一个问题：**SQL 里的 `a.cust_no`，到底是哪张实体表的哪个字段？**

- 加工脚本解析器：要把专题层的每个目标字段，穿过 CTE、子查询、临时表，一路追到整合层、贴源层的实体字段，补血缘、回填加工逻辑。
- 静态校验器：要判断模型写的 SQL 里每个字段是否真实存在（R4）、关联是否属于已知关联（R6）、汇总会不会发散（R7）。

两边各写一套，就会出现「解析器认为对、校验器认为错」的情况。所以把这部分抽成一个共享模块 `metadata/sqlscope.py`，两边都调它，行为由同一组单元测试钉住。

| 能力 | 接口 | 谁用 |
|------|------|------|
| 解析、静音 | `parse_statements`、`quiet_sqlglot` | 解析器、校验器 R1 |
| 找实体表 | `table_id`、`physical_tables`（排除 CTE 名） | 校验器 R3、血缘图 |
| 按元数据限定字段 | `qualify_with_catalog`，返回限定后的树和字段清单缺口 | 解析器、校验器 R4 |
| 别名查找 | `ScopeResolver.source_of`（相关子查询沿外层找）、`sources`、`owners` | 校验器 R4 |
| 输出列 | `ScopeResolver.output_names`（UNION 取最左分支，有未展开的 `*` 时为 None） | 校验器 R4 |
| 字段追溯 | `resolve_output`、`resolve_column`，结果是 `Resolution(kind, sources)` | 解析器、校验器的「实际用到的字段」 |
| 展开表达式 | `render`、`render_output` | 解析器回填加工逻辑、过滤条件 |
| 等值关联键 | `joins`，返回 `ScopeJoin(join_type, alias, condition, keys)` | 解析器、校验器 R6 |
| 小工具 | `conjuncts`（只拆 AND）、`unwrap`（剥壳）、`rename_outputs`（按位置改名） | 两边都用 |

数字指标：

- 单元测试 60 项，只用模拟库和手写的小 SQL，不调模型，2 秒跑完。
- 模拟库 7 个加工脚本：56 个目标字段，直取 32、加工 24，与计划的期望值一致。
- 真实专题库（只读验证，只记数字）：46 条 INSERT 全部限定成功；1424 个目标字段全部追溯出结果，追溯不到的 0 个；ON 里「列 = 列」294 处，270 处（92%）两侧都追到了实体字段；整个过程约 3 秒。

---

## 📖 讲透

### 1. 作用域：别名只在自己那一层有效

一条 SQL 里可以有好几层查询：主查询、每个 CTE、FROM 里的派生表、WHERE 里的子查询、UNION 的每个分支。每一层叫一个**作用域**，每层 FROM 里起的别名只在这一层有效。

这和存储过程里临时表、游标的可见范围是一回事：存储过程里声明的游标，只在声明它的那个块里能用；内层块可以引用外层块的变量，外层块看不到内层块的。SQL 里对应的规则是：

- 模拟库脚本 21 里有三个 CTE（`cur`、`ytd`、`mtd`），每个里面都用 `a` 作别名，但三个 `a` 互不相干，各自只在自己的 CTE 里有效。主查询里写 `a.cust_no` 是错的，因为主查询的 FROM 里没有 `a`。
- **相关子查询**可以引用外层的别名，例如 `WHERE EXISTS (SELECT 1 FROM dim.corp_cust c WHERE c.cust_no = a.cust_no)`，子查询里的 `a` 指外层的存款账户表。这就是「内层块看得到外层变量」。
- CTE 和普通的派生表不能引用外层或同层兄弟的别名。sqlglot 给每个作用域标了 `can_be_correlated`，`source_of` 只在这个标记为真时才往外层找，所以「CTE 里用了主查询的别名」会被判成别名未定义，而不是被错误地放过。

### 2. 限定字段：先查数据字典，再给每个字段挂上表

没带别名的字段（`SELECT cust_nm FROM ... a JOIN ... c`），要知道两张表各有哪些字段才能判断属于谁。数据库的做法是解析时查数据字典；我们的做法是把元数据库当数据字典，调 sqlglot 的 `qualify`，它会给每张表加别名、给每个字段挂上所属的别名。

这里有三个细节，都是探针在真实脚本上踩出来的：

1. **每条语句只建它用到的那几张表的结构。** 每条语句都把整个专题库几百张表的结构交给 `qualify`，46 条语句要 31.8 秒；每条语句只放它引用到的表，46 条约 1.2 秒。类比：跑一个作业只加载它用到的维表，而不是把整个数仓都拉进内存。
2. **字段清单缺的字段先补进结构，再单独记账。** 脚本里写了 `a.no_such_col`，字段清单里没有。这时 `qualify` 会直接抛错，整条语句就解析不了。做法是：先在没限定的树上按作用域找出「别名 → 实体表」，把「别名.字段」引用了、字段清单里却没有的字段补进这条语句的小结构，同时记成**字段清单缺口**返回。解析器拿它报导入警告，校验器拿它报 R4。类比：装载时遇到维表里没有的代码值，不让整个作业失败，而是先挂一个默认值让数据进来，再把这些代码值写进异常表，单独核对。
3. **还失败就去掉结构重试一次，再失败才报错。** 例如 `USING (某字段)` 两张表都没有这个字段，带结构会报错，不带结构可以照常限定。真正结构性的错误，例如引用了 CTE 里不存在的列，去掉结构也会失败，这时抛 `SqlScopeError`。

### 3. 字段追溯：顺着 mapping 文档一层层往上翻

限定完以后，每个字段都知道属于哪个别名，别名又知道指向实体表还是另一个作用域。追溯就是顺着这条链往上走，和平时查一个指标的来源时，顺着 mapping 文档一层层往上翻是同一件事。

每个目标字段的判定步骤：

1. **剥壳**：去掉别名、括号、转成非日期时间类型的 `CAST`、`TRIM`。这些不改变取值。
2. 剥完**是一个字段**：它指向实体表就是**直取**（passthrough）；指向 CTE、派生表，就到那一层对同名输出字段继续追溯，结果原样继承。
3. 剥完**不是字段**：收集表达式里本层的全部字段（标量子查询也进去收集），各自追溯，有任何实体来源就是**加工**（derived），来源取并集。
4. 一个实体来源都没有时，依次判断：
   - 引用的字段全为空，且只经过 CAST、COALESCE、四则运算、SUM 这类保持空值的运算，是**空值**（null）；
   - 含 `now()`、`sysdate` 之类的，是**加工时间**（etl_time）；
   - 含聚合函数或窗口函数（`COUNT(*)`、`COUNT(1)`、`ROW_NUMBER() OVER ()`），或者引用的字段本身就是这类加工，是**加工**，来源为空；
   - 其余，也就是纯字面量和 CAST 的字面量，是**常量**（constant）。
5. 中途断了（别名没定义、有歧义）记为**追溯不到**（unresolved）。

| 类别 | 模拟库或手写的例子 | 追溯到的实体字段 |
|------|-------------------|------------------|
| 直取 | `cur.cust_no`（CTE 里是 `a.cust_no`） | `dwd.corp_dep_acct_d.cust_no` |
| 直取 | `CAST(a.acct_no AS VARCHAR(40))`、`TRIM(a.org_no)` | 被转换的那个字段 |
| 加工 | `cur.dep_bal_rmb`（CTE 里是 `SUM(CASE WHEN a.is_margin = '0' THEN a.bal_rmb ELSE 0 END)`） | `bal_rmb`、`is_margin` |
| 加工 | `CAST(a.open_dt AS DATE)`、`to_char(...)`、`COALESCE(...)` | 里面用到的字段 |
| 加工 | `COUNT(*)`、`ROW_NUMBER() OVER ()`、CTE 里 `COUNT(*) AS cnt` 再到外层 `COALESCE(n.cnt, 0)` | 无（由行计数得到的度量） |
| 空值 | `NULL`、`CAST(NULL AS VARCHAR(10))` | 无 |
| 常量 | `'1'`、`CAST('1' AS VARCHAR(1))`、`DATE '${data_dt}'` | 无 |
| 加工时间 | `now()`、`CURRENT_TIMESTAMP`、`sysdate` | 无 |

为什么转成日期不算直取：转成文本、数值只是换个存储类型，值不变；转成日期往往伴随格式解析，`'20260930'` 和 `DATE '2026-09-30'` 已经不是同一个东西了，后面拿它推导关联键域时不能混在一起。

为什么 `COUNT(*)` 算加工：它一个字段都没引用，但取值是数行数得来的度量，比如「借据笔数」「项目数」，每个客户、每个机构都不一样。类比报表里的「笔数」指标：它没有对应的源字段，却是实打实加工出来的。判成常量的话，后面给模型的证据会写成「脚本里是常量」，会误导模型。所以只有纯字面量才是常量，带聚合函数、窗口函数的都是加工，只是来源为空。

**UNION 按位置合并。** UNION 的第 i 列对应每个分支的第 i 列，和分支里的别名无关。各分支都是直取，结果是直取，来源取并集；全是空值，结果是空值；只要有一个分支是直取或加工，结果是加工；其余按加工时间、常量归类。

**记忆化和深度上限。** 一个 CTE 的输出字段可能被几十个目标字段引用，每次都重新追一遍很浪费。所以同一个解析器实例按「（作用域, 输出名）」缓存结果，相当于 ETL 里把中间结果落一张临时表，后面的步骤直接复用。追溯最多穿过 12 层，超过的记为追溯不到。有一个细节：中途撞到深度上限得到的结果和深度有关，不能缓存，否则从更近的地方再追溯时会拿到错误的缓存。

### 4. 展开表达式：把口径写成人和模型都看得懂的样子

专题层字段的口径说明往往一模一样。模拟库里「存款余额」和「全口径存款余额」的口径说明都只写了「折人民币」，区别只在加工脚本里。光看最外层的 `cur.dep_bal_rmb`，什么也看不出来。`render` 把 CTE 层层展开，换成实体表全名：

```text
dep_bal_rmb      SUM(CASE WHEN dwd.corp_dep_acct_d.is_margin = '0' THEN dwd.corp_dep_acct_d.bal_rmb ELSE 0 END)
dep_bal_tot_rmb  SUM(dwd.corp_dep_acct_d.bal_rmb)
```

一眼就能看出：前者排除了保证金，后者没有排除。规则：

- 指向实体表的字段写成「模式.表.字段」。
- 指向单分支查询的字段，换成那个投影展开后的表达式；展开结果不是原子节点（字段、常量、函数调用、窗口）时加括号，保证运算顺序不变：`c.x + 1`，而 `c.x` 是 `a.acct_bal * 2`，展开成 `(ods.core_dep_acct.acct_bal * 2) + 1`。
- 指向 UNION 的字段写成 `UNION(分支1, 分支2, ...)`。
- 最多展开 8 层，最长 1500 字符，超出截断并以「…」结尾。
- 过滤条件也用同一个函数展开。例如手写的派生表里有 `ROW_NUMBER() OVER (PARTITION BY a.cust_no ORDER BY a.bal_rmb DESC) AS rn`，外层的 `x.rn = 1` 会展开成 `ROW_NUMBER() OVER (PARTITION BY dwd.corp_dep_acct_d.cust_no ORDER BY dwd.corp_dep_acct_d.bal_rmb DESC) = 1`，上溯到整合层查数时能看出原来取的是「每个客户余额最大的一条」。

### 5. 等值关联键：穿过 CTE 追到实体表

`joins()` 从三处收集等值条件：各个 JOIN 的 `ON`（只按 AND 拆，不进 OR）、`USING`、以及 WHERE 里两侧别名不同的等值（记为 `IMPLICIT`，按别名对归组，所以 `a.ccy_cd = r.ccy_cd AND a.data_dt = r.data_dt` 是一组复合键）。两侧剥壳后都是字段时各自追溯，两侧都是直取才算一个键对。

两个容易想错的地方：

- **两侧是同一个实体字段的键对要丢掉。** 脚本 21 的主查询写的是 `cur.cust_no = ytd.cust_no`，`cur` 和 `ytd` 两个 CTE 的客户号都来自 `dwd.corp_dep_acct_d.cust_no`，追溯完是「同一个字段等于它自己」，对推导表间关联没有意义。关联本身保留（它是口径证据），只是不出键对。
- **自关联要保留不同字段的键对。** 机构表的 `o.up_org_no = p.org_no` 两侧是同一张表，但字段不同，这正是上下级关系，要留着。

键对一律把被关联的那张表放右边，例如 `JOIN dim.corp_cust c ON c.cust_no = a.cust_no` 记成「存款账户表.客户号 → 客户维表.客户号」。来源是 UNION 时两侧取笛卡尔积，最多 4 组，防止爆炸。

---

## 🔧 选型

| 决策 | 选了什么 | 没选什么 | 原因 |
|------|----------|----------|------|
| SQL 解析 | sqlglot 的语法树和作用域树 | 正则、自己写解析器 | 加工脚本里有 CTE、子查询、UNION、窗口函数，正则管不住；sqlglot 已经有作用域（`build_scope`）和字段限定（`qualify`），拿来就能用。类比：用数据库自带的执行计划分析 SQL，而不是自己去切字符串 |
| 字段追溯 | 在作用域树上自己写的、带记忆化的追溯 | `sqlglot.lineage` | `lineage` 每个字段单独从头走一遍，不分直取和加工，也不认我们的 UNION 合并规则；自己写的版本按「（作用域, 输出名）」缓存，1424 个字段约 3 秒 |
| 表结构 | 每条语句只建它用到的表 | 把整个专题库的结构交给每条语句 | 实测 31.8 秒对 1.2 秒。sqlglot 每次收到字典都会重建结构对象，几百张表的结构重建很慢 |
| 字段清单缺口 | 先补进结构，单独返回 | 让 `qualify` 报错 | 报错会让整条语句废掉，缺一个字段就丢掉几十个字段的血缘；补上再记账，既能继续解析，又不丢问题 |
| 结果类型 | 冻结的数据类 `Resolution(kind, sources)` | 字典、元组 | 可以比较、可以放进集合、可以当缓存的值；`sources` 用 frozenset，顺序无关 |

---

## 💡 可迁移

**1. 先补后跑，异常单独记账。** 「字段清单缺口」的做法可以搬到任何「上游资料不全、又不能因此整体失败」的场景：缺的部分先补默认值让流程走完，补过的内容逐条记下来交给人核对。数据质量管理里叫「放行加留痕」。

**2. 递归追溯要记忆化，但被截断的结果不能缓存。** 任何沿依赖链往上找的程序（血缘追溯、调度依赖分析、指标拆解）都可以按「节点」缓存结果；只要有深度上限或防环，就要区分「完整结果」和「被截断的结果」，后者不能进缓存。

**3. 两个模块的共同底层抽出来，用测试钉住行为。** 解析器和校验器对同一个字段的理解必须一致，所以由一个模块提供，一组测试约束。后面的子代理只看接口和测试，就能放心地在上面搭东西。

---

## 👨‍💻 代码

下面的片段取自 `metadata/sqlscope.py`，为了讲解做了删节。

**限定字段（`qualify_with_catalog`）。** 先在没限定的树上找出用到的已知表和缺口，建小结构，再限定；失败就去掉结构重试一次：

```python
with quiet_sqlglot():
    tree = normalize_identifiers(expr.copy(), dialect=DIALECT)
    known, gaps = _referenced_tables(tree, catalog)      # 别名 → 已知实体表；「别名.字段」里字段清单没有的
    schema = _small_schema(known, gaps)                  # 只含这些表，缺口也补进去
    try:
        return qualify(tree.copy(), dialect=DIALECT, schema=schema, **QUALIFY_OPTIONS), gaps
    except Exception:
        pass                                             # 带结构失败，下面去掉结构再试一次
    try:
        return qualify(tree, dialect=DIALECT, **QUALIFY_OPTIONS), gaps
    except Exception as error:
        raise SqlScopeError(f"SQL 无法按作用域解析: {error}") from error
```

**判定一个投影的类别（`_expression`）。** 剥壳后是字段就追溯它；否则看其中各字段的追溯结果：

```python
core = unwrap(expr)
if isinstance(core, exp.Column) and not core.is_star:
    return self._column(scope, core, depth)
results = [self._column(owner, column, depth) for owner, column in self._value_columns(scope, core)]
sources = frozenset().union(*(result.sources for result in results))
if sources:
    return Resolution("derived", sources)
kinds = {result.kind for result in results}
if "unresolved" in kinds:
    return _UNRESOLVED
return _without_sources(core, kinds)                     # 空值、加工时间、行计数这类加工或常量
```

**追不到实体来源时怎么分（`_without_sources`）。** 判断有先后：先看是不是恒为空，再看加工时间，然后是聚合、窗口函数，最后才是常量：

```python
if kinds <= {"null"} and _stays_null(expr):
    return _NULL                      # 例如 COALESCE(CAST(cte.x AS ...), CAST(NULL AS ...))，cte.x 恒为空
if "etl_time" in kinds or _has_time_function(expr):
    return _ETL_TIME                  # now()、sysdate
if "derived" in kinds or _has_aggregate(expr):
    return _MEASURE                   # COUNT(*)、ROW_NUMBER() OVER ()：加工，来源为空
return _CONSTANT                      # '1'、CAST('1' AS VARCHAR(1))
```

**记忆化（`_output`）。** 中途撞到深度上限的结果不缓存：

```python
if depth > self.max_depth or key in self._active:
    self._cuts += 1
    return _UNRESOLVED
...
if self._cuts == cuts:                # 中途没有撞到上限，结果与深度无关，可以缓存
    self._memo[key] = result
```

**一次真实的运行。** 对模拟库脚本 21 做解析器会做的事：取出 INSERT 的查询部分，按位置把投影改名成目标字段，限定，再逐个追溯、展开，最后列出关联：

```python
query = rename_outputs(insert.expression, targets)
qualified, gaps = qualify_with_catalog(query, store)
root = build_scope(qualified)
resolver = ScopeResolver(root, catalog=store)
for name in ["cust_no", "dep_bal_rmb", "dep_bal_tot_rmb", "ytd_avg_bal_rmb"]:
    res = resolver.resolve_output(root, name)
    print(name, res.kind, sorted(r.column for r in res.sources))
    print(resolver.render_output(root, name))
```

输出：

```text
cust_no          passthrough  ['cust_no']
                 dwd.corp_dep_acct_d.cust_no
dep_bal_rmb      derived      ['bal_rmb', 'is_margin']
                 SUM(CASE WHEN dwd.corp_dep_acct_d.is_margin = '0' THEN dwd.corp_dep_acct_d.bal_rmb ELSE 0 END)
dep_bal_tot_rmb  derived      ['bal_rmb']
                 SUM(dwd.corp_dep_acct_d.bal_rmb)
ytd_avg_bal_rmb  derived      ['bal_rmb', 'is_margin']
                 ROUND(SUM(CASE WHEN dwd.corp_dep_acct_d.is_margin = '0' THEN dwd.corp_dep_acct_d.bal_rmb ELSE 0 END) / (CAST('${data_dt}' AS DATE) - CAST(DATE_TRUNC('YEAR', CAST('${data_dt}' AS DATE)) AS DATE) + 1), 2)
JOIN ytd []
JOIN mtd []
JOIN c [('dwd.corp_dep_acct_d.cust_no', 'dim.corp_cust.cust_no')]
```

最后三行是关联：主查询里 `cur` 与 `ytd`、`mtd` 的关联键两侧是同一个实体字段，所以没有键对；`cur` 里关联客户维表的那一处，追到了「存款账户表.客户号 → 客户维表.客户号」。

字段清单缺口和伪列：

```text
>>> qualify_with_catalog(parse("SELECT a.cust_no, a.no_such_col, sysdate AS etl_dt FROM dwd.corp_dep_acct_d a"), store)
SELECT a.cust_no AS cust_no, a.no_such_col AS no_such_col, sysdate AS etl_dt FROM dwd.corp_dep_acct_d AS a
{FieldRef(table='dwd.corp_dep_acct_d', column='no_such_col')}
```

`no_such_col` 没有让限定失败，而是作为缺口返回；`sysdate` 没被挂到表上，追溯时判成加工时间。

**真实专题库上的验证（只读，只记数字）：**

| 项目 | 结果 | 探针 |
|------|------|------|
| 限定成功的语句 | 46 / 46 | 46 / 46 |
| 目标字段 | 1424，追溯不到 0 | 1424 |
| 直取 / 加工 | 706 / 468 | 约 736 / 455 |
| 空值 / 常量 / 加工时间 | 144 / 64 / 42 | 约 124 / 68 / 41 |
| 字段清单缺口 | 13 个字段，共 20 处引用 | 约 20 处 |
| ON 里「列 = 列」 | 294 处，两侧都追到实体字段的 270 处 | 268 处，244 处 |
| WHERE 跨别名等值 | 8 处，全部追到 | 21 处 |
| 耗时 | 约 3 秒（解析、限定、追溯、展开、关联全部） | — |

和探针的差异：

- ON 多出的 26 处全部在 UNION 分支（19）和子查询（7）里，而且都能追到实体字段；只数主查询、CTE、派生表时正好是 268 处、244 处，与探针一致。追不到的 24 处两边相同，都是一侧经过了函数加工，或者一侧是常量（多是内联码表里的代码）。
- 空值多出 20 个：其中 19 个是同一类预留字段，写法是 `COALESCE(CAST(cte.x AS ...), CAST(NULL AS ...))`，而 `cte.x` 在 UNION 的每个分支里都是 NULL，取值恒为空。这里把它们判成空值，召回时会被排除。
- 加工多 13 个：都是 `COUNT(*)` 这类不引用字段的聚合，在 CTE 或子查询里算好，12 个由外层直接引用，1 个外层又包了一层 `COALESCE`。它们是行计数得到的度量，判成加工、来源为空，不算常量。
- 常量 64 个：49 个由字面量构成，包括纯字面量、CAST 的字面量、各分支都是字面量的 UNION，以及在这类字段上再做 CASE、COALESCE 的；另外 15 个来自内联码表。码表的 CTE 写成 `SELECT * FROM (VALUES ('1', '大型'), ('2', '中型')) t(code, name)`，再关联它取名称。码表的值是写死的常量，没有实体来源，所以判成常量；判成直取会凭空多出一个叫 `t` 的「实体表」，后面推导关联键域时会出错。直取比探针少 30 个，其中 15 个应当就是它们，探针很可能把它们算成了直取。
- WHERE 跨别名等值在脚本原文里只数到 8 处，对不上探针的 21 处，原因没有查清。

---

## ⚠️ 常见坑

1. **sqlglot 的 `qualify` 会直接改传进去的树。** 不先复制，调用方手里的原树就被改掉了；带结构失败后重试时也会拿到改了一半的树。所以每次都先 `copy()`。
2. **关掉校验也挡不住「别名.不存在的列」。** `validate_qualify_columns=False` 只是不检查没限定上的列，`a.no_such_col` 仍然会抛 `Unknown column`。所以要先把这种字段补进结构。
3. **WITH 可以写在 INSERT 前面。** `WITH x AS (...) INSERT INTO t SELECT ... FROM x` 解析出来，CTE 挂在 INSERT 节点上，而不是 SELECT 上。只取 INSERT 的查询部分会丢掉 CTE，要把它挪过去。
4. **`sysdate`、`localtimestamp`、`user` 在 PostgreSQL 方言里被解析成字段。** 带结构限定时它们保持不带别名，靠一份伪列名单识别：没带别名、又没有任何来源有这个字段时，按系统值处理。不带结构限定时，只有一张来源表的查询会把 `sysdate` 挂到这张表上，变成一个「表字段」；这只会发生在去掉结构重试的那条路上，是已知的局限。
5. **UNION 按位置对应，不按名字。** 第二个分支写的别名不算数，取第 i 列就是第 i 列。`output_names` 只看最左分支。
6. **同一个别名在不同的作用域里指不同的东西。** 脚本 21 的三个 CTE 都用 `a`，所以缓存的键必须带上作用域，不能只用「别名.字段」。
7. **`COUNT(*)` 没有引用任何字段，但不能判成常量。** 只看「有没有引用字段」，它会落进常量，后面给模型的证据就会写成「脚本里是常量」。实际上它是行计数得到的度量。所以有聚合函数或窗口函数、又追不到实体来源的，判加工、来源为空。经过 CTE 也一样：CTE 里 `COUNT(*) AS cnt`，外层 `COALESCE(n.cnt, 0)` 仍是加工。真实脚本里有 13 个目标字段属于这种情况。
8. **解析 `DO $$ ... $$` 这类块会刷警告。** sqlglot 认不出时退化成 `Command` 并打一条警告，几十个脚本跑下来满屏都是。解析、限定、生成文本都包在 `quiet_sqlglot()` 里。

---

## 面试话术

> 「加工脚本解析和 SQL 校验都要回答『这个字段来自哪张表』，我把它抽成共享模块：基于 sqlglot 的作用域树，按语句只给用到的表建结构来限定字段，缺的字段先补上再记成缺口；然后带记忆化地把每个字段穿过 CTE、子查询、UNION 追到实体表，分成直取、加工、空值等五类。真实 46 个脚本、1424 个字段 3 秒跑完，零失败。」

被追问时可以展开的三点：

- 为什么不用 `sqlglot.lineage`：它每个字段从头走一遍、不区分直取和加工；自己写的按作用域缓存，规则也能按业务定。
- 为什么每条语句单独建结构：每条语句都给整库结构，46 条语句要 30 多秒；按语句只建用到的表，约 1.2 秒。
- 字段清单缺口怎么来的：sqlglot 遇到「别名.不存在的列」直接报错，先补后记账，既不丢整条语句，又能把问题报给人。
