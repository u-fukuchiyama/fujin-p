"""
まちなか (machinaka) キャンパス - 常駐型施設運営アプリ

「えきなかキャンパス」(ekinaka) と同様のイベント管理機構を持つが、
次の点が異なる:

  * 常駐している職員が専有イベントを管理する（申請者 = apply）
  * 大学の地域連携係が職員に指示を行い、日報を点検する（承認者 = approve）
  * 日報機能を備え、開閉館時刻・訪問者数・本文を記録する
  * 点検担当は事前登録された地域連携係員のみが実施可能 (inspect 権限)
"""
from flask import Blueprint

machinaka_bp = Blueprint(
    'machinaka',
    __name__,
    template_folder='templates',
    url_prefix='/machinaka'
)

from . import routes   # noqa: E402,F401
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa: E402,F401
