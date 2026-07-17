"""
gui.py -- Flet 0.21.2 GUI
Light theme, high-contrast result list.
"""
from __future__ import annotations

import asyncio
import json

import flet as ft

from archery import ArcheryQueryError, ArcherySession, LoginError, SessionExpiredError, login
from cli import entries_to_csv, entries_from_csv
from credentials import load as creds_load, save as creds_save, cred_path, clear as creds_clear
from models import I18nEntry

COL_ZH    = 160
COL_APPID = 150
COL_KEY   = 230
COL_EN    = 190
COL_STATUS = 90


def _header_row():
    return ft.Container(
        bgcolor="#E3E8F0",
        padding=ft.padding.symmetric(horizontal=12, vertical=8),
        border=ft.border.only(bottom=ft.BorderSide(1, "#B0BAC8")),
        content=ft.Row([
            ft.Text("中文",       weight=ft.FontWeight.BOLD, width=COL_ZH,    color="#1A1A2E", no_wrap=True),
            ft.Text("trip_appid", weight=ft.FontWeight.BOLD, width=COL_APPID, color="#1A1A2E", no_wrap=True),
            ft.Text("key",        weight=ft.FontWeight.BOLD, width=COL_KEY,   color="#1A1A2E", no_wrap=True),
            ft.Text("英文",       weight=ft.FontWeight.BOLD, width=COL_EN,    color="#1A1A2E", no_wrap=True),
            ft.Text("状态",       weight=ft.FontWeight.BOLD, width=COL_STATUS, color="#1A1A2E", no_wrap=True),
        ], spacing=8),
    )


def _simple_row(entry, alt=False):
    bgcolor = "#F8F9FB" if alt else "#FFFFFF"
    lookup = {
        "found":     ("#2E7D32", "✓ 已找到"),
        "not_found": ("#C62828", "✗ 未找到"),
        "confirmed": ("#1565C0", "✓ 已确认"),
    }
    status_color, status_text = lookup.get(entry.status, ("#555555", entry.status))
    return ft.Container(
        bgcolor=bgcolor,
        padding=ft.padding.symmetric(horizontal=12, vertical=10),
        border=ft.border.only(bottom=ft.BorderSide(1, "#E8ECF0")),
        content=ft.Row([
            ft.Text(entry.zh_cn,            width=COL_ZH,    color="#1A1A2E", no_wrap=True),
            ft.Text(entry.trip_appid or "-", width=COL_APPID, color="#444444", no_wrap=True),
            ft.Text(entry.key or "-",        width=COL_KEY,   color="#444444", no_wrap=True),
            ft.Text(entry.en_us or "-",      width=COL_EN,    color="#444444", no_wrap=True),
            ft.Text(status_text, color=status_color, weight=ft.FontWeight.W_500,
                    width=COL_STATUS, no_wrap=True),
        ], spacing=8),
    )


class AmbiguousRow(ft.UserControl):
    def __init__(self, entry, on_confirmed):
        super().__init__()
        self._entry = entry
        self._on_confirmed = on_confirmed
        self._rg = ft.RadioGroup(
            content=ft.Column(
                [ft.Radio(
                    value=f"{c.trip_appid}|{c.key}",
                    label=f"{c.trip_appid}  /  {c.key}  --  {c.en_us or '(no en)'}",
                    label_style=ft.TextStyle(color="#1A1A2E", size=13),
                ) for c in entry.candidates],
                spacing=4,
            )
        )
        self._error = ft.Text("请先选择一项", color="#C62828", size=11, visible=False)
        self._confirm_btn = ft.ElevatedButton(
            "确认选择",
            icon=ft.icons.CHECK_CIRCLE_OUTLINE,
            on_click=self._confirm,
            style=ft.ButtonStyle(
                bgcolor={"": "#1565C0"},
                color={"": "#FFFFFF"},
                padding=ft.padding.symmetric(horizontal=16, vertical=8),
            ),
        )

    def build(self):
        return ft.Container(
            bgcolor="#EEF2FF",
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            border=ft.border.only(
                left=ft.BorderSide(3, "#1565C0"),
                bottom=ft.BorderSide(1, "#C5CAE9"),
            ),
            content=ft.Column([
                ft.Row([
                    ft.Text(self._entry.zh_cn, width=COL_ZH,
                            weight=ft.FontWeight.BOLD, color="#1A1A2E", no_wrap=True),
                    ft.Text("—", width=COL_APPID, color="#888888", no_wrap=True),
                    ft.Text("待确认", width=COL_KEY, color="#1565C0", italic=True, no_wrap=True),
                    ft.Text("—", width=COL_EN, color="#888888", no_wrap=True),
                    ft.Container(
                        bgcolor="#FFF3E0", border_radius=4,
                        padding=ft.padding.symmetric(horizontal=6, vertical=2),
                        content=ft.Text("⚠ 歧义", color="#E65100", size=12,
                                        weight=ft.FontWeight.W_500),
                    ),
                ], spacing=8),
                ft.Container(
                    margin=ft.margin.only(left=8, top=8),
                    padding=ft.padding.all(12),
                    bgcolor="#FFFFFF", border_radius=6,
                    border=ft.border.all(1, "#C5CAE9"),
                    content=ft.Column([
                        ft.Text("请选择正确的 key：", size=12, color="#555555",
                                weight=ft.FontWeight.W_500),
                        self._rg,
                        ft.Row([self._error, self._confirm_btn], spacing=12),
                    ], spacing=8),
                ),
            ], spacing=0),
        )

    def _confirm(self, _):
        selected = self._rg.value
        if not selected:
            self._error.visible = True
            self.update()
            return
        appid, _, key = selected.partition("|")
        for c in self._entry.candidates:
            if c.trip_appid == appid and c.key == key:
                self._entry.trip_appid = c.trip_appid
                self._entry.key = c.key
                self._entry.en_us = c.en_us
                self._entry.status = "confirmed"
                break
        self._on_confirmed()


class MainView(ft.View):
    def __init__(self, page):
        self._page = page
        self._session = None
        self._entries = []

        self._login_status = ft.Text("未登录", color="#C62828", size=13)
        self._login_btn = ft.ElevatedButton(
            "登录 Archery", icon=ft.icons.LOGIN,
            on_click=self._open_login_dialog,
            style=ft.ButtonStyle(bgcolor={"": "#1565C0"}, color={"": "#FFFFFF"}),
        )

        self._json_input = ft.TextField(
            label='粘贴 AI 提取的 JSON，格式：{"texts": ["文本1", "文本2", ...]}',
            multiline=True, min_lines=4, max_lines=8, expand=True,
            border_color="#B0BAC8", focused_border_color="#1565C0",
            text_style=ft.TextStyle(color="#1A1A2E"),
            label_style=ft.TextStyle(color="#555555"),
        )
        self._input_error = ft.Text("", color="#C62828", size=12, visible=False)
        self._query_btn = ft.ElevatedButton(
            "开始查询", icon=ft.icons.SEARCH, on_click=self._start_query,
            style=ft.ButtonStyle(bgcolor={"": "#2E7D32"}, color={"": "#FFFFFF"}),
        )
        self._query_progress = ft.ProgressRing(visible=False, width=20, height=20)

        self._result_header = _header_row()
        self._result_header.visible = False
        self._result_list = ft.Column([], spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)

        self._export_btn = ft.ElevatedButton(
            "导出 CSV", icon=ft.icons.DOWNLOAD,
            on_click=self._export_csv, disabled=True,
            style=ft.ButtonStyle(
                bgcolor={"": "#1565C0", "disabled": "#B0BAC8"},
                color={"": "#FFFFFF"},
            ),
        )
        self._import_btn = ft.OutlinedButton(
            "导入 CSV（研发确认后）", icon=ft.icons.UPLOAD_FILE,
            on_click=self._import_csv,
            style=ft.ButtonStyle(
                side={"": ft.BorderSide(1, "#1565C0")},
                color={"": "#1565C0"},
            ),
        )

        self._file_picker_save = ft.FilePicker(on_result=self._on_save_picked)
        self._file_picker_open = ft.FilePicker(on_result=self._on_import_picked)

        super().__init__(
            route="/",
            bgcolor="#FFFFFF",
            controls=[
                ft.AppBar(
                    title=ft.Text("i18n Key 查询工具", color="#FFFFFF"),
                    bgcolor="#1565C0",
                    actions=[
                        ft.Container(
                            content=self._login_status,
                            margin=ft.margin.only(right=8),
                            alignment=ft.alignment.center,
                        ),
                        ft.Container(
                            content=self._login_btn,
                            margin=ft.margin.only(right=8),
                        ),
                    ],
                ),
                ft.Container(
                    padding=16, expand=True, bgcolor="#FFFFFF",
                    content=ft.Column([
                        ft.Row([self._json_input]),
                        self._input_error,
                        ft.Row([self._query_btn, self._query_progress], spacing=12),
                        ft.Divider(color="#E0E0E0"),
                        self._result_header,
                        self._result_list,
                        ft.Divider(color="#E0E0E0"),
                        ft.Row(
                            [self._import_btn, self._export_btn],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                    ], expand=True, spacing=8),
                ),
            ],
            padding=0,
        )

    # --- Login ---

    def _open_login_dialog(self, _):
        saved = creds_load()
        username_field = ft.TextField(
            label="用户名",
            autofocus=not bool(saved.get("username")),
            value=saved.get("username", ""),
            text_style=ft.TextStyle(color="#1A1A2E"),
        )
        password_field = ft.TextField(
            label="密码", password=True, can_reveal_password=True,
            value=saved.get("password", ""),
            autofocus=bool(saved.get("username")),
            text_style=ft.TextStyle(color="#1A1A2E"),
        )
        remember_cb = ft.Checkbox(
            label="记住密码", value=bool(saved),
            label_style=ft.TextStyle(color="#444444", size=13),
        )
        saved_hint = ft.Text(
            f"已读取保存的凭据（{cred_path()}）",
            size=11, color="#888888", visible=bool(saved),
        )
        error_text = ft.Text("", color="#C62828", size=12, visible=False)
        progress = ft.ProgressRing(visible=False, width=18, height=18)
        login_btn = ft.ElevatedButton(
            "登录",
            style=ft.ButtonStyle(bgcolor={"": "#1565C0"}, color={"": "#FFFFFF"}),
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("登录 Archery", color="#1A1A2E"),
            bgcolor="#FFFFFF",
            content=ft.Column(
                [username_field, password_field, remember_cb, saved_hint, error_text],
                tight=True, spacing=8, width=340,
            ),
            actions=[
                progress,
                ft.TextButton("取消", on_click=lambda _: self._close_dialog(dlg)),
                login_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        async def do_login(_):
            error_text.visible = False
            login_btn.disabled = True
            progress.visible = True
            self._page.update()
            uname = (username_field.value or "").strip()
            pwd = password_field.value or ""
            if not uname or not pwd:
                error_text.value = "请填写用户名和密码"
                error_text.visible = True
                login_btn.disabled = False
                progress.visible = False
                self._page.update()
                return
            try:
                session = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: login(uname, pwd)
                )
                if remember_cb.value:
                    creds_save(uname, pwd)
                else:
                    creds_clear()
                self._session = session
                self._login_status.value = f"已登录：{uname}"
                self._login_status.color = "#A5D6A7"
                self._close_dialog(dlg)
            except LoginError as e:
                error_text.value = str(e)
                error_text.visible = True
                login_btn.disabled = False
                progress.visible = False
                self._page.update()
            except Exception as e:
                error_text.value = f"网络错误：{e}"
                error_text.visible = True
                login_btn.disabled = False
                progress.visible = False
                self._page.update()

        login_btn.on_click = do_login
        self._page.dialog = dlg
        dlg.open = True
        self._page.update()

    def _close_dialog(self, dlg):
        dlg.open = False
        self._page.update()

    # --- Query ---

    def _start_query(self, _):
        async def run():
            self._input_error.visible = False
            raw = (self._json_input.value or "").strip()
            try:
                data = json.loads(raw)
                texts = data["texts"]
                if not isinstance(texts, list) or not texts:
                    raise ValueError
            except Exception:
                self._input_error.value = '格式不合法，期望：{"texts": ["文本1", ...]}'
                self._input_error.visible = True
                self._page.update()
                return
            if self._session is None:
                self._snack("请先登录 Archery", error=True)
                return
            self._query_btn.disabled = True
            self._query_progress.visible = True
            self._page.update()
            try:
                entries = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._session.query_texts(texts)
                )
                self._entries = entries
                self._render_results()
            except SessionExpiredError:
                self._session = None
                self._login_status.value = "未登录"
                self._login_status.color = "#C62828"
                self._snack("Session 已过期，请重新登录", error=True)
            except ArcheryQueryError as e:
                self._snack(f"查询失败：{e}", error=True)
            except Exception as e:
                self._snack(f"网络错误：{e}", error=True)
            finally:
                self._query_btn.disabled = False
                self._query_progress.visible = False
                self._page.update()

        self._page.run_task(run)

    def _render_results(self):
        rows = []
        for i, entry in enumerate(self._entries):
            if entry.status == "ambiguous":
                rows.append(AmbiguousRow(entry, on_confirmed=self._render_results))
            else:
                rows.append(_simple_row(entry, alt=(i % 2 == 1)))
        self._result_list.controls = rows
        self._result_header.visible = bool(self._entries)
        self._export_btn.disabled = not bool(self._entries)
        self._page.update()

    # --- Export / Import ---

    def _export_csv(self, _):
        self._file_picker_save.save_file(
            dialog_title="保存 CSV",
            file_name="i18n_keys.csv",
            allowed_extensions=["csv"],
        )

    def _on_save_picked(self, e):
        if not e.path:
            return
        path = e.path if e.path.endswith(".csv") else e.path + ".csv"
        try:
            written = entries_to_csv(self._entries, path, ambiguous="all")
            self._snack(f"已导出 {written} 行 → {path}")
        except PermissionError:
            self._snack("无写入权限，请选择其他路径", error=True)
        except Exception as ex:
            self._snack(f"导出失败：{ex}", error=True)

    def _import_csv(self, _):
        self._file_picker_open.pick_files(
            dialog_title="选择要导入的 CSV（研发确认后）",
            allowed_extensions=["csv"],
            allow_multiple=False,
        )

    def _on_import_picked(self, e):
        if not e.files:
            return
        path = e.files[0].path
        try:
            entries = entries_from_csv(path)
            self._entries = entries
            self._render_results()
            ambiguous_count = sum(1 for en in entries if en.status == "ambiguous")
            confirmed_count = sum(1 for en in entries if en.status == "confirmed")
            msg = f"已导入 {len(entries)} 条"
            if confirmed_count:
                msg += f"，其中 {confirmed_count} 条已确认"
            if ambiguous_count:
                msg += f"，{ambiguous_count} 条仍有歧义"
            self._snack(msg)
        except Exception as ex:
            self._snack(f"导入失败：{ex}", error=True)

    # --- Helpers ---

    def _snack(self, msg, error=False):
        self._page.snack_bar = ft.SnackBar(
            content=ft.Text(msg, color="#FFFFFF"),
            bgcolor="#C62828" if error else "#2E7D32",
            open=True,
        )
        self._page.update()


def run_gui():
    def main(page):
        page.title = "i18n Key 查询工具"
        page.bgcolor = "#FFFFFF"
        page.window_width = 1000
        page.window_height = 720
        page.window_min_width = 750
        page.window_min_height = 520
        page.theme = ft.Theme(color_scheme_seed="#1565C0", use_material3=True)

        main_view = MainView(page)
        page.overlay.append(main_view._file_picker_save)
        page.overlay.append(main_view._file_picker_open)
        page.views.clear()
        page.views.append(main_view)
        page.update()

    ft.app(target=main)
