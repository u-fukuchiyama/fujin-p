"""ゆにこん（universal_form_composer）— こんかの部品群を「プロジェクト」として預かり，
フォーム（作品）の閲覧・書き出し，部品と SQL テーブルの対応付け，担当者への依頼を受け持つ。

v0.1 は殻：プロジェクトの一覧・閲覧・基本情報の編集・削除・エクスポート・インポート。
"""

from flask import Blueprint

unicon_bp = Blueprint(
    'unicon', __name__,
    url_prefix='/unicon',
    template_folder='templates',
)

from . import routes  # noqa: E402,F401
