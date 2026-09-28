# happyhappyhappy

给产品/文案处理 i18n 词条的小工具。程序本体既是**桌面界面**，也是**命令行工具**——
命令行那部分主要给 AI 助手（skill）驱动，人一般只开界面。

## 它能做什么

1. **截图查词条**：对界面截图 → AI 识别出中文 → 工具去 i18n 库查出 key 和英文 → 导出 CSV。
2. **文档取表 → shark 导入文件**：从《RezenOne翻译需求》里按小标题取一张表 → 用 key 反查 appId
   → 按 appId 生成 shark 导入用的 Excel（一个项目一个文件）→ **人工**在 shark 上导入。
3. **把校验结果写进飞书文档**：把结果按固定六列表格，插进指定飞书文档的指定标题下面（截图一并上传）。

## 怎么用

**平时**：双击 `happyhappyhappy.exe`，开界面操作。

**命令行**（AI 助手用得多，人也可以自己跑）：

```bash
happyhappyhappy --cli docx-titles   --doc <词条表> --keyword 入住
happyhappyhappy --cli shark-export  --doc <词条表> --title 预订-入住 --use-saved-creds
happyhappyhappy --cli doc-submit    --doc-url <飞书文档链接> --title <标题> --input <结果JSON> --screenshot <截图>
```

⚠️ **命令的产出都是文件**（结果 JSON、CSV、xlsx、report.txt），**屏幕上看不到输出是正常的**——
直接打开生成的文件看就行。在 cmd 里跑建议加 `start /wait`，否则提示符会先回来（程序还在后台跑）。

## 目录里都有什么

| 文件 / 目录 | 说明 |
|---|---|
| `happyhappyhappy.exe` | 程序本体（双击开界面，带 `--cli` 走命令行） |
| `doc/spec.md` | 需求与设计文档，**细节都在这里** |
| `doc/happyhappyhappy-i18n/SKILL.md` | 给 AI 助手看的使用说明（skill 真源） |
| `doc/RezenOne翻译需求.docx` | copywriter 维护的词条表 |
| `doc/PrePublishImportTranslation.xlsx` | shark 导入模板（已内嵌进程序，一般不用管） |
| `doc/test-data/` | 演示数据 |

## 给改代码的人

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

python main.py                 # 开发时跑界面
python main.py --cli --help    # 开发时跑命令行

flet pack main.py --name "happyhappyhappy" --icon "img/app.ico"   # 打包成 dist/happyhappyhappy.exe
```

打包、依赖、常见坑（`flet` 命令找不到、控制台、模板更新等）都写在 `doc/spec.md` 里。

## 测试

```bash
pytest tests/test_cli.py tests/test_docx_table.py tests/test_shark.py \
       tests/test_archery_lookup.py tests/test_feishu_doc.py -v
```

## skill 的落点（改 skill 时看这里）

使用说明只有一份真源：`doc/happyhappyhappy-i18n/SKILL.md`。改完要同步到两处：

| 落点 | 位置 |
|---|---|
| 仓库（真源） | `doc/happyhappyhappy-i18n/SKILL.md` |
| Claude 桌面应用 | 用 `save_skill` 覆盖安装 |
| 豆包 | 复制到 `用户名\Doubao\skills\happyhappyhappy-i18n\SKILL.md` |
