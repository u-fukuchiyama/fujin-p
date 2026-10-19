"""
ext_engagement.settings — 設定の画面（サイトの admin だけ．2026-10-19）

  GET  /settings                 設定の画面（Slack 通知の出力チャンネルと Bot）
  POST /api/settings/slack       送り先の保存（JSON {ch_no, bot}）
  POST /api/settings/slack/test  いまの送り先へ試し送信

送り先は表 ext_engagement_settings に入り，slack_notifier.get_slack_settings() が通知のたびに読む．
チャンネルと Bot の候補は Slack_port（チャンネル番号と SLACK_BOTS の鍵）から取る．
トップの「設定」ボタンは，ユーザ区分が admin の人にだけ出す（連携審査の事務局グループには出さない）．
"""
import logging

from flask import jsonify, render_template, request, session

from decorators import login_required

from . import ext_engagement_bp
from . import slack_notifier as sn


def _is_site_admin():
    from .routes import get_user_category
    uid = session.get('user_id')
    return bool(uid) and get_user_category(uid) == 'admin'


@ext_engagement_bp.context_processor
def _inject_site_admin():
    try:
        return {'is_site_admin': _is_site_admin()}
    except Exception:
        return {'is_site_admin': False}


def _deny():
    return ('<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">設定は admin だけが変えられます</h2>'
            '<p style="font-family:sans-serif;padding:0 40px"><a href="/ext_engagement/">連携申請審査のトップへ戻る</a></p>'), 403


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
        logging.warning("ext settings: Slack_port を読めません: %s", e)
        return [], [], f'Slack_port を読めません: {e}'


@ext_engagement_bp.route('/settings')
@login_required
def settings_page():
    if not _is_site_admin():
        return _deny()
    chans, bots, err = _choices()
    return render_template('ext_engagement/settings.html', chans=chans, bots=bots, error=err,
                           st=sn.get_slack_settings(), default_bot=sn.SLACK_PORT_BOT)


@ext_engagement_bp.route('/api/settings/slack', methods=['POST'])
@login_required
def api_settings_slack():
    if not _is_site_admin():
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
        sn.save_slack_settings(ch_no, bot, session.get('user_id'))
    except Exception as e:
        return jsonify({'success': False, 'error': f'保存できません: {e}'})
    return jsonify({'success': True})


@ext_engagement_bp.route('/api/settings/slack/test', methods=['POST'])
@login_required
def api_settings_slack_test():
    if not _is_site_admin():
        return jsonify({'success': False, 'error': '設定は admin だけが変えられます'}), 403
    st = sn.get_slack_settings()
    r = sn._send('連携申請審査からの試し送信です（設定の画面から送りました）．', '試し送信')
    return jsonify({'success': bool(r.get('ok')), 'error': r.get('error'), 'ch_no': st['ch_no'], 'bot': st['bot']})
