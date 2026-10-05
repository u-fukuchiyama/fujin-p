"""
slack_notifier.py
キターレオンライン審議 用 Slack 通知モジュール。

配置：
    kitare_deliberation/slack_notifier.py
    （routes.py と同じディレクトリ。アプリ内で完結し、他の Blueprint には
     依存しない＝FUJIN-P のアプリ独立性原則を守る）

通知の対象（設計メモ v2 の7に対応）：
    1. 申請があったとき   … チャンネル一括通知 ＋ 委員長への個別 DM
    2. 締切が来たとき     … チャンネル一括通知（日次バッチから）
    3. 審議の開始         … チャンネル一括通知（業務会議が受理→開始操作時）
    4. 審議の終了（議決） … チャンネル一括通知
    付随：重要発言（alert）のアウェアネス通知

設計原則：
    この関数は「失敗しても呼び出し元を巻き込まない」ことを最優先とする。
    Slack 送信が失敗しても例外を投げず、{'ok': False, ...} を返すだけ。
    審議という本処理は、通知の成否に関わらず成立させる。

事前準備（Slack 側）：
    1. Slack アプリ（Bot）を用意し、Bot Token (xoxb-...) を取得
    2. スコープ: chat:write（投稿）、users:read.email（メール→ユーザー検索）、
       im:write（DM チャンネルを開く）
    3. 投稿先チャンネルを作り、Bot を /invite で招待しておく
       （プライベートチャンネルは招待必須。忘れると not_in_channel エラー）

設定（config 経由。コードに直書きしない。後で設定する）：
    SLACK_BOT_TOKEN          : xoxb-...（ethics_review と共用可）
    KITARE_SLACK_CHANNEL_ID  : C...（審議通知の投稿先チャンネル）
"""
import logging

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK_AVAILABLE = True
except ImportError:
    _SLACK_SDK_AVAILABLE = False
    logging.warning("slack_notifier(kitare): slack_sdk が見つかりません。"
                    "Slack 通知は無効化されます（pip install slack_sdk）。")


_ERROR_JA = {
    'channel_not_found': 'チャンネルが見つかりません（チャンネルIDを確認してください）',
    'not_in_channel':    'Bot がそのチャンネルに参加していません（/invite で招待してください）',
    'is_archived':       'そのチャンネルはアーカイブ済みです',
    'invalid_auth':      'トークンが無効です（SLACK_BOT_TOKEN を確認してください）',
    'token_revoked':     'トークンが失効しています',
    'missing_scope':     'トークンに必要なスコープがありません',
    'account_inactive':  'Bot アカウントが無効化されています',
    'ratelimited':       'レート制限中です（しばらく待って再試行してください）',
}


def _load_slack_config():
    """config から SLACK_BOT_TOKEN と KITARE_SLACK_CHANNEL_ID を読む。
       読めなければ (None, None)。"""
    try:
        from config import Config
        token      = getattr(Config, 'SLACK_BOT_TOKEN', None)
        channel_id = getattr(Config, 'KITARE_SLACK_CHANNEL_ID', None)
        return token, channel_id
    except Exception as e:
        logging.error("slack_notifier(kitare): config 読み込み失敗: %s", e)
        return None, None


# ════════════════════════════════════════════════════════════════
# チャンネル投稿
# ════════════════════════════════════════════════════════════════

def post_to_channel(token, channel_id, text):
    """指定チャンネルにメッセージを1件投稿する。例外は投げない。
       成功 {'ok': True, 'ts':..., 'channel':...} / 失敗 {'ok': False, 'error':...}"""
    if not _SLACK_SDK_AVAILABLE:
        return {'ok': False, 'error': 'slack_sdk が未インストールです'}
    if not token:
        return {'ok': False, 'error': 'SLACK_BOT_TOKEN が設定されていません'}
    if not channel_id:
        return {'ok': False, 'error': '投稿先チャンネルIDが設定されていません'}
    if not text:
        return {'ok': False, 'error': '投稿本文が空です'}

    try:
        client = WebClient(token=token)
        resp = client.chat_postMessage(channel=channel_id, text=text,
                                       link_names=True)
        return {'ok': True, 'ts': resp.get('ts', ''),
                'channel': resp.get('channel', '')}
    except SlackApiError as e:
        err = e.response.get('error', str(e)) if e.response else str(e)
        msg = _ERROR_JA.get(err, f'Slack APIエラー: {err}')
        logging.error("slack_notifier(kitare).post_to_channel error: %s "
                      "(channel=%s)", err, channel_id)
        return {'ok': False, 'error': msg}
    except Exception as e:
        logging.error("slack_notifier(kitare).post_to_channel "
                      "unexpected error: %s", e)
        return {'ok': False, 'error': f'通知送信に失敗しました: {e}'}


def _send(text, log_label):
    """channel へ text を投稿する内部共通処理。"""
    token, channel_id = _load_slack_config()
    result = post_to_channel(token, channel_id, text)
    if not result.get('ok'):
        logging.warning("slack_notifier(kitare): %s の送信に失敗 — %s",
                        log_label, result.get('error'))
    return result


def notify_application_submitted(case_display_id, title, applicant_name=None,
                                 case_url=None):
    """【1】申請があったことをチャンネルへ一括通知する。
       routes.py の api_submit から、commit 後に呼ぶ。"""
    lines = [
        '<!channel> 新しい審議案件の申請がありました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('業務会議（委員長・事務局）で受理判断をお願いします。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '申請通知')


def notify_review_started(case_display_id, title, deadline=None, case_url=None):
    """【3】審議が開始されたことをチャンネルへ一括通知する。
       routes.py の api_start_review から、commit 後に呼ぶ。"""
    lines = [
        '<!channel> 運営委員会の審議が開始されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
    ]
    if deadline:
        lines.append(f'■ 審議期日：{deadline}')
    lines.append('')
    lines.append('委員の皆さまは案件ページで審議にご参加ください。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '審議開始通知')


def notify_decision(case_display_id, title, decision_label, case_url=None):
    """【4】委員長の議決をチャンネルへ一括通知する。
       routes.py の api_decide から、commit 後に呼ぶ。
       decision_label … '可決' '否決' '継続審議' のいずれか。"""
    lines = [
        '<!channel> 委員長による議決が示されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
        f'■ 議決：{decision_label or "（不明）"}',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '議決通知')


def notify_deadline_reached(case_display_id, title, deadline=None,
                            case_url=None):
    """【2】審議期日が到来したことをチャンネルへ一括通知する。
       日次バッチ（routes.py の api_deadline_batch）から呼ぶ。
       忘れ防止のリマインダ一発。"""
    lines = [
        '<!channel> 審議案件の審議期日が来ています。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
    ]
    if deadline:
        lines.append(f'■ 審議期日：{deadline}')
    lines.append('')
    lines.append('委員長は議決（可決／否決／継続審議）をご検討ください。'
                 '継続審議とする場合は新しい審議期日を設定してください。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '締切通知')


def notify_alert_message(case_display_id, title, case_url=None):
    """付随：審議トラックに重要発言（alert）が投稿されたことを通知する。
       中身は載せず案件番号と誘導だけ。
       routes.py の api_post_review_message から、alert 投稿後に呼ぶ。"""
    lines = [
        '<!channel> 審議中の案件について重要な発言があります。ご確認ください。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
        '',
        '審議トラックに重要な発言が投稿されました。'
        '担当の方は案件ページでご確認ください。',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '重要発言通知')


# ════════════════════════════════════════════════════════════════
# 個人宛 DM（メールアドレス照合方式）
#   FUJIN-P 上のメールから Slack ユーザーを引き当て DM する。
#   FUJIN-P と Slack で同じメールの人にのみ届く。
#   必要スコープ：users:read.email、im:write、chat:write
# ════════════════════════════════════════════════════════════════

def lookup_slack_user_id_by_email(email):
    """メールアドレスから Slack ユーザー ID を引く。例外は投げない。"""
    if not _SLACK_SDK_AVAILABLE:
        return {'ok': False, 'error': 'slack_sdk が未インストールです'}
    if not email:
        return {'ok': False, 'error': 'メールアドレスが空です'}

    token, _channel = _load_slack_config()
    if not token:
        return {'ok': False, 'error': 'SLACK_BOT_TOKEN が設定されていません'}

    try:
        client = WebClient(token=token)
        resp = client.users_lookupByEmail(email=email)
        user = resp.get('user') or {}
        uid = user.get('id')
        if not uid:
            return {'ok': False, 'error': 'ユーザーIDを取得できませんでした'}
        return {'ok': True, 'user_id': uid}
    except SlackApiError as e:
        err = e.response.get('error', str(e)) if e.response else str(e)
        if err == 'users_not_found':
            msg = f'このメールアドレスのSlackユーザーが見つかりません（{email}）'
        else:
            msg = _ERROR_JA.get(err, f'Slack APIエラー: {err}')
        logging.warning("slack_notifier(kitare).lookup_slack_user_id_by_email: %s",
                        err)
        return {'ok': False, 'error': msg}
    except Exception as e:
        logging.error("slack_notifier(kitare).lookup_slack_user_id_by_email "
                      "unexpected error: %s", e)
        return {'ok': False, 'error': f'ユーザー検索に失敗しました: {e}'}


def post_dm_to_user(slack_user_id, text):
    """指定 Slack ユーザーへ DM を1件送る。例外は投げない。"""
    if not _SLACK_SDK_AVAILABLE:
        return {'ok': False, 'error': 'slack_sdk が未インストールです'}
    if not slack_user_id:
        return {'ok': False, 'error': '送り先のSlackユーザーIDがありません'}
    if not text:
        return {'ok': False, 'error': '投稿本文が空です'}

    token, _channel = _load_slack_config()
    if not token:
        return {'ok': False, 'error': 'SLACK_BOT_TOKEN が設定されていません'}

    try:
        client = WebClient(token=token)
        open_resp = client.conversations_open(users=slack_user_id)
        dm_channel = (open_resp.get('channel') or {}).get('id')
        if not dm_channel:
            return {'ok': False, 'error': 'DMチャンネルを開けませんでした'}
        resp = client.chat_postMessage(channel=dm_channel, text=text)
        return {'ok': True, 'ts': resp.get('ts', ''),
                'channel': resp.get('channel', '')}
    except SlackApiError as e:
        err = e.response.get('error', str(e)) if e.response else str(e)
        msg = _ERROR_JA.get(err, f'Slack APIエラー: {err}')
        logging.error("slack_notifier(kitare).post_dm_to_user error: %s "
                      "(user=%s)", err, slack_user_id)
        return {'ok': False, 'error': msg}
    except Exception as e:
        logging.error("slack_notifier(kitare).post_dm_to_user "
                      "unexpected error: %s", e)
        return {'ok': False, 'error': f'DM送信に失敗しました: {e}'}


def send_dm_by_email(email, text, log_label='DM'):
    """メールアドレスから Slack ユーザーを引き当て DM を送る高レベル関数。
       例外は投げない。照合失敗時も {'ok': False} を返すだけ。"""
    look = lookup_slack_user_id_by_email(email)
    if not look.get('ok'):
        logging.warning("slack_notifier(kitare): %s — メール照合に失敗 — %s",
                        log_label, look.get('error'))
        return {'ok': False, 'error': look.get('error')}

    result = post_dm_to_user(look['user_id'], text)
    if not result.get('ok'):
        logging.warning("slack_notifier(kitare): %s の送信に失敗 — %s",
                        log_label, result.get('error'))
    return result


def notify_application_to_recipients(recipient_emails, case_display_id, title,
                                     applicant_name=None, case_url=None):
    """【1の後半】申請があったことを宛先（当面は委員長）へ DM で通知する。
       routes.py の api_submit から、commit 後・チャンネル通知とあわせて呼ぶ。

    Args:
        recipient_emails : 宛先メールアドレスのリスト（委員長たちのメール。
                           将来 routes 側で宛先グループを差し替えれば変わる）。
    Returns:
        {'ok':..., 'sent': 成功数, 'failed': 失敗数, 'results': [...]}
    例外は投げない。1人への失敗が他をブロックしない。
    """
    lines = [
        '新しい審議案件の申請がありました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 案件名：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('業務会議で受理判断をお願いします。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    text = '\n'.join(lines)

    results, sent, failed = [], 0, 0
    for email in (recipient_emails or []):
        if not email:
            continue
        r = send_dm_by_email(email, text, log_label='申請通知DM')
        ok = bool(r.get('ok'))
        results.append({'email': email, 'ok': ok, 'error': r.get('error')})
        if ok:
            sent += 1
        else:
            failed += 1

    if sent == 0 and failed == 0:
        logging.warning("slack_notifier(kitare): 申請通知DM — 宛先が0件です")
        return {'ok': False, 'sent': 0, 'failed': 0, 'results': []}
    return {'ok': sent > 0, 'sent': sent, 'failed': failed, 'results': results}
