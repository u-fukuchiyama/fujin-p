-- 業務実績報告の追加欄（自己評価へのコメント・エビデンス）を T_ann_plan_requests に足す（nishida$fujinp で1回だけ実行）
ALTER TABLE `T_ann_plan_requests`
  ADD COLUMN `author_self_eval_note` text COMMENT '執筆者の自己評価へのコメント（業務実績報告）' AFTER `author_self_eval`,
  ADD COLUMN `author_evidence` mediumtext COMMENT 'エビデンス（業務実績報告，Markdown）' AFTER `author_self_eval_note`;
