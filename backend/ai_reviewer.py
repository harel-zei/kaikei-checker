"""
AI選別層（PoC）— ルールベースが拾った候補を、Claudeが「税理士の目視判断」に
近い観点で選別する。閾値では表現しづらい感覚的な取捨選択を担う。

設計方針:
- 顧問先の識別情報（会社名・事業所ID等）は送らない。金額・日付・勘定科目・
  摘要・件数のみ送る（最小限）。
- ANTHROPIC_API_KEY 未設定・APIエラー時はルールの結果をそのまま返す
  （AIがチェックを壊さない = フェイルオープン）。
- まずは重複仕訳(5-2)のみ対象。効果確認後に他カテゴリへ拡大する。
"""
import os
import json

try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

# モデルは既定でOpus 4.8。コスト優先ならAI_MODEL=claude-haiku-4-5等で切替可
AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-4-8")
_MAX_CANDIDATES = 120  # 1リクエストで送る候補の上限


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


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


def review_issues(issues: list) -> list:
    """指摘リストを受け取り、AIが選別した結果を返す。
    現状は check_id '5-2'（重複仕訳）のみAI選別し、他はそのまま通す。"""
    if not is_enabled() or not issues:
        return issues

    targets = [(i, iss) for i, iss in enumerate(issues)
               if iss.get("check_id") == "5-2" and iss.get("detail")]
    if not targets:
        return issues

    try:
        verdicts = _judge_duplicates([iss["detail"] for _, iss in targets[:_MAX_CANDIDATES]])
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

    result = [iss for i, iss in enumerate(issues) if i not in drop_idx]
    return result


def _judge_duplicates(details: list) -> dict:
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
        max_tokens=8000,
        system=[{
            "type": "text",
            "text": _RUBRIC,
            "cache_control": {"type": "ephemeral"},  # ルーブリックはキャッシュ
        }],
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
