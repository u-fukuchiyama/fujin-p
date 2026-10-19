"""
kitare_deliberation.settings — 設定の画面（admin だけ．2026-10-19）

  GET  /settings                 設定の画面（Slack 通知の出力チャンネルと Bot）
  POST /api/settings/slack       送り先の保存（JSON {ch_no, bot}）
  POST /api/settings/slack/test  いまの送り先へ試し送信

送り先は表 kitare_settings に入り，slack_notifier.get_slack_target() が通知のたびに読む．
チャンネルと Bot の候補は Slack_port（チャンネル番号と SLACK_BOTS の鍵）から取る．
"""
import logging

from flask import jsonify, render_template, request, session

from decorators import login_required

from . import kitare_deliberation_bp
from . import slack_notifier as sn


def _is_admin():
    from .routes import is_admin
    return is_admin(session.get('user_id'))


def _deny():
    from .routes import _deny as deny
    return deny('設定は admin だけが変えられます')


def _choices():
    """(チャンネル一覧, Bot 一覧, エラー文)"""
    port = sn._port()
    if port is None:
        return [], [], sn.NO_PORT
    try:
        chans = port.channels()
        bots = [{'key': b['key'], 'name': b.get('name') or '', 'ok': b.get('ok'), 'error': b.get('error')}
                for b in port.bots()]
        return chans, bots, None
    except Exception as e:
        logging.warning("kitare settings: Slack_port を読めません: %s", e)
        return [], [], f'Slack_port を読めません: {e}'


@kitare_deliberation_bp.route('/settings')
@login_required
def settings_page():
    if not _is_admin():
        return _deny()
    chans, bots, err = _choices()
    ch_no, bot = sn.get_slack_target()
    return render_template('kitare_deliberation/settings.html', chans=chans, bots=bots, error=err,
                           ch_no=ch_no, bot=bot, default_ch=sn.SLACK_PORT_CH_NO, default_bot=sn.SLACK_PORT_BOT)


@kitare_deliberation_bp.route('/api/settings/slack', methods=['POST'])
@login_required
def api_settings_slack():
    if not _is_admin():
        return jsonify({'success': False, 'error': '設定は admin だけが変えられます'}), 403
    d = request.get_json(silent=True) or {}
    chans, bots, err = _choices()
    if err:
        return jsonify({'success': False, 'error': err})
    try:
        ch_no = int(d.get('ch_no'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'チャンネルを選んでください'})
    bot = (d.get('bot') or '').strip()
    if ch_no not in {c['no'] for c in chans}:
        return jsonify({'success': False, 'error': f'チャンネル {ch_no} 番は Slack_port に登録されていません'})
    if bot not in {b['key'] for b in bots}:
        return jsonify({'success': False, 'error': f'Bot「{bot}」は Slack_port にありません'})
    try:
        sn.save_slack_target(ch_no, bot, session.get('user_id'))
    except Exception as e:
        return jsonify({'success': False, 'error': f'保存できません: {e}'})
    return jsonify({'success': True})


@kitare_deliberation_bp.route('/api/settings/slack/test', methods=['POST'])
@login_required
def api_settings_slack_test():
    if not _is_admin():
        return jsonify({'success': False, 'error': '設定は admin だけが変えられます'}), 403
    ch_no, bot = sn.get_slack_target()
    r = sn._send('キターレオンライン審議からの試し送信です（設定の画面から送りました）．', '試し送信')
    return jsonify({'success': bool(r.get('ok')), 'error': r.get('error'), 'ch_no': ch_no, 'bot': bot})
