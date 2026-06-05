"""
会計データ確認シート Excel出力モジュール
xlsxwriter を使用。F列は ☑/☐ のドロップダウンセル。
☑ を選択すると行全体がグレーアウト（全 Excel バージョン対応）。
"""
import io, re
import xlsxwriter

# ── カラー定数 ──────────────────────────────────────────
C_ORANGE_BG   = "#F47A20"
C_ORANGE_PALE = "#FDE8D8"
C_ORANGE_HEAD = "#7A3800"
C_WHITE       = "#FFFFFF"
C_ROW_ALT     = "#FDF9F5"
C_DONE_BG     = "#D0D0D0"
C_DONE_FG     = "#888888"
C_BORDER      = "#E8D5C0"
C_INSTR_BG    = "#FEF5ED"
C_INSTR_FG    = "#9A7060"

CHECK_CHAR   = "☑"   # チェックあり
UNCHECK_CHAR = "☐"   # チェックなし


def build_checksheet_xlsx(
    issues: list,
    period: str = "",
    client_name: str = "",
) -> bytes:
    buf = io.BytesIO()
    wb  = xlsxwriter.Workbook(buf, {"in_memory": True})

    _build_checksheet(wb, issues, period)
    _build_sample_sheet(wb)

    wb.close()
    return buf.getvalue()


def _build_checksheet(wb, issues, period):
    ws = wb.add_worksheet("チェックシート")

    # ── フォーマット ──
    fmt_title = wb.add_format({
        "bold": True, "font_size": 14, "font_name": "メイリオ",
        "bg_color": C_ORANGE_BG, "font_color": C_WHITE,
        "align": "center", "valign": "vcenter",
        "border": 2, "border_color": "#C09060",
    })
    fmt_period = wb.add_format({
        "font_name": "メイリオ", "font_size": 11,
        "bg_color": "#FFFAF6", "valign": "vcenter",
        "left": 1, "right": 1, "bottom": 1, "border_color": C_BORDER,
    })
    fmt_instr = wb.add_format({
        "font_name": "メイリオ", "font_size": 9, "italic": True,
        "font_color": C_INSTR_FG, "bg_color": C_INSTR_BG,
        "valign": "vcenter",
        "left": 1, "right": 1, "bottom": 1, "border_color": C_BORDER,
    })
    fmt_header = wb.add_format({
        "bold": True, "font_size": 11, "font_name": "メイリオ",
        "bg_color": C_ORANGE_PALE, "font_color": C_ORANGE_HEAD,
        "align": "center", "valign": "vcenter",
        "border": 1, "border_color": C_BORDER,
    })
    fmt_check_header = wb.add_format({
        "bold": True, "font_size": 11, "font_name": "メイリオ",
        "bg_color": C_ORANGE_PALE, "font_color": C_ORANGE_HEAD,
        "align": "center", "valign": "vcenter",
        "border": 1, "border_color": C_BORDER,
    })

    def row_fmt(bg, wrap=False, center=False):
        return wb.add_format({
            "font_name": "メイリオ", "font_size": 10,
            "bg_color": bg, "valign": "vcenter",
            "text_wrap": wrap,
            "align": "center" if center else "left",
            "border": 1, "border_color": C_BORDER,
        })

    # 通常行フォーマット（白・偶数）
    fw   = row_fmt(C_WHITE)
    fw_w = row_fmt(C_WHITE, wrap=True)
    fw_c = row_fmt(C_WHITE, center=True)
    # 奇数行フォーマット（薄クリーム）
    fa   = row_fmt(C_ROW_ALT)
    fa_w = row_fmt(C_ROW_ALT, wrap=True)
    fa_c = row_fmt(C_ROW_ALT, center=True)

    # ── 列幅 ──
    ws.set_column("A:A", 18)
    ws.set_column("B:B", 22)
    ws.set_column("C:C", 12)
    ws.set_column("D:D", 62)
    ws.set_column("E:E", 26)
    ws.set_column("F:F", 10)
    ws.set_column("G:G", 20)

    # ── 行高 ──
    ws.set_row(0, 28)
    ws.set_row(1, 18)
    ws.set_row(2, 20)
    ws.set_row(3, 22)

    # ── Row1: タイトル ──
    ws.merge_range("A1:G1", "会計データ確認シート", fmt_title)

    # ── Row2: 対象月 ──
    period_val = period or "　　　　年　　　月分"
    ws.merge_range("A2:G2",
        f"対象月：{period_val}　　　　　　　　　　　　　　担当：　　　　　　　",
        fmt_period)

    # ── Row3: 操作方法 ──
    ws.merge_range("A3:G3",
        "【操作方法】　F列（対応済）のセルをクリック → ☑ を選択 → 行がグレーアウトされます。解除は ☐ を選択してください。",
        fmt_instr)

    # ── Row4: 列ヘッダー ──
    headers = ["勘定科目", "補助科目", "日付", "確認内容（Harel入力欄）", "ご回答", "対応済", "備考"]
    for col, h in enumerate(headers):
        ws.write(3, col, h, fmt_check_header if col == 5 else fmt_header)

    # ── 条件付き書式: F列 = "☑" → 行をグレーアウト ──
    done_fmt = wb.add_format({
        "font_color": C_DONE_FG,
        "bg_color":   C_DONE_BG,
        "font_name":  "メイリオ",
    })
    last_data_row = max(len(issues) + 4, 50)  # 0-indexed
    ws.conditional_format(4, 0, last_data_row, 6, {
        "type":     "formula",
        "criteria": f'=$F5="{CHECK_CHAR}"',
        "format":   done_fmt,
    })

    # ── データ行 ──
    prev_base = None
    for i, issue in enumerate(issues):
        row = 4 + i   # 0-indexed (Excel row 5+)

        full_acc = str(issue.get("account", ""))
        month    = str(issue.get("month",   ""))
        message  = str(issue.get("message", ""))

        if "（" in full_acc and full_acc.endswith("）"):
            p    = full_acc.index("（")
            base = full_acc[:p]
            sub  = full_acc[p+1:-1]
        else:
            base = full_acc
            sub  = ""

        display_base = base if base != prev_base else ""
        if base:
            prev_base = base

        # 日付フォーマット変換
        # "2026-04-30" → "2026年4月30日"
        # "2026-04"    → "2026年4月"
        # "全期間"等   → そのまま
        date_str = month
        if len(month) == 10 and month[4] == "-" and month[7] == "-":
            y, m, d = month.split("-")
            date_str = f"{y}年{int(m)}月{int(d)}日"
        elif len(month) == 7 and month[4] == "-":
            y, m = month.split("-")
            date_str = f"{y}年{int(m)}月"

        clean_msg = re.sub(r'^【[^】]+】', '', message).strip()

        is_alt = (i % 2 == 1)
        f_n = fa   if is_alt else fw
        f_w = fa_w if is_alt else fw_w
        f_c = fa_c if is_alt else fw_c

        ws.write(row, 0, display_base, f_n)
        ws.write(row, 1, sub,          f_n)
        ws.write(row, 2, date_str,     f_c)
        ws.write(row, 3, clean_msg,    f_w)
        ws.write(row, 4, "",           f_n)           # ご回答
        ws.write(row, 5, UNCHECK_CHAR, f_c)           # 対応済（初期値 ☐）
        ws.write(row, 6, "",           f_n)           # 備考

        # ドロップダウン: ☑ か ☐ を選択
        ws.data_validation(row, 5, row, 5, {
            "validate": "list",
            "source":   [CHECK_CHAR, UNCHECK_CHAR],
            "show_error": False,
        })

        lines = max(1, len(clean_msg) // 40 + 1)
        ws.set_row(row, max(18, min(lines * 15, 80)))

    # ── ウィンドウ枠の固定（4行目ヘッダーを固定）──
    ws.freeze_panes(4, 0)


def _build_sample_sheet(wb):
    ws = wb.add_worksheet("記入例")

    fmt_t = wb.add_format({
        "bold": True, "font_size": 14, "font_name": "メイリオ",
        "bg_color": C_ORANGE_BG, "font_color": C_WHITE,
        "align": "center", "valign": "vcenter",
    })
    fmt_h = wb.add_format({
        "bold": True, "font_size": 11, "font_name": "メイリオ",
        "bg_color": C_ORANGE_PALE, "font_color": C_ORANGE_HEAD,
        "align": "center", "valign": "vcenter",
        "border": 1, "border_color": C_BORDER,
    })
    def sf(bg, wrap=False, center=False):
        return wb.add_format({
            "font_name": "メイリオ", "font_size": 10, "bg_color": bg,
            "valign": "vcenter", "text_wrap": wrap,
            "align": "center" if center else "left",
            "border": 1, "border_color": C_BORDER,
        })
    fw = sf(C_WHITE); fw_w = sf(C_WHITE, wrap=True); fw_c = sf(C_WHITE, center=True)
    fa = sf(C_ROW_ALT); fa_w = sf(C_ROW_ALT, wrap=True); fa_c = sf(C_ROW_ALT, center=True)

    ws.set_column("A:A", 18); ws.set_column("B:B", 22)
    ws.set_column("C:C", 12); ws.set_column("D:D", 62)
    ws.set_column("E:E", 26); ws.set_column("F:F", 10); ws.set_column("G:G", 20)
    ws.set_row(0, 28); ws.set_row(1, 18); ws.set_row(2, 22)

    ws.merge_range("A1:G1", "会計データ確認シート", fmt_t)
    ws.merge_range("A2:G2", "対象月：　　　　年　　　月分　　　　　　　　　　　　　　　担当：　　　　　　　",
        wb.add_format({"font_name": "メイリオ", "font_size": 11}))
    for col, h in enumerate(["勘定科目","補助科目","日付","確認内容（Harel入力欄）","ご回答","対応済","備考"]):
        ws.write(2, col, h, fmt_h)

    # 条件付き書式
    done_fmt = wb.add_format({"font_color": C_DONE_FG, "bg_color": C_DONE_BG, "font_name": "メイリオ"})
    ws.conditional_format(3, 0, 15, 6, {
        "type": "formula", "criteria": f'=$F4="{CHECK_CHAR}"', "format": done_fmt,
    })

    samples = [
        ("売掛金", "ｱｲﾑﾋﾞｰ",   "", "3月分158,130円の回収がないようです",          CHECK_CHAR),
        ("",      "ｻﾛﾝｻｻﾞﾝ",  "", "4/30の売り上げが2件入っているのはないか",       UNCHECK_CHAR),
        ("",      "諸口",     "", "前期から-1,320円残っているので確認が必要",       UNCHECK_CHAR),
        ("買掛金", "箔一産業",  "", "３月分の仕入計上がされていないか",              UNCHECK_CHAR),
        ("",      "ﾌﾟﾗﾑ",     "", "3月計上 260,271円の支払いがない",               UNCHECK_CHAR),
        ("未払費用","役員退職金","", "永井等さま分 30,000,000円が残っている。",     UNCHECK_CHAR),
    ]
    for i, (acc, sub, date, content, chk) in enumerate(samples):
        row = 3 + i
        is_alt = i % 2 == 1
        f_n = fa if is_alt else fw
        f_w = fa_w if is_alt else fw_w
        f_c = fa_c if is_alt else fw_c
        ws.write(row, 0, acc,     f_n)
        ws.write(row, 1, sub,     f_n)
        ws.write(row, 2, date,    f_c)
        ws.write(row, 3, content, f_w)
        ws.write(row, 4, "",      f_n)
        ws.write(row, 5, chk,     f_c)
        ws.write(row, 6, "",      f_n)
        ws.data_validation(row, 5, row, 5, {
            "validate": "list", "source": [CHECK_CHAR, UNCHECK_CHAR], "show_error": False,
        })
        ws.set_row(row, 22)

    ws.merge_range(3+len(samples)+1, 0, 3+len(samples)+1, 6,
        "【操作方法】　F列（対応済）のセルをクリック → ☑ を選択 → 行がグレーアウト。解除は ☐ を選択。",
        wb.add_format({"font_name":"メイリオ","font_size":9,"font_color":C_INSTR_FG,"bg_color":C_INSTR_BG,"italic":True}))
