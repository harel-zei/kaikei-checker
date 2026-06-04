"""
会計データ確認シート Excel出力モジュール
テンプレート形式（チェックシート・記入例）に準拠して xlsx を生成する
"""
import io
import re
from openpyxl import Workbook
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side,
    GradientFill,
)
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.formatting.rule import Rule
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

# ── カラー定数 ──────────────────────────────────────────
COL_TITLE_BG    = "F47A20"   # オレンジ（ヘッダー背景）
COL_TITLE_FG    = "FFFFFF"   # 白文字
COL_HEADER_BG   = "FDE8D8"   # 薄オレンジ（列ヘッダー背景）
COL_HEADER_FG   = "7A3800"   # 茶文字
COL_DONE_BG     = "D0D0D0"   # グレー（対応済み行）
COL_BORDER      = "E8D5C0"   # ボーダー色
COL_ROW_ALT     = "FDF9F5"   # 交互行背景（薄クリーム）

THIN  = Side(style="thin",   color=COL_BORDER)
MED   = Side(style="medium", color="C09060")
BORDER_ALL  = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
BORDER_MED  = Border(left=MED,  right=MED,  top=MED,  bottom=MED)


def _cell_style(ws, coord, value=None, *,
                bold=False, size=11, fg=None, bg=None,
                halign="left", valign="center",
                wrap=False, border=None):
    cell = ws[coord]
    if value is not None:
        cell.value = value
    cell.font      = Font(name="メイリオ", size=size, bold=bold,
                          color=fg or "000000")
    cell.alignment = Alignment(horizontal=halign, vertical=valign,
                               wrap_text=wrap)
    if bg:
        cell.fill = PatternFill("solid", fgColor=bg)
    if border:
        cell.border = border
    return cell


def build_checksheet_xlsx(
    issues: list,
    period: str = "",
    client_name: str = "",
) -> bytes:
    """
    チェック結果を xlsx に変換して bytes で返す。

    Args:
        issues      : チェック結果リスト（message/account/month を持つ dict）
        period      : 対象月（例: "2026年5月分"）
        client_name : クライアント名
    """
    wb = Workbook()
    ws_check  = wb.active
    ws_check.title = "チェックシート"
    ws_sample = wb.create_sheet("記入例")

    _build_sheet(ws_check, issues, period, client_name)
    _build_sample_sheet(ws_sample)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_sheet(ws, issues, period, client_name):
    # ── 列幅設定 ──
    ws.column_dimensions["A"].width = 18   # 勘定科目
    ws.column_dimensions["B"].width = 22   # 補助科目
    ws.column_dimensions["C"].width = 12   # 日付
    ws.column_dimensions["D"].width = 60   # 確認内容
    ws.column_dimensions["E"].width = 25   # ご回答
    ws.column_dimensions["F"].width = 10   # 対応済
    ws.column_dimensions["G"].width = 20   # 備考

    # ── 行高設定 ──
    ws.row_dimensions[1].height = 28  # タイトル
    ws.row_dimensions[2].height = 18
    ws.row_dimensions[3].height = 18
    ws.row_dimensions[4].height = 22  # ヘッダー

    # ── Row1: タイトル ──
    ws.merge_cells("A1:G1")
    _cell_style(ws, "A1", "会計データ確認シート",
                bold=True, size=14, fg=COL_TITLE_FG, bg=COL_TITLE_BG,
                halign="center", border=BORDER_MED)

    # ── Row2: 対象月・担当 ──
    ws.merge_cells("A2:G2")
    period_val = period or "　　　　年　　　月分"
    _cell_style(ws, "A2",
                f"対象月：{period_val}　　　　　　　　　　　　　　担当：　　　　　　　",
                size=11, bg="FFFAF6")

    # ── Row3: 操作方法 ──
    ws.merge_cells("A3:G3")
    _cell_style(ws, "A3",
                "【操作方法】　F列（対応済）の ▼ をクリック → ✓ を選択 → 行がグレーアウトされます。",
                size=9, fg="9A7060", bg="FEF5ED")

    # ── Row4: 列ヘッダー ──
    headers = ["勘定科目", "補助科目", "日付", "確認内容（Harel入力欄）", "ご回答", "対応済", "備考"]
    for col_idx, h in enumerate(headers, 1):
        coord = f"{get_column_letter(col_idx)}4"
        _cell_style(ws, coord, h,
                    bold=True, size=11, fg=COL_HEADER_FG, bg=COL_HEADER_BG,
                    halign="center", border=BORDER_ALL)

    # ── データ検証（対応済列 F）──
    dv = DataValidation(
        type="list", formula1='"TRUE,FALSE"',
        allow_blank=True, showDropDown=False,
    )
    dv.sqref = f"F5:F{max(len(issues) + 10, 100)}"
    ws.add_data_validation(dv)

    # ── データ行 ──
    row_idx = 5
    prev_base_acc = None

    for issue in issues:
        full_account = str(issue.get("account", ""))
        month        = str(issue.get("month",   ""))
        message      = str(issue.get("message", ""))

        # 勘定科目と補助科目に分解
        if "（" in full_account and full_account.endswith("）"):
            p      = full_account.index("（")
            base   = full_account[:p]
            sub    = full_account[p+1:-1]
        else:
            base = full_account
            sub  = ""

        # 同じ勘定科目は2行目以降を空白
        display_base = base if base != prev_base_acc else ""
        if base:
            prev_base_acc = base

        # 日付表示
        date_str = month
        if len(month) == 7 and month[4] == "-":
            y, m = month.split("-")
            date_str = f"{y}年{int(m)}月"

        # メッセージ整形（【〇〇】プレフィックスを除去）
        clean_msg = re.sub(r'^【[^】]+】', '', message).strip()

        # 行背景（交互）
        row_bg = "FFFFFF" if row_idx % 2 == 0 else COL_ROW_ALT

        row_data = [display_base, sub, date_str, clean_msg, "", False, ""]
        for col_idx, val in enumerate(row_data, 1):
            coord  = f"{get_column_letter(col_idx)}{row_idx}"
            halign = "center" if col_idx in (3, 6) else "left"
            wrap   = (col_idx == 4)
            _cell_style(ws, coord, val,
                        size=10, bg=row_bg,
                        halign=halign, wrap=wrap,
                        border=BORDER_ALL)

        # 行の高さ（メッセージが長い場合）
        lines = max(1, len(clean_msg) // 30 + clean_msg.count("\n") + 1)
        ws.row_dimensions[row_idx].height = max(20, min(lines * 15, 60))

        row_idx += 1

    # ── 条件付き書式: 対応済=TRUE の行をグレーアウト ──
    gray_fill = PatternFill(start_color=COL_DONE_BG, end_color=COL_DONE_BG,
                            fill_type="solid")
    gray_font = Font(color="888888", name="メイリオ")
    ds   = DifferentialStyle(fill=gray_fill, font=gray_font)
    rule = Rule(type="expression", dxf=ds, formula=[f'$F5=TRUE'])
    ws.conditional_formatting.add(f"A5:G{row_idx}", rule)

    # ── ウィンドウ固定（4行目まで固定）──
    ws.freeze_panes = "A5"


def _build_sample_sheet(ws):
    """「記入例」シートを再現"""
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 60
    ws.column_dimensions["E"].width = 25
    ws.column_dimensions["F"].width = 10
    ws.column_dimensions["G"].width = 20

    ws.merge_cells("A1:G1")
    _cell_style(ws, "A1", "会計データ確認シート",
                bold=True, size=14, fg=COL_TITLE_FG, bg=COL_TITLE_BG,
                halign="center")
    ws.merge_cells("A2:G2")
    _cell_style(ws, "A2",
                "対象月：　　　　年　　　月分　　　　　　　　　　　　　　　担当：　　　　　　　",
                size=11)

    headers = ["勘定科目", "補助科目", "日付", "確認内容（Harel入力欄）", "ご回答", "対応済", "備考"]
    for i, h in enumerate(headers, 1):
        coord = f"{get_column_letter(i)}3"
        _cell_style(ws, coord, h, bold=True, fg=COL_HEADER_FG, bg=COL_HEADER_BG,
                    halign="center", border=BORDER_ALL)

    samples = [
        ("売掛金", "ｱｲﾑﾋﾞｰ", "", "3月分158,130円の回収がないようです"),
        ("",       "ｻﾛﾝｻｻﾞﾝ", "", "4/30の売り上げが2件入っているのはないか"),
        ("",       "諸口",    "", "前期から-1,320円残っているので確認が必要"),
        ("買掛金",  "箔一産業", "", "３月分の仕入計上がされていないか　4/30 2,337,775円支払分"),
        ("",       "ﾌﾟﾗﾑ",    "", "3月計上 260,271円の支払いがない　翌々月以降支払いか"),
        ("未払費用", "役員退職金", "", "永井等さま分 30,000,000円が残っている。支払日未達で問題ないか"),
    ]
    for r_idx, (acc, sub, date, content) in enumerate(samples, 4):
        row_bg = "FFFFFF" if r_idx % 2 == 0 else COL_ROW_ALT
        for c_idx, val in enumerate([acc, sub, date, content, "", False, ""], 1):
            coord = f"{get_column_letter(c_idx)}{r_idx}"
            _cell_style(ws, coord, val, size=10, bg=row_bg,
                        halign="center" if c_idx in (3, 6) else "left",
                        wrap=(c_idx == 4), border=BORDER_ALL)

    ws.merge_cells(f"A{len(samples)+5}:G{len(samples)+5}")
    _cell_style(ws, f"A{len(samples)+5}",
                "【操作方法】　F列（対応済）の ▼ をクリック → ✓ を選択 → 行がグレーアウトされます。解除するには ▼ から空白（　）を選択してください。",
                size=9, fg="9A7060", bg="FEF5ED", wrap=True)
