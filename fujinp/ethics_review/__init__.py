"""
ethics_review - 人を対象とする研究倫理審査システム

連携審査システム ext_engagement をベースに、論文査読型の
申請者チャンネルと審査トラックの二系統を持つ倫理審査モジュール。
"""
from flask import Blueprint

ethics_review_bp = Blueprint(
    'ethics_review',
    __name__,
    url_prefix='/ethics_review',
    template_folder='templates',
)

from . import routes  # noqa
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa
