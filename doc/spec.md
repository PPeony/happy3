# 效率工具 — i18n Key 查询工具 规格文档

## 背景与目标

携程 copywriter 在校验国际化词条时，需要同时提供 key、中文原文、英文译文，以及对应界面的截图。

对于后端词条，产品可以在界面上看到中文显示，但无法直接获得 i18n key。本工具的目标是让产品人员能够快速完成从截图到结构化 CSV 的全流程，减少手工翻查的工作量。

---

## 整体流程

```
[人工] 截图  →  [AI识图] 提取中文  →  [工具] 查 Archery  →  [工具] 歧义确认  →  [工具] 导出 CSV  →  [人工] 上传飞书
```

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

## 界面结构

### 主界面

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

---

## 技术栈

| 项目 | 选型 |
|------|------|
| 语言 | Python 3.x（开发机 3.14，打包用 3.12） |
| UI 框架 | Flet 0.21.2 |
| 网络请求 | `requests` 库 |
| 登录 | 账号密码直接 POST `/authenticate/` |
| CSV 导出/导入 | Python 内置 `csv` 模块 |
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
```

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
- push 到 `master` 分支自动触发打包，产物挂在 GitHub Actions Artifacts（保留 30 天）
- 打 tag（如 `v1.0.0`）自动创建 GitHub Release，两个平台安装包作为附件
- 本机（Windows）开发阶段直接运行 `python main.py` 启动 GUI，或 `python main.py --cli ...` 使用 CLI

---

## 飞书多维表格上传

### 背景

词条校验完成后，copywriter 需要把结果（含截图）提交给飞书 copywriter 系统。本功能支持将查询结果一键上传为飞书在线多维表格，并可附带界面截图。

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

## 待定/超出范围

- 图片 URL 字段：CSV 中留空（飞书多维表格已通过附件字段承载截图，CSV 不需要）

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

#### `upload` — 将查询 JSON 上传到飞书多维表格

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
pytest tests/test_cli.py tests/test_feishu.py -v
# 端到端（需内网 + 保存凭据）
pytest tests/test_cli.py -m integration -v
```