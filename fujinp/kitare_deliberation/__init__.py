"""
kitare_deliberation - キターレオンライン審議
Blueprint 定義

研究倫理審査(ethics_review)を簡単化した、固定メンバーによる
オンライン審議システム。アプリ独立性原則に従い、他の Blueprint には
依存しない（user_groups.utils のみ共通基盤として利用）。

登録例（アプリ本体の app factory 側、app.py）:
    from .kitare_deliberation import kitare_deliberation_bp
    app.register_blueprint(kitare_deliberation_bp,
                           url_prefix='/kitare_deliberation')
"""
from flask import Blueprint

kitare_deliberation_bp = Blueprint(
    'kitare_deliberation',
    __name__,
    template_folder='templates',
)

# routes をインポートしてルートを登録（循環 import 回避のため末尾で）
from . import routes  # noqa: E402,F401
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa: E402,F401
