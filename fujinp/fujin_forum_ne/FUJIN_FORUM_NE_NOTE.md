# えふえふね（fujin_forum_ne）v1.0 — 配置とアプシャ登録メモ  2026-09-09

えふえふ（fujin_forum）v1.3 を複製し，名前空間だけを付け替えた独立アプリ．
もう一つの Slack ワークスペース側のフォーラムとして使い，取込元はすらくみね（surakumine）．

## 配置
- `~/fujinp/fujin_forum_ne/`（__init__.py・routes.py・templates/fujin_forum_ne/*.html）
- `schema.sql` を MySQL コンソールで実行（6テーブル．えふえふのテーブルには触らない）
- 添付の公開領域 `~/static/ffneimgs/` は初回の公開操作で自動生成
- 添付の保護領域 `~/fujinp/fujin_forum_ne/data/files/` は初回アップロード時に自動生成

## アプシャの登録内容
- app_name: fujin_forum_ne　表示名: えふえふね　アイコン: 🗨　kind: app
- blueprints: module=fujinp.fujin_forum_ne, attr=fujin_forum_ne_bp, name=fujin_forum_ne, url_prefix=（空．Blueprint 側で /fujin_forum_ne）
- launchers: endpoint=fujin_forum_ne.index
- config_keys: UPLOAD_BASE_DIR（えふえふと共用．任意）
- tables: fujin_forum_ne_channels / _access_groups / _posts / _reactions / _attachments / _reads

## 依存する他アプリ
- まいぐる：user_groups / user_group_memberships（無くても動く）
- すらくみね（surakumine）：取込元（surakumine_messages / _files / _users / _channels と data/files/）

## 廃止するとき
1. アプシャでランチャを外し，アプリを登録から削除して発行・Reload
2. `DROP TABLE fujin_forum_ne_reactions, fujin_forum_ne_attachments, fujin_forum_ne_posts, fujin_forum_ne_reads, fujin_forum_ne_access_groups, fujin_forum_ne_channels;`
3. `~/fujinp/fujin_forum_ne/` と `~/static/ffneimgs/` を削除
残したい中身があれば，先にチャンネルごとの JSON エクスポートを取っておく．えふえふの既存チャンネルへは「JSON からの取込」で移せる．
