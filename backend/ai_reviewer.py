"""
AI選別層 — ルールベースの結果を、担当者の判断履歴（feedback_store）とClaudeで磨く。

「使うほど精度が上がる」ための3段構え:

  ① 学習済み抑制（AI不要・決定的）
     担当者が繰り返し「不要」と判断したパターン（チェックID×勘定科目）を抑制する。
     確実に効くので、APIキーが無くても動作する。

  ② AI選別（重複仕訳）
     機械的に抽出した重複候補を、税理士の実務感覚で取捨選択する。
     その際、この顧問先で過去に「妥当」「不要」とされた実例をClaudeに渡し、
     判断基準を合わせていく。

  ③ 見逃しの探索
     担当者が「システムが拾えなかった」と登録した観点をもとに、
     Claudeが今回のデータ（科目×月の集計）から同種の論点を探す。

設計方針:
- 顧問先の識別情報（会社名・事業所ID等）は送らない。
- ③に渡すのは科目×月の集計値のみ（摘要・取引先名は送らない）。
- ANTHROPIC_API_KEY 未設定・APIエラー時はルールの結果をそのまま返す
  （AIがチェックを壊さない = フェイルオープン）。
"""
import os
import json

try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

import feedback_store

# モデルは既定で Opus 5（Opus 4.8 と同価格でより高性能）。
# コスト優先なら AI_MODEL=claude-haiku-4-5 等で切替可。
AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-5")
_MAX_CANDIDATES = 120   # 1リクエストで送る重複候補の上限
_MAX_ACCOUNTS = 80      # 見逃し探索で送る科目数の上限


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ══════════════════════════════════════════════════════════
# ① 学習済み抑制（AI不要）
# ══════════════════════════════════════════════════════════
def apply_learned_suppression(issues: list, client_name: str = None) -> tuple:
    """担当者が繰り返し「不要」と判断したパターンの指摘を抑制する。

    安全のため error（要修正）は抑制しない。warning / info のみ対象。
    Returns: (残った指摘, 抑制した件数)
    """
    try:
        patterns = feedback_store.noise_patterns(client_name)
    except Exception:
        return issues, 0
    if not patterns:
        return issues, 0

    keys = {(p["check_id"], p["account"]) for p in patterns}
    kept, dropped = [], 0
    for iss in issues:
        key = (str(iss.get("check_id") or iss.get("category") or ""),
               str(iss.get("account") or ""))
        if iss.get("level") != "error" and key in keys:
            dropped += 1
            continue
        kept.append(iss)
    return kept, dropped


# ══════════════════════════════════════════════════════════
# ② AI選別（重複仕訳）
# ══════════════════════════════════════════════════════════
_RUBRIC = """あなたは日本の税理士事務所の会計チェック補助AIです。
システムが機械的に「同一日付・同額・同一勘定科目の仕訳」を重複候補として抽出しました。
各候補について、税理士の実務感覚で「顧客に確認すべき重複の疑い」か「許容してよい正常な取引」かを判定してください。

【許容してよい（keep=false）の典型】
- 振込手数料・利息・チャージなど、少額で同額が日常的に発生する定型取引
- リース料・賃借料など、同額の契約を複数持つのが自然な費用
- 摘要が異なり、明らかに別取引と分かるもの（別の取引先・別の物件・別の番号）

【確認すべき（keep=true）の典型】
- 金額が大きく、偶然の同額一致とは考えにくいもの（例: 数十万円以上の売掛金・売上・仕入）
- 摘要が完全に同一で、同じ取引を二重計上した疑いがあるもの
- 同一日に同額・同科目・同摘要が複数あり、二重入力の可能性が高いもの

金額の絶対額だけで機械的に線引きせず、取引の性質・摘要の具体性・件数を総合的に見て判断してください。
各候補に keep（true/false）と、日本語で簡潔な reason（30字程度）を返してください。

出力は必ず次の形式のJSONのみとし、前後に説明文やコードフェンスを付けないでください:
{"verdicts":[{"index":0,"keep":true,"reason":"..."}, ...]}"""


def _feedback_hint(client_name: str) -> str:
    """過去の担当者判断をプロンプトに添えるテキストを作る（無ければ空文字）"""
    try:
        noise = feedback_store.noise_patterns(client_name, min_count=1, min_ratio=0.5)
        useful = feedback_store.useful_examples(client_name, limit=10)
    except Exception:
        return ""
    if not noise and not useful:
        return ""

    lines = ["\n\n【この顧問先での過去の担当者判断（判断基準を合わせてください）】"]
    if noise:
        lines.append("・過去に「不要（誤検知）」と判断された指摘の傾向:")
        for p in noise[:10]:
            lines.append(f"  - {p['check_id']} / {p['account']}（{p['noise']}件が不要と判断）")
    if useful:
        lines.append("・過去に「妥当」と判断された指摘の例:")
        for r in useful[:10]:
            lines.append(f"  - {r.get('account','')}: {str(r.get('summary',''))[:80]}")
    lines.append("同様の傾向のものは、過去の判断に沿って評価してください。")
    return "\n".join(lines)


def review_issues(issues: list, client_name: str = None) -> list:
    """指摘リストをAIが選別する。check_id '5-2'（重複仕訳）のみ対象、他はそのまま通す。"""
    if not is_enabled() or not issues:
        return issues

    targets = [(i, iss) for i, iss in enumerate(issues)
               if iss.get("check_id") == "5-2" and iss.get("detail")]
    if not targets:
        return issues

    try:
        verdicts = _judge_duplicates(
            [iss["detail"] for _, iss in targets[:_MAX_CANDIDATES]],
            client_name,
        )
    except Exception as e:
        # 失敗時はルール結果をそのまま返す（AIでチェックを止めない）
        print(f"[ai_reviewer] スキップ（{e}）", flush=True)
        return issues

    drop_idx = set()
    for local_i, (global_i, iss) in enumerate(targets):
        v = verdicts.get(local_i)
        if v is None:
            continue
        if not v.get("keep", True):
            drop_idx.add(global_i)
        else:
            reason = str(v.get("reason", "")).strip()
            if reason:
                iss["message"] += f"\n〔AI判断〕{reason}"

    return [iss for i, iss in enumerate(issues) if i not in drop_idx]


def _judge_duplicates(details: list, client_name: str = None) -> dict:
    """候補リストをClaudeに送り {index: {keep, reason}} を返す。"""
    import anthropic
    client = anthropic.Anthropic()

    candidates = [
        {
            "index": i,
            "amount": d.get("amount"),
            "count": d.get("count"),
            "account": d.get("account"),
            "dates": d.get("dates", []),
            "descriptions": d.get("descriptions", []),
        }
        for i, d in enumerate(details)
    ]

    resp = client.messages.create(
        model=AI_MODEL,
        max_tokens=16000,
        system=[
            {
                "type": "text",
                "text": _RUBRIC,
                "cache_control": {"type": "ephemeral"},  # ルーブリックはキャッシュ
            },
            # 顧問先ごとの学習内容は可変なのでキャッシュ対象外の別ブロックにする
            {"type": "text", "text": _feedback_hint(client_name) or "（過去の判断履歴なし）"},
        ],
        messages=[{
            "role": "user",
            "content": (
                "以下の重複候補を判定してください。JSONのみで返してください。\n"
                + json.dumps(candidates, ensure_ascii=False)
            ),
        }],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = json.loads(_extract_json(text))
    return {v["index"]: v for v in data.get("verdicts", [])}


# ══════════════════════════════════════════════════════════
# ③ 見逃しの探索
# ══════════════════════════════════════════════════════════
_MISSED_RUBRIC = """あなたは日本の税理士事務所の会計チェック補助AIです。
担当者が過去に「システムが拾えなかったが、実際には指摘すべきだった」と登録した観点を渡します。
同じ観点で、今回の月次データ（勘定科目ごとの月別発生額）を見て、確認すべき点を挙げてください。

【重視すること】
- 毎月発生している費用が、特定の月だけ計上されていない（計上漏れの疑い）
- 期の途中から計上が途絶えている（解約か計上漏れかの確認）
- 金額が不自然に変動している（桁誤り・二重計上の疑い）
- 担当者が過去に指摘した観点と同種のもの

【重視しないこと】
- 単に金額が大きい・小さいだけのもの
- 季節性で説明できる変動
- 根拠が数字から読み取れない推測

確信が持てないものは挙げないでください。挙げる場合は必ず、
どの科目の何月がどうなっているかを数字で具体的に示してください。
該当が無ければ空の配列を返してください。

出力は必ず次の形式のJSONのみとし、前後に説明文やコードフェンスを付けないでください:
{"findings":[{"account":"勘定科目名","month":"YYYY-MM","message":"確認内容（80字程度）"}, ...]}"""


def find_missed_issues(df, client_name: str = None, max_findings: int = 10) -> list:
    """担当者が登録した「見逃し」観点をもとに、AIが同種の論点を探す。

    Claudeに渡すのは科目×月の集計値のみ（摘要・取引先名は送らない）。
    見逃し登録が無い場合は何もしない（＝使い込むほど働くようになる）。
    """
    if not is_enabled() or df is None or df.empty:
        return []
    try:
        missed = feedback_store.missed_examples(client_name)
    except Exception:
        return []
    if not missed:
        return []  # 学習材料が無いうちは動かさない

    try:
        matrix = _monthly_matrix(df)
        if not matrix:
            return []
        findings = _ask_missed(matrix, missed, max_findings)
    except Exception as e:
        print(f"[ai_reviewer] 見逃し探索スキップ（{e}）", flush=True)
        return []

    out = []
    for f in findings[:max_findings]:
        msg = str(f.get("message", "")).strip()
        if not msg:
            continue
        out.append({
            "level": "info", "category": "AI 学習チェック",
            "check_id": "AI-1",
            "account": str(f.get("account", ""))[:60] or "（科目不明）",
            "month": str(f.get("month", ""))[:20] or "全期間",
            "message": (
                f"【AI・参考】{msg}\n"
                "※ 担当者が過去に登録した「見逃し」の観点からAIが抽出した候補です。"
                "ルールによる確定的な指摘ではないため、内容をご確認ください。"
            ),
        })
    return out


def _monthly_matrix(df) -> list:
    """科目×月の発生額マトリクスを作る（借方・貸方の純額）。"""
    import pandas as pd

    work = df[df["date"].notna()].copy()
    if work.empty:
        return []
    work["_fp"] = work["date"].dt.to_period("M").astype(str)

    d = work.groupby([work["debit_account"].fillna("").astype(str).str.strip(), "_fp"])["debit_amount"].sum()
    c = work.groupby([work["credit_account"].fillna("").astype(str).str.strip(), "_fp"])["credit_amount"].sum()

    totals: dict = {}
    for (acc, period), amt in d.items():
        if acc and acc != "nan":
            totals.setdefault(acc, {})[period] = totals.setdefault(acc, {}).get(period, 0.0) + float(amt)
    for (acc, period), amt in c.items():
        if acc and acc != "nan":
            totals.setdefault(acc, {})[period] = totals.setdefault(acc, {}).get(period, 0.0) + float(amt)

    # 金額規模の大きい科目に絞る（トークン量の抑制）
    ranked = sorted(totals.items(), key=lambda kv: -sum(abs(v) for v in kv[1].values()))
    return [
        {"account": acc, "monthly": {k: round(v) for k, v in sorted(months.items())}}
        for acc, months in ranked[:_MAX_ACCOUNTS]
    ]


def _ask_missed(matrix: list, missed: list, max_findings: int) -> list:
    import anthropic
    client = anthropic.Anthropic()

    hints = "\n".join(
        f"- {m.get('account','')}: {str(m.get('description',''))[:120]}"
        for m in missed[-20:]
    )
    resp = client.messages.create(
        model=AI_MODEL,
        max_tokens=8000,
        system=[{
            "type": "text",
            "text": _MISSED_RUBRIC,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": (
                "【担当者が過去に登録した見逃しの観点】\n" + (hints or "（なし）")
                + f"\n\n【今回の科目×月の発生額】\n{json.dumps(matrix, ensure_ascii=False)}"
                + f"\n\n最大{max_findings}件まで、確信が持てるものだけをJSONで返してください。"
            ),
        }],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = json.loads(_extract_json(text))
    return data.get("findings", [])


def _extract_json(text: str) -> str:
    """モデル出力からJSON本体を取り出す（コードフェンスや前後テキストを除去）。"""
    t = text.strip()
    if "```" in t:
        # ```json ... ``` の中身を取り出す
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    # 最初の { から最後の } まで
    s, e = t.find("{"), t.rfind("}")
    return t[s:e + 1] if s != -1 and e != -1 else "{}"


# ══════════════════════════════════════════════════════════
# ④ 意図に照らした精査（ルールの条件ではなく、守りたいことで見る）
# ══════════════════════════════════════════════════════════
# ルールは「条件」でしかないため、条件に合致しても実態として問題ないもの
# （誤検知）と、条件から外れるが意図に照らせば問題のもの（見逃し）が生じる。
# check_intents.py に明文化した「各チェックが何を守りたいか」をClaudeに渡し、
# 意図の側から再評価させる。ルールベースでは原理的にできない部分を担う。

_INTENT_MAX_ISSUES = 60    # 再評価に送る指摘数の上限
_INTENT_MAX_EXTRA = 8      # 追加で挙げさせる論点の上限

_INTENT_TASK = """上記の意図を踏まえ、次の2つを行ってください。

【A】ルールが出した指摘の再評価
各指摘について、意図に照らして「確認する価値があるか」を判定します。
- keep=true  … 意図に照らしても確認すべき
- keep=false … 条件には合致したが、データを見る限り実態として問題ない
keep=false とする場合は、なぜそう言えるかを数字を挙げて示してください。
根拠が示せないものは keep=true にしてください（迷ったら残す）。

【B】意図に照らした追加の観点
ルールの条件からは外れるが、意図に照らすと確認すべき事象を挙げてください。
月次推移から読み取れる事実のみを根拠とし、推測で挙げないでください。
該当が無ければ空の配列を返してください。

出力は必ず次の形式のJSONのみとし、前後に説明文やコードフェンスを付けないでください:
{"review":[{"index":0,"keep":true,"reason":"..."}],
 "additional":[{"account":"科目名","month":"YYYY-MM","message":"確認内容（100字程度）","basis":"根拠となる数字"}]}"""


def review_by_intent(issues: list, df, client_name: str = None) -> tuple:
    """意図に照らして指摘を再評価し、追加の観点も抽出する。

    Returns: (再評価後の指摘リスト, 意図から追加された指摘リスト)
    フェイルオープン: 失敗時は入力をそのまま返す。
    """
    if not is_enabled() or not issues:
        return issues, []

    # error（要修正）はAIの判断で消さない。確実性の高いルール指摘を守る。
    targets = [(i, iss) for i, iss in enumerate(issues) if iss.get("level") != "error"]
    if not targets:
        return issues, []

    try:
        import check_intents
        matrix = _monthly_matrix(df) if df is not None and not df.empty else []
        payload = [
            {
                "index": local_i,
                "check_id": iss.get("check_id") or iss.get("category"),
                "level": iss.get("level"),
                "account": iss.get("account"),
                "month": iss.get("month"),
                # メッセージは要約して送る（全文は冗長）
                "message": str(iss.get("message", ""))[:300],
            }
            for local_i, (_, iss) in enumerate(targets[:_INTENT_MAX_ISSUES])
        ]
        result = _ask_intent_review(check_intents.catalog_text(), payload, matrix,
                                    _feedback_hint(client_name))
    except Exception as e:
        print(f"[ai_reviewer] 意図精査スキップ（{e}）", flush=True)
        return issues, []

    # ── A: 再評価の反映 ──
    drop_idx = set()
    for v in result.get("review", []):
        try:
            local_i = int(v.get("index"))
        except (TypeError, ValueError):
            continue
        if local_i >= len(targets):
            continue
        global_i, iss = targets[local_i]
        reason = str(v.get("reason", "")).strip()
        if v.get("keep") is False and reason:
            # 消すのではなく、判断の根拠を添えて「参考」に降格する。
            # AIの誤判断で指摘が消えるのを避けるため、担当者の目には残す。
            iss["level"] = "info"
            iss["message"] += f"\n〔意図に照らした再評価〕{reason}"
            iss["category"] = f"{iss.get('category','')}（要確認度: 低）"
        elif reason:
            iss["message"] += f"\n〔AI補足〕{reason}"

    kept = [iss for i, iss in enumerate(issues) if i not in drop_idx]

    # ── B: 意図から導いた追加の観点 ──
    extra = []
    for f in result.get("additional", [])[:_INTENT_MAX_EXTRA]:
        msg = str(f.get("message", "")).strip()
        if not msg:
            continue
        basis = str(f.get("basis", "")).strip()
        extra.append({
            "level": "info", "category": "AI 意図精査",
            "check_id": "AI-2",
            "account": str(f.get("account", ""))[:60] or "（科目不明）",
            "month": str(f.get("month", ""))[:20] or "全期間",
            "message": (
                f"【AI・参考】{msg}"
                + (f"\n【根拠】{basis}" if basis else "")
                + "\n※ ルールの条件には該当しませんが、チェックの意図に照らしてAIが"
                  "確認を提案した項目です。確定的な指摘ではありません。"
            ),
        })
    return kept, extra


def _ask_intent_review(catalog: str, issues_payload: list, matrix: list,
                       feedback_hint: str) -> dict:
    import anthropic
    client = anthropic.Anthropic()

    resp = client.messages.create(
        model=AI_MODEL,
        max_tokens=16000,
        system=[
            # 意図カタログは毎回同じなのでキャッシュする
            {"type": "text", "text": catalog, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": _INTENT_TASK},
            {"type": "text", "text": feedback_hint or "（過去の判断履歴なし）"},
        ],
        messages=[{
            "role": "user",
            "content": (
                "【ルールが出した指摘】\n"
                + json.dumps(issues_payload, ensure_ascii=False)
                + "\n\n【勘定科目ごとの月別発生額】\n"
                + json.dumps(matrix, ensure_ascii=False)
                + "\n\nJSONのみで返してください。"
            ),
        }],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return json.loads(_extract_json(text))
