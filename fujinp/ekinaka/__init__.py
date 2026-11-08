from flask import Blueprint

ekinaka_bp = Blueprint(
    'ekinaka',
    __name__,
    url_prefix='/ekinaka',
    template_folder='templates'
)

from . import routes  # noqa: E402, F401
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa: E402, F401
# サービスモード（通常稼働／サービス停止。admin が切り替える）
from . import service_mode  # noqa: E402, F401
