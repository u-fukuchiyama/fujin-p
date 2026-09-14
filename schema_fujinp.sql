-- ============================================================
-- FUJIN-P Migration Schema : fujinp
-- Generated : 2026-09-07 00:42 JST
-- Source    : nishida4fujinp$fujinp (nishida4fujinp / PythonAnywhere)
-- ============================================================
--
-- このファイルは「fujinp」データベース専用です。
--
-- 使い方（マイグレーション先 target）:
--   1. MySQL コンソールを開く
--   2. 下の USE 文の target を自分のアカウント名に書き換えて実行
--   3. source でこのファイルを読み込む
--
-- 注: 外部キー制約（FOREIGN KEY）は除去してあります。
--     テーブル・カラム・データ構造はそのままで、参照整合性の
--     自動チェックのみ無効化した緩やかなスキーマです。
--
-- ============================================================

-- ↓ target を自分のアカウント名に書き換えてください
USE `target$fujinp`;

SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS `T_03_01_学部入試実施状況` (
  `入学年度` int DEFAULT NULL,
  `学部` varchar(255) DEFAULT NULL,
  `学科` varchar(255) DEFAULT NULL,
  `項目` varchar(255) DEFAULT NULL,
  `実績` int DEFAULT NULL,
  `序列` int DEFAULT NULL,
  `備考` text,
  `出典URL` text
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `T_06_04_受託共同研究事業費受入実績` (
  `整理番号` varchar(255) DEFAULT NULL,
  `委託者` varchar(255) DEFAULT NULL,
  `委託年度` int DEFAULT NULL,
  `内容` text,
  `所属` varchar(255) DEFAULT NULL,
  `担当者` varchar(255) DEFAULT NULL,
  `経費` int DEFAULT NULL,
  `直接経費` int DEFAULT NULL,
  `間接経費` int DEFAULT NULL,
  `備考` text
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE IF NOT EXISTS `colrep_013ba21b797d` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_0144b19b1345` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_02c55e800b1a` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_032ce119b118` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_07516d4cc66f` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_07bff7f752fc` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_12ccdb03c66f` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_2a47b5b2fb23` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_2b6e4a4f08f3` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_335a6d19e095` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_3b284024a40b` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_426a1e9d666f` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_454e02d2323a` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_4dce5cdfa37a` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_51fae22534a6` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_54cad1701842` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_55c21fd8d1b4` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_57ec1f3ffc1d` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_58efc6d3e53e` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_6638f7999aac` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_6841ff0f27f3` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_745f02f791fc` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_7684e82675f2` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_7aeccdb67c02` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_88e9a96bfe21` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_8df40b655951` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_919a4733ba65` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_92da6af41945` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_9a0bddfce285` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_9f3930ccfc82` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_a4e68bd4ce08` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_access_groups` (
  `project_id` int NOT NULL,
  `group_id` int NOT NULL,
  PRIMARY KEY (`project_id`,`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `colrep_adf61b4fdd70` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_bc2fc714e5ea` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_bd656070e750` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_c04611c09b99` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_c48af2826d1a` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_c5d8bcf97426` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_c6545d4ecf4b` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_ca5e309dba1a` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_d6f8f1f861f2` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_d939e384463c` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_eb61e571d010` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_ec2f8d454746` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_ee12491c315c` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_f11a709979cf` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_f337cbb9d4d8` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_f772cc3fccab` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_fd8c6d12403b` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル';

CREATE TABLE IF NOT EXISTS `colrep_fe6b8cd3f47a` (
  `id` int NOT NULL AUTO_INCREMENT,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `カラム名` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `担当者アカウント` int NOT NULL,
  `説明` text COLLATE utf8mb4_unicode_ci COMMENT '管理者から入力者への説明',
  `content` longtext COLLATE utf8mb4_unicode_ci COMMENT '入力内容',
  `備考` text COLLATE utf8mb4_unicode_ci COMMENT '入力者から管理者への説明',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT '作業中' COMMENT '進捗状況：作業中/改訂中/完了',
  PRIMARY KEY (`id`),
  KEY `idx_担当者` (`担当者アカウント`),
  KEY `idx_カラム名` (`カラム名`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='CoRePoプロジェクト用データテーブル（JSONインポート）';

CREATE TABLE IF NOT EXISTS `colrep_projects` (
  `id` int NOT NULL AUTO_INCREMENT,
  `プロジェクト名` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `更新日時` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `責任者` int NOT NULL,
  `テーブル名` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `Composer` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `is_public` tinyint(1) DEFAULT '0',
  `access_policy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'private',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_project_name` (`プロジェクト名`),
  UNIQUE KEY `uk_table_name` (`テーブル名`),
  KEY `idx_updated` (`更新日時`),
  KEY `idx_responsible` (`責任者`),
  KEY `idx_public` (`is_public`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `document_access_groups` (
  `doc_id` int NOT NULL,
  `group_id` int NOT NULL,
  PRIMARY KEY (`doc_id`,`group_id`),
  KEY `idx_doc_id` (`doc_id`),
  KEY `idx_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `public_documents` (
  `id` int NOT NULL AUTO_INCREMENT,
  `title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `public_description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `owner_memo` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `content` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  `created_by` int DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `access_policy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT 'public',
  `file_type` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'MIMEタイプ（バイナリアップロード時）',
  `file_path` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'ストレージ上の相対パス',
  `corepo_source_json` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT 'CoRePoプロジェクトのソース(JSON)。アーカイブ保存時に格納。再インポート用。',
  PRIMARY KEY (`id`),
  KEY `idx_title` (`title`),
  KEY `idx_created_at` (`created_at`),
  KEY `idx_created_by` (`created_by`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


SET FOREIGN_KEY_CHECKS = 1;
