-- すらくみね（surakumine）テーブル定義 v1.0
-- すらくみ（slack_minutes）v2.3 の最終形と同じ構造で，名前だけ surakumine_* にしたもの．
-- 新規サイトではこの6本をそのまま流す（アプシャのテーブルタブの DDL と同一）．
-- PythonAnywhere の MySQL コンソールに貼って実行する．

CREATE TABLE `surakumine_sessions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL COMMENT '操作した users.id',
  `channel_id` varchar(32) NOT NULL COMMENT 'Slack チャンネル ID（C...）',
  `channel_name` varchar(200) NOT NULL COMMENT 'チャンネル名',
  `fetched_at` datetime NOT NULL COMMENT '取得実行日時（JST）',
  `status` varchar(16) NOT NULL DEFAULT 'running' COMMENT 'running / done / error',
  `mode` varchar(16) NOT NULL DEFAULT 'diff' COMMENT 'diff（差分）/ archive（完全アーカイブ）',
  `phase` varchar(16) DEFAULT NULL COMMENT 'archive の進行段階 history/threads/files/done',
  `state_json` text COMMENT 'archive の再開用状態',
  `fetched_count` int DEFAULT NULL COMMENT '取得メッセージ総数',
  `saved_count` int DEFAULT NULL COMMENT '新規保存数',
  `updated_count` int DEFAULT NULL COMMENT '再同期（上書き）した件数',
  `reply_count` int DEFAULT NULL COMMENT '取り込んだスレッド返信数',
  `file_count` int DEFAULT NULL COMMENT '処理した添付ファイル数',
  PRIMARY KEY (`id`),
  KEY `idx_channel` (`channel_id`),
  KEY `idx_fetched` (`fetched_at`),
  KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='すらくみね：取得記録';

CREATE TABLE `surakumine_messages` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `session_id` int NOT NULL COMMENT '初めて保存した surakumine_sessions.id',
  `channel_id` varchar(32) NOT NULL COMMENT 'Slack チャンネル ID',
  `channel_name` varchar(200) NOT NULL COMMENT 'チャンネル名',
  `slack_ts` varchar(32) NOT NULL COMMENT 'Slack メッセージ ts（一意キー）',
  `sender_id` varchar(32) DEFAULT NULL COMMENT 'Slack ユーザー ID',
  `sender_name` varchar(200) DEFAULT NULL COMMENT '表示名（取得時点）',
  `text` text COMMENT 'メッセージ本文（Slack mrkdwn のまま）',
  `posted_at` datetime DEFAULT NULL COMMENT '投稿日時（JST）',
  `thread_ts` varchar(32) DEFAULT NULL COMMENT 'スレッド親 ts',
  `subtype` varchar(32) DEFAULT NULL COMMENT 'Slack subtype（thread_broadcast 等）',
  `reply_count` int NOT NULL DEFAULT '0' COMMENT 'Slack が返す返信数（親のみ）',
  `edited_at` datetime DEFAULT NULL COMMENT '最終編集日時（JST）',
  `reactions_json` text COMMENT 'リアクション [{name,count,users:[表示名]}]',
  `raw_json` mediumtext COMMENT 'Slack API が返したメッセージの生データ',
  `created_at` datetime NOT NULL COMMENT 'レコード作成日時（JST）',
  `updated_at` datetime DEFAULT NULL COMMENT '最終同期日時（JST）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_channel_ts` (`channel_id`,`slack_ts`),
  KEY `idx_session` (`session_id`),
  KEY `idx_posted` (`posted_at`),
  KEY `idx_channel` (`channel_id`),
  KEY `idx_thread` (`channel_id`,`thread_ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='すらくみね：メッセージ';

CREATE TABLE `surakumine_users` (
  `user_id` varchar(32) NOT NULL COMMENT 'Slack ユーザー ID',
  `name` varchar(200) DEFAULT NULL COMMENT 'ハンドル名',
  `display_name` varchar(200) DEFAULT NULL COMMENT '表示名',
  `real_name` varchar(200) DEFAULT NULL COMMENT '本名',
  `is_bot` tinyint(1) NOT NULL DEFAULT '0',
  `deleted` tinyint(1) NOT NULL DEFAULT '0',
  `fetched_at` datetime NOT NULL COMMENT '取得日時（JST）',
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='すらくみね：ユーザー名の永続キャッシュ';

CREATE TABLE `surakumine_channels` (
  `channel_id` varchar(32) NOT NULL COMMENT 'Slack チャンネル ID',
  `name` varchar(200) NOT NULL COMMENT 'チャンネル名',
  `is_private` tinyint(1) NOT NULL DEFAULT '0',
  `visibility` enum('private','public','domestic','group','domestic_group') NOT NULL DEFAULT 'private' COMMENT '公開範囲：private=adminのみ / public=ゲストにも / domestic=構成員だけ / group / domestic_group',
  `topic` text,
  `purpose` text,
  `slack_created_at` datetime DEFAULT NULL COMMENT 'Slack 上の作成日時（JST）',
  `last_archived_at` datetime DEFAULT NULL COMMENT '完全アーカイブ取得の最終完了日時（JST）',
  `updated_at` datetime NOT NULL COMMENT 'レコード更新日時（JST）',
  PRIMARY KEY (`channel_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='すらくみね：チャンネル情報と公開範囲';

CREATE TABLE `surakumine_access_groups` (
  `channel_id` varchar(32) NOT NULL COMMENT 'surakumine_channels.channel_id',
  `group_id` int NOT NULL COMMENT 'user_groups.id',
  PRIMARY KEY (`channel_id`,`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='すらくみね：許可グループ';

CREATE TABLE `surakumine_files` (
  `id` int NOT NULL AUTO_INCREMENT,
  `file_id` varchar(32) NOT NULL COMMENT 'Slack ファイル ID',
  `channel_id` varchar(32) NOT NULL,
  `slack_ts` varchar(32) NOT NULL COMMENT '添付元メッセージの ts',
  `name` varchar(500) DEFAULT NULL,
  `title` varchar(500) DEFAULT NULL,
  `mimetype` varchar(100) DEFAULT NULL,
  `filetype` varchar(32) DEFAULT NULL,
  `size` bigint DEFAULT NULL COMMENT 'バイト数（Slack の申告値）',
  `url_private` text COMMENT 'url_private_download（要 Bot Token）',
  `local_path` varchar(600) DEFAULT NULL COMMENT 'data/files/ からの相対パス',
  `status` varchar(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/done/error/expired',
  `error` text,
  `created_at` datetime NOT NULL COMMENT '登録日時（JST）',
  `downloaded_at` datetime DEFAULT NULL COMMENT '保存完了日時（JST）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_file_msg` (`file_id`,`channel_id`,`slack_ts`),
  KEY `idx_channel_ts` (`channel_id`,`slack_ts`),
  KEY `idx_status` (`channel_id`,`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='すらくみね：添付ファイルの目録と保存状態';
