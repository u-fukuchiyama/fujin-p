"""
mid_term_progress - 事業全貌（中期計画・年度計画の策定・報告・評価・閲覧）アプリ
"""
from flask import Blueprint

mid_term_progress_bp = Blueprint(
    'mid_term_progress',
    __name__,
    template_folder='templates',
    url_prefix='/mid_term_progress'
)

from . import routes
from . import workflow   # 編集者用・執筆者用ダッシュボード
from . import structure  # 年度計画策定（細目集の組み立てと部門の割り当て）
from . import progress   # 編集者ダッシュボード（入口）と進捗状況報告
from . import authorview # 執筆者ダッシュボード
from . import hojin      # 法人評価と計画番号ごとの評価
from . import migrate    # まるごと移行（8表を1ファイルで書き出し・取り込み）
