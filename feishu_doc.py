"""
feishu_doc.py — 把校验结果写进飞书文档（docx 块）

跟 `feishu.py` 的区别：那边是**多维表格**（Bitable，已废弃），这边是**文档块**。
飞书文档是"块（block）树"，写内容 = 往某个父块下面挂子块，核心接口是"创建嵌套块"：

    POST /open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/descendant

一次请求能把带父子关系的整棵树建出来（标题块 + 表格块 + 每个单元格里的内容块）。
截图要先上传拿 file_token，再让 image 块引用它。

⚠️ 块结构（各 block_type 的字段名、表格 rows/columns 的写法）是按公开资料写的，
**还没有在真文档上实测过**——所以留了 `probe()` 做最小验证：
先插「一个标题 + 一张 2×2 小表格 + 一张图」，跑通再写完整流程。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from feishu_auth import get_user_access_token

FEISHU_BASE = "https://open.feishu.cn/open-apis"

# 文档块类型（飞书 docx）
BLOCK_TEXT = 2
BLOCK_HEADING1 = 3
BLOCK_IMAGE = 27
BLOCK_TABLE = 31
BLOCK_TABLE_CELL = 32

# 表头：跟《RezenOne翻译需求》里那张表完全一致
TABLE_HEADER = ["序号", "key", "中文", "英文", "en-US by copywriters", "截图"]

# 单文档的编辑操作限流是 3 次/秒，留点余量
_MIN_INTERVAL = 0.4
_last_call = 0.0


class FeishuDocError(Exception):
    """飞书文档接口调用失败"""


# ---------------------------------------------------------------------------
# 文档链接 / id
# ---------------------------------------------------------------------------


def parse_document_ref(link_or_id: str) -> tuple[str, str]:
    """
    把链接/ID 解析成 `(类型, token)`，类型是 `docx` 或 `wiki`。

    - `https://xxx/docx/<id>` → ("docx", id)
    - `https://xxx/wiki/<node_token>` → ("wiki", node_token)
      文档挂在知识库里时，URL 上那个是**节点 token**，不是 document_id，
      要再调一次 wiki 接口换成 obj_token（见 `FeishuDocClient.resolve`）。
    - 直接给一段 id → ("docx", id)
    """
    text = (link_or_id or "").strip()
    if not text:
        raise FeishuDocError("没给文档链接")

    hit = re.search(r"/docx/([A-Za-z0-9]+)", text)
    if hit:
        return "docx", hit.group(1)

    hit = re.search(r"/wiki/([A-Za-z0-9]+)", text)
    if hit:
        return "wiki", hit.group(1)

    if re.fullmatch(r"[A-Za-z0-9]{10,}", text):
        return "docx", text

    raise FeishuDocError(f"认不出这是不是文档链接：{text}")


# ---------------------------------------------------------------------------
# 块构造
# ---------------------------------------------------------------------------


def _text_run(content: str) -> dict:
    return {"text_run": {"content": content}}


def text_block(content: str, block_id: str, heading: bool = False) -> dict:
    """文本块 / 一级标题块"""
    payload = {"elements": [_text_run(content)], "style": {}}
    if heading:
        return {"block_id": block_id, "block_type": BLOCK_HEADING1, "heading1": payload,
                "children": []}
    return {"block_id": block_id, "block_type": BLOCK_TEXT, "text": payload, "children": []}


def image_size(path: str) -> tuple[int, int]:
    """
    读图片宽高——创建 image 块时 token / width / height **三个都必填**（少一个报 1770001）。

    只认 PNG / JPEG / GIF / BMP 的文件头（标准库搞定，不引 Pillow）；认不出来就给个默认值，
    反正宽高只是显示尺寸。
    """
    with open(path, "rb") as f:
        head = f.read(32)

    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")

    if head.startswith(b"\xff\xd8"):                      # JPEG：翻到 SOFn 段
        import struct
        with open(path, "rb") as f:
            f.read(2)
            while True:
                byte = f.read(1)
                if not byte:
                    break
                if byte != b"\xff":
                    continue
                marker = f.read(1)
                while marker == b"\xff":                   # 填充字节
                    marker = f.read(1)
                if marker in (b"\xc0", b"\xc1", b"\xc2", b"\xc3", b"\xc5", b"\xc6",
                              b"\xc7", b"\xc9", b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"):
                    f.read(3)
                    height, width = struct.unpack(">HH", f.read(4))
                    return width, height
                size_bytes = f.read(2)
                if len(size_bytes) < 2:
                    break
                f.seek(struct.unpack(">H", size_bytes)[0] - 2, 1)

    if head.startswith(b"GIF") and len(head) >= 10:
        return int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")

    if head.startswith(b"BM") and len(head) >= 26:
        return (int.from_bytes(head[18:22], "little", signed=True),
                abs(int.from_bytes(head[22:26], "little", signed=True)))

    return 400, 300          # 认不出来就别让整个流程失败，给个默认显示尺寸


def image_block(file_token: str, block_id: str, size: tuple[int, int] = (400, 300)) -> dict:
    """图片块：token / width / height 都是必填（少一个报 1770001，实测踩过）"""
    width, height = size
    return {"block_id": block_id, "block_type": BLOCK_IMAGE,
            "image": {"token": file_token, "width": int(width), "height": int(height)},
            "children": []}


def table_cell_block(block_id: str, child_ids: list[str]) -> dict:
    """
    单元格是容器块：里面**至少要有一个子块**（空也要塞空文本块），
    而且必须带 `table_cell: {}`——少了这个字段接口直接报 invalid param（实测踩过）。
    """
    if not child_ids:
        raise FeishuDocError("单元格里至少要有一个子块")
    return {"block_id": block_id, "block_type": BLOCK_TABLE_CELL, "table_cell": {},
            "children": child_ids}


def _block_id_factory(prefix: str):
    counter = {"n": 0}

    def next_id() -> str:
        counter["n"] += 1
        return f"{prefix}{counter['n']}"

    return next_id


@dataclass
class TablePayload:
    """一张表要写进文档的东西：顶层子块 id + 所有后代块"""
    children_id: list[str]
    descendants: list[dict] = field(default_factory=list)

    @property
    def block_count(self) -> int:
        return len(self.descendants)


def build_table(
    header: list[str],
    rows: list[list[str]],
    image_token: str | None = None,
    image_column: int = -1,
    image_size_px: tuple[int, int] = (400, 300),
) -> TablePayload:
    """
    组装一张表：表头行 + 数据行；`image_column` 那列每行都插同一张图（指向 image_token）。

    行的每个单元格里至少放一个块；截图列放 image 块（没有 token 时退化成空文本块）。
    """
    columns = len(header)
    for i, row in enumerate(rows):
        if len(row) != columns:
            raise FeishuDocError(f"第 {i + 1} 行有 {len(row)} 列，表头是 {columns} 列")

    nid = _block_id_factory("b")
    payload = TablePayload(children_id=[])
    table_id = nid()
    cell_ids: list[str] = []

    all_rows = [header] + rows
    # 先算好每个单元格放什么（图片块 or 文本块）
    cell_contents: list[tuple[str, dict]] = []      # (单元格 id, 里面的内容块)
    for r, row in enumerate(all_rows):
        for c, value in enumerate(row):
            cell_id = nid()
            cell_ids.append(cell_id)

            is_image_cell = (c == columns + image_column) if image_column < 0 else (c == image_column)
            if r == 0:
                is_image_cell = False          # 表头那格永远是文字

            content_id = nid()
            if is_image_cell and image_token:
                content = image_block(image_token, content_id, image_size_px)
            else:
                content = text_block(value, content_id)
            cell_contents.append((cell_id, content))

    # ⚠️ 顺序很关键：descendants 里**父块必须排在子块前面**。
    # 所以是「表格 → 单元格 → 单元格里的内容」，不是先写内容再写单元格（顺序反了会报 1770001）。
    payload.descendants.append({
        "block_id": table_id,
        "block_type": BLOCK_TABLE,
        "table": {"property": {"row_size": len(all_rows), "column_size": columns}},
        "children": cell_ids,
    })
    for cell_id, content in cell_contents:
        payload.descendants.append(table_cell_block(cell_id, [content["block_id"]]))
        payload.descendants.append(content)

    payload.children_id.append(table_id)
    return payload


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class FeishuDocClient:
    def __init__(self, app_id: str = "", app_secret: str = "",
                 force_reauth: bool = False, timeout: int = 30) -> None:
        self.timeout = timeout
        # 复用 feishu_auth 的 OAuth（scope 里必须含文档读写权限，见 spec）
        self.token = get_user_access_token(app_id, app_secret, force_reauth)

    # --- 基础请求 ---

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _throttle(self) -> None:
        """单文档编辑限流 3 次/秒，这里统一节流"""
        global _last_call
        wait = _MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()

    def _request(self, method: str, url: str, *, retries: int = 3, **kwargs) -> dict:
        last_error = ""
        for attempt in range(retries):
            self._throttle()
            resp = requests.request(method, url, headers=self._headers, timeout=self.timeout, **kwargs)
            if resp.status_code == 429:
                time.sleep(1.0 * (attempt + 1))       # 撞上限流就退避重试
                last_error = "429 请求过于频繁"
                continue
            try:
                data = resp.json()
            except ValueError:
                raise FeishuDocError(f"HTTP {resp.status_code} 返回非 JSON：{resp.text[:300]}")
            if data.get("code") == 0:
                return data.get("data") or {}
            # 块数/层级超限、schema 不对这类错误不重试，直接把「发出去的报文」也带出来，
            # 省得靠猜是哪个字段不对（排查块结构时的关键线索）
            raise FeishuDocError(
                f"HTTP {resp.status_code} code={data.get('code')} msg={data.get('msg')} "
                f"—— {json_hint(data)}\n"
                f"  请求地址：{url}\n"
                f"  请求体：{describe_payload(kwargs.get('json'))}"
            )
        raise FeishuDocError(last_error or "请求失败")

    # --- 具体接口 ---

    def upload_image(self, image_path: str, document_id: str) -> str:
        """上传截图到该文档，返回 file_token（parent_type=docx_image）"""
        size = os.path.getsize(image_path)
        if size > 20 * 1024 * 1024:
            raise FeishuDocError(f"截图超过 20MB（{size} 字节），飞书这个接口不收")
        name = os.path.basename(image_path)
        with open(image_path, "rb") as f:
            data = self._request(
                "POST", f"{FEISHU_BASE}/drive/v1/medias/upload_all",
                data={"file_name": name, "parent_type": "docx_image",
                      "parent_node": document_id, "size": str(size)},
                files={"file": (name, f)},
            )
        token = data.get("file_token")
        if not token:
            raise FeishuDocError(f"上传成功但没拿到 file_token：{data}")
        return token

    def resolve(self, link_or_id: str) -> str:
        """链接/ID → document_id（wiki 链接会去查一次节点）"""
        kind, token = parse_document_ref(link_or_id)
        if kind == "docx":
            return token

        try:
            data = self._request(
                "GET", f"{FEISHU_BASE}/wiki/v2/spaces/get_node",
                params={"token": token, "obj_type": "wiki"},
            )
        except FeishuDocError as e:
            # 文档挂在知识库里时，链接给的是节点 token，必须先换成 document_id——这一步要 wiki 权限
            raise FeishuDocError(
                f"把知识库链接换成文档 token 失败（这一步需要 wiki 只读权限）。原始报错：{e}"
            ) from e
        node = data.get("node") or {}
        obj_type, obj_token = node.get("obj_type"), node.get("obj_token")
        if obj_type != "docx" or not obj_token:
            raise FeishuDocError(
                f"知识库里的这个节点不是文档（obj_type={obj_type}），拿不到 document_id"
            )
        return obj_token

    def create_children(self, document_id: str, parent_block_id: str,
                        index: int, blocks: list[dict]) -> list[dict]:
        """
        「创建块」接口：往父块下插若干同级块，返回创建出来的块（带真实 block_id）。

        跟 create_descendant 的区别：这个不用自己编临时 id，适合"先建一个空块、拿到 id 再填内容"
        的场景——图片块就是这么用的。
        """
        data = self._request(
            "POST",
            f"{FEISHU_BASE}/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children",
            json={"index": index, "children": blocks},
        )
        return data.get("children") or []

    def create_image_placeholder(self, document_id: str, parent_block_id: str,
                                 index: int) -> str:
        """
        建一个**空的**图片块（`image.token` 传空字符串），返回它的 block_id。

        图片块不能"先上传拿 token 再引用"——飞书的用法是先建占位块，再把图片上传绑定到这个块上
        （见 upload_image_to_block）。实测：直接传上传得到的 token 会报 1770001 invalid param。
        """
        created = self.create_children(document_id, parent_block_id, index,
                                       [{"block_type": BLOCK_IMAGE, "image": {"token": ""}}])
        if not created or not created[0].get("block_id"):
            raise FeishuDocError(f"创建空图片块没返回 block_id：{created}")
        return created[0]["block_id"]

    def put_image_in_block(self, document_id: str, image_path: str, block_id: str) -> str:
        """
        把一个本地图片放进指定的空图片块：**上传素材 → PATCH 替换**（两步，缺一不可）。

        返回上传拿到的 file_token。
        """
        file_token = self.upload_image_to_block(image_path, block_id)
        self.replace_image(document_id, block_id, file_token)
        return file_token

    def upload_image_to_block(self, image_path: str, block_id: str) -> str:
        """上传图片并绑定到刚建的空图片块（parent_node 传**块的 block_id**，不是文档 id）"""
        size = os.path.getsize(image_path)
        if size > 20 * 1024 * 1024:
            raise FeishuDocError(f"截图超过 20MB（{size} 字节），飞书这个接口不收")
        name = os.path.basename(image_path)
        with open(image_path, "rb") as f:
            data = self._request(
                "POST", f"{FEISHU_BASE}/drive/v1/medias/upload_all",
                data={"file_name": name, "parent_type": "docx_image",
                      "parent_node": block_id, "size": str(size)},
                files={"file": (name, f)},
            )
        token = data.get("file_token")
        if not token:
            raise FeishuDocError(f"上传成功但没拿到 file_token：{data}")
        return token

    def replace_image(self, document_id: str, block_id: str, file_token: str) -> dict:
        """
        第三步：把上传好的素材"换"到那个空图片块上。

        少了这一步，块里始终是空的（前端看就是个占位）。请求体是：
            {"replace_image": {"token": "<上传拿到的 file_token>"}}
        """
        return self._request(
            "PATCH",
            f"{FEISHU_BASE}/docx/v1/documents/{document_id}/blocks/{block_id}",
            json={"replace_image": {"token": file_token}},
        )

    def list_blocks(self, document_id: str) -> list[dict]:
        """列出文档所有块（分页取全）"""
        items: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self._request(
                "GET", f"{FEISHU_BASE}/docx/v1/documents/{document_id}/blocks", params=params
            )
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
        return items

    def create_descendant(self, document_id: str, parent_block_id: str,
                          index: int, payload: TablePayload) -> dict:
        """在建指定父块下、第 index 个位置批量创建嵌套块"""
        body = {
            "index": index,
            "children_id": payload.children_id,
            "descendants": payload.descendants,
        }
        return self._request(
            "POST",
            f"{FEISHU_BASE}/docx/v1/documents/{document_id}/blocks/{parent_block_id}/descendant",
            json=body,
        )


def json_hint(data: dict) -> str:
    """把飞书的报错说得更直白一点（尤其"缺权限"这种，要直接说去哪儿加）"""
    code = data.get("code")
    message = str(data.get("msg") or "")

    if code in (99991672, 99991679):
        needed = extract_needed_scopes(message)
        return (
            f"缺少权限：{needed or '（见上面 msg）'}。"
            "去飞书开发者后台给应用加上对应权限 → **创建版本并发布** → 然后加 --reauth 重新授权一次"
            "（权限改了不发版，重新授权也拿不到）"
        )

    hints = {
        1770001: "参数不合法（多半是块结构 schema 不对）",
        1770002: "文档不存在（document_id 对不对？）",
        1770003: "文档已删除",
        1770004: "文档块数超上限",
        1770005: "块层级过深",
        1770006: "schema 不匹配",
        1770007: "子块数超上限",
        1770014: "父子关系不匹配",
        99991400: "请求频率超限",
    }
    return hints.get(code, "")


def describe_payload(body) -> str:
    """
    把请求体压成一行给日志看。

    块结构那种请求体很长（几百个块），全打出来没法读——这里只留结构骨架：
    顶层字段 + descendants 里前几个块的 block_type/children，图片和表格的字段单独点出来。
    """
    if not isinstance(body, dict):
        return "（无请求体）"

    if "descendants" not in body:
        return json.dumps(body, ensure_ascii=False)[:600]

    head = {k: v for k, v in body.items() if k != "descendants"}
    blocks = body.get("descendants") or []
    brief = []
    for b in blocks[:6]:
        item = {"block_id": b.get("block_id"), "block_type": b.get("block_type"),
                "children": b.get("children")}
        if "image" in b:
            item["image"] = b["image"]
        if "table" in b:
            item["table"] = b["table"]
        brief.append(item)
    return (f"{json.dumps(head, ensure_ascii=False)}；"
            f"descendants 共 {len(blocks)} 个，前 {len(brief)} 个："
            f"{json.dumps(brief, ensure_ascii=False)}")


def extract_needed_scopes(message: str) -> str:
    """从报错里抠出"required ... privileges: [a, b, c]"那串权限名"""
    hit = re.search(r"privileges[^\[]*\[([^\]]+)\]", message)
    if not hit:
        return ""
    return "、".join(x.strip() for x in hit.group(1).split(",") if x.strip())


# ---------------------------------------------------------------------------
# 定位插入点
# ---------------------------------------------------------------------------


def block_plain_text(block: dict) -> str:
    """把块里的文字拼出来（标题/文本块都在各自的字段里）"""
    for key, value in block.items():
        if not isinstance(value, dict):
            continue
        elements = value.get("elements")
        if not isinstance(elements, list):
            continue
        parts = []
        for el in elements:
            run = el.get("text_run") if isinstance(el, dict) else None
            if run and run.get("content"):
                parts.append(run["content"])
        if parts:
            return "".join(parts)
    return ""


def find_insert_position(blocks: list[dict], title: str) -> tuple[str, int]:
    """
    找到标题块，返回「它所在的父块 id」和「插到它后面的位置下标」。

    找不到就报错并列出文档里前几个标题，省得靠猜。
    """
    want = re.sub(r"\s+", "", title or "").lower()
    by_id = {b.get("block_id"): b for b in blocks}

    for block in blocks:
        if re.sub(r"\s+", "", block_plain_text(block)).lower() != want:
            continue
        parent_id = block.get("parent_id")
        parent = by_id.get(parent_id)
        if not parent:
            raise FeishuDocError(f"找到标题「{title}」但拿不到它的父块（parent_id={parent_id}）")
        children = parent.get("children") or []
        try:
            index = children.index(block.get("block_id")) + 1
        except ValueError:
            raise FeishuDocError(f"标题「{title}」不在其父块的子块列表里")
        return parent_id, index

    titles = [block_plain_text(b) for b in blocks if block_plain_text(b)][:10]
    raise FeishuDocError(f"文档里找不到标题「{title}」。文档里前几个有文字的块是：{titles}")


# ---------------------------------------------------------------------------
# 对外的两个入口：探针 / 正式写入
# ---------------------------------------------------------------------------


def locate_table(client: "FeishuDocClient", document_id: str, table_id: str) -> tuple[list[str], list[str]]:
    """
    建完表格后回查一次，按**行优先**顺序返回 (单元格 block_id 列表, 每个单元格里第一个子块 id 列表)。

    写入图片时要用：单元格的真实 block_id 建之前不知道，而"往单元格里插块"必须先有它。
    整张表只回查一次（`find_cell_placeholder` 是单个单元格的版本，给探针用）。
    """
    blocks = client.list_blocks(document_id)
    by_id = {b.get("block_id"): b for b in blocks}

    table = by_id.get(table_id)
    if not table:
        raise FeishuDocError(f"回查时找不到刚建的表格块 {table_id}")

    cell_ids = table.get("children") or []
    first_children = []
    for cell_id in cell_ids:
        kids = (by_id.get(cell_id) or {}).get("children") or []
        first_children.append(kids[0] if kids else "")
    return cell_ids, first_children


def find_cell_placeholder(client: "FeishuDocClient", document_id: str, table_id: str,
                          row: int, col: int, columns: int) -> tuple[str, str]:
    """
    建完表格后，把「第 row 行第 col 列」那个单元格找出来（**含表头，表头算第 0 行**），
    返回 (单元格 block_id, 里面第一个子块的 block_id)。

    因为单元格的真实 block_id 只有建完才知道，要往里面插东西就得这样回查一次。
    """
    blocks = client.list_blocks(document_id)
    by_id = {b.get("block_id"): b for b in blocks}

    table = by_id.get(table_id)
    if not table:
        raise FeishuDocError(f"回查时找不到刚建的表格块 {table_id}")
    cells = table.get("children") or []
    index = row * columns + col
    if index >= len(cells):
        raise FeishuDocError(f"表格只有 {len(cells)} 个单元格，取不到第 {index} 个")

    cell_id = cells[index]
    cell = by_id.get(cell_id) or {}
    kids = cell.get("children") or []
    return cell_id, (kids[0] if kids else "")


def probe(document_link: str, title: str, image_path: str | None = None,
          app_id: str = "", app_secret: str = "", force_reauth: bool = False) -> dict:
    """
    分步最小验证，插到「title」标题下面，每步都比上一步复杂一点：

      1/4  一个文本块                          ← 权限、父块、index、最基本的块结构
      2/4  一张 2×2 小表格（无图）              ← 表格 + 单元格结构
      3/4  一个独立图片块（建空块 → 上传素材 → PATCH 换图）  ← 图片块本身的正确用法
      4/4  表格里某个单元格放图                 ← "图片塞进单元格"能不能行

    哪一步失败就报哪一步（报错里还会带发出去的请求体）。
    这些都是【探针】块，人眼确认后可以删。
    """
    client = FeishuDocClient(app_id, app_secret, force_reauth)
    document_id = client.resolve(document_link)      # wiki 链接要查一次节点

    blocks = client.list_blocks(document_id)
    parent_id, index = find_insert_position(blocks, title)

    steps: list[str] = []

    def step(label: str, fn):
        try:
            result = fn()
        except FeishuDocError as e:
            raise FeishuDocError(f"【探针 {label}】失败：{e}") from e
        steps.append(f"{label} ✓")
        return result

    # 1/4 文本块（嵌套块接口）
    nid = _block_id_factory("p")
    text_id = nid()
    step("1/4 文本块", lambda: client.create_descendant(
        document_id, parent_id, index,
        TablePayload(children_id=[text_id],
                     descendants=[text_block("【探针 1/4】文本块写入正常，可删", text_id)]),
    ))
    index += 1

    # 2/4 表格（无图，嵌套块接口）
    step("2/4 表格（无图）", lambda: client.create_descendant(
        document_id, parent_id, index,
        build_table(header=["列A", "列B"], rows=[["1", "1"], ["2", "2"]]),
    ))
    index += 1

    image_token = None
    if not image_path:
        steps.append("3/4、4/4 跳过（没传 --screenshot）")
        return {"document_id": document_id, "parent_block_id": parent_id,
                "index": index, "image_token": image_token, "steps": steps}

    size = image_size(image_path)
    print(f"[feishu] 图片尺寸 {size[0]}x{size[1]}")

    # 3/4 独立图片块：先建空块（token 传空字符串）→ 再上传图片绑定到这个块
    image_block_id = step("3/4 建空图片块",
                          lambda: client.create_image_placeholder(document_id, parent_id, index))
    image_token = step("3/4 上传素材并把图片换上去",
                       lambda: client.put_image_in_block(document_id, image_path, image_block_id))
    index += 1

    # 4/4 表格里放图：先建表（截图格留空文本占位）→ 回查单元格 → 插空图片块 → 上传绑定
    created = step("4/4 表格（含图占位）", lambda: client.create_descendant(
        document_id, parent_id, index,
        build_table(header=["列A", "列B"], rows=[["1", ""], ["2", ""]]),
    ))
    index += 1
    table_id = (created.get("children") or [{}])[0].get("block_id")
    cell_id, _placeholder = find_cell_placeholder(client, document_id, table_id, row=1, col=1, columns=2)
    cell_image_id = step("4/4 在单元格里建空图片块",
                         lambda: client.create_image_placeholder(document_id, cell_id, 0))
    step("4/4 上传素材并把图片换上去",
         lambda: client.put_image_in_block(document_id, image_path, cell_image_id))

    return {
        "document_id": document_id,
        "parent_block_id": parent_id,
        "index": index,
        "image_token": image_token,
        "table_id": table_id,
        "steps": steps,
    }
