"""
main.py — 入口文件

GUI 模式（默认）：
    python main.py

CLI 模式：
    python main.py --cli query  --texts-file texts.json --cookie '...' --csrf x
    python main.py --cli export --input result.json --output out.csv
    python main.py --cli full   --texts-file texts.json --cookie '...' --csrf x --output out.csv

打包后将 `python main.py` 替换为对应平台的可执行文件：
    Windows：happyhappyhappy.exe --cli ...
    macOS：  ./happyhappyhappy --cli ...
"""
import sys


def main() -> None:
    # 检测是否为 CLI 模式：第一个参数为 --cli
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from cli import run_cli
        run_cli(sys.argv[2:])
    else:
        from gui import run_gui
        run_gui()


if __name__ == "__main__":
    main()
