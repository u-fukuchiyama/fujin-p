-- 事業全貌の設定表（fujinp の DB に作る）．執筆者に開いている依頼（k='active_sets'）と担当部門の並び（k='matrix_columns'）を置く．
-- 旧版はアプリのディレクトリの data/active_sets.json・data/matrix_columns.json に置いていた．表が無くても，最初に保存したときにアプリが作る
CREATE TABLE IF NOT EXISTS `mid_term_progress_settings` (
  `k` varchar(64) NOT NULL COMMENT '設定名',
  `v` mediumtext COMMENT '設定値（JSON）',
  `updated_at` datetime DEFAULT NULL,
  `updated_by` int DEFAULT NULL,
  PRIMARY KEY (`k`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
