# -*- mode: python ; coding: utf-8 -*-
#
# 这个 spec 是 `flet pack` 上一次运行时生成的，可以直接用它构建：
#     python -m PyInstaller happyhappyhappy.spec
#
# 注意：再跑一次 `flet pack` 会把这个文件重新生成、覆盖掉下面的改动。
#
# 原文件里的 version='D:\...\Temp\59faf33b-...' 是上次 flet pack 生成的临时文件路径，早已失效，
# 留着会让构建报错，已删掉。需要版本信息就自己写一个 version 文件再把路径填回 EXE() 里。
#
# console=False：这个包是给产品双击用的，不该多一个黑窗口。
# 代价是 PyInstaller 会把 sys.stdout / stderr 置成 None、`print()` 静默失效——
# 所以 CLI 的产出**一律走文件**（结果 JSON / CSV / xlsx / report.txt），调用方读文件而不是读屏幕。
# 老的子命令本来就是这样（--output 出文件），新的 docx-titles / shark-export 也按这个约定做。

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='happyhappyhappy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['img\\app.ico'],
)
