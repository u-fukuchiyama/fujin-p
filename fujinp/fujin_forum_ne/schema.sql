-- えふえふね（fujin_forum_ne）テーブル定義 v1.0
-- えふえふ v1.3 の最終形と同じ構造で，名前だけ fujin_forum_ne_* にしたもの．
-- 新規に立てるときはこの6本をそのまま流す（アプシャのテーブルタブの DDL と同一）．
-- PythonAnywhere の MySQL コンソールに貼って実行する．

CREATE TABLE `fujin_forum_ne_channels` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'チャンネル名（# なし）',
  `description` text COLLATE utf8mb4_unicode_ci COMMENT '説明',
  `share_key` enum('private','public','domestic','group','domestic_group') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'private' COMMENT '公開範囲：private=作成者と admin / public=ゲストにも / domestic=構成員だけ / group / domestic_group',
  `created_by` int DEFAULT NULL COMMENT 'users.id',
  `created_at` datetime NOT NULL COMMENT 'JST',
  `updated_at` datetime NOT NULL COMMENT 'JST',
  `is_archived` tinyint(1) NOT NULL DEFAULT '0' COMMENT '1=読み取り専用',
  `sort_order` double NOT NULL DEFAULT '0',
  `slack_channel_id` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '取込元の Slack チャンネル ID（すらくみね）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふね：チャンネル';

CREATE TABLE `fujin_forum_ne_access_groups` (
  `channel_id` int NOT NULL,
  `group_id` int NOT NULL COMMENT 'user_groups.id',
  PRIMARY KEY (`channel_id`,`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふね：許可グループ';

CREATE TABLE `fujin_forum_ne_posts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `channel_id` int NOT NULL,
  `parent_id` int DEFAULT NULL COMMENT '返信なら親記事の id（1段）',
  `user_id` int DEFAULT NULL COMMENT 'users.id（Slack 由来で対応づかない場合は NULL）',
  `author_name` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '表示名（投稿時点）',
  `body_md` mediumtext COLLATE utf8mb4_unicode_ci COMMENT 'Markdown 本文',
  `created_at` datetime NOT NULL COMMENT 'JST（Slack 由来は元の投稿日時）',
  `updated_at` datetime NOT NULL COMMENT 'JST',
  `edited_at` datetime DEFAULT NULL COMMENT '本文を編集した日時',
  `deleted_at` datetime DEFAULT NULL COMMENT '論理削除',
  `source` enum('user','slack') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'user',
  `slack_ts` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '取込元の Slack ts（冪等取込の照合キー）',
  `reply_count` int NOT NULL DEFAULT '0',
  `last_reply_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_slack` (`channel_id`,`slack_ts`),
  KEY `idx_channel_parent` (`channel_id`,`parent_id`,`created_at`),
  KEY `idx_parent` (`parent_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふね：記事と返信';

CREATE TABLE `fujin_forum_ne_reactions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `post_id` int NOT NULL,
  `user_id` int DEFAULT NULL COMMENT 'users.id（Slack 由来は NULL）',
  `reactor_name` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `emoji` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_emoji` (`post_id`,`user_id`,`emoji`),
  KEY `idx_post` (`post_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふね：リアクション';

CREATE TABLE `fujin_forum_ne_attachments` (
  `id` int NOT NULL AUTO_INCREMENT,
  `post_id` int DEFAULT NULL COMMENT '本文が参照する記事（投稿前は NULL）',
  `channel_id` int NOT NULL DEFAULT '0' COMMENT 'アクセス権の判定に使うチャンネル',
  `name` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `mimetype` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `size` bigint DEFAULT NULL,
  `local_path` varchar(600) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '保護領域 data/files/ からの相対パス（原本）',
  `public_path` varchar(600) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '公開複製の URL パス．未公開は NULL',
  `uploaded_by` int DEFAULT NULL COMMENT 'users.id（Slack 由来は NULL）',
  `source` enum('user','slack') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'user',
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_post` (`post_id`),
  KEY `idx_channel` (`channel_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふね：添付（取込・記録用）';

CREATE TABLE `fujin_forum_ne_reads` (
  `user_id` int NOT NULL,
  `channel_id` int NOT NULL,
  `last_read_at` datetime NOT NULL,
  PRIMARY KEY (`user_id`,`channel_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='えふえふね：既読';
