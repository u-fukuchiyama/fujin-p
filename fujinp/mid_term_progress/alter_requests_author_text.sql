-- 既存の T_ann_plan_requests に，執筆者の提出を置く列を足す（nishida$fujinp で1回だけ実行）
ALTER TABLE `T_ann_plan_requests`
  ADD COLUMN `author_text` mediumtext COMMENT '執筆者からの報告（提出）' AFTER `author_status`,
  ADD COLUMN `author_self_eval` tinyint DEFAULT NULL COMMENT '執筆者の自己評価案（業務実績報告）' AFTER `author_text`,
  ADD COLUMN `author_text_at` datetime DEFAULT NULL AFTER `author_self_eval`,
  ADD COLUMN `author_text_by` int DEFAULT NULL AFTER `author_text_at`;
