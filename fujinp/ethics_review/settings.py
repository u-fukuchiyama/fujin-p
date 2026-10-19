"""
ethics_review.settings — 設定の画面（admin だけ．2026-10-19）

  GET  /settings                 設定の画面（Slack 通知の出力チャンネルと Bot）
  POST /api/settings/slack       送り先の保存（JSON {ch_no, business_ch_no, committee_ch_no, bot}）
  POST /api/settings/slack/test  いまの送り先へ試し送信（JSON {purpose}：'' / 'business' / 'committee'）

送り先は表 ethics_settings に入り，slack_notifier.get_slack_settings() が通知のたびに読む．
チャンネルと Bot の候補は Slack_port（チャンネル番号と SLACK_BOTS の鍵）から取る．
"""
import logging

from flask import jsonify, render_template, request, session

from decorators import login_required

from . import ethics_review_bp
from . import slack_notifier as sn

PURPOSE_LABEL = {'': '既定', 'business': '業務会議（事務局）向け', 'committee': '委員会向け'}


def _is_admin():
    from .routes import is_admin
    return is_admin(session.get('user_id'))


def _deny():
    return ('<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">設定は admin だけが変えられます</h2>'
            '<p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査のトップへ戻る</a></p>'), 403


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
        logging.warning("ethics settings: Slack_port を読めません: %s", e)
        return [], [], f'Slack_port を読めません: {e}'


@ethics_review_bp.route('/settings')
@login_required
def settings_page():
    if not _is_admin():
        return _deny()
    chans, bots, err = _choices()
    return render_template('ethics_review/settings.html', chans=chans, bots=bots, error=err,
                           st=sn.get_slack_settings(), default_ch=sn.SLACK_PORT_CH_NO,
                           default_bot=sn.SLACK_PORT_BOT)


def _opt_int(v):
    if v in (None, '', 0, '0'):
        return None
    return int(v)


@ethics_review_bp.route('/api/settings/slack', methods=['POST'])
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
        business = _opt_int(d.get('business_ch_no'))
        committee = _opt_int(d.get('committee_ch_no'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'チャンネルを選んでください'})
    bot = (d.get('bot') or '').strip()
    known = {c['no'] for c in chans}
    for n in (ch_no, business, committee):
        if n is not None and n not in known:
            return jsonify({'success': False, 'error': f'チャンネル {n} 番は Slack_port に登録されていません'})
    if bot not in {b['key'] for b in bots}:
        return jsonify({'success': False, 'error': f'Bot「{bot}」は Slack_port にありません'})
    try:
        sn.save_slack_settings(ch_no, business, committee, bot, session.get('user_id'))
    except Exception as e:
        return jsonify({'success': False, 'error': f'保存できません: {e}'})
    return jsonify({'success': True})


@ethics_review_bp.route('/api/settings/slack/test', methods=['POST'])
@login_required
def api_settings_slack_test():
    if not _is_admin():
        return jsonify({'success': False, 'error': '設定は admin だけが変えられます'}), 403
    purpose = (request.get_json(silent=True) or {}).get('purpose') or ''
    if purpose not in PURPOSE_LABEL:
        purpose = ''
    st = sn.get_slack_settings()
    ch_no = sn.channel_for(purpose or None, st)
    r = sn._send(f'研究倫理審査からの試し送信です（{PURPOSE_LABEL[purpose]}の送り先．設定の画面から送りました）．',
                 '試し送信', purpose=purpose or None)
    return jsonify({'success': bool(r.get('ok')), 'error': r.get('error'), 'ch_no': ch_no, 'bot': st['bot'],
                    'label': PURPOSE_LABEL[purpose]})
