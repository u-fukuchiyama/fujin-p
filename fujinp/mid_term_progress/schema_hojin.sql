-- 法人評価と計画番号ごとの評価（nishida$fujinp に作る）
CREATE TABLE IF NOT EXISTS `T_ann_eval_questions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `fiscal_year` int NOT NULL COMMENT '評価の対象年度（業務実績報告の年度）',
  `q_no` int NOT NULL COMMENT '質問番号',
  `kind` varchar(10) DEFAULT '質問' COMMENT '種別：質問／意見',
  `plan_nos` varchar(200) DEFAULT NULL COMMENT '関係計画番号（カンマ区切り）',
  `question` mediumtext COMMENT '質問・意見の本文',
  `owner_groups` text COMMENT '責任部門（ユーザグループ名のJSON配列）',
  `answer` mediumtext COMMENT '大学からの回答（とりまとめ）',
  `status` enum('received','discussing','drafting','fixed') DEFAULT 'received' COMMENT '受付／検討中／回答案作成／確定',
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  `updated_by` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_q` (`fiscal_year`,`q_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `T_ann_eval_posts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `question_id` int NOT NULL,
  `user_id` int DEFAULT NULL,
  `user_name` varchar(100) DEFAULT NULL,
  `body` mediumtext,
  `created_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_q` (`question_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 計画番号ごとの評価は常設表 T_ann_plan_evaluations を使う（自己評価・評価委員会の評点・コメント）．
-- とりまとめの状態（とりまとめ中／承認済み）を置く列を1つ足す（1回だけ実行）
ALTER TABLE `T_ann_plan_evaluations`
  ADD COLUMN `self_eval_status` enum('drafting','approved') DEFAULT 'drafting' COMMENT '自己評価のとりまとめ：とりまとめ中／承認済み' AFTER `self_eval_reason`;
