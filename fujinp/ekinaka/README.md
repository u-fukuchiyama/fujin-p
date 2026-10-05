# えきなか (ekinaka) Blueprint

**駅ナカキャンパス** のイベント申請・管理・公開アプリ。  
FUJIN-P プラットフォーム用 Flask Blueprint として実装。

---

## ファイル構成

```
ekinaka/
├── __init__.py            # Blueprint 定義
├── routes.py              # 全ルート・ロジック
├── schema.sql             # DB テーブル定義
├── README.md              # このファイル
└── templates/
    ├── ekinaka_dashboard.html    # 閲覧者メイン画面（モバイルファースト）
    ├── ekinaka_event_detail.html # イベント詳細・操作
    ├── ekinaka_apply.html        # 申請フォーム（新規・編集兼用）
    ├── ekinaka_my.html           # マイ申請一覧
    ├── ekinaka_manage.html       # 承認者管理画面
    └── ekinaka_settings.html     # アクセス権限設定
```

---

## セットアップ

### 1. DBテーブルの作成

```bash
mysql -u [USER] -p [DBNAME] < ekinaka/schema.sql
```

または PythonAnywhere の MySQL コンソールで `schema.sql` の内容を実行。

### 2. Blueprint の登録

`app.py` に追記：

```python
from ekinaka import ekinaka_bp
app.register_blueprint(ekinaka_bp)
```

### 3. 定数の確認（routes.py 冒頭）

環境に合わせて調整してください：

```python
ANON_GROUP_ID            = 0                   # 匿名グループID（変更不要）
USER_GROUP_MEMBERS_TABLE = 'user_group_members' # まいぐるのメンバーテーブル名
USER_GROUPS_TABLE        = 'user_groups'         # まいぐるのグループテーブル名
USERNAME_COL             = 'username'            # users テーブルの表示名カラム
```

### 4. 権限設定

初回は承認権限グループが未設定のため、DB に直接挿入する：

```sql
-- まいぐるで管理者グループの group_id を確認してから実行
INSERT INTO ekinaka_access_settings (access_type, group_id, group_label)
VALUES
  ('approve', [管理者group_id], '管理者'),
  ('apply',   [申請者group_id], '教職員'),
  ('view',    [閲覧者group_id], '学内閲覧');
```

その後は `/ekinaka/settings` で GUI 設定が可能。

---

## 権限モデル

| 権限 | 説明 |
|------|------|
| `view` | 公開日前プレビュー閲覧。`group_id=0`（匿名）を含めると常時全公開。 |
| `apply` | イベント申請・公演登録。 |
| `approve` | 承認・管理・設定変更。全申請共通の1グループ。 |

---

## ステータス遷移

```
pending  ──[承認者: 承認]──→ approved
         ──[承認者: 不承認]─→ rejected
         ──[申請者/承認者: 公開日前]──→ withdrawn（非表示）

approved ──[申請者/承認者: 公開日前]──→ withdrawn（非表示）
         ──[申請者[取消不可]/承認者]──→ cancelled（「中止」表示）
         ──[申請者[取消不可]/承認者]──→ postponed（「延期」表示）

rejected/cancelled/postponed/withdrawn
         ──[承認者のみ: 復元]──→ approved
```

**取り下げと中止/延期の違い：**
- `withdrawn`（取り下げ）: 公開日前のみ可。閲覧者には表示されない。
- `cancelled`（中止）/ `postponed`（延期）: 公開日後の取り下げ。「中止」「延期」として閲覧者に周知。

---

## 公開ルール

```
実効公開日 = approver_public_date OR applicant_public_date

条件A: status in (approved, cancelled, postponed)
       AND 実効公開日 <= 今日
       → 全員（ログインなし含む）に公開

条件B: status in (approved, cancelled, postponed)
       AND ユーザが view_group に所属
       → 公開日前でもプレビュー閲覧可

例外:  申請者は自分の申請を常に閲覧可
例外:  承認者は全イベントを常に閲覧可
```

---

## URL一覧

| メソッド | URL | 説明 |
|---------|-----|------|
| GET | `/ekinaka/` | ダッシュボード（日/週/月切替） |
| GET | `/ekinaka/event/<id>` | イベント詳細 |
| GET/POST | `/ekinaka/apply` | 新規申請 |
| GET/POST | `/ekinaka/event/<id>/edit` | 申請編集（pending のみ） |
| GET | `/ekinaka/my` | マイ申請一覧 |
| POST | `/ekinaka/event/<id>/withdraw` | 取り下げ |
| POST | `/ekinaka/event/<id>/cancel` | 中止（申請者） |
| POST | `/ekinaka/event/<id>/postpone` | 延期（申請者） |
| POST | `/ekinaka/event/<id>/performance/add` | 公演追加 |
| POST | `/ekinaka/performance/<id>/edit` | 公演編集 |
| POST | `/ekinaka/performance/<id>/delete` | 公演削除 |
| GET | `/ekinaka/manage` | 承認者管理画面 |
| POST | `/ekinaka/event/<id>/approve` | 承認 |
| POST | `/ekinaka/event/<id>/reject` | 不承認 |
| POST | `/ekinaka/event/<id>/set_status` | ステータス変更（承認者） |
| POST | `/ekinaka/event/<id>/update_notes` | 備考・公開日更新（承認者） |
| GET/POST | `/ekinaka/settings` | アクセス権限設定 |

---

## 設計メモ

- **場所の重複**: アプリでは制御しない。重複があっても申請者間・承認者で人間的に調整。
- **公演の自由度**: 申請者はイベント期間内で公演日時を自由に設定可能（多少のゆとり許容）。再承認不要。
- **モバイルファースト**: 全テンプレートはスマートフォン対応（max-width: 640px）。
- **まいぐる連携**: `user_group_members` テーブルを通じてグループ権限を管理。

---

## 依存

- Flask（FUJIN-P 標準）
- mysql-connector-python（FUJIN-P 標準）
- セッション `user_id`（FUJIN-P ログイン機構）
- `user_groups` / `user_group_members` テーブル（まいぐる）
- `users` テーブル（`id`, `username` カラム）
