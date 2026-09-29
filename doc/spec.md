# 效率工具 — i18n Key 查询与 shark 导入文件生成 规格文档

## 背景与目标

本工具服务两条链路：

**链路 A — 截图查词条**：携程 copywriter 在校验国际化词条时，需要同时提供 key、中文原文、英文译文，以及对应界面的截图。对于后端词条，产品可以在界面上看到中文显示，但无法直接获得 i18n key。本工具让产品人员快速完成从截图到结构化 CSV 的全流程，减少手工翻查的工作量。（校验结果最终要提交给飞书 copywriter 系统，这一步**2026-09-28 已重启**，见"飞书结果提交（重启中）"。）

**链路 B — 文档取表生成 shark 导入文件**：copywriter 在飞书文档《RezenOne翻译需求》里维护词条表，校对完成后的英文（`en-US by copywriters` 列）需要回写到 shark（词条管理平台）。shark 以 appId 区分项目，本工具从文档里按小标题取出表格，用 key 反查 appId，按 appId 生成 shark 的导入文件，人工在 shark 上按项目导入。

---

## 整体流程

**链路 A：**

```
[人工] 截图  →  [AI识图] 提取中文  →  [工具] 查 Archery  →  [工具] 歧义确认  →  [工具] 导出 CSV
```

**链路 B：**

```
[人工] 输入小标题  →  [工具] 模糊定位表格  →  [工具] 校验列契约  →  [工具] 抽行
                  →  [工具] key 反查 trip_appid  →  [工具] 按 appId 生成 xlsx  →  [人工] 在 shark 导入
```

**链路 A 详细步骤：**

### 第一步：截图（人工完成）

产品对需要校验的界面进行截图，保存到本地。

### 第二步：AI 识图提取中文（人工完成，在浏览器/AI 产品内操作）

将截图上传到支持识图的 AI（如 ChatGPT、Claude 网页版等），让 AI 从截图中提取所有中文文本，生成如下 JSON 格式：

```json
{
  "texts": [
    "确认订单",
    "取消",
    "支付金额",
    "请输入备注"
  ]
}
```

用户将此 JSON 复制后，粘贴到本工具的输入框中。

> **CLI 模式**：本工具同时提供命令行接口，AI 助手（如豆包）可以识图后直接调用 CLI 完成整个流程，无需人工介入粘贴步骤。详见"命令行接口"章节。

### 第三步：查询 Archery 数据库（工具内自动完成）

工具通过 Archery（内网 SQL 查询平台）查询 i18n 数据库，找到每条中文对应的 key 和英文。

**认证流程：**

1. 工具弹出登录对话框，用户输入账号密码
2. 工具向 `http://archery.rezen.work/authenticate/` 发送 AJAX 登录请求
3. 登录成功后，从响应 cookie 中提取 `csrftoken` 和 `sessionid`
4. 后续所有查询请求携带上述 cookie 和 CSRF Token
5. 支持"记住密码"——凭据保存至 `~/.happyhappyhappy/credentials.json`，下次启动自动填入

**表结构：**

```sql
CREATE TABLE `i18n_translated_message` (
  `id`          bigint(20)   NOT NULL AUTO_INCREMENT COMMENT '自增标识',
  `trip_appid`  varchar(32)  NOT NULL COMMENT '携程appid',
  `key`         varchar(128) DEFAULT NULL COMMENT '词条key',
  `language_cd` varchar(64)  NOT NULL COMMENT '语种',
  `value`       text         NOT NULL COMMENT '翻译结果',
  `create_time` datetime     NOT NULL COMMENT '创建时间',
  `_timestamp`  timestamp    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_appid_language_cd_key` (`trip_appid`, `language_cd`, `key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='词条的翻译结果';
```

关键字段说明：
- `trip_appid`：携程应用 ID，不同模块的 key 可能重名，需一并输出作为区分依据
- `key`：i18n key，同一 `trip_appid` + `key` 下每个语种各一行
- `language_cd`：语种标识，中文为 `zh-CN`，英文为 `en-US`
- `value`：对应语种的翻译文本

**查询接口：**

```
POST http://archery.rezen.work/query/
Content-Type: application/x-www-form-urlencoded; charset=UTF-8

instance_name=OL_SET_PMS_RO
db_name=pms_i18n
schema_name=
tb_name=
sql_content=<见下方查询策略>
limit_num=1000
```

所需 Header：
- `Cookie: csrftoken=xxx; sessionid=xxx`
- `X-CSRFToken: xxx`（值与 csrftoken 相同）
- `X-Requested-With: XMLHttpRequest`
- `Referer: http://archery.rezen.work/sqlquery/`

**查询策略（批量 JOIN，减少请求次数）：**

所有中文文本一次性查出，并同时 JOIN 出英文，整个流程只需发起 **2 次** HTTP 请求（1 次查询 + 必要时 1 次补充查询，见下文）。

第一步：将所有中文文本用 `IN` 批量查 `zh-CN` 行，同时 self-join 同一 `(trip_appid, key)` 的 `en-US` 行：

```sql
SELECT
    zh.trip_appid,
    zh.`key`,
    zh.value   AS zh_cn,
    en.value   AS en_us
FROM i18n_translated_message zh
LEFT JOIN i18n_translated_message en
    ON zh.trip_appid = en.trip_appid
    AND zh.`key` = en.`key`
    AND en.language_cd = 'en-US'
WHERE zh.language_cd = 'zh-CN'
    AND zh.value IN ('确认订单', '取消', '支付金额')
```

> 注意：实际构造 SQL 时，需对每个中文值做 SQL 转义（单引号转义为 `''`），防止注入。

**查询结果处理逻辑：**

1. 按 `value`（即 `zh_cn`）对结果分组，白名单过滤（只保留 `trip_appid` 在白名单内的行）
2. 每组命中 0 行 → 标记为 `not_found`
3. 每组命中 1 行 → 直接采用，标记为 `found`
4. 每组命中多行（同一中文对应多个 `trip_appid`/`key` 组合）→ 保留所有候选，标记为 `ambiguous`，进入第四步人工确认
5. 若用户声明"同一接口"（CLI `--same-interface` 参数）：用已 `found` 条目的 `trip_appid` 过滤歧义候选，只剩 1 条则自动升级为 `found`；若 `found` 条目 appid 不一致则警告

CSV 输出字段：`trip_appid`、`key`、`zh_cn`、`en_us`、`status`

### 第四步：歧义确认（工具内，需要人工操作）

当某条中文对应多个 key 时，需要产品或研发人工判断应该使用哪个。

**界面要求：**

- 以列表或表格形式展示所有待确认项
- 每个歧义项显示：中文、候选 key 列表（同时显示对应的英文和其他上下文字段）
- 用户点选正确的 key 后，该项状态变为"已确认"
- 所有歧义项确认完成后，"导出 CSV"按钮变为可用

### 第五步：导出 CSV

点击导出，生成 CSV 文件，列结构如下：

| 列名 | 说明 | 是否必填 |
|------|------|----------|
| trip_appid | 携程应用 ID | 是 |
| key | i18n key | 是 |
| zh_cn | 中文原文 | 是 |
| en_us | 英文译文 | 是 |
| image_url | 界面截图链接 | 否，暂不实现，留空 |
| status | 状态（found / not_found / ambiguous / confirmed） | 是 |
| note | 歧义项填"待研发确认"，其余留空 | 是 |

CSV 保存到用户指定路径（通过系统文件对话框选择）。

---

## 链路 B：文档取表 → 生成 shark 导入文件

### 背景

copywriter 在飞书文档《RezenOne翻译需求》里维护词条表，每张表对应界面的一个小标题（如"预订-入住"）。
校对完成后，`en-US by copywriters` 列就是最终英文，需要回写到 shark（词条管理平台）。

shark 的两个约束决定了本功能的形态：

1. **shark 以 appId 区分项目**，同一份文件只能导进一个项目，所以要按 appId 拆成多个文件；
2. **shark 有固定导入模板**，key 必须与 shark 上已有的 key 完全一致。

文档里只有 key、没有 appId，所以要用 key 去 Archery 反查 appId。key 已知、不需要模糊匹配。

### 第一步：输入

**词条表有两个数据源，二选一**：

- `--feishu-doc <链接>`：**直接读飞书活文档**（`/docx/` 或 `/wiki/` 链接都行）。
  不用先导出 135MB 的 docx，读的是**当下这一份**——也就不会出现"文档更新了但没人重新导出、
  工具读到旧快照"这种很难发现的错；
- `--doc <路径>`：读本地导出的 docx，离线可用。

两个源产出的东西完全一样（`TableMeta` / `TableContent` / `DocRow`），所以下游（反查 appId、
生成 xlsx、报告）完全不用改。来源无关的部分——**六列契约、小标题模糊匹配、抽行规则**——在
`table_doc.py` 里共用，docx 源（`docx_table.py`）和飞书源（`feishu_table.py`）各自只负责"把文档
变成二维文本表"。列契约绝不能有两份实现，否则早晚一边报错一边放行。

飞书源读的是块树，对应关系：期 → 1 级标题块、小标题 → 2 级及以上标题块、表格 → 表格块（单元格按
行优先平铺）、单元格内容 → 单元格里的文本块。**读的时候会自检并在结果里带 `warnings`**：单元格数
对不上"行×列"（多半是合并单元格，列位置会错位）、表格块没带行列数、整篇没读到表格——这些是直连
读文档才能提前发现的问题。

- **本地 docx**（下面这段仍适用）：**由使用方提供路径**，用 `--doc <路径>` 传入——绝对/相对路径都行、文件名随意、
  不需要放进特定目录（工具要交付给别人用，不能要求对方去准备/下载词条表）。
  开发时的便利：文件若就在 `doc/` 下（工具目录、其上一级或当前目录），不传 `--doc` 也能找到；
  找不到时报错会列出查过的路径。
- **小标题关键词**：如 `预订-入住`，支持模糊匹配
- 每次只处理**一张表**，所以不会有跨小标题合并的问题

### 第二步：小标题模糊匹配

**解析方式**：只读 zip 包里的 `word/document.xml`（约 2.2MB），**不整包解压**。文档本身 135MB，几乎全是单元格截图（406 张），整包解压既慢又占内存。

**文档结构**：正文按顺序是「期」（标题 1 级）→「小标题」（2/3 级）→ 紧跟的表格。某个小标题底下没有表格时跳过该标题（现状：`CRS` 只有标题没有表）。

**匹配规则**：忽略大小写、首尾空格、全角/半角差异与连续空白，先做子串匹配；无命中时用 `difflib` 给相近候选。

**命中多条时必须由人确认**，不允许工具自行选择（AI 调用场景下同样要停下确认）。展示内容为「期 + 小标题 + 数据行数」。

### 第三步：列契约（强制）

表格必须是以下六列，**列名与顺序都固定，且表头在第一行**：

| 序号 | key | 中文 | 英文 | en-US by copywriters | 截图 |
|---|---|---|---|---|---|

- 增列、删列、改名都会导致解析失败，工具直接报错并打印「期望六列 / 实际列」，不做兼容猜测。
- `序号` 与 `截图` 只作人工核对，不参与导出（截图列全是内嵌图片，解析时跳过）。
- 此契约必须写在给文档维护者与 AI 看的使用文档（skill 正文）里。

**文档现状对照（2026-09-24 实测）**：

| 期 | 小标题 | 实际列 | 是否合规 |
|---|---|---|---|
| 第五期 | 预订-订单列表 | 序号/key/中文/英文/截图 | ✗ 缺 copywriters 列 |
| 第四期 | 侧边栏功能 | 序号/key/中文/English/截图 | ✗ |
| 第四期 | 客史 | 序号/key/中文/英文/截图 | ✗ |
| 第三期 | 客户字典和错误码 / 客史错误码 | 序号/key/中文/英文/截图 | ✗ |
| 第二期 | 预订-入住 / 换房/升级 / 同住 / 联房 | 六列 | ✓ |
| 第二期 | 首页 | 序号/key/中文/英文/截图 | ✗ |
| 第一期 | 预订-新建预订 | 序号/key/中文/英文 by AI/en-US by copywriters/Comments/截图 | ✗ |
| 第一期 | 预订-分房 | 序号/key/中文/英文/en-US by copywriters/Comments/截图 | ✗ |

即：今天只有第二期的四张表可直接使用，其余表要由文档维护者先改成六列。

### 第四步：抽行规则

| 情况 | 处理 |
|---|---|
| key 为空 | 跳过该行，计入报告（现状：预订-入住 5 行、预订-订单列表 60 行为空） |
| `en-US by copywriters` 为空 | 不导出——空值会把 shark 上已有的英文覆盖掉 |
| 同一 key 出现多次 | 取最后一条，被覆盖的条数计入报告 |
| key 前后有空格 | 去掉后使用 |
| 整行都是空的 | 跳过 |

### 第五步：用 key 反查 appId

**普通 key** —— 精确匹配，一次 `IN` 批量完成：

```sql
SELECT DISTINCT trip_appid, `key`
FROM i18n_translated_message
WHERE `key` IN ('reception_total_price', ...)
```

两个容易踩的点：

- **不加 `language_cd` 过滤**：appId 跟语种无关，`DISTINCT trip_appid, key` 之后结果集大小一样；
  加了过滤反而会让"只有非 zh-CN 行"的词条被漏掉。之前就是因为这个条件漏过词条。
- **库里的写法可能和文档不同**：MySQL 默认大小写不敏感，所以 `reception_Register_guest_info` 能查到
  库里写作 `reception_register_guest_info` 的那一行，但**返回的是库里的拼写**。必须把它映射回文档里的 key，
  并把库里的写法记成真身（写进导入文件用真身）。不这么做的话，那一行会被静默丢掉、最后误报成 `not_found`。

**纯数字 key（错误码那种）** —— 文档里只留了截断后的一段数字，要用 LIKE 找回完整 key。
真身的形态不确定，所以**按可能性从高到低依次试，前一趟没命中的才进下一趟**：

真身形态已确认：**形如 `reception_error_10194060`——数字在末尾、前面有下划线**。

```sql
-- 第一趟：带下划线的后缀（真身的实际形态；\_ 才是字面下划线，不然 _ 会被当单字符通配符）
WHERE (`key` LIKE '%\_10194060' OR ...)

-- 第二趟：放宽成"任意字符结尾"，防别的写法（abc10194060）
WHERE (`key` LIKE '%10194060' OR ...)

-- 第三趟：兜底"包含"（数字在中间或开头，如 1019406012 / err_10194060_code）
WHERE (`key` LIKE '%10194060%' OR ...)
```

SQL 侧只用来"取候选"，真正的形态判定在 Python 里做（`endswith("_" + 数字)` 等），
所以 LIKE 的通配符语义差异不会导致张冠李戴——不匹配的候选直接丢掉。

数字里不会出现 `%` / `_`，不用转义通配符。查到真身后：

- **写进 shark 导入文件的 TransKey 必须是真身**（完整 key、库里的拼写），不是文档里的截断值——
  写错会更新到错误的词条上；
- 还原/纠正关系记进报告（`expanded_keys`：文档里的写法 → 库里真正的写法），方便人工核对；
- 一个前缀命中**多个**完整 key → 无法确定该更新哪个，不导出，计入报告（`multi_real_key`）。

认证与查询接口同链路 A（见"查询接口"）。

**注意**：`i18n_translated_message` 的唯一索引是 `(trip_appid, language_cd, key)`，`key` **不是全局唯一**，理论上同一 key 可能挂在多个 appId 下。因此：

- 一个 key 命中多个 appId → **不自动选**，该 key 不导出，计入报告由人工判断；
- 命中 0 个 → 计入报告「未匹配 key 清单」，由人工在文档里补全 key 后重跑；
- 不做白名单过滤，但不在白名单（`APPID_WHITELIST`）内的 appId 会在报告中标注出来。

### 第六步：生成 shark 导入文件

**模板**：shark 导入页面要求的空 Excel，表头为 `TransKey | Description | en-US | zh-CN`，
第 2-4 行是释义、示例与上传提示（模板自己注明"上传时请删除第 2-4 行"）。
真源是 `doc/PrePublishImportTranslation.xlsx`，但**已内嵌进程序**（`shark_template.py`，
由 `tools/embed_shark_template.py` 生成），运行时不需要模板文件，调用方只传参数。

**生成规则**：

- 只写表头 + 数据，**数据从第 2 行开始**，不写模板的第 2-4 行；
- 列映射：`TransKey = key`、`Description = 空`、`en-US = en-US by copywriters`、`zh-CN = 中文`；
- **一个 appId 一个文件**，命名为 `{appid}_{小标题}.xlsx`，默认输出到 `shark_import/`（CLI `--outdir`、GUI 可选目录）；
- 生成方式：按模板的 zip 结构重写 `xl/worksheets/sheet1.xml`，其余部件原样保留，保证与模板格式完全一致（模板用内联字符串 `t="str"`，结构简单）。

**附带报告**（`{outdir}/report.txt` 给人看 + `{outdir}/happyhappyhappy-shark-export.json` 给 AI 读）包含：未匹配 key 清单、一个 key 多个 appId 的清单、key 为空被跳过的行、重复 key 覆盖计数、每个 appId 的输出文件名与行数。

### 第七步：导入 shark（人工）

工具**不调用 shark 接口**，只生成文件。由人工在 shark 上按项目（appId）选择对应文件导入。自动上传作为后续可选项，本期不做。

### 异常处理

| 场景 | 处理方式 |
|---|---|
| 小标题无命中 | 列出相近候选让人确认 |
| 小标题命中多条 | 列出「期 + 小标题 + 行数」让人选，不自动选 |
| 小标题底下没有表格 | 报错并说明该标题无表 |
| 表头不符六列 | 报错并打印「期望六列 / 实际列」 |
| key 查不到 appId | 计入报告，人工补全 key 后重跑 |
| 一个 key 命中多个 appId | 计入报告，该 key 不导出 |
| 文档路径不存在 / 不是 docx | 报错并提示正确用法 |

### CLI

```bash
# 列出/搜索小标题（供人工或 AI 选择）
happyhappyhappy --cli docx-titles --doc <词条表路径> [--keyword 入住] [--output 结果文件]

# 生成 shark 导入文件
happyhappyhappy --cli shark-export \
  --doc <词条表路径> \
  --title "预订-入住" \
  --outdir shark_import \
  --use-saved-creds \
  [--output 结果文件]
```

结果一律写 JSON **文件**，不走 stdout（`--json` 只是兼容旧调用保留的沉默参数）。
默认路径：`docx-titles` → 当前目录的 `happyhappyhappy-docx-titles.json`；
`shark-export` → `{outdir}/happyhappyhappy-shark-export.json`。认证参数同链路 A（`--use-saved-creds` 等）。

**输出通道（三层）**：打包成没有控制台的 exe（Windows GUI 子系统，`flet pack` 默认）时
`sys.stdout` 是 `None`，`print()` 会静默返回——命令成功、退出码 0，但调用方什么都看不到。所以：

**所以结论是：CLI 的产出全部走文件，不依赖 stdout**（老的那些子命令本来就是这么干的：
`query`/`full` 的产出是 `--output` 指定的文件，屏幕上的那行确认只是顺带）。
两个新子命令的结果 JSON 带 `ok` 字段：成功 `true`，失败 `false` 且 `error` 里是原因——
**出错也落文件**，所以调用方只要读文件就能判断结果，不看屏幕也不受影响。

⚠️ 这个包是 **GUI 子系统**程序，而 **cmd.exe 不等 GUI 程序**——它立刻还提示符，看起来像"异步执行"，
其实进程还在后台跑（OAuth + 多次接口调用）。cmd 里要用 `start /wait` 才是"跑完再返回"；
不加也可以，但别急着读结果文件。用 subprocess 这类方式调用不受影响（本来就会等进程）。

结果 JSON 与 `report.txt` 里还带 `tool` / `程序：` 一行（可执行文件或源码的构建时间），专门用来排查「改了源码但没重新打包」——这个坑反复踩过，有它就能一眼对照。

### GUI

「选文档 → 输入关键词 → 从候选列表选小标题 → 预览（命中行数 + 六列校验结果 + 未匹配 key 提示）→ 生成文件」，
完成后展示「appId → 文件名 → 行数」清单与报告。GUI 是人手动兜底路径，不是主路径。

### AI（skill）调用流程

1. 用户给出文档路径与小标题（口语化，如"第二期那个预订-入住"）
2. AI 调 `docx-titles` 拿候选；命中多条时**停下**让用户选
3. AI 调 `shark-export` 生成文件
4. AI 读 JSON/报告，汇报「appId → 文件名 → 行数」，并把未匹配 key 清单交给用户补全

---

## 界面结构

界面分两块：链路 A（查询，即下面的主界面）与链路 B（文档取表生成 shark 文件）。

### 主界面（链路 A）

```
┌──────────────────────────────────────────────────────────┐
│  i18n Key 查询工具                          [登录 Archery] │
├──────────────────────────────────────────────────────────┤
│  粘贴 AI 提取的 JSON：                                     │
│  ┌────────────────────────────────────────────────────┐  │
│  │ {"texts": ["确认订单", "取消", ...]}               │  │
│  └────────────────────────────────────────────────────┘  │
│  [开始查询]                                               │
├──────────────────────────────────────────────────────────┤
│  查询结果：                                               │
│  ┌──────────┬──────────────────────┬────────┬────────┐   │
│  │ 中文     │ key                  │ 英文   │ 状态   │   │
│  ├──────────┼──────────────────────┼────────┼────────┤   │
│  │ 确认订单 │ pos.order.confirm    │ Confirm│ ✓      │   │
│  │ 取消     │ [多个，待确认]        │ -      │ ⚠ 歧义 │   │
│  │ 请输入   │ -                    │ -      │ ✗ 未找到│  │
│  └──────────┴──────────────────────┴────────┴────────┘   │
├──────────────────────────────────────────────────────────┤
│  [确认歧义项]                          [导出 CSV]         │
└──────────────────────────────────────────────────────────┘
```

### 登录子窗口

- 弹出 AlertDialog，包含用户名、密码输入框和"记住密码"勾选框
- 登录成功后对话框关闭，主界面顶栏显示"已登录：用户名"
- 若勾选"记住密码"，凭据写入 `~/.happyhappyhappy/credentials.json`，下次打开自动填入
- 若 session 失效（查询返回重定向），自动提示重新登录

### 歧义确认子窗口

- 列出所有歧义中文条目
- 每条展开显示候选 key 列表（含英文），单选
- 全部选完后点"确认"返回主界面

### 链路 B 界面

```
┌──────────────────────────────────────────────────────────┐
│  文档取表 → shark 导入文件                                 │
├──────────────────────────────────────────────────────────┤
│  文档：[doc/RezenOne翻译需求.docx            ] [选择文件]  │
│  小标题关键词：[入住              ]  [搜索]                │
│  ┌────────────────────────────────────────────────────┐  │
│  │ 第二期  预订-入住  (copywriter 1)        26 行      │  │
│  │ 第二期  首页                             50 行      │  │
│  └────────────────────────────────────────────────────┘  │
│  校验：✓ 六列合规   待导出 21 行   跳过 5 行（key 为空）   │
├──────────────────────────────────────────────────────────┤
│  [生成 shark 导入文件]                                    │
│  输出：100074326_预订-入住.xlsx（21 行）                   │
│        未匹配 key 3 个，见 report.txt                      │
└──────────────────────────────────────────────────────────┘
```

---

## 技术栈

| 项目 | 选型 |
|------|------|
| 语言 | Python 3.x（开发机 3.14，打包用 3.12） |
| UI 框架 | Flet 0.21.2 |
| 网络请求 | `requests` 库 |
| 登录 | 账号密码直接 POST `/authenticate/` |
| CSV 导出/导入 | Python 内置 `csv` 模块 |
| docx 解析（链路 B） | `zipfile` + 标准库 `xml.etree.ElementTree` 直接读 `word/document.xml`，不整包解压、不用 python-docx（文档 135MB、406 张内嵌截图）。**只用标准库，不引 lxml**——少一个 C 扩展依赖，PyInstaller 打包也不会漏模块 |
| xlsx 生成（链路 B） | 按模板 zip 结构重写 `xl/worksheets/sheet1.xml`，其余部件原样保留；模板内嵌在 `shark_template.py`（由 `tools/embed_shark_template.py` 从 `doc/PrePublishImportTranslation.xlsx` 生成），运行时不需要模板文件 |
| 模糊匹配（链路 B） | `difflib`（标准库） |
| 打包 | GitHub Actions，`flet pack` 命令（基于 PyInstaller） |
| 目标平台 | Windows、macOS |

---

## 数据模型

```python
@dataclass
class I18nEntry:
    zh_cn: str                  # 原始中文
    trip_appid: str | None      # 携程应用 ID
    key: str | None             # 最终确认的 key（None 表示未找到）
    en_us: str | None           # 英文译文
    candidates: list[Candidate] # 候选项列表（歧义时有多个），每项含 trip_appid/key/en_us
    status: Literal["found", "not_found", "ambiguous", "confirmed"]

@dataclass
class Candidate:
    trip_appid: str
    key: str
    en_us: str | None

# ---- 链路 B（文档取表 → shark 导入文件）----

@dataclass
class DocTable:
    period: str                 # 所属"期"，如 "第二期： ddl 3 Jul (complete)"
    title: str                  # 小标题，如 "预订-入住  (copywriter 1)"
    rows: list[DocRow]

@dataclass
class DocRow:
    seq: str                    # 序号（仅人工核对，不导出）
    key: str                    # key（已去首尾空格）
    zh_cn: str                  # 中文
    en_us: str                  # 英文（AI/原始译文，不导出）
    en_copywriter: str          # en-US by copywriters（校对后的英文，导出的就是它）

@dataclass
class SharkRow:
    appid: str
    key: str                    # → TransKey
    en_us: str                  # → en-US
    zh_cn: str                  # → zh-CN
    description: str = ""       # → Description，固定留空

@dataclass
class SharkExportResult:
    appid: str
    file_path: str
    row_count: int

@dataclass
class SkippedKey:
    key: str
    reason: Literal["empty_key", "not_found", "multi_appid", "empty_copywriter"]
```

`DocRow` 只在六列校验通过后产生；`SkippedKey` 汇总进报告，不写进 xlsx。

---

## Session 管理

- 登录后 cookie 保存在内存中
- 支持"记住密码"：凭据持久化到 `~/.happyhappyhappy/credentials.json`，下次启动自动填入
- 若查询返回非 200 或返回登录重定向，提示用户重新登录

---

## 错误处理

| 场景 | 处理方式 |
|------|----------|
| JSON 格式不合法 | 输入框下方显示红色提示，拒绝查询 |
| 网络不通 | Toast 提示"无法连接 Archery，请检查网络" |
| 未登录就点查询 | 提示"请先登录 Archery" |
| 查询超时（>10s） | 超时提示，支持重试 |
| 导出时路径无权限 | 提示用户选择其他路径 |

---

## 打包说明

- 打包由 GitHub Actions 执行，使用 `flet pack main.py --name "happyhappyhappy" --icon "img/app.ico"`
- 打包产物为独立可执行文件：Windows 为 `happyhappyhappy.exe`，macOS 为 `happyhappyhappy`，无需 Python 环境
- **控制台**：`console=False`（给产品双击的包不该多一个黑窗口）。代价是 PyInstaller 会把
  `sys.stdout` / `stderr` 置成 `None`、`print()` 静默失效——所以 CLI 的产出**一律走文件**，
  调用方读文件。老的 `query`/`full` 本来就靠 `--output` 出文件，新子命令也按这个约定做，
  且成功/失败都写进同一个结果文件（`ok` 字段区分）
- **数据文件**：onefile 只打包 Python 代码，词条表 135MB 也不该塞进去，把它放在 exe 旁边即可
  （`cli.py` 按「PyInstaller 解包目录 → exe 目录 → **exe 目录的上一级** → 当前工作目录」找——
  exe 在 `dist/`、`doc/` 在仓库根目录是最常见的布局，所以要往上找一级；找不到时报错会列出查过的路径，
  可用 `--doc` 指定）。**shark 模板不用找**：已内嵌在 `shark_template.py` 里，运行时零依赖磁盘文件，
  `--template` 只是调试时的覆盖入口；模板变了就更新 `doc/PrePublishImportTranslation.xlsx` 后跑
  `python tools/embed_shark_template.py` 重新生成（测试会检查两者是否一致）
- push 到 `master` 分支自动触发打包，产物挂在 GitHub Actions Artifacts（保留 30 天）
- 打 tag（如 `v1.0.0`）自动创建 GitHub Release，两个平台安装包作为附件
- 本机（Windows）开发阶段直接运行 `python main.py` 启动 GUI，或 `python main.py --cli ...` 使用 CLI
- **本机打包**（从 README 挪过来的细节）：`flet pack main.py --name "happyhappyhappy" --icon "img/app.ico"`
  或等价地 `python -m PyInstaller happyhappyhappy.spec`（后者绕开 flet 命令、直接吃仓库里的 spec）。
  报"无法将 flet 项识别为 cmdlet"就是 venv 没激活或 flet 装到别的解释器了，三种解法：
  ① `.venv\Scripts\Activate.ps1` 后再 `flet pack`；② 直接 `.venv\Scripts\flet.exe pack ...`；
  ③ `python -m PyInstaller happyhappyhappy.spec`。注意 **`flet pack` 会重新生成 spec、覆盖里面的注释**
- **依赖**：运行期只有 `flet` + `requests`；链路 B 的 docx 解析用标准库 `zipfile` +
  `xml.etree.ElementTree`，所以打包**不用加 `--hidden-import`**

---

## 飞书结果提交（重启中）

> **状态：2026-09-28 重启**。链路 B 已交付验证通过，现在做这一步。
> 需求本体不变：链路 A 的校验结果（key、中文、英文、截图）要提交给飞书 copywriter 系统，
> 载体是**已有的飞书文档**（不是多维表格，也不是新建文档）。

### 已确认的四个决定（2026-09-28）

1. **表格列**：跟《RezenOne翻译需求》里那张表完全一致——
   `序号 | key | 中文 | 英文 | en-US by copywriters | 截图`（链路 A 没有校对值时该列留空）；
2. **截图**：先上传拿 `file_token` 再插入；**每一行的截图单元格都插同一张图**
   （链路 A 一次只有一张界面截图；飞书 API 不支持合并单元格，只能重复插）；
3. **写入位置**：写进**已有文档**的**指定标题下面**——用户提供文档链接 + 那个标题的文字，
   工具按标题找到对应的块，把内容插在它后面（同级、它下面一行）；
4. **文档链接**由用户提供（不是固定配置）。

### 技术方案：docx 块（blocks），不是多维表格

飞书文档是"块树"：写内容 = 往某个父块下挂子块。核心接口是**创建嵌套块**，一次请求能把带父子关系的
整棵树建出来（标题块 + 表格块 + 每个单元格里的内容块）：

```
POST /open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/descendant
GET  /open-apis/docx/v1/documents/{document_id}/blocks          # 找"指定标题"那个块、拿它的父块与位置
POST /open-apis/drive/v1/medias/upload_all                      # 截图；parent_type=docx_image, parent_node=document_id
```

约束（已查清楚，实现时要照顾到）：

- 表格块 `block_type=31`，要带行数/列数；**单元格是容器块，里面至少得有一个子块**（空也要塞空文本块）；
- **限流**：单应用 3 次/秒，**单文档编辑操作也是 3 次/秒**（超了 429）→ 写入要节流 + 重试；
- 一次请求能带的块数有上限（超了报 1770004/1770005/1770007）→ 行多要**分批**；
- 截图单张 ≤20MB；`document_id` 从链接取（`/docx/<id>`；若文档挂在知识库，要先换成 `obj_token`）。

### 幂等

文档是"追加"式写入，同一批写两次就会重复，而且多人同时编辑会冲突。
每次写入前先插一个**带时间戳的小标题**当批次标记，失败重跑时能看出上一批写到哪了；
结果同时落 JSON 文件（`ok` / 文档链接 / 写入块数），沿用链路 B 的输出约定。

### 实现状态（2026-09-28）

- `feishu_doc.py`：链接→document_id、上传图（`docx_image`）、组装六列表格块、按标题找插入位置、
  限流（0.4s 间隔）+ 429 退避重试、`probe()` 探针；
- CLI 新增 `doc-submit`：`--doc-url --title [--input 链路A结果] [--screenshot] [--rows-per-table] [--probe] [--reauth]`，
  结果写 JSON 文件（`ok`/文档链接/写入块数），沿用链路 B 的输出约定；
- `feishu_auth.py` 的 scope 加了 `docx:document`，并且它不再是"待删的历史包袱"（OAuth 被新链路复用）；
- **正式流程（非探针）的做法**：① 先建表，**截图列留空**（单元格必须至少有一个子块，所以放空文本块占位）；
  ② 建完表**回查一次**（`locate_table`）拿到每个单元格的真实 block_id；
  ③ 逐行在"截图"格里走图片三步（建空块 → 上传 → PATCH）。所以 **N 行 = N 次建块 + N 次上传 + N 次 PATCH**，
  限流按 3 次/秒自动节流，行多会慢；一次回查拿到整张表的单元格，别每行查一次。
- 演示数据在 `doc/test-data/doc-submit-demo.json`（手写的假数据，格式同 `--cli query` 的输出）。
- 离线用例 9 个（`tests/test_feishu_doc.py`）：链接解析、块结构自洽（单元格必须有子块、
  截图只在数据行、列数校验）、按标题找位置。**块的具体字段仍待在真文档上验**——这就是探针要干的。

### 块结构的三条硬要求（2026-09-28 实测踩出来的）

第一版写完直接报 `400 code=1770001 invalid param`，对照官方示例改对三处：

1. **`descendants` 里父块必须排在子块前面** —— 原来是"单元格内容 → 单元格 → 最后才放表格"，
   子块先于父块，必然报错。正确顺序是「表格 → 每个单元格 → 单元格里的内容」；
2. **单元格块必须带 `"table_cell": {}`** —— 少了这个字段同样判成 invalid param；
3. **表格属性只给 `row_size` / `column_size`** —— 多传 `header_row` 也可能被判无效（先不加，跑通再说）；
4. **图片块的用法是"先建空块、再绑图"，不是"先上传再引用"**：
   ① 用**「创建块」接口**（`blocks/{id}/children`，**不是**创建嵌套块）建一个 `block_type=27`、
   `image.token` 传**空字符串**的占位块，拿到它的真实 `block_id`；
   ② 再把图片上传、`parent_type=docx_image`、`parent_node` 传**那个块的 block_id**（不是文档 id）。
   ③ **PATCH 那个块**把图片换上去：`{"replace_image": {"token": "<上传拿到的 file_token>"}}`——
   少这一步块里永远是空的（前端看就是个占位）。
   我原来"先上传（parent_node 传文档 id）→ 让 image 块引用 token"的做法就是 1770001 的来源。
   单元格里的图片还要多一步：表格建完才知道单元格的真实 block_id，得先 `list_blocks` 回查
   （见 `find_cell_placeholder`）。

（前三条是探针 1/3、2/3 一次通过的；第 4 条是 3/3 报错后才定下来的——探针分步的价值就在这，
一眼看出"结构对了、只是图片块字段不全"。）

叶子块（文本/图片）统一补 `"children": []`。`tests/test_feishu_doc.py` 里加了三条回归用例盯这些
（父块先于子块、单元格带 table_cell、表格属性只有两个字段）。

### 实现顺序：先探针，再完整流程

块的具体字段没法在开发环境实测（`open.feishu.cn` 在本机网络策略下读不到官方文档），
所以**第一步只做探针**，而且是**分步**的，哪一步炸一眼看出：

```
1/3  一个文本块                        ← 权限、父块、index、最基本的块结构
2/3  一张 2×2 小表格（无图）            ← 表格 + 单元格结构
3/3  一张 2×2 小表格（某个单元格带图）  ← 图片上传 + image 块
```

跑通、人眼确认效果后再写完整流程。避免照着猜的 schema 写一大版再返工。

### 前置条件（不满足一定失败）

- **权限**：现有 OAuth 的 scope 只有 `bitable:app drive:drive`，写文档要加**文档读写**权限。
  步骤：飞书开发者后台给应用加权限 → **发一版** → 用户重新授权一次（`--reauth`）。凭据本身不用换；
- 文档要对**授权用的那个账号**（user token）有编辑权限；用 tenant token 的话得把文档共享给机器人。

### 可复用的现成代码

`feishu_auth.py` 的 OAuth 取 user_access_token 直接可用（只需把 scope 加上文档权限）；
`feishu.py` 的 `upload_image()` 就是同一个上传接口，把 `parent_type` 从 `bitable_image` 改成 `docx_image`、
`parent_node` 传 `document_id` 即可。要重写的是"往文档里写块"这部分（`feishu.py` 保持 deprecated 不动）。



### 技术选型：多维表格（Bitable）

选择多维表格而非电子表格，原因：
- 附件字段天然支持图片，点击可全屏预览
- 可切换"画册视图"，以截图为主视觉浏览
- 结构化字段与 CSV 列完美对应
- 电子表格的单元格图片不能与文字共存，体验差

### 飞书应用权限要求

在飞书开发者后台 [open.feishu.cn/app](https://open.feishu.cn/app) 创建自建应用，申请以下权限：

| 权限 scope | 用途 | 是否必须 |
|---|---|---|
| `bitable:app` | 创建多维表格、字段、批量写记录 | 必须 |
| `drive:drive` | 上传截图图片，获取 file_token | 有截图时必须 |

### 凭据说明

飞书 `app_id` 和 `app_secret` **已硬编码在代码中**（`feishu.py` / `feishu_auth.py`），打包后无需用户填写。用户只需在首次使用时通过浏览器 OAuth 授权一次，token 自动缓存 30 天并刷新。

user token 缓存于 `~/.happyhappyhappy/feishu_user_token.json`。`feishu_config.py` 保留，仅用于可选的 `folder_token` 配置。

### API 调用链路（3 步）

```
Step 1: POST /auth/v3/tenant_access_token/internal
        → tenant_access_token（有效期 2h，运行期间缓存）

Step 2（有截图时）: POST /drive/v1/medias/upload_all
        parent_type=bitable_image, parent_node=<app_token>
        → file_token

Step 3: POST /bitable/v1/apps                          建表
        POST /bitable/v1/apps/:token/tables/:id/fields  建字段（7个）
        POST /bitable/v1/apps/:token/tables/:id/records/batch_create  写记录
        → 在线链接 URL
```

多维表格字段定义：

| 字段名 | 类型 | 说明 |
|---|---|---|
| zh_cn | 文本(1) | 中文原文 |
| key | 文本(1) | i18n key |
| trip_appid | 文本(1) | 携程应用 ID |
| en_us | 文本(1) | 英文译文 |
| status | 文本(1) | found / not_found / ambiguous / confirmed |
| note | 文本(1) | 歧义行填"待研发确认" |
| 截图 | 附件(17) | 界面截图，所有记录共享同一张图 |

歧义条目（ambiguous）展开为多行写入，与 CSV 导出逻辑一致。

### GUI 操作

查询完成后，"导出 CSV"旁新增"**上传飞书**"按钮：
1. 点击弹出对话框，填写 App ID / App Secret（支持记住）
2. 可选择本地截图文件（所有记录共享同一张）
3. 可自定义表格标题（留空自动生成时间戳标题）
4. 上传成功后 SnackBar 提示链接，并自动复制到剪贴板

### CLI `upload` 子命令

```bash
happyhappyhappy --cli upload \
  --input result.json \
  [--screenshot ui_screenshot.png] \
  [--title "POS收银台词条_20260731"] \
  [--output link.txt] \
  --use-saved-feishu
```

认证参数（三选一）：

| 参数 | 说明 |
|---|---|
| `--use-saved-feishu` | 使用 `~/.happyhappyhappy/feishu.json` 中保存的凭据（推荐） |
| `--feishu-app-id` + `--feishu-app-secret` | 直接传入 |
| `--save-feishu` | 本次登录后将凭据保存到本地 |

### 典型豆包 AI 调用流程（含截图）

```
1. 豆包识图，提取中文列表，写入 texts.json
2. 豆包将截图保存到本地，如 /tmp/ui_screenshot.png
3. 豆包询问用户：这些文本是否来自同一接口？
4. 豆包调用查询：
   happyhappyhappy --cli query \
     --texts-file texts.json \
     --use-saved-creds [--same-interface] \
     --output result.json
5. 豆包调用上传：
   happyhappyhappy --cli upload \
     --input result.json \
     --screenshot /tmp/ui_screenshot.png \
     --use-saved-feishu
6. 豆包读取输出的飞书链接，在对话中汇报给用户
```

---

## skill 与文档维护

这个工具从设计之初就是**给 AI 用**的：AI 读使用文档、按文档调 CLI 跑完整流程，人只看结果。因此：

- **使用文档真源**：`doc/happyhappyhappy-i18n/SKILL.md`（也就是安装到 AI 助手账户里的那份）。
  改列契约、加场景都改这里，改完用 `save_skill` 重新安装；`doc/ai_tool_guide.md` 只是指向它的指针，
  避免同一份指南维护两处、慢慢漂移。
- **CLI 要面向 AI 设计**：每步都能单独跑、结果写结构化 JSON 文件（别让 AI 靠正则扒 stdout、也别依赖 stdout）、
  报错自带"下一步该干什么"、用退出码区分失败类型（详见"链路 B → CLI"）。
- **需要人定的必须设闸口**：小标题命中多条不许工具自选；截图场景的文本清单要等用户确认。
- **skill 正文不能写死路径**：应用只能装单文件 skill，正文里的路径要在运行时确定或直接问用户。

---

## 待定/超出范围

- 图片 URL 字段：CSV 中留空。
- shark 自动上传：本期不做，文件生成后由人工在 shark 上按项目导入。
- 文档列契约改造：第一期两张（含 Comments、"英文 by AI"）、第三/四/五期（五列，无 copywriters 列）尚未改成
  固定六列，需文档维护者先改，否则工具拒绝解析（对照表见"链路 B 第三步"）。
- 纯数字 key 已按 `LIKE '前缀%'` 处理（见链路 B 第五步）；单个前缀命中多个完整 key 时需要人工判断，
  目前只是列进报告，没有辅助手段——如果这种 case 很多，再考虑加交互确认。
- 飞书结果提交：**需求成立但挂起**，重启前先确认"对应格式的飞书文档"具体是什么（模板、截图怎么放、新建还是写入已有文档）；
  多维表格（Bitable）链路不采用，`feishu.py` / `feishu_auth.py` / `feishu_config.py` 保留并标记 deprecated。

---

## 命令行接口（CLI）

本工具同时暴露命令行接口，供 AI 助手（如豆包、ChatGPT 插件等）在识图后自动驱动整个流程，无需人工粘贴。

### 调用方式

打包后为可执行文件，命令中的 `happyhappyhappy` 对应实际产物：

| 环境 | 可执行文件 | 示例调用 |
|------|-----------|----------|
| 开发（本机） | `python main.py` | `python main.py --cli query ...` |
| Windows 打包产物 | `happyhappyhappy.exe` | `happyhappyhappy.exe --cli query ...` |
| macOS 打包产物（GUI） | `happyhappyhappy.app` | 双击启动 |
| macOS 打包产物（CLI） | `happyhappyhappy.app/Contents/MacOS/happyhappyhappy` | `happyhappyhappy.app/Contents/MacOS/happyhappyhappy --cli query ...` |

> macOS 直接运行 `happyhappyhappy` 会被系统用 `open` 命令当作 app bundle 打开，不认 `--cli` 参数。CLI 模式必须调用 bundle 内部的二进制文件。

### 子命令

#### `query` — 查询 i18n key，输出 JSON

```bash
happyhappyhappy --cli query \
  --texts-file texts.json \
  --use-saved-creds \
  [--same-interface] \
  [--output result.json]
```

#### `export` — 将查询 JSON 导出为 CSV

输入必须是 `query` 产出的 JSON，不接受 CSV。

```bash
happyhappyhappy --cli export \
  --input result.json \
  --output output.csv \
  [--ambiguous all|skip]   # 默认 all
```

#### `import` — 处理研发确认后的 CSV，输出干净 CSV

研发在 CSV 里删掉歧义行的多余候选，只保留正确的一行后发回。此命令重新处理：
- 某中文只剩 1 行 ambiguous → 升级为 `confirmed`
- 仍有多行 → 保持 `ambiguous`

```bash
happyhappyhappy --cli import \
  --input dev_confirmed.csv \
  --output final.csv
```

#### `full` — 一步完成查询 + 导出 CSV

```bash
happyhappyhappy --cli full \
  --texts-file texts.json \
  --use-saved-creds \
  --output output.csv \
  [--same-interface] \
  [--ambiguous all|skip]
```

#### `docx-titles` — 列出/搜索文档里的小标题

```bash
happyhappyhappy --cli docx-titles \
  --doc <词条表路径> \
  [--keyword 入住] \
  [--json]
```

输出每个小标题所属的「期」、名称与数据行数，供人工或 AI 选择。详见"链路 B：文档取表 → 生成 shark 导入文件"。

#### `shark-export` — 生成 shark 导入文件

```bash
happyhappyhappy --cli shark-export \
  --doc <词条表路径> \
  --title "预订-入住" \
  --outdir shark_import \
  --use-saved-creds \
  [--json]
```

按 appId 输出 `{appid}_{小标题}.xlsx` 与报告，不调用 shark 接口，导入由人工完成。
`--doc` 只在默认位置找不到词条表时才传；shark 模板已内嵌在程序里，正常不用传 `--template`
（它只是调试用的覆盖入口）。

#### `upload` — 将查询 JSON 上传到飞书多维表格【不采用】

> 载体选错了：要求是写进**对应格式的飞书文档**，不是多维表格。该子命令将从 CLI 中移除；
> `feishu.py` 等代码保留但标记 deprecated。需求本身挂起，详见"飞书结果提交（挂起）"。

```bash
happyhappyhappy --cli upload \
  --input result.json \
  [--screenshot ui_screenshot.png] \
  [--title "表格标题"] \
  [--output link.txt] \
  --use-saved-feishu
```

输出飞书在线链接到 stdout，`--output` 可选择同时写入文件。

#### 认证参数（`query` / `full` 三选一）

| 参数 | 说明 |
|------|------|
| `--use-saved-creds` | 使用 `~/.happyhappyhappy/credentials.json` 中保存的账密（推荐） |
| `--username` / `--password` | 直接传入账密 |
| `--cookie` + `--csrf` | 直接传入已有 session cookie（高级用法） |
| `--save-creds` | 登录成功后将账密保存到本地 |

#### `--same-interface` 参数说明

当截图中所有词条来自同一个接口时，加此参数：

- 工具用已 `found` 条目的 `trip_appid` 过滤歧义候选
- 同一中文只剩 1 个候选 → 自动升级为 `found`
- 若 `found` 条目本身 appid 不一致 → 输出警告"同一接口返回的 trip_appid 不同"，不自动处理

### 典型 AI 调用流程

```
1. AI 识图，提取中文列表，写入 texts.json
2. AI 询问用户：这些文本是否来自同一接口？
3. AI 调用：happyhappyhappy --cli full --texts-file texts.json --use-saved-creds [--same-interface] --output result.csv
4. AI 读取 result.csv，在对话中汇报结果
```

# 打包命令
```bash
flet pack main.py --name "happyhappyhappy" --icon "img/app.ico"
```
正式包发布
```bash
git tag v1.0.0
git push origin v1.0.0
```

测试
```bash
# 单元测试
pytest tests/test_cli.py tests/test_docx_table.py tests/test_shark.py -v
# 飞书相关（已废弃，仅在确认删除前保留）
pytest tests/test_feishu.py -v
# 端到端（需内网 + 保存凭据）
pytest tests/test_cli.py -m integration -v
```