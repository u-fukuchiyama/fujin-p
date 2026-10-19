## 第1段階 — 種（カーネル＋アプシャ）を立ち上げる

この文書は，導入スクリプト `fujinp_kernel_install.py` を使って，新品の PythonAnywhere サイトに FUJIN-P の種を立ち上げる手順です．
種が立ち上がった状態とは，admin でログインすると管理者ダッシュボードが出て，そこにアプシャのカードがある状態を指します．
アプリはまだ1本も入っていません．
種のカーネルとアプシャは，スクリプトを作った日の版です．元のサイトと同じ版にそろえるのは第2段階で行います．

所要はおよそ30分です．
手で行うのは，データベースの作成，`config.py` の用意，WSGI の設定，最初の admin の登録の4つだけです．
ファイルの配置とテーブルの作成は自動です．

## 用意するもの

全体マニュアルの「用意するもの」にある4つのファイルと `config.py` を手元に置きます．
この段階で使うのは，カーネルの JSON（`fujinp_kernel_<日時>.json`）と `config.py` だけです．

PythonAnywhere の Web タブで，Flask の Web アプリを1つ作っておきます．
Python の版は元のサイトに合わせます．

## 1. 導入スクリプトを取り出す

PythonAnywhere の Files 画面を開き，ホームディレクトリ（`/home/<アカウント名>/`）にカーネルの JSON をアップロードします．

Bash コンソールを開き，次を実行します（`fujinp_kernel_<日時>.json` は実際のファイル名に置き換えます）．

```text
cd ~
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); f=[x for x in d['files'] if x['path']=='fujinp_kernel_install.py'][0]; open('fujinp_kernel_install.py','w').write(f['content'])" fujinp_kernel_<日時>.json
python3 fujinp_kernel_install.py --list
```

ホームディレクトリに `fujinp_kernel_install.py` ができ，その中のファイルの一覧が出ます．
`--list` は何も書き込みません．

カーネルの JSON が無いときは，FUJIN-P の公開リポジトリ（GitHub）の https://github.com/u-fukuchiyama/fujin-p/blob/main/fujinp/migration_assistant/data_for_distribution/fujinp_kernel_install.py か，FUJIN-P のサイトのまいあしの「🚚 別サイトへのインストール」の画面からスクリプトを入手します．

## 2. ファイルを展開する

```text
python3 fujinp_kernel_install.py --files
```

ホームディレクトリに `app.py`・`auth.py`・`db.py` などが置かれ，`templates/` と `fujinp/`（`fujinp/admin/`・`fujinp/app_share/`）ができます．
既にあるファイルは触りません．
上書きしたいときは `--files --force` を付けます．その場合，上書きされる前のファイルは `fujinp_seed_backup_<日時>/` に退避されます．

## 3. データベースを作る

PythonAnywhere の Databases タブで，`<アカウント名>$default`・`<アカウント名>$fujinp`・`<アカウント名>$public` の3つを作ります．
種が使うのは `$default` だけですが，第2段階でアプリを入れると残りの2つも使います．

同じ画面に出ている接続情報（ホスト名・ユーザ名・パスワード）を次で使います．

## 4. 依存ライブラリを入れる

```text
cd ~
pip3.13 install --user -r requirements.txt
```

`pip3.13` の数字は，Web タブに出ている Python の版に合わせます．
版が違うと，サイトを開いたときに `ModuleNotFoundError` になります．

## 5. config.py を置く

元のサイトの `config.py` を控えてあるときは，それをホームディレクトリにアップロードします．
同じアカウント名で作り直すなら，そのまま使えます．
アカウント名が違うときは，`DB_ACCOUNT`・`BASE_URL`・`DB_HOST`・`DB_USER`・`DB_PASSWORD` を新しいサイトの値に直します．
データベースのパスワードは Databases タブで設定したものです．

控えが無いときは，雛形から書きます．

```text
cd ~
cp config_template.py config.py
```

Files 画面で `config.py` を開き，`<...>` の箇所を埋めます．埋めるのは次の6つです．
`SECRET_KEY` はセッションの署名に使う長いランダム文字列で，`python3 -c "import secrets; print(secrets.token_urlsafe(48))"` で作った値を貼ります．
`DB_ACCOUNT` は PythonAnywhere のアカウント名，`BASE_URL` は `https://<アカウント名>.pythonanywhere.com` です．
`DB_HOST`・`DB_USER`・`DB_PASSWORD` は Databases タブに出ている値をそのまま入れます．
Google 認証とメールは空のままで構いません．種はメールアドレスとパスワードのログインで立ち上げ，Google 認証はあとから足します．

## 6. テーブルを作る

```text
cd ~
python3 fujinp_kernel_install.py --schema
```

`<アカウント名>$default` に14のテーブルが作られ，アプシャの区画6行とカーネル3行（`_platform`・`admin`・`app_share`）が入り，最後に正本 `fujinp/app_registry.json` が書き出されます．
作られたテーブルの一覧が表示されます．
既にあるテーブルには触りません．何度実行しても同じ結果になります．

接続できないときは，`config.py` の接続の4項目と，データベースが作ってあるかを順に確かめてください．

## 7. WSGI を設定して Reload する

Web タブの WSGI configuration file を開き，中身を次のようにします（`<アカウント名>` は自分のものに置き換えます）．

```text
import sys

path = '/home/<アカウント名>'
if path not in sys.path:
    sys.path.insert(0, path)

from app import app as application
```

Virtualenv を使っている場合は，その欄にパスを入れます．
使っていない場合は空のままで構いません（手順4で `--user` を付けて入れたので，そのまま読まれます）．

保存して，緑の Reload ボタンを押します．

## 8. 最初の admin を登録する

```text
cd ~
python3 fujinp_seed/make_admin.py
```

メールアドレス・氏名・パスワードを訊かれます．パスワードは8文字以上にしてください．
入力が済むと `users` テーブルへの `INSERT` 文が1つ表示されます．
Databases タブから MySQL コンソールを開き（`<アカウント名>$default` を選びます），表示された文をそのまま貼って実行します．

このスクリプトはデータベースに接続せず，何も書き込みません．パスワードのハッシュを作って文を組み立てるだけです．

あとで利用者のデータを戻すと，この行は戻したデータに入れ替わります．
戻すデータに admin の利用者が入っていることを，戻す前に確かめておいてください．

## 9. ログインする

ブラウザで `https://<アカウント名>.pythonanywhere.com/` を開きます．
ログイン画面が出るので，8で登録したメールアドレスとパスワードを入れます．

管理者ダッシュボードが出て，アプシャのカード（📦 アプシャ）があれば，種は立ち上がっています．
続けて第2段階に進みます．

## うまくいかないときの確認順

サイトを開いて Something went wrong が出るときは，Web タブのエラーログを見ます．
`ModuleNotFoundError` なら手順4のライブラリ，`config` に関するものなら手順5，データベースに関するものなら手順3と6を見直します．

ログイン画面は出るがログインできないときは，手順8の `INSERT` が実際に実行されたかを MySQL コンソールで確かめます（`SELECT id, email, category FROM users;`）．

ログインはできるがダッシュボードにアプシャのカードが出ないときは，`fujinp/app_registry.json` が書けているかを見ます．
無ければ手順6をもう一度実行します．

管理者ダッシュボードで「ユーザ管理」を押して画面が出ないときは，手順6でテーブルが全部できているかを確かめます．

## 種に入っていないもの

`config.py` は入っていません．手順5で置きます．

`static/` は入っていません．カーネルのテンプレートは静的ファイルを参照していないためです．

アプシャの文書編集画面にある画像のアップロードは，マイノート（`my_md_notes`）が入っているサイトでだけ動きます．
種の段階では，押しても「画像アップロード失敗」と出ます．第2段階で全アプリを入れると動くようになります．
