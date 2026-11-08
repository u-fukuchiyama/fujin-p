"""
ekinaka - サービスモード（fujinp 側．fujinpshowcase からの移行の受け入れ用）

admin がダッシュボードのヘッダのボタンで2つのモードを切り替える．
  normal  : 通常稼働（受け付け再開）
  stopped : サービス停止．一般利用者には「移行作業のため一時停止」とだけ表示する
admin はどのモードでも通常どおりすべての画面を使える（移行データの取り込みと確認を含む）．

モードはデータベースではなく <app>/static/migration_staging/service_mode.txt に置く．
移行データの取り込みは表を入れ替えるが，このファイルには触れないので，
停止中に何度取り込んでも停止は解けない．
この置き場は GitHub に送らず Web にも配信されない．
"""
import os
import logging

from flask import request, redirect, url_for, abort, render_template, session

from decorators import login_required

from . import ekinaka_bp
from .routes import is_admin_user


MODES = {
    'normal':  '通常稼働',
    'stopped': 'サービス停止',
}

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MODE_FILE = os.path.join(APP_DIR, 'static', 'migration_staging', 'service_mode.txt')

# 停止中も一般利用者に開けておくエンドポイント
#   display_serve はメッセージディスプレイの機体が取りに来る画像（認証なし）なので止めない
ALWAYS_OPEN = {'ekinaka.return_to_fujin', 'ekinaka.display_serve'}


def get_service_mode():
    """現在のモード．読めないときは normal を返す（アプリを止めない）．"""
    try:
        with open(MODE_FILE, encoding='utf-8') as f:
            mode = f.read().strip()
    except FileNotFoundError:
        return 'normal'
    except Exception as e:
        logging.error('ekinaka service_mode read error: %s', e)
        return 'normal'
    return mode if mode in MODES else 'normal'


def set_service_mode(mode):
    os.makedirs(os.path.dirname(MODE_FILE), exist_ok=True)
    tmp = MODE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(mode)
    os.replace(tmp, MODE_FILE)


# ────────────────────────────────────────────
# 一般利用者の入口で止める
# ────────────────────────────────────────────
@ekinaka_bp.before_request
def _service_gate():
    if is_admin_user():
        return None
    if request.endpoint in ALWAYS_OPEN:
        return None
    if get_service_mode() == 'normal':
        return None
    html = render_template('ekinaka_service_notice.html', mode='stopped', moved_url=None)
    return html, 503, {'Retry-After': '3600'}


# ────────────────────────────────────────────
# admin のモード切り替え
# ────────────────────────────────────────────
@ekinaka_bp.route('/service_mode', methods=['POST'])
@login_required
def service_mode_set():
    if not session.get('user_id') or not is_admin_user():
        abort(403)
    mode = request.form.get('mode', '')
    if mode not in MODES:
        abort(400)
    set_service_mode(mode)
    return redirect(url_for('ekinaka.index'))


@ekinaka_bp.context_processor
def _inject_service_mode():
    try:
        if is_admin_user():
            return {'ekinaka_service_mode': get_service_mode(),
                    'ekinaka_service_modes': MODES}
    except Exception:
        pass
    return {'ekinaka_service_mode': None, 'ekinaka_service_modes': MODES}
