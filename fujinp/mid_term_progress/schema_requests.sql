-- 中期計画進捗：執筆依頼の状態・執筆者の提出・評価室からのコメント（nishida$fujinp に作る）
-- 依頼の単位＝年度×中期計画番号×年度計画番号×細目×段階（plan／progress／result）
-- 進捗報告・業務実績報告では，執筆者の提出（author_text）をここに置き，
-- 編集者がまとめた／承認した報告を正本 T_ann_plan_details（progress_text／result_text）に置く
CREATE TABLE IF NOT EXISTS `T_ann_plan_requests` (
  `id` int NOT NULL AUTO_INCREMENT,
  `fiscal_year` int NOT NULL COMMENT '年度',
  `mid_plan_no` int NOT NULL COMMENT '中期計画番号',
  `annual_plan_no` int NOT NULL COMMENT '年度計画番号',
  `detail_no` int NOT NULL COMMENT '細目番号',
  `stage` enum('plan','progress','result') NOT NULL COMMENT '段階：計画／進捗報告／業務実績報告',
  `owner_group` varchar(100) DEFAULT NULL COMMENT '依頼時の担当グループ（控え）',
  `editor_status` enum('requested','confirmed') DEFAULT NULL COMMENT '編集者側：依頼中／確認済み（NULL＝未依頼）',
  `author_status` enum('writing','done','revised') DEFAULT NULL COMMENT '執筆者側：執筆中／執筆完了／改訂済み',
  `author_text` mediumtext COMMENT '執筆者からの報告（提出）',
  `author_self_eval` tinyint DEFAULT NULL COMMENT '執筆者の自己評価案（業務実績報告）',
  `author_self_eval_note` text COMMENT '執筆者の自己評価へのコメント（業務実績報告）',
  `author_evidence` mediumtext COMMENT 'エビデンス（業務実績報告，Markdown）',
  `author_text_at` datetime DEFAULT NULL,
  `author_text_by` int DEFAULT NULL,
  `comment` text COMMENT '評価室からのコメント',
  `requested_at` datetime DEFAULT NULL,
  `requested_by` int DEFAULT NULL,
  `editor_updated_at` datetime DEFAULT NULL,
  `editor_updated_by` int DEFAULT NULL,
  `author_updated_at` datetime DEFAULT NULL,
  `author_updated_by` int DEFAULT NULL,
  `comment_updated_at` datetime DEFAULT NULL,
  `comment_updated_by` int DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_req` (`fiscal_year`,`mid_plan_no`,`annual_plan_no`,`detail_no`,`stage`),
  KEY `idx_year_stage` (`fiscal_year`,`stage`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
