"""
tools/embed_shark_template.py — 把 shark 导入模板内嵌成 Python 模块

shark 的导入模板是常量，程序不需要在磁盘上找它，所以把它的字节直接写进
`shark_template.py`，运行时从常量解码，调用方只要传参数就能生成 xlsx。

模板更新后（shark 换了导入格式）重新生成：

    python tools/embed_shark_template.py

脚本本身不依赖第三方库，在仓库根目录或任意目录跑都可以（按脚本位置定位仓库根）。
"""
from __future__ import annotations

import base64
import hashlib
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "doc" / "PrePublishImportTranslation.xlsx"
TARGET = REPO_ROOT / "shark_template.py"

TEMPLATE = '''"""
shark_template.py — 内置的 shark 导入模板（自动生成，别手改）

模板就是 shark 导入页面要求的空 Excel：表头 TransKey / Description / en-US / zh-CN，
第 2-4 行是释义、示例和上传提示。生成导入文件时以它当底板，只重写
xl/worksheets/sheet1.xml，其余部件原样保留，保证格式与 shark 要求一致。

为什么要内嵌：模板是常量，生成的文件每次结构都一样，没必要让程序在磁盘上找模板文件
（打包成 exe 后还要处理"exe 在 dist/、模板在仓库根目录"这类路径问题）。内嵌之后，
调用方只要传参数，程序自己就能产出 xlsx。

模板真源：{sha_doc}（sha256 {sha_short}…）
模板更新后重新生成本文件：
    python tools/embed_shark_template.py
"""
from __future__ import annotations

import base64
import hashlib

SOURCE_FILE = "{sha_doc}"
SOURCE_SHA256 = "{sha}"

_B64 = (
{b64}
)

TEMPLATE_BYTES = base64.b64decode(_B64)


def verify() -> bool:
    """自检：内嵌内容与记录的哈希一致"""
    return hashlib.sha256(TEMPLATE_BYTES).hexdigest() == SOURCE_SHA256
'''


def render(raw: bytes, source_name: str) -> str:
    sha = hashlib.sha256(raw).hexdigest()
    b64 = "\n".join(
        f'    "{line}"' for line in textwrap.wrap(base64.b64encode(raw).decode(), 96)
    )
    return TEMPLATE.format(
        sha=sha,
        sha_short=sha[:16],
        sha_doc=source_name,
        b64=b64,
    )


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"模板不存在：{SOURCE}")
    raw = SOURCE.read_bytes()
    TARGET.write_text(render(raw, f"doc/{SOURCE.name}"), encoding="utf-8")
    print(f"已生成 {TARGET.relative_to(REPO_ROOT)}"
          f"（{len(raw)} 字节模板 → {TARGET.stat().st_size} 字节模块）")


if __name__ == "__main__":
    main()
