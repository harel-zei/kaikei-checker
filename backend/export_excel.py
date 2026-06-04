"""
会計データ確認シート Excel出力モジュール
xlsxwriter を使用してネイティブチェックボックス付き xlsx を生成する。

- F列（対応済）: クリックするだけで ✓/□ が切り替わる本物のチェックボックス
- チェックを入れると行全体がグレーアウト（条件付き書式）
"""
import io
import re
import xlsxwriter

# ── カラー定数 ──────────────────────────────────────────
C_ORANGE        = "#F47A20"
C_ORANGE_PALE   = "#FDE8D8"
C_ORANGE_HEAD   = "#7A3800"
C_ORANGE_BG     = "#FDF9F5"
C_WHITE         = "#FFFFFF"
C_BORDER        = "#E8D5C0"
C_ROW_ALT       = "#FDF9F5"
C_DONE_BG       = "#D0D0D0"
C_DONE_FG       = "#888888"
C_TITLE_BG      = "#F47A20"
C_TITLE_FG      = "#FFFFFF"
C_INSTR_BG      = "#FEF5ED"
C_INSTR_FG      = "#9A7060"


def build_checksheet_xlsx(
    issues: list,
    period: str = "",
    client_name: str = "",
) -> bytes:
    """
    チェック結果を xlsx に変換して bytes で返す。
    「チェックシート」「記入例」の2シート構成。
    F列（対応済）にネイティブチェックボックスを配置し、
    ☑ になると行全体がグレーアウトする。
    """
    buf = io.BytesIO()
    wb  = xlsxwriter.Workbook(buf, {"in_memory": True})

    _build_checksheet(wb, issues, period, client_name)
    _build_sample_sheet(wb)

    wb.close()
    return buf.getvalue()


# ────────────────────────────────────────────────────────
# チェックシート本体
# ────────────────────────────────────────────────────────
def _build_checksheet(wb, issues, period, client_name):
    ws = wb.add_worksheet("チェックシート")

    # ── フォーマット定義 ──
    fmt_title = wb.add_format({
        "bold": True, "font_size": 14,
        "font_name": "メイリオ",
        "bg_color": C_TITLE_BG, "font_color": C_TITLE_FG,
        "align": "center", "valign": "vcenter",
        "border": 2, "border_color": "#C09060",
    })
    fmt_period = wb.add_format({
        "font_name": "メイリオ", "font_size": 11,
        "bg_color": "#FFFAF6",
        "valign": "vcenter",
    })
    fmt_instr = wb.add_format({
        "font_name": "メイリオ", "font_size": 9,
        "font_color": C_INSTR_FG, "bg_color": C_INSTR_BG,
        "italic": True, "valign": "vcenter",
    })
    fmt_header = wb.add_format({
        "bold": True, "font_size": 11,
        "font_name": "メイリオ",
        "bg_color": C_ORANGE_PALE, "font_color": C_ORANGE_HEAD,
        "align": "center", "valign": "vcenter",
        "border": 1, "border_color": C_BORDER,
    })
    def make_row_fmt(bg, is_wrap=False):
        return wb.add_format({
            "font_name": "メイリオ", "font_size": 10,
            "bg_color": bg, "valign": "vcenter",
            "text_wrap": is_wrap,
            "border": 1, "border_color": C_BORDER,
        })
    fmt_row_w  = make_row_fmt(C_WHITE,    True)
    fmt_row_a  = make_row_fmt(C_ROW_ALT,  True)
    fmt_row_c  = make_row_fmt(C_WHITE,    False)
    fmt_row_ca = make_row_fmt(C_ROW_ALT,  False)

    # ── 列幅 ──
    ws.set_column("A:A", 18)   # 勘定科目
    ws.set_column("B:B", 22)   # 補助科目
    ws.set_column("C:C", 12)   # 日付
    ws.set_column("D:D", 62)   # 確認内容
    ws.set_column("E:E", 26)   # ご回答
    ws.set_column("F:F", 10)   # 対応済（チェックボックス）
    ws.set_column("G:G", 20)   # 備考

    # ── 行高 ──
    ws.set_row(0, 28)  # タイトル
    ws.set_row(1, 18)
    ws.set_row(2, 18)
    ws.set_row(3, 22)  # ヘッダー

    # ── Row1: タイトル ──
    ws.merge_range("A1:G1", "会計データ確認シート", fmt_title)

    # ── Row2: 対象月 ──
    period_val = period or "　　　　年　　　月分"
    ws.merge_range("A2:G2",
                   f"対象月：{period_val}　　　　　　　　　　　　　　担当：　　　　　　　",
                   fmt_period)

    # ── Row3: 操作方法 ──
    ws.merge_range("A3:G3",
                   "【操作方法】　F列（対応済）のチェックボックスをクリック → ✓ でグレーアウト、もう一度クリックで解除",
                   fmt_instr)

    # ── Row4: 列ヘッダー ──
    for col, h in enumerate(
        ["勘定科目", "補助科目", "日付", "確認内容（Harel入力欄）", "ご回答", "対応済", "備考"]
    ):
        ws.write(3, col, h, fmt_header)

    # ── 条件付き書式: チェックボックス=TRUE → 行全体をグレーアウト ──
    # xlsxwriter では F列（col 5）の checkbox が TRUE のとき F列= 1
    done_fmt = wb.add_format({
        "font_color": C_DONE_FG,
        "bg_color":   C_DONE_BG,
        "font_name":  "メイリオ",
    })
    last_row = max(len(issues) + 5, 50)
    ws.conditional_format(4, 0, last_row, 6, {
        "type":     "formula",
        "criteria": "=$F5=TRUE",
        "format":   done_fmt,
    })

    # ── データ行 ──
    prev_base = None
    for i, issue in enumerate(issues):
        row = 4 + i  # Excel row (0-indexed): row 4 = Excel row 5

        full_acc = str(issue.get("account", ""))
        month    = str(issue.get("month", ""))
        message  = str(issue.get("message", ""))

        # 勘定科目・補助科目に分解
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

        # 日付整形
        date_str = month
        if len(month) == 7 and month[4] == "-":
            y, m = month.split("-")
            date_str = f"{y}年{int(m)}月"

        # メッセージ整形
        clean_msg = re.sub(r'^【[^】]+】', '', message).strip()

        # 行背景
        is_alt = (i % 2 == 1)
        f_text = fmt_row_a if is_alt else fmt_row_w
        f_ctr  = fmt_row_ca if is_alt else fmt_row_c

        ws.write(row, 0, display_base, f_text)
        ws.write(row, 1, sub,          f_text)
        ws.write(row, 2, date_str,     f_ctr)
        ws.write(row, 3, clean_msg,    f_text)
        ws.write(row, 4, "",           f_text)  # ご回答（空欄）

        # ── F列: チェックボックス ──
        ws.insert_checkbox(row, 5, {
            "checked":     False,
            "size":        {"width": 72, "height": 18},
        })

        ws.write(row, 6, "", f_text)  # 備考（空欄）

        # 行高（メッセージ長さに応じて）
        lines = max(1, len(clean_msg) // 40 + clean_msg.count("\n") + 1)
        ws.set_row(row, max(18, min(lines * 15, 80)))


# ────────────────────────────────────────────────────────
# 記入例シート
# ────────────────────────────────────────────────────────
def _build_sample_sheet(wb):
    ws = wb.add_worksheet("記入例")

    fmt_title = wb.add_format({
        "bold": True, "font_size": 14,
        "font_name": "メイリオ",
        "bg_color": C_TITLE_BG, "font_color": C_TITLE_FG,
        "align": "center", "valign": "vcenter",
    })
    fmt_header = wb.add_format({
        "bold": True, "font_size": 11, "font_name": "メイリオ",
        "bg_color": C_ORANGE_PALE, "font_color": C_ORANGE_HEAD,
        "align": "center", "valign": "vcenter",
        "border": 1, "border_color": C_BORDER,
    })
    def make_fmt(bg, wrap=False):
        return wb.add_format({
            "font_name": "メイリオ", "font_size": 10,
            "bg_color": bg, "valign": "vcenter",
            "text_wrap": wrap,
            "border": 1, "border_color": C_BORDER,
        })
    fw = make_fmt(C_WHITE,   True)
    fa = make_fmt(C_ROW_ALT, True)
    fc = make_fmt(C_WHITE)
    fca= make_fmt(C_ROW_ALT)

    ws.set_column("A:A", 18); ws.set_column("B:B", 22)
    ws.set_column("C:C", 12); ws.set_column("D:D", 62)
    ws.set_column("E:E", 26); ws.set_column("F:F", 10)
    ws.set_column("G:G", 20)
    ws.set_row(0, 28); ws.set_row(1, 18); ws.set_row(2, 22)

    ws.merge_range("A1:G1", "会計データ確認シート", fmt_title)
    ws.merge_range("A2:G2",
                   "対象月：　　　　年　　　月分　　　　　　　　　　　　　　　担当：　　　　　　　",
                   wb.add_format({"font_name": "メイリオ", "font_size": 11}))

    for col, h in enumerate(
        ["勘定科目", "補助科目", "日付", "確認内容（Harel入力欄）", "ご回答", "対応済", "備考"]
    ):
        ws.write(2, col, h, fmt_header)

    samples = [
        ("売掛金", "ｱｲﾑﾋﾞｰ",  "", "3月分158,130円の回収がないようです"),
        ("",      "ｻﾛﾝｻｻﾞﾝ", "", "4/30の売り上げが2件入っているのはないか"),
        ("",      "諸口",    "", "前期から-1,320円残っているので確認が必要"),
        ("買掛金", "箔一産業", "", "３月分の仕入計上がされていないか　4/30 2,337,775円支払分"),
        ("",      "ﾌﾟﾗﾑ",    "", "3月計上 260,271円の支払いがない　翌々月以降支払いか"),
        ("未払費用", "役員退職金", "", "永井等さま分 30,000,000円が残っている。支払日未達で問題ないか"),
    ]
    done_fmt = wb.add_format({"font_color": C_DONE_FG, "bg_color": C_DONE_BG, "font_name": "メイリオ"})
    ws.conditional_format(3, 0, len(samples)+5, 6, {
        "type": "formula", "criteria": "=$F4=TRUE", "format": done_fmt,
    })

    for i, (acc, sub, date, content) in enumerate(samples):
        row    = 3 + i
        is_alt = i % 2 == 1
        f_t = fa if is_alt else fw
        f_c = fca if is_alt else fc
        ws.write(row, 0, acc,     f_t)
        ws.write(row, 1, sub,     f_t)
        ws.write(row, 2, date,    f_c)
        ws.write(row, 3, content, f_t)
        ws.write(row, 4, "",      f_t)
        ws.insert_checkbox(row, 5, {"checked": i == 0})  # 1行目だけチェック済み（例示）
        ws.write(row, 6, "",      f_t)
        ws.set_row(row, 22)

    instr_fmt = wb.add_format({
        "font_name": "メイリオ", "font_size": 9,
        "font_color": C_INSTR_FG, "bg_color": C_INSTR_BG,
        "italic": True, "text_wrap": True,
    })
    ws.merge_range(
        3 + len(samples) + 1, 0,
        3 + len(samples) + 1, 6,
        "【操作方法】　F列（対応済）のチェックボックスをクリック → ✓ でグレーアウト、もう一度クリックで解除。",
        instr_fmt,
    )
