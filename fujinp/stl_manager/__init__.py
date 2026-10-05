"""
stl_manager (えすてぃまね) — Student Launch 報告書管理アプリ

「まちなか (machinaka) キャンパス」のイベント／日報管理機構を参考にしつつ、
次の点が異なる:

  * 主体は STL（Student Launch）の学生団体（15前後）。
    団体は年度ごとに別レコード（年度途中の追加・削除あり、論理削除）。
  * 各団体の editor メンバーが Markdown エディタで報告書を執筆・提出する。
    報告書は「提出日ベース」で記録し、月キーは持たない。
  * 事務局（staff = approve 相当）がダッシュボードの一覧表で
    「月 × 団体」に射影した提出状況を確認し、受理・指示を行う。
  * 事務局は別途「開示記事」をコピペで作成し、記事ごとに
    scope（group / internal / public）＋ 許可 user_group（複数可）で
    開示範囲を切り替える。
  * 当月レポートの提出判定は事務局が設定する「締め日」を基準にする。
"""
from flask import Blueprint

stl_bp = Blueprint(
    'stl',
    __name__,
    template_folder='templates',
    url_prefix='/stl'
)

from . import routes   # noqa: E402,F401
# データ移行（fujinpshowcase → fujinp）の書き出し・取り込み（admin専用）
from . import migration  # noqa: E402,F401
