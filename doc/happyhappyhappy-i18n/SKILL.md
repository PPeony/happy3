---
name: happyhappyhappy-i18n
description: Use when a user sends a UI screenshot to extract i18n keys, wants shark import files built from the RezenOne翻译需求 docx, or wants the check result written into a Feishu doc.
---

# happyhappyhappy — AI 工具指南

你在帮产品/文案处理 i18n 词条。本工具 `happyhappyhappy` 有三条流程：

- **A：截图 → 查词条 → CSV**。从界面截图识别中文，去内网 i18n 库查 key 和英文，导出 CSV。
- **B：文档取表 → shark 导入文件**。从《RezenOne翻译需求》文档里按小标题取一张表，
  用 key 反查 appId，按 appId 生成 shark（词条管理平台）的导入文件，由人工在 shark 上按项目导入。
- **C：把结果写进飞书文档**。把校验结果按固定六列表格，插进指定飞书文档的指定标题下面（截图一并上传）。

**关键纪律：B 流程"选哪张表"必须由用户确认，命中多条时不许自己挑一张。**

---

## 前置条件

1. **凭据**：用户需先在 GUI 登录过并勾了"记住密码"，凭据在 `~/.happyhappyhappy/credentials.json`。
   没有就先让用户打开程序登录。
2. **可执行文件**：`happyhappyhappy` 已装进 PATH，**直接调用即可，不需要知道它装在哪**，
   命令里就写 `happyhappyhappy`。唯一的例外是 macOS：直接跑 `.app` 会被系统当成 app 打开、不认 `--cli`，
   这种情况用 bundle 里的二进制全路径 `happyhappyhappy.app/Contents/MacOS/happyhappyhappy`。
   注意 CLI 的产出**都在文件里**，屏幕上看不到东西是正常的（打包产物没有控制台，
   `sys.stdout` / `stderr` 都是 `None`）——读结果文件即可，见下面 B1。
   另外这个 exe 是 **GUI 子系统**程序，**cmd.exe 不等它跑完就返回提示符**（看着像异步，其实还在后台跑）——
   在 cmd 里要用 `start /wait happyhappyhappy.exe ...`；不加的话别急着读结果文件，等十几秒再看。
3. **词条表**（B 流程用）：**由使用方提供路径**，用 `--doc <路径>` 传给工具——绝对路径、相对路径都行，
   文件名随意，不需要放进任何特定目录。开发时如果它就放在 `doc/` 下（工具目录、其上一级或当前目录），
   不传 `--doc` 也能找到，但这只是便利，别依赖。
   shark 导入模板是常量、已经内嵌在程序里，**不需要传 `--template`**（那是调试用的覆盖入口）。
   用户没给路径时就直接问他要，别自己去找一份来用。

---

## 一、B 流程：文档 → shark 导入文件

### 文档列契约（这条同时是给文档维护者的要求）

表格必须是下面六列，**列名与顺序都不能变，表头必须是第一行**：

| 序号 | key | 中文 | 英文 | en-US by copywriters | 截图 |

- 增列、删列、改名、加说明行都会让工具**直接报错**（报错里会同时打印期望列与实际列），工具不做猜测。
- `序号`、`截图` 只用于人工核对，不参与导出。
- `key` 为空的行会被跳过并计入报告。
- `en-US by copywriters` 为空的行**不导出**——空值会把 shark 上已有的英文覆盖掉。
- 截至 2026-09-24，只有第二期的四张表（预订-入住、换房/升级、同住、联房）符合这个契约；
  第一期两张带 `Comments` / `英文 by AI`，第三、四、五期是五列。这些表要先让文档维护者改成六列，
  改之前工具会拒绝解析。

### Step B1 — 搜索小标题

```bash
happyhappyhappy --cli docx-titles --doc <词条表路径> --keyword 入住
```

**结果一律写文件，读文件，别指望屏幕输出。** 打包产物是没有控制台的 GUI 程序（`console=False`），
`sys.stdout` / `stderr` 都是 `None`，`print()` 会**静默失效**——命令成功、退出码 0，屏幕上什么都没有。
两条新子命令的结果 JSON 都带 `ok` 字段：`true` 成功；`false` 时 `error` 里是原因
（**出错也会落文件**，所以不看屏幕也能知道为什么失败）。

- 结果文件默认是**当前目录**的 `happyhappyhappy-docx-titles.json`，可用 `--output` 指定别处。
- 关键词忽略大小写、空格、全半角，也可以是"第二期 预订-入住"这种组合。
- 什么都不传则列出全部表，返回里带 `period` / `title` / `row_count` / `compliant`。
- **命中多条 → 把候选列给用户，让用户选。命中 0 条 → 把相近候选给用户。都不要自己决定。**
- `compliant: false` 的表不能导出，先告诉用户需要改文档。

### Step B2 — 生成 shark 导入文件

```bash
happyhappyhappy --cli shark-export \
  --doc <词条表路径> \
  --title 预订-入住 \
  --outdir shark_import \
  --use-saved-creds
```

- `--title` 是 B1 里用户选中的小标题关键词，必须唯一命中，否则报错并列出候选。
- 工具用 key 反查 appId（key 已知，不做模糊匹配）。
- **key 会被校正成库里的写法**，结果 JSON 的 `expanded_keys` 里是 `文档里的写法 → 库里真正的写法`，
  汇报时可以带上让用户核对。两种校正：
  ① 纯数字 key（错误码那种）在文档里只剩截断后的一段数字，真身形如 `reception_error_10194060`，按「带下划线的后缀 → 任意字符的后缀 → 包含」三趟 LIKE 依次找回完整 key；
  ② 库里大小写和文档不同（如文档 `reception_Register_guest_info` / 库里 `reception_register_guest_info`）。
  **写进导入文件的一律是库里的写法**。一趟里命中多个完整 key 时不猜，列进 `skipped`（`multi_real_key`）。
- **一个 appId 一个文件**，命名 `{appid}_{小标题}.xlsx`，写在 `--outdir` 下。
  ⚠️ 所以**一张表可能产出好几个文件**——用户问"某个 key 怎么没导出来"时，
  先把所有产出的 xlsx 都搜一遍，再看 `skipped`。
- **结果文件**（默认 `<outdir>/happyhappyhappy-shark-export.json`，可用 `--output` 指定）里带
  `ok` / `tool`（构建时间）/ `files`（appid、path、row_count、in_whitelist）
  / `skipped`（key、reason、detail）/ `report_path`；`<outdir>/report.txt` 是同一份内容的人类可读版，
  第一行 `程序：` 就是构建时间。
- 文件格式对齐 shark 模板（模板已内嵌在程序里，不用传）：表头 `TransKey / Description / en-US / zh-CN`，
  数据从第 2 行开始，模板自带的释义/示例行不写进去；`Description` 留空；
  `en-US` 取 copywriters 校对值；`zh-CN` 取文档"中文"列。

退出码：`0` 成功；`1` 取表失败/列不符/文档问题；`2` Archery 登录过期（让用户重新登录）；`3` 查询失败；
`4` 一行都没导出（当失败处理）。

结果 JSON 里 `skipped[].reason` 的取值：`not_found`（库里查不到 appId）、`multi_appid`（命中多个 appId）、
`multi_real_key`（数字 key 匹配到多个完整 key）、`empty_key`（文档里 key 为空）、
`empty_copywriter`（校对值为空，按规则不导出）。

### Step B3 — 汇报（必须说全三件事）

1. **appId → 文件名 → 行数**，让用户知道哪个文件要导入 shark 的哪个项目（一张表可能拆成多个文件，要列全）；
2. **未导出的 key**：`not_found` 的说明库里确实没这个 key（数字 key 已按后缀/包含几趟找回，还查不到多半是文档里写错了）；
   `multi_appid` / `multi_real_key` 的要人工判断该归哪个；
3. 提醒用户**人工**在 shark 上按项目导入——本期没有上传接口，不要尝试替用户上传。

报告里如果标了"该 appId 不在白名单里"，也要一并告诉用户。

---

## 二、A 流程：截图 → 查词条 → CSV

### Step A1 — 收到截图

**把截图存到固定路径**，后面 Step A4 汇报时引用：`~/Desktop/ui_screenshot.png`（Windows/macOS 同）。

提取前**先问用户一个问题**：

> 这些文本是否来自同一个接口或同一类数据？（如果是，我可以自动推断重复词条的归属）

用户答"是" → Step A3 的命令加 `--same-interface`；答"不是/不确定" → 不加。

### Step A2 — 提取中文并**停下等确认**

把识别到的中文按序号列给用户：

> 我从截图中识别到以下中文文本，请确认是否正确：
> 1. 金额
> 2. 房间号
> 3. 确认订单

**停在这里，用户确认前不要往下走。** 用户回"对/没问题/确认"→ 继续；回"少了XX/去掉XX/改成XX"→ 改完重新确认。

### Step A3 — 查询并导出

把确认后的清单写进 `~/Desktop/texts.json`：

```json
{"texts": ["金额", "房间号", "确认订单"]}
```

```bash
happyhappyhappy --cli full \
  --texts-file ~/Desktop/texts.json \
  --use-saved-creds \
  --output ~/Desktop/i18n_result.csv \
  [--same-interface]
```

### Step A4 — 读 CSV 并汇报

读 `~/Desktop/i18n_result.csv`（UTF-8 with BOM），在对话里汇报，不要让用户自己打开文件：

> 查询完成，共 N 条：
> - ✓ 金额 → key: pos.amount，英文: Amount
> - ⚠ 确认订单 → 有歧义，已写入 CSV，请发给研发确认

状态含义：`found` 唯一命中（含 same-interface 自动解决的）；`not_found` 库里没有；
`ambiguous` 多个候选，CSV 里带 `note=待研发确认`，让用户发研发；`confirmed` 已确认。

### 歧义回流

研发在 CSV 里删掉多余候选行、每个中文只留一行后发回，跑：

```bash
happyhappyhappy --cli import --input dev_confirmed.csv --output final.csv
```

---

## 三、写飞书文档：把结果提交给 copywriter

用户说"把结果写到飞书文档 / 提交给 copywriter / 写到某文档里"时走这条。
需要用户提供**文档链接**和**文档里那个标题的文字**。

```bash
happyhappyhappy --cli doc-submit \
  --doc-url <飞书文档链接> \
  --title <文档里那个标题，一字不差> \
  --input <链路 A 的查询结果 JSON> \
  --screenshot <界面截图>
```

- `--doc-url` 支持 `/docx/xxx` 和 `/wiki/xxx` 两种链接（知识库链接会自动换成文档 token）；
- `--title` 必须和文档里那个标题**一字不差**（空格、全半角都算），工具按它找位置、把内容插在它**下面一行**。
  找不到会报错，并把它在文档里看到的前几个标题列出来——**拿候选问用户，别自己猜**；
- 表格固定六列（序号 / key / 中文 / 英文 / en-US by copywriters / 截图）；链路 A 没有校对值，
  所以 `en-US by copywriters` 那列留空；
- **截图**：每一行的截图格都插同一张图。飞书那边一张图要走三步（建空图片块 → 上传素材 → PATCH 换图），
  所以 **N 行 = N 次建块 + N 次上传 + N 次 PATCH**，再叠加 3 次/秒的限流——**十几二十秒很正常，别当卡死**；
- 每次写入会先插一个带时间戳的**批次小标题**，重跑时能看出上一批写到哪；
- 行数多会按 `--rows-per-table`（默认 20）自动拆成多张表；
- 结果写在当前目录的 `happyhappyhappy-doc-submit.json`（`ok` / 文档链接 / 行数 / 写完的图片数）。

### `--probe` 只用来验证环境，正式写入别加

`--probe` 是最小验证：只插一个文本块 + 一张 2×2 小表格 + 一张图，用来确认权限和块结构没问题。
它插的块都带【探针】字样、可以删。**正式写入不要带 `--probe`。**

### 授权只需要一次

第一次跑会打开浏览器，让用户点"同意授权"；之后 token 缓存 30 天、自动刷新。
**平时不要加 `--reauth`**——那是"强制重新授权"，加了就次次弹浏览器。
只有改了权限、换了账号、或缓存彻底过期时才需要。

---

## 四、CLI 参考

| 子命令 | 输入 | 输出 |
|---|---|---|
| `full` | texts.json + 凭据 | CSV |
| `query` | texts.json + 凭据 | JSON |
| `export` | `query` 的 JSON | CSV |
| `import` | 研发改过的 CSV | 干净的 CSV |
| `docx-titles` | docx + 关键词 | 小标题列表 → 结果 JSON |
| `shark-export` | docx + 小标题 + 凭据 | `{appid}_{小标题}.xlsx` + report.txt + 结果 JSON |
| `doc-submit` | 飞书文档链接 + 标题 + 结果 JSON | 往文档里写六列表格（结果 JSON 带 `ok`） |

认证参数（`query` / `full` / `shark-export` 通用，选一个）：

| 参数 | 说明 |
|---|---|
| `--use-saved-creds` | 用 `~/.happyhappyhappy/credentials.json`（推荐） |
| `--username` / `--password` | 直接传账密 |
| `--cookie` + `--csrf` | 传已有 session cookie（高级） |
| `--save-creds` | 登录成功后保存账密 |

### `--same-interface`

用户确认所有文本来自同一接口时加：用已命中条目的 `trip_appid` 过滤歧义候选，只剩一个就升级为 `found`；
若命中条目的 appid 本身不一致，打印警告且不自动处理——这时把警告原样告诉用户。

---

## 五、错误处理

| 报错 | 原因 | 怎么办 |
|---|---|---|
| `Error: no saved credentials` | 没登录过 | 让用户开程序登录并勾"记住密码" |
| `Auth error: Session 已过期` | session 过期 | 重新登录 |
| `Error: 没找到小标题「…」。相近候选：…` | 关键词没命中 | 拿候选问用户 |
| `Error: 关键词「…」命中多张表：…` | 命中多条 | 把候选给用户选，或让用户加 `--period` |
| `Error: 表格「…」的列不符合契约` | 文档列不合规 | 把期望六列与实际列告诉用户，让维护者改文档 |
| `Error: 文档不存在/不是合法的 docx` | `--doc` 路径给错了 | 拿报错里的路径跟用户核对，问他要正确路径 |
| `Query error: ...` | Archery SQL 出错 | 让用户找研发 |
| network timeout | 不在内网 | 让用户确认 VPN / 办公网 |
| 退出码 4 | 一行都没导出 | 把 skipped 清单汇报给用户，按原因处理 |
| 数字 key 报 `not_found`、但 `skipped` 的 detail 里**没有**「已按…N 趟匹配过」这句 | 跑的是旧包 | 报告第一行有 `程序：xxx（构建时间）`，拿它跟源码修改时间对一下；对不上就让用户重新打包 |
| 命令跑完屏幕上看不到东西 | 打包产物没有控制台，stdout/stderr 都是 `None`，这是**正常现象** | 不要当成失败：去读结果文件（默认 `happyhappyhappy-docx-titles.json` / `<outdir>/happyhappyhappy-shark-export.json` / `happyhappyhappy-doc-submit.json`），看 `ok` 字段 |
| 找不到文档里的标题（报错里列了候选） | `--title` 跟文档里的文字不一致 | 拿报错列出的标题问用户要哪个 |
| 报错里出现 `20029` | 回调地址没登记在该应用的「安全设置 → 重定向 URL」 | 让用户把 `http://localhost:19721/callback` **原样**加进去（只写 `http://localhost` 不算） |
| 报错里出现 `99991679` / `99991672`（缺权限） | 后台没加权限，或加了没**发版** | 让用户去后台加权限 → **创建版本并发布** → 再用 `--reauth` 重新授权一次 |
| 报错里出现 `1770001` | 块结构不对（多半是工具版本旧） | 让用户重新打包；报错里会带发出去的请求体 |
| cmd 里提示符立刻回来了、像"没跑完" | exe 是 GUI 子系统程序，**cmd.exe 不等 GUI 程序**（进程其实还在后台跑） | 用 `start /wait happyhappyhappy.exe --cli ...`；或等十几秒再读结果文件 / `happyhappyhappy-cli.log` |

---

## 六、不要做的事

- 不要调用 `upload`（多维表格那条已经移除了）；写飞书文档走 `doc-submit`，别自己拼接口。
- 不要为了"让工具跑通"去改文档结构；文档列不对就反馈给人去改。
- 不要在用户没确认时替用户选表；也不要替用户在 shark 上导入。
