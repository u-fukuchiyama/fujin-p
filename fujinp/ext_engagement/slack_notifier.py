"""
slack_notifier.py
ext_engagement（連携申請審査）用 Slack 通知モジュール。

配置：
    ext_engagement/slack_notifier.py
    （routes.py と同じディレクトリ。ext_engagement アプリ内で完結し、
     他の Blueprint には依存しない＝FUJIN-P のアプリ独立性原則を守る）

ethics_review の slack_notifier.py と同じ構造・同じ方式で作ってある。
連携審査用にカスタマイズした点は次の2つだけ：
  - 読み込む config 定数が EXT_ENGAGEMENT_SLACK_CHANNEL_ID
  - 通知本文が連携審査の文面（結論通知・委員長リマインド）

この連携審査アプリで使う通知：
  1. チャンネル投稿（連携審査委員会のプライベートチャンネルへ）
     - 審議開始宣言：委員長または事務局が審議開始を宣言したとき
       （初回の宣言／委員構成を変更しての再宣言の両方）
     - 審議コメント通知：委員が審議コメントを投稿し「委員会全員に
       通知を発出」を選んだとき（opt in）。<!channel> 付き。
     - 結論通知：委員長が結論を宣言したとき。<!channel> 付き。
  2. 個人宛 DM（メールアドレス照合方式）
     - 案件登録通知：事務局が新しい案件を登録したとき、委員長へ
     - 委員長リマインド：期日チェック（check_deadlines.py）が委員長へ

設計原則（ethics_review と同じ）：
    この関数群は「失敗しても呼び出し元を巻き込まない」ことを最優先とする。
    Slack への送信が失敗しても例外を投げず、{'ok': False, ...} を返すだけ。
    結論宣言・期日チェックという本処理は、通知の成否に関わらず成立させる。

事前準備（Slack 側）：
    1. Slack アプリ（Bot）を用意し、Bot Token (xoxb-...) を取得する
       ※ ethics_review と同じ Bot・同じ Token を流用してよい
    2. Bot Token のスコープに以下を付与する：
         chat:write        … メッセージ投稿
         users:read.email  … メールアドレスからユーザー検索（DM用）
         im:write          … DM チャンネルを開く（DM用）
       ※ ethics_review がチャンネル投稿のみで DM を使っていない場合、
         users:read.email と im:write が未付与の可能性がある。
         その場合は Slack App 設定で追加し、ワークスペースへ再インストール。
    3. 投稿先の連携審査委員会プライベートチャンネルを作る
    4. そのチャンネルに Bot を /invite で招待しておく
       （プライベートチャンネルは招待必須。忘れると not_in_channel エラー）
    5. チャンネル ID（C... で始まる文字列）を控える

設定（config 経由で渡す。コードに直書きしない）：
    SLACK_BOT_TOKEN                  : xoxb-...（ethics_review と共通でよい）
    EXT_ENGAGEMENT_SLACK_CHANNEL_ID  : C...（連携審査委員会チャンネル）
"""
import logging

try:
    # PythonAnywhere では slack_sdk を pip install しておく
    # （ethics_review が既に使っているため、追加インストール不要のはず）
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK_AVAILABLE = True
except ImportError:
    # slack_sdk 未インストールでもアプリ全体は落とさない
    _SLACK_SDK_AVAILABLE = False
    logging.warning("slack_notifier(ext_engagement): slack_sdk が見つかりません。"
                     "Slack 通知は無効化されます（pip install slack_sdk）。")


# Slack API のエラーコード → 日本語の説明
_ERROR_JA = {
    'channel_not_found': 'チャンネルが見つかりません（チャンネルIDを確認してください）',
    'not_in_channel':    'Bot がそのチャンネルに参加していません（/invite で招待してください）',
    'is_archived':       'そのチャンネルはアーカイブ済みです',
    'invalid_auth':      'トークンが無効です（SLACK_BOT_TOKEN を確認してください）',
    'token_revoked':     'トークンが失効しています',
    'missing_scope':     'トークンに必要なスコープがありません'
                         '（chat:write / users:read.email / im:write を確認）',
    'account_inactive':  'Bot アカウントが無効化されています',
    'ratelimited':       'レート制限中です（しばらく待って再試行してください）',
    'users_not_found':   'このメールアドレスのSlackユーザーが見つかりません',
}


# ════════════════════════════════════════════════════════════════
# config 読み込み
# ════════════════════════════════════════════════════════════════

def _load_slack_config():
    """
    config から SLACK_BOT_TOKEN と EXT_ENGAGEMENT_SLACK_CHANNEL_ID を読む。
    Returns:
        (token, channel_id) … 読めなければ (None, None)
    """
    try:
        from config import Config
        token      = getattr(Config, 'SLACK_BOT_TOKEN', None)
        channel_id = getattr(Config, 'EXT_ENGAGEMENT_SLACK_CHANNEL_ID', None)
        return token, channel_id
    except Exception as e:
        logging.error("slack_notifier(ext_engagement): config 読み込み失敗: %s", e)
        return None, None


# ════════════════════════════════════════════════════════════════
# チャンネル投稿（結論通知）
# ════════════════════════════════════════════════════════════════

def post_to_channel(token, channel_id, text):
    """
    指定したチャンネルにメッセージを 1 件投稿する。

    Args:
        token      : Slack Bot Token (xoxb-...)
        channel_id : 投稿先チャンネル ID（C... で始まる文字列）
        text       : 投稿本文。@channel メンションを含めたい場合は
                     呼び出し側で本文に '<!channel>' を埋め込む

    Returns:
        成功: {'ok': True,  'ts': '...', 'channel': 'C...'}
        失敗: {'ok': False, 'error': '日本語のエラーメッセージ'}

    この関数は例外を投げない。失敗は必ず戻り値で表現する。
    """
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
        resp = client.chat_postMessage(
            channel=channel_id,
            text=text,
            link_names=True,
        )
        return {
            'ok':      True,
            'ts':      resp.get('ts', ''),
            'channel': resp.get('channel', ''),
        }
    except SlackApiError as e:
        err = e.response.get('error', str(e)) if e.response else str(e)
        msg = _ERROR_JA.get(err, f'Slack APIエラー: {err}')
        logging.error("slack_notifier(ext_engagement).post_to_channel error: "
                      "%s (channel=%s)", err, channel_id)
        return {'ok': False, 'error': msg}
    except Exception as e:
        logging.error("slack_notifier(ext_engagement).post_to_channel "
                      "unexpected error: %s", e)
        return {'ok': False, 'error': f'通知送信に失敗しました: {e}'}


def notify_conclusion(text):
    """
    結論宣言を連携審査委員会チャンネルへ投稿する高レベル関数。
    routes.py の api_case_conclude / api_notify_slack から呼ぶのはこれ一本。

    Args:
        text : 投稿本文（呼び出し側が build_conclusion_message で組み立てる。
               冒頭に <!channel> を含む）

    Returns:
        {'ok': True/False, ...}（呼び出し元はログ目的で見るだけでよい）

    例外は投げない。設定が無い・SDK が無い場合も静かに {'ok': False} を返す。
    """
    token, channel_id = _load_slack_config()
    result = post_to_channel(token, channel_id, text)
    if not result.get('ok'):
        logging.warning("slack_notifier(ext_engagement): 結論通知の送信に失敗 — %s",
                         result.get('error'))
    return result


def notify_review_started(text):
    """
    審議開始宣言を連携審査委員会チャンネルへ投稿する高レベル関数。
    routes.py の api_send_to_review（審議開始宣言）から呼ぶ。

    初回の宣言と、委員構成を変更しての再宣言の両方で使う。
    本文（冒頭の <!channel> を含む）は呼び出し側で組み立てて渡す。

    Args:
        text : 投稿本文

    Returns:
        {'ok': True/False, ...}

    例外は投げない。
    """
    token, channel_id = _load_slack_config()
    result = post_to_channel(token, channel_id, text)
    if not result.get('ok'):
        logging.warning("slack_notifier(ext_engagement): "
                        "審議開始通知の送信に失敗 — %s", result.get('error'))
    return result


def notify_review_comment(text):
    """
    審議コメントの投稿を連携審査委員会チャンネルへ通知する高レベル関数。
    routes.py の api_comment_post から、投稿者が「委員会全員に通知を発出」を
    選んだとき（opt in）にのみ呼ぶ。

    本文（冒頭の <!channel> を含む）は呼び出し側で組み立てて渡す。
    通知には案件番号・課題名（どの案件かのリファレンス）、投稿した委員名、
    コメント本文を載せる。

    Args:
        text : 投稿本文

    Returns:
        {'ok': True/False, ...}

    例外は投げない。
    """
    token, channel_id = _load_slack_config()
    result = post_to_channel(token, channel_id, text)
    if not result.get('ok'):
        logging.warning("slack_notifier(ext_engagement): "
                        "審議コメント通知の送信に失敗 — %s", result.get('error'))
    return result


def notify_motion(text):
    """
    委員長提案（起案・採択・取り下げ）を連携審査委員会チャンネルへ通知する。
    motions.py から呼ぶ。本文（冒頭の <!channel> を含む）は呼び出し側で組み立てる。
    例外は投げない。
    """
    token, channel_id = _load_slack_config()
    result = post_to_channel(token, channel_id, text)
    if not result.get('ok'):
        logging.warning("slack_notifier(ext_engagement): "
                        "委員長提案通知の送信に失敗 — %s", result.get('error'))
    return result


# ════════════════════════════════════════════════════════════════
# 個人宛 DM（メールアドレス照合方式）
#
#   FUJIN-P 側のメールアドレスから Slack ユーザーを引き当て、
#   その人と Bot の DM を開いてメッセージを送る。
#   名簿テーブル（users.slack_user_id）は使わない。
#   FUJIN-P と Slack で同じメールアドレスを使っている人にのみ届く。
#
#   必要スコープ：users:read.email（メール→ユーザー検索）、
#                 im:write（DM チャンネルを開く）、chat:write（投稿）
# ════════════════════════════════════════════════════════════════

def lookup_slack_user_id_by_email(email):
    """
    メールアドレスから Slack ユーザー ID（U... で始まる文字列）を引く。

    Returns:
        成功: {'ok': True,  'user_id': 'U...'}
        失敗: {'ok': False, 'error': '日本語のエラーメッセージ'}

    例外は投げない。照合できない（Slack に同じメールのユーザーがいない）
    場合も {'ok': False} を返すだけで、呼び出し元は止めない。
    """
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
        logging.warning("slack_notifier(ext_engagement)."
                        "lookup_slack_user_id_by_email: %s", err)
        return {'ok': False, 'error': msg}
    except Exception as e:
        logging.error("slack_notifier(ext_engagement)."
                      "lookup_slack_user_id_by_email unexpected error: %s", e)
        return {'ok': False, 'error': f'ユーザー検索に失敗しました: {e}'}


def post_dm_to_user(slack_user_id, text):
    """
    指定した Slack ユーザーへ DM を 1 件送る。

    conversations.open で Bot とそのユーザーの DM チャンネルを開き、
    そのチャンネルへ chat_postMessage する。

    Args:
        slack_user_id : 送り先の Slack ユーザー ID（U... で始まる）
        text          : 本文

    Returns:
        成功: {'ok': True,  'ts': '...', 'channel': 'D...'}
        失敗: {'ok': False, 'error': '日本語のエラーメッセージ'}

    この関数は例外を投げない。
    """
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
        # DM チャンネルを開く
        open_resp = client.conversations_open(users=slack_user_id)
        dm_channel = (open_resp.get('channel') or {}).get('id')
        if not dm_channel:
            return {'ok': False, 'error': 'DMチャンネルを開けませんでした'}
        # DM チャンネルへ投稿
        resp = client.chat_postMessage(channel=dm_channel, text=text)
        return {
            'ok':      True,
            'ts':      resp.get('ts', ''),
            'channel': resp.get('channel', ''),
        }
    except SlackApiError as e:
        err = e.response.get('error', str(e)) if e.response else str(e)
        msg = _ERROR_JA.get(err, f'Slack APIエラー: {err}')
        logging.error("slack_notifier(ext_engagement).post_dm_to_user error: "
                      "%s (user=%s)", err, slack_user_id)
        return {'ok': False, 'error': msg}
    except Exception as e:
        logging.error("slack_notifier(ext_engagement).post_dm_to_user "
                      "unexpected error: %s", e)
        return {'ok': False, 'error': f'DM送信に失敗しました: {e}'}


def send_dm_by_email(email, text, log_label='DM'):
    """
    メールアドレスから Slack ユーザーを引き当て、その人へ DM を送る。
    lookup_slack_user_id_by_email と post_dm_to_user をつないだ高レベル関数。

    routes.py / check_deadlines.py からはこれ一本を呼べばよい。

    Args:
        email     : 送り先の人の（FUJIN-P 上の）メールアドレス
        text      : 本文
        log_label : 失敗ログに出す通知の種類名（例 '委員長リマインドDM'）

    Returns:
        {'ok': True/False, ...}

    例外は投げない。メール照合に失敗（FUJIN-P と Slack でメールが
    食い違う）場合も {'ok': False} を返すだけ。呼び出し元の本処理は止めない。
    """
    look = lookup_slack_user_id_by_email(email)
    if not look.get('ok'):
        logging.warning("slack_notifier(ext_engagement): %s — メール照合に失敗 — %s",
                         log_label, look.get('error'))
        return {'ok': False, 'error': look.get('error')}

    result = post_dm_to_user(look['user_id'], text)
    if not result.get('ok'):
        logging.warning("slack_notifier(ext_engagement): %s の送信に失敗 — %s",
                         log_label, result.get('error'))
    return result
