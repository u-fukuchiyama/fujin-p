"""
stl_manager - サービスモード（fujinp 側．fujinpshowcase からの移行の受け入れ用）

admin がダッシュボードのヘッダのボタンで2つのモードを切り替える．
  normal  : 通常稼働（受け付け再開）
  stopped : サービス停止．一般利用者には「移行作業のため一時停止」とだけ表示する
admin はどのモードでも通常どおりすべての画面を使える（移行データの取り込みと確認を含む）．

モードは stl_settings（skey='service_mode'）に保存する．表は既存のままで，
列の追加はない．移行データの取り込みは stl_settings を入れ替えるが，
この行だけは取り込みの前後で保つ（migration.py）．
"""
import logging

import mysql.connector
from flask import request, redirect, url_for, abort, render_template, session

from decorators import login_required
from db import DatabaseConfig

from . import stl_bp
from .routes import is_admin_user, get_jst_now


SERVICE_KEY = 'service_mode'

MODES = {
    'normal':  '通常稼働',
    'stopped': 'サービス停止',
}

# 停止中・移行後も一般利用者に開けておくエンドポイント
ALWAYS_OPEN = {'stl.return_to_fujin'}


def get_service_mode():
    """現在のモード．読めないときは normal を返す（アプリを止めない）．"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        try:
            cur = conn.cursor()
            cur.execute("SELECT sval FROM stl_settings WHERE skey=%s", (SERVICE_KEY,))
            row = cur.fetchone()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logging.error('stl_manager service_mode read error: %s', e)
        return 'normal'
    mode = row[0] if row else 'normal'
    return mode if mode in MODES else 'normal'


def set_service_mode(mode):
    conn = mysql.connector.connect(**DatabaseConfig.default())
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO stl_settings (skey, sval, updated_at)
            VALUES (%s,%s,%s)
            ON DUPLICATE KEY UPDATE sval=VALUES(sval), updated_at=VALUES(updated_at)
        """, (SERVICE_KEY, mode, get_jst_now()))
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ────────────────────────────────────────────
# 一般利用者の入口で止める
# ────────────────────────────────────────────
@stl_bp.before_request
def _service_gate():
    if is_admin_user():
        return None
    if request.endpoint in ALWAYS_OPEN:
        return None
    mode = get_service_mode()
    if mode == 'normal':
        return None

    html = render_template('stl_manager/service_notice.html', mode=mode)
    return html, 503, {'Retry-After': '3600'}


# ────────────────────────────────────────────
# admin のモード切り替え
# ────────────────────────────────────────────
@stl_bp.route('/service_mode', methods=['POST'])
@login_required
def service_mode_set():
    if not session.get('user_id') or not is_admin_user():
        abort(403)
    mode = request.form.get('mode', '')
    if mode not in MODES:
        abort(400)
    set_service_mode(mode)
    return redirect(url_for('stl.index'))


@stl_bp.context_processor
def _inject_service_mode():
    try:
        if is_admin_user():
            return {'stl_service_mode': get_service_mode(),
                    'stl_service_modes': MODES}
    except Exception:
        pass
    return {'stl_service_mode': None, 'stl_service_modes': MODES}
