"""
tests/test_feishu_doc.py

覆盖 feishu_doc.py 里**不联网**就能验的部分：
  - 文档链接 → document_id
  - 表格块结构（单元格必须有子块、截图列只在数据行、列数校验）
  - 按标题找插入位置（父块 + 下标）

块的具体 schema 还要在真文档上跑 `--probe` 才能确认，这些用例只保证结构自洽。

运行：pytest tests/test_feishu_doc.py -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# feishu 模块在**导入时**就会读一次应用凭据，这里先塞测试用的假值，免得本机没配就跑不了用例
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

import feishu
import feishu_doc
from feishu_doc import FeishuDocError, TABLE_HEADER


def _block(block_id, block_type, *, parent_id="", children=None, text=None, heading=False):
    block = {"block_id": block_id, "block_type": block_type, "parent_id": parent_id}
    if children is not None:
        block["children"] = children
    if text is not None:
        key = "heading1" if heading else "text"
        block[key] = {"elements": [{"text_run": {"content": text}}], "style": {}}
    return block


# ---------------------------------------------------------------------------
# 链接解析
# ---------------------------------------------------------------------------


def test_parse_document_ref_docx_link_and_raw_id():
    assert feishu_doc.parse_document_ref("https://triptest.feishu.cn/docx/AbCd123456") == ("docx", "AbCd123456")
    assert feishu_doc.parse_document_ref("AbCd123456") == ("docx", "AbCd123456")
    assert feishu_doc.parse_document_ref("  https://x.feishu.cn/docx/XYZ789?from=xx  ") == ("docx", "XYZ789")


def test_parse_document_ref_wiki_link_keeps_node_token():
    """知识库链接：URL 上那个是节点 token，不是 document_id，要再查一次换"""
    ref = feishu_doc.parse_document_ref("https://trip.larkenterprise.com/wiki/EapbwJt8YiZVIjk3uXycutu6n8b")
    assert ref == ("wiki", "EapbwJt8YiZVIjk3uXycutu6n8b")


def test_parse_document_ref_rejects_garbage():
    with pytest.raises(FeishuDocError):
        feishu_doc.parse_document_ref("随便写点啥")
    with pytest.raises(FeishuDocError):
        feishu_doc.parse_document_ref("")


def test_resolve_wiki_link_uses_obj_token(monkeypatch):
    """wiki 链接要换成文档 token：调 wiki 接口拿 obj_token"""
    client = feishu_doc.FeishuDocClient.__new__(feishu_doc.FeishuDocClient)   # 跳过 OAuth
    client.timeout = 30

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("params")))
        return {"node": {"obj_type": "docx", "obj_token": "RealDocToken123"}}

    monkeypatch.setattr(client, "_request", fake_request)

    assert client.resolve("https://trip.larkenterprise.com/wiki/NodeToken456") == "RealDocToken123"
    assert "wiki/v2/spaces/get_node" in calls[0][1]
    assert calls[0][2] == {"token": "NodeToken456", "obj_type": "wiki"}

    # /docx/ 链接不用查，直接返回
    assert client.resolve("https://x.feishu.cn/docx/Direct789") == "Direct789"


def test_resolve_wiki_link_rejects_non_docx_node(monkeypatch):
    client = feishu_doc.FeishuDocClient.__new__(feishu_doc.FeishuDocClient)
    client.timeout = 30
    monkeypatch.setattr(client, "_request",
                        lambda *a, **kw: {"node": {"obj_type": "sheet", "obj_token": "xxx"}})
    with pytest.raises(FeishuDocError):
        client.resolve("https://x.feishu.cn/wiki/NodeToken456")


# ---------------------------------------------------------------------------
# 表格块结构
# ---------------------------------------------------------------------------


def test_build_table_structure():
    payload = feishu_doc.build_table(TABLE_HEADER, [["1", "k", "中文", "en", "", ""]])

    # 顶层只有表格块一个
    assert len(payload.children_id) == 1
    table = [b for b in payload.descendants if b["block_type"] == feishu_doc.BLOCK_TABLE][0]
    assert table["block_id"] == payload.children_id[0]
    assert table["table"]["property"]["row_size"] == 2          # 表头 + 1 数据行
    assert table["table"]["property"]["column_size"] == len(TABLE_HEADER)

    # 单元格数 = 行×列，且每个单元格必须有子块
    cells = [b for b in payload.descendants if b["block_type"] == feishu_doc.BLOCK_TABLE_CELL]
    assert len(cells) == 2 * len(TABLE_HEADER)
    assert all(c["children"] for c in cells)
    assert len(table["children"]) == len(cells)

    # 单元格里引用的子块都真实存在
    ids = {b["block_id"] for b in payload.descendants}
    for cell in cells:
        for child_id in cell["children"]:
            assert child_id in ids


def test_build_table_rejects_wrong_column_count():
    with pytest.raises(FeishuDocError) as err:
        feishu_doc.build_table(TABLE_HEADER, [["1", "k", "中文"]])
    assert "6 列" in str(err.value)


def test_build_table_puts_image_only_in_data_rows_of_image_column():
    payload = feishu_doc.build_table(
        TABLE_HEADER,
        [["1", "k", "中", "en", "", ""], ["2", "k2", "中2", "en2", "", ""]],
        image_token="tok123",
        image_column=5,
    )
    images = [b for b in payload.descendants if b["block_type"] == feishu_doc.BLOCK_IMAGE]
    # 两行数据各一张，表头那格不放图
    assert len(images) == 2
    assert {i["image"]["token"] for i in images} == {"tok123"}


def test_build_table_without_image_token_falls_back_to_empty_text():
    payload = feishu_doc.build_table(TABLE_HEADER, [["1", "k", "中", "en", "", ""]], image_column=5)
    assert not [b for b in payload.descendants if b["block_type"] == feishu_doc.BLOCK_IMAGE]


# ---------------------------------------------------------------------------
# 找插入位置
# ---------------------------------------------------------------------------


def _doc_blocks():
    """造一份小文档：根块 → [标题「预订-入住」, 一个文本块]"""
    return [
        _block("page", 1, children=["h1", "t1"]),
        _block("h1", feishu_doc.BLOCK_HEADING1, parent_id="page", text="预订-入住", heading=True),
        _block("t1", feishu_doc.BLOCK_TEXT, parent_id="page", text="正文"),
    ]


def test_find_insert_position_after_heading():
    parent_id, index = feishu_doc.find_insert_position(_doc_blocks(), "预订-入住")
    assert parent_id == "page"
    assert index == 1          # 紧跟在标题后面


def test_find_insert_position_ignores_whitespace_and_case():
    blocks = _doc_blocks()
    assert feishu_doc.find_insert_position(blocks, " 预订-入住 ")[1] == 1
    blocks[1]["heading1"]["elements"][0]["text_run"]["content"] = "Reservation Check In"
    assert feishu_doc.find_insert_position(blocks, "reservation check in")[1] == 1


def test_find_insert_position_not_found_lists_candidates():
    with pytest.raises(FeishuDocError) as err:
        feishu_doc.find_insert_position(_doc_blocks(), "不存在的标题")
    message = str(err.value)
    assert "找不到标题" in message
    assert "预订-入住" in message          # 把文档里有哪些标题列出来，省得猜


# ---------------------------------------------------------------------------
# 应用凭据加载：读不出来要能说清为什么（2026-09-28 文件明明存在却说"未找到"）
# ---------------------------------------------------------------------------


def test_credentials_prefer_env(tmp_path):
    got = feishu._load_app_credentials(env={"FEISHU_APP_ID": "i", "FEISHU_APP_SECRET": "s"},
                                      home=tmp_path)
    assert got == ("i", "s")


def test_credentials_missing_file_reports_resolved_path(tmp_path):
    with pytest.raises(RuntimeError) as err:
        feishu._load_app_credentials(env={}, home=tmp_path)
    message = str(err.value)
    assert "不存在" in message
    assert str(tmp_path) in message          # 把解析出来的用户目录打出来，省得猜
    assert "查过的地方" in message


def test_credentials_reads_utf8_bom_file(tmp_path):
    """PowerShell 的 Set-Content -Encoding UTF8 会写 BOM，按 utf-8 读会让 json 解析失败"""
    cfg = tmp_path / ".happyhappyhappy"
    cfg.mkdir()
    (cfg / "feishu.json").write_text(
        "\ufeff" + json.dumps({"app_id": "cli_x", "app_secret": "sec"}), encoding="utf-8"
    )
    assert feishu._load_app_credentials(env={}, home=tmp_path) == ("cli_x", "sec")


def test_credentials_wrong_field_names_reports_actual_fields(tmp_path):
    cfg = tmp_path / ".happyhappyhappy"
    cfg.mkdir()
    (cfg / "feishu.json").write_text(json.dumps({"appId": "x", "secret": "y"}), encoding="utf-8")
    with pytest.raises(RuntimeError) as err:
        feishu._load_app_credentials(env={}, home=tmp_path)
    message = str(err.value)
    assert "没有 app_id / app_secret" in message
    assert "appId" in message                 # 告诉你文件里到底有啥字段


# ---------------------------------------------------------------------------
# 报错翻译
# ---------------------------------------------------------------------------


def test_permission_error_is_translated_into_actionable_hint():
    # 飞书那条缺权限的报错，要变成「加什么权限 + 要发版 + 重新授权」
    msg = ("Unauthorized. required one of these privileges under the user identity: "
           "[wiki:wiki, wiki:wiki:readonly, wiki:node:read]，应用未获取所需的用户授权：[...]")
    hint = feishu_doc.json_hint({"code": 99991679, "msg": msg})
    assert "wiki:node:read" in hint          # 缺什么权限，点名
    assert "发布" in hint                     # 光加权限不发版没用
    assert "--reauth" in hint


def test_extract_needed_scopes_from_message():
    assert feishu_doc.extract_needed_scopes(
        "required one of these privileges: [a:b, c:d]"
    ) == "a:b、c:d"
    assert feishu_doc.extract_needed_scopes("完全无关的报错") == ""


def test_block_error_hints():
    assert "schema" in feishu_doc.json_hint({"code": 1770006, "msg": "x"})
    assert "频率" in feishu_doc.json_hint({"code": 99991400, "msg": "x"})


def test_table_blocks_are_ordered_parent_before_child():
    """descendants 里父块必须排在子块前面——顺序反了飞书会报 1770001 invalid param"""
    payload = feishu_doc.build_table(
        TABLE_HEADER, [["1", "k", "中", "en", "", ""]], image_token="tok", image_column=5
    )
    ids = [b["block_id"] for b in payload.descendants]
    pos = {bid: i for i, bid in enumerate(ids)}
    for block in payload.descendants:
        for child_id in (block.get("children") or []):
            assert pos[child_id] > pos[block["block_id"]], (
                f"{block['block_id']}(type={block['block_type']}) 的子块 {child_id} 排在它前面了"
            )


def test_table_cells_carry_table_cell_field():
    """单元格块少 `table_cell: {}` 会被判成 invalid param"""
    payload = feishu_doc.build_table(TABLE_HEADER, [["1", "k", "中", "en", "", ""]])
    cells = [b for b in payload.descendants if b["block_type"] == feishu_doc.BLOCK_TABLE_CELL]
    assert cells
    assert all("table_cell" in cell for cell in cells)
    # 表格属性只带 row_size / column_size（多传字段也可能被判无效）
    table = [b for b in payload.descendants if b["block_type"] == feishu_doc.BLOCK_TABLE][0]
    assert set(table["table"]["property"]) == {"row_size", "column_size"}


# ---------------------------------------------------------------------------
# 图片块：token/width/height 三个都必填（少一个报 1770001）
# ---------------------------------------------------------------------------


def test_image_block_carries_token_width_height():
    block = feishu_doc.image_block("tok123", "b1", (640, 480))
    assert block["block_type"] == feishu_doc.BLOCK_IMAGE
    assert block["image"] == {"token": "tok123", "width": 640, "height": 480}


def _png(width, height):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x00" * 8)


def _jpeg(width, height):
    body = b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + b"\xff\xc0" + (len(body) + 2).to_bytes(2, "big") + body


def test_image_size_reads_png_and_jpeg(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(_png(640, 480))
    assert feishu_doc.image_size(str(png)) == (640, 480)

    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(_jpeg(1024, 768))
    assert feishu_doc.image_size(str(jpg)) == (1024, 768)


def test_image_size_reads_gif_and_bmp(tmp_path):
    gif = tmp_path / "a.gif"
    gif.write_bytes(b"GIF89a" + (300).to_bytes(2, "little") + (200).to_bytes(2, "little") + b"\x00" * 4)
    assert feishu_doc.image_size(str(gif)) == (300, 200)

    bmp = tmp_path / "a.bmp"
    header = bytearray(30)
    header[0:2] = b"BM"
    header[18:22] = (500).to_bytes(4, "little")
    header[22:26] = (400).to_bytes(4, "little")
    bmp.write_bytes(bytes(header))
    assert feishu_doc.image_size(str(bmp)) == (500, 400)


def test_image_size_falls_back_on_unknown_format(tmp_path):
    weird = tmp_path / "a.bin"
    weird.write_bytes(b"not an image at all")
    assert feishu_doc.image_size(str(weird)) == (400, 300)      # 不让整个流程失败


# ---------------------------------------------------------------------------
# 图片块的正规用法：先建空块 → 再把图片绑到这个块上
# ---------------------------------------------------------------------------


def _bare_client():
    """跳过 OAuth 的客户端，只测请求组装"""
    client = feishu_doc.FeishuDocClient.__new__(feishu_doc.FeishuDocClient)
    client.timeout = 30
    return client


def test_create_image_placeholder_uses_children_api_with_empty_token(monkeypatch):
    client = _bare_client()
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"children": [{"block_id": "real_img_block", "block_type": 27}]}

    monkeypatch.setattr(client, "_request", fake_request)

    block_id = client.create_image_placeholder("doc1", "parent1", 3)

    assert block_id == "real_img_block"
    assert calls[0][1].endswith("/docx/v1/documents/doc1/blocks/parent1/children")
    assert calls[0][2]["json"] == {
        "index": 3,
        "children": [{"block_type": feishu_doc.BLOCK_IMAGE, "image": {"token": ""}}],
    }


def test_create_image_placeholder_raises_without_block_id(monkeypatch):
    client = _bare_client()
    monkeypatch.setattr(client, "_request", lambda *a, **kw: {"children": []})
    with pytest.raises(FeishuDocError):
        client.create_image_placeholder("doc1", "parent1", 0)


def test_upload_image_binds_to_block_not_document(monkeypatch, tmp_path):
    """parent_node 必须是**图片块的 block_id**，不是文档 id"""
    client = _bare_client()
    img = tmp_path / "shot.png"
    img.write_bytes(_png(10, 20))

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"file_token": "tok_abc"}

    monkeypatch.setattr(client, "_request", fake_request)

    token = client.upload_image_to_block(str(img), "img_block_id")

    assert token == "tok_abc"
    assert calls[0][1].endswith("/drive/v1/medias/upload_all")
    assert calls[0][2]["data"]["parent_type"] == "docx_image"
    assert calls[0][2]["data"]["parent_node"] == "img_block_id"


def test_find_cell_placeholder_walks_table_cells():
    """建完表后要靠回查拿到单元格（和它里面的占位块）的真实 block_id"""

    class FakeClient:
        def list_blocks(self, document_id):
            return [
                {"block_id": "t1", "block_type": feishu_doc.BLOCK_TABLE,
                 "children": ["c0", "c1", "c2", "c3"]},
                {"block_id": "c3", "block_type": feishu_doc.BLOCK_TABLE_CELL, "children": ["p3"]},
                {"block_id": "p3", "block_type": feishu_doc.BLOCK_TEXT},
            ]

    cell_id, placeholder_id = feishu_doc.find_cell_placeholder(
        FakeClient(), "doc1", "t1", row=1, col=1, columns=2
    )
    assert cell_id == "c3"          # 第 1 行（含表头算第 0 行）第 1 列
    assert placeholder_id == "p3"


def test_find_cell_placeholder_raises_when_table_missing():
    class FakeClient:
        def list_blocks(self, document_id):
            return []

    with pytest.raises(FeishuDocError):
        feishu_doc.find_cell_placeholder(FakeClient(), "doc1", "nope", row=1, col=1, columns=2)


def test_put_image_in_block_uploads_then_patches(monkeypatch, tmp_path):
    """三步里的后两步：先上传素材，再 PATCH 块把图片换上去——顺序和请求体都要对"""
    client = _bare_client()
    img = tmp_path / "shot.png"
    img.write_bytes(_png(10, 20))

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"file_token": "tok_1"} if "upload_all" in url else {}

    monkeypatch.setattr(client, "_request", fake_request)

    token = client.put_image_in_block("doc1", str(img), "img_block")

    assert token == "tok_1"
    assert len(calls) == 2
    assert calls[0][0] == "POST" and calls[0][1].endswith("/drive/v1/medias/upload_all")
    assert calls[0][2]["data"]["parent_node"] == "img_block"      # 绑到块上，不是文档 id
    assert calls[1][0] == "PATCH"
    assert calls[1][1].endswith("/docx/v1/documents/doc1/blocks/img_block")
    assert calls[1][2]["json"] == {"replace_image": {"token": "tok_1"}}


# ---------------------------------------------------------------------------
# 正式流程（doc-submit）：每行的截图格都要走"建空块 → 上传 → PATCH"
# ---------------------------------------------------------------------------


def test_cmd_doc_submit_puts_image_into_every_row(monkeypatch, tmp_path):
    import argparse
    import json

    import cli

    logo = tmp_path / "shot.png"
    logo.write_bytes(_png(10, 20))
    data = tmp_path / "in.json"
    data.write_text(json.dumps({"results": [
        {"zh_cn": "入住成功", "status": "found", "trip_appid": "100074326",
         "key": "reception_checkIn_success", "en_us": "Check-in successful"},
        {"zh_cn": "取消", "status": "found", "trip_appid": "100074326",
         "key": "payable_cancel", "en_us": "Cancel"},
    ]}), encoding="utf-8")

    calls = []

    class FakeClient:
        def __init__(self, *a, **kw):
            self.table_created = False       # 建表前后 list_blocks 返回的东西不一样

        def resolve(self, link):
            return "docid"

        def list_blocks(self, doc):
            if not self.table_created:
                return [
                    {"block_id": "page", "block_type": 1, "children": ["h1"]},
                    {"block_id": "h1", "block_type": 3, "parent_id": "page",
                     "heading1": {"elements": [{"text_run": {"content": "测试列表"}}]}},
                ]
            # 建表之后：回查能拿到表格和它的 18 个单元格（3 行 × 6 列）
            return [
                {"block_id": "tbl1", "block_type": feishu_doc.BLOCK_TABLE,
                 "children": [f"c{i}" for i in range(18)]},
            ] + [
                {"block_id": f"c{i}", "block_type": feishu_doc.BLOCK_TABLE_CELL,
                 "children": [f"p{i}"]} for i in range(18)
            ]

        def create_descendant(self, doc, parent, index, payload):
            calls.append(("create_descendant", parent, index, payload))
            self.table_created = True
            return {"children": [{"block_id": "tbl1", "block_type": feishu_doc.BLOCK_TABLE}]}

        def locate_table(self, doc, table_id):
            # 2 行数据 + 表头 = 3 行 × 6 列
            return [f"c{i}" for i in range(18)], [f"p{i}" for i in range(18)]

        def create_image_placeholder(self, doc, cell_id, index):
            calls.append(("placeholder", cell_id, index))
            return f"img_{cell_id}"

        def put_image_in_block(self, doc, path, block_id):
            calls.append(("put", block_id))
            return "tok"

        def upload_image(self, *a, **kw):
            raise AssertionError("不该再用旧的 先上传再引用 写法")

    monkeypatch.setattr(cli.feishu_doc, "FeishuDocClient", FakeClient)

    args = argparse.Namespace(
        doc_url="https://x.feishu.cn/docx/abc", title="测试列表", input=str(data),
        screenshot=str(logo), probe=False, rows_per_table=20, label=None,
        output=str(tmp_path / "r.json"), reauth=False, json=False,
    )
    cli.cmd_doc_submit(args)

    placeholders = [c for c in calls if c[0] == "placeholder"]
    assert [c[1] for c in placeholders] == ["c11", "c17"]      # 第 1、2 行的截图列
    assert all(c[2] == 0 for c in placeholders)                 # 插在单元格第一个位置
    assert [c[1] for c in calls if c[0] == "put"] == ["img_c11", "img_c17"]

    result = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["rows"] == 2
    assert result["image_cells"] == 2
