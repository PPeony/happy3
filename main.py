"""
main.py — 入口文件

GUI 模式（默认）：
    python main.py

CLI 模式：
    happyhappyhappy.exe --cli docx-titles --doc <词条表> --keyword 入住
    happyhappyhappy.exe --cli shark-export --doc <词条表> --title 预订-入住 --use-saved-creds
    happyhappyhappy.exe --cli doc-submit --doc-url <飞书文档> --title <标题> --probe
    happyhappyhappy.exe --cli query  --texts-file texts.json --use-saved-creds

CLI 的产出**一律是文件**（结果 JSON、CSV、xlsx、report.txt），不依赖命令行输出：
打包成 Windows GUI 子系统程序（`flet pack` 默认，也是给产品双击的那个包）时
PyInstaller 会把 sys.stdout / stderr 都置成 None，`print()` 会静默失效——命令成功、退出码 0，
但调用方什么都看不到。

所以除了各子命令自己写的结果文件，这里还会把**整次调用的输出**（包括 argparse 的报错、
未捕获的异常栈）追加到**当前目录**的 `happyhappyhappy-cli.log`。排查"为什么没结果"先看它。
"""
import os
import sys
import traceback
from datetime import datetime

CLI_LOG = "happyhappyhappy-cli.log"


class _Tee:
    """写往 stdout/stderr 的内容同时留一份在内存里，退出时落盘。"""

    def __init__(self, real, buffer: list) -> None:
        self._real = real
        self._buffer = buffer

    def write(self, text: str) -> int:
        if text:
            self._buffer.append(text)
        if self._real is not None:
            try:
                self._real.write(text)
            except Exception:
                pass
        return len(text or "")

    def flush(self) -> None:
        if self._real is not None:
            try:
                self._real.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


def _run_cli(argv: list) -> None:
    """跑 CLI，并把这次调用的全部输出（含报错）追加到 happyhappyhappy-cli.log"""
    buffer: list = []
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(real_out, buffer)
    sys.stderr = _Tee(real_err, buffer)

    exit_code = 0
    try:
        from cli import run_cli
        run_cli(argv)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        exit_code = 1
        buffer.append("\n" + traceback.format_exc())
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        try:
            text = "".join(buffer).rstrip()
            with open(os.path.join(os.getcwd(), CLI_LOG), "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} | 退出码 {exit_code} =====\n")
                f.write("命令行: happyhappyhappy --cli " + " ".join(argv) + "\n")
                f.write((text + "\n") if text else "（这次调用没有任何输出）\n")
        except Exception:
            pass          # 写日志失败绝不能影响主流程

    sys.exit(exit_code)


def main() -> None:
    # 检测是否为 CLI 模式：第一个参数为 --cli
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        _run_cli(sys.argv[2:])
    else:
        from gui import run_gui
        run_gui()


if __name__ == "__main__":
    main()
