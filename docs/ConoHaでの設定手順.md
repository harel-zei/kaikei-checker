# ConoHaでの設定手順（freee連携の復旧とSupabase脱却）

ConoHaの**管理画面（コントロールパネル）自体には、このアプリの設定項目はありません。**
管理画面から「コンソール」を開き、そこでコマンドを実行します。

> ℹ️ このアプリはPython製のWebアプリのため、**ConoHa VPS**での運用が前提です。
> （ConoHa WINGのような共有レンタルサーバーでは動作しません）

---

## 手順0: コンソール（黒い画面）を開く

**方法A: ConoHaの管理画面から開く（追加ソフト不要・おすすめ）**

1. [ConoHaのコントロールパネル](https://manage.conoha.jp/) にログイン
2. 左メニューの「**サーバー**」をクリック
3. 対象サーバーの名前をクリック
4. 画面右上あたりの「**コンソール**」ボタンをクリック
5. 黒い画面が開くので、ログインします
   - `login:` → ユーザー名（多くは `root`）
   - `Password:` → サーバー作成時に設定したパスワード（**入力しても画面に表示されません**。そのまま打ってEnter）

> ブラウザのコンソールは日本語入力やコピペがしづらいことがあります。
> その場合は方法Bが快適です。

**方法B: パソコンのターミナルからSSHで接続する**

Windowsなら「PowerShell」、Macなら「ターミナル」を開いて：

```bash
ssh root@（サーバーのIPアドレス）
```

IPアドレスはConoHa管理画面のサーバー詳細に表示されています。

---

## 手順1: アプリの場所と起動方法を調べる

引っ越し時の構成によって置き場所が違うため、まず確認します。
以下を**そのままコピーして貼り付け**、Enterを押してください。

```bash
echo "=== アプリの場所 ==="; find / -name "main.py" -path "*backend*" 2>/dev/null | head
echo "=== 起動中のプロセス ==="; ps aux | grep -E "uvicorn|gunicorn" | grep -v grep
echo "=== systemdサービス ==="; systemctl list-units --type=service | grep -iE "kaikei|uvicorn|fastapi"
echo "=== Docker ==="; docker ps 2>/dev/null | head
```

**出力例と読み方**

| 出力 | 意味 |
|---|---|
| `/home/kaikei-checker/backend/main.py` | アプリの場所は `/home/kaikei-checker` |
| `kaikei-checker.service` が出る | systemdで常駐している（再起動は `systemctl restart`） |
| `docker ps` に出る | Dockerで動いている（手順が変わります） |
| プロセスだけ出てserviceが無い | 手動起動（`nohup` や `screen`）の可能性 |

> **この出力をそのまま共有していただければ、以降のコマンドを環境に合わせて具体化します。**
> 以下は「`/home/kaikei-checker` にあり、systemdで動いている」場合の例です。

---

## 手順2: 最新のコードを取得する

```bash
cd /home/kaikei-checker          # ← 手順1で判明した場所に置き換え
git pull origin deploy/render-5yr4fy
```

> `git pull` でエラーが出る場合、引っ越し時にgit管理でなくファイルコピーで
> 配置された可能性があります。その場合はご相談ください。

---

## 手順3: 設定ファイル（.env）を作る

```bash
cd /home/kaikei-checker
cp .env.example .env
nano .env                        # nano がなければ vi .env
```

エディタが開いたら、以下の項目に値を入れます。

```
APP_USERNAME=admin
APP_PASSWORD=（ログイン用パスワード）

FREEE_CLIENT_ID=（freeeアプリ管理画面の Client ID）
FREEE_CLIENT_SECRET=（同 Client Secret）
FREEE_REDIRECT_URI=https://（新しいドメイン）/api/freee/callback

SUPABASE_URL=          ← 空のまま（サーバー内保存にするため）
SUPABASE_SECRET_KEY=   ← 空のまま

ANTHROPIC_API_KEY=（AI選別を使う場合のみ）
```

**nanoの保存方法**: `Ctrl` + `O` → Enter → `Ctrl` + `X`
**viの保存方法**: `Esc` → `:wq` → Enter

`.env` は機密情報を含むため、他のユーザーから読めないようにします。

```bash
chmod 600 .env
```

---

## 手順4: Supabaseのデータをサーバー内へ移行する

前期データ・クライアント設定・freee連携トークンを引き取ります。
**Supabaseの接続情報は、このコマンドの中だけで使います**（`.env` には書きません）。

```bash
cd /home/kaikei-checker

# 念のためバックアップ
cp -r backend/client_data backend/client_data.bak 2>/dev/null

# ① まず中身を確認するだけ（書き込みなし）
SUPABASE_URL=https://uwrespfqnbamwpuvrtwc.supabase.co \
SUPABASE_SECRET_KEY=（SupabaseのSecret Key） \
python3 backend/tools/migrate_from_supabase.py --dry-run
```

顧問先名とファイルの一覧が表示されます。想定どおりなら本実行します。

```bash
# ② 本実行（--dry-run を外すだけ）
SUPABASE_URL=https://uwrespfqnbamwpuvrtwc.supabase.co \
SUPABASE_SECRET_KEY=（SupabaseのSecret Key） \
python3 backend/tools/migrate_from_supabase.py
```

> Supabase の Secret Key は、Supabaseの管理画面
> 「Project Settings」→「API」→ `service_role` のキーです。

---

## 手順5: freee側のコールバックURLを更新する

**ここはConoHaではなく、freeeの画面での作業です。**

1. [freeeアプリストア](https://app.secure.freee.co.jp/developers/applications) にログイン
2. 対象アプリ →「アプリ管理」→「基本情報」
3. **コールバックURL** を新しいドメインに変更して保存

```
https://（新しいドメイン）/api/freee/callback
```

> ⚠️ `.env` の `FREEE_REDIRECT_URI` と**1文字も違わない**ようにしてください。
> 末尾のスラッシュ、http と https の違いでも認可が弾かれます。
> freeeは本番環境で **https 必須**のため、独自ドメイン＋SSL証明書が必要です。

---

## 手順6: アプリを再起動して確認する

```bash
# systemdの場合（サービス名は手順1で確認したもの）
sudo systemctl restart kaikei-checker
sudo systemctl status kaikei-checker      # 「active (running)」ならOK
```

ブラウザでアプリを開き、以下を確認します。

- [ ] ログインできる
- [ ] STEP1のクライアント一覧に**顧問先が表示される**（＝移行成功）
- [ ] STEP2に「🔗 freeeから取得」タブが**表示される**（＝freee設定成功）
- [ ] freeeタブで事業所が選べる（＝トークン移行成功）

---

## うまくいかないときの確認

| 症状 | 対処 |
|---|---|
| 「freeeから取得」タブが出ない | `.env` の3項目（CLIENT_ID / SECRET / REDIRECT_URI）が揃っているか。`chmod 600 .env` 後にアプリを再起動したか |
| 連携ボタンでfreeeがエラー | コールバックURLの不一致（手順5）。httpsか、末尾は `/api/freee/callback` か |
| 顧問先一覧が空 | 移行スクリプト（手順4）が未実行。`ls backend/client_data/` で中身を確認 |
| 再起動後にアプリが起動しない | `journalctl -u kaikei-checker -n 50` でエラーを確認 |

エラーメッセージが出た場合は、**その文面をそのまま共有**してください。
（`.env` の内容を共有する際は、**パスワードやキーの値は伏せて**ください）

---

## 移行後にやっておくこと

サーバー内保存に切り替えると、**バックアップは自前の責任**になります。
cronで日次バックアップを設定しておくことをお勧めします。

```bash
crontab -e
```

以下を追記（毎日3時に取得、14日分を保持）：

```
0 3 * * * tar czf /var/backups/kaikei-$(date +\%F).tgz -C /home/kaikei-checker/backend client_data && find /var/backups -name 'kaikei-*.tgz' -mtime +14 -delete
```
