"""
slack_notifier.py
ethics_review（研究倫理審査）用 Slack 通知モジュール — 最小構成。

配置：
    ethics_review/slack_notifier.py
    （routes.py と同じディレクトリ。ethics_review アプリ内で完結し、
     他の Blueprint には依存しない＝FUJIN-P のアプリ独立性原則を守る）

今回の対象：
    申請があったとき、実験用プライベートチャンネルへ通知を投稿するだけ。
    DM・宛先解決・名簿などは扱わない。

設計原則：
    この関数は「失敗しても呼び出し元を巻き込まない」ことを最優先とする。
    Slack への送信が失敗しても例外を投げず、{'ok': False, ...} を返すだけ。
    審査申請という本処理は、通知の成否に関わらず成立させる。

事前準備（Slack 側）：
    1. Slack アプリ（Bot）を用意し、Bot Token (xoxb-...) を取得する
    2. Bot Token のスコープに 'chat:write' を付与する
    3. 投稿先の実験用プライベートチャンネルを作る
    4. そのチャンネルに Bot を /invite で招待しておく
       （プライベートチャンネルは招待必須。これを忘れると not_in_channel エラー）
    5. チャンネル ID（C... で始まる文字列）を控える
       チャンネル名のメニュー →「チャンネル詳細」最下部に ID が表示される

設定（config 経由で渡す。コードに直書きしない）：
    SLACK_BOT_TOKEN              : xoxb-...
    ETHICS_SLACK_CHANNEL_ID      : C...（実験用プライベートチャンネル）
"""
import logging

try:
    # PythonAnywhere では slack_sdk を pip install しておく
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK_AVAILABLE = True
except ImportError:
    # slack_sdk 未インストールでもアプリ全体は落とさない
    _SLACK_SDK_AVAILABLE = False
    logging.warning("slack_notifier: slack_sdk が見つかりません。"
                     "Slack 通知は無効化されます（pip install slack_sdk）。")


# Slack API のエラーコード → 日本語の説明
_ERROR_JA = {
    'channel_not_found': 'チャンネルが見つかりません（チャンネルIDを確認してください）',
    'not_in_channel':    'Bot がそのチャンネルに参加していません（/invite で招待してください）',
    'is_archived':       'そのチャンネルはアーカイブ済みです',
    'invalid_auth':      'トークンが無効です（SLACK_BOT_TOKEN を確認してください）',
    'token_revoked':     'トークンが失効しています',
    'missing_scope':     'トークンに chat:write スコープがありません',
    'account_inactive':  'Bot アカウントが無効化されています',
    'ratelimited':       'レート制限中です（しばらく待って再試行してください）',
}


def post_to_channel(token, channel_id, text):
    """
    指定したチャンネルにメッセージを 1 件投稿する。

    Args:
        token      : Slack Bot Token (xoxb-...)
        channel_id : 投稿先チャンネル ID（C... で始まる文字列）
        text       : 投稿本文。@channel メンションを含めたい場合は
                     呼び出し側で本文に '<!channel>' を埋め込む
                     （build_application_message がそれを行う）

    Returns:
        成功: {'ok': True,  'ts': '1234567890.123456', 'channel': 'C...'}
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
            # link_names は @channel/@here の特殊メンションには不要だが、
            # 将来 @username を使う場合に備えて True にしておく
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
        logging.error("slack_notifier.post_to_channel error: %s (channel=%s)",
                      err, channel_id)
        return {'ok': False, 'error': msg}
    except Exception as e:
        # ネットワーク障害など SlackApiError 以外
        logging.error("slack_notifier.post_to_channel unexpected error: %s", e)
        return {'ok': False, 'error': f'通知送信に失敗しました: {e}'}


def build_application_message(case_display_id, title, applicant_name=None,
                              case_url=None, mention_channel=True):
    """
    「申請がありました」の投稿本文を組み立てる。

    Args:
        case_display_id : 案件番号（ethics_cases.case_display_id）
        title           : 研究課題名（ethics_cases.title）
        applicant_name  : 申請者名（任意。無ければ省略）
        case_url        : 案件ページへのリンク（任意）
        mention_channel : True なら先頭に <!channel>（@channel メンション）を付ける

    Returns:
        str : 投稿本文
    """
    lines = []
    if mention_channel:
        # Slack で @channel として展開される特殊メンション記法
        lines.append('<!channel> 新しい研究倫理審査の申請がありました。')
    else:
        lines.append('新しい研究倫理審査の申請がありました。')

    lines.append('')
    lines.append(f'■ 案件番号：{case_display_id or "（番号未付与）"}')
    lines.append(f'■ 研究課題：{title or "（無題）"}')
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')

    return '\n'.join(lines)


def notify_application_submitted(case_display_id, title,
                                 applicant_name=None, case_url=None):
    """
    申請受付を実験用チャンネルへ通知する高レベル関数。
    routes.py の api_submit から呼ぶのはこれ一本。

    config から SLACK_BOT_TOKEN と ETHICS_SLACK_CHANNEL_ID を読み、
    本文を組み立てて投稿する。

    Returns:
        {'ok': True/False, ...}（呼び出し元はログ目的で見るだけでよい）

    例外は投げない。設定が無い・SDK が無い場合も静かに {'ok': False} を返す。
    """
    try:
        from config import Config
        token      = getattr(Config, 'SLACK_BOT_TOKEN', None)
        channel_id = getattr(Config, 'ETHICS_SLACK_CHANNEL_ID', None)
    except Exception as e:
        logging.error("slack_notifier: config 読み込み失敗: %s", e)
        return {'ok': False, 'error': 'config が読み込めません'}

    text = build_application_message(
        case_display_id=case_display_id,
        title=title,
        applicant_name=applicant_name,
        case_url=case_url,
        mention_channel=True,
    )
    result = post_to_channel(token, channel_id, text)

    if not result.get('ok'):
        # 通知失敗はログに残すだけ。呼び出し元（申請処理）は止めない。
        logging.warning("slack_notifier: 申請通知の送信に失敗 — %s",
                         result.get('error'))
    return result


# ════════════════════════════════════════════════════════════════
# 追加分：審査の節目イベントの通知（2・3・7番）
#
# いずれもチャンネルへの一括通知。個人メンション（@委員長など）は
# 委員 Slack ID 対応表が必要なため、ここには含めない（次段階）。
#
# 共通方針：
#   - config から token と channel_id を読む（_load_slack_config）
#   - 本文を組み立てて post_to_channel に渡すだけ
#   - 例外は投げない。失敗は {'ok': False} とログのみ
# ════════════════════════════════════════════════════════════════

def _load_slack_config():
    """
    config から SLACK_BOT_TOKEN と ETHICS_SLACK_CHANNEL_ID を読む。
    Returns:
        (token, channel_id) … 読めなければ (None, None)
    """
    try:
        from config import Config
        token      = getattr(Config, 'SLACK_BOT_TOKEN', None)
        channel_id = getattr(Config, 'ETHICS_SLACK_CHANNEL_ID', None)
        return token, channel_id
    except Exception as e:
        logging.error("slack_notifier: config 読み込み失敗: %s", e)
        return None, None


def _resolve_channel(purpose):
    """
    通知の宛先チャンネルIDを用途別に解決する。

    purpose:
        'business'  … 業務会議（事務局）向け。
                      ETHICS_SLACK_BUSINESS_CHANNEL_ID を優先。
        'committee' … 委員会向け。
                      ETHICS_SLACK_COMMITTEE_CHANNEL_ID を優先。
        その他      … 既定チャンネルのみ。

    いずれも、専用チャンネルIDが config に無ければ、既定の
    ETHICS_SLACK_CHANNEL_ID にフォールバックする。
    これにより、今は Slack が1チャンネルしかなくても全通知が
    既定チャンネルに届き、将来 config に専用キーを足すだけで
    業務会議用・委員会用へ自動的に分かれる。

    Returns:
        (token, channel_id)
    """
    token = None
    default_channel = None
    specific_channel = None
    try:
        from config import Config
        token           = getattr(Config, 'SLACK_BOT_TOKEN', None)
        default_channel = getattr(Config, 'ETHICS_SLACK_CHANNEL_ID', None)
        if purpose == 'business':
            specific_channel = getattr(
                Config, 'ETHICS_SLACK_BUSINESS_CHANNEL_ID', None)
        elif purpose == 'committee':
            specific_channel = getattr(
                Config, 'ETHICS_SLACK_COMMITTEE_CHANNEL_ID', None)
    except Exception as e:
        logging.error("slack_notifier: config 読み込み失敗: %s", e)
        return None, None
    return token, (specific_channel or default_channel)


def _send(text, log_label, purpose=None):
    """
    channel へ text を投稿する内部共通処理。
    log_label … 失敗ログに出す通知の種類名（例 '担当委員依頼通知'）。
    purpose   … 宛先チャンネルの用途。'business' / 'committee' / None。
                None なら既定チャンネル（従来どおり）。
    """
    if purpose:
        token, channel_id = _resolve_channel(purpose)
    else:
        token, channel_id = _load_slack_config()
    result = post_to_channel(token, channel_id, text)
    if not result.get('ok'):
        logging.warning("slack_notifier: %s の送信に失敗 — %s",
                         log_label, result.get('error'))
    return result


def notify_reviewers_assigned(case_display_id, title, case_url=None):
    """
    【2番】担当委員の指名があったことをチャンネルへ一括通知する。

    誰が指名されたかは明示しない（委員全員に意向確認を促す方式）。
    routes.py の api_assign_reviewers から、指名・commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}（呼び出し元はログ目的で見るだけ）
    """
    lines = [
        '<!channel> 担当審査委員の指名が行われました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
        '',
        '担当を依頼された委員は、できる限り速やかに案件ページから'
        '意向表明（応諾／辞退）をお願いします。',
        '※ ご自身が担当に含まれているかは案件ページでご確認ください。',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '担当委員依頼通知')


def notify_review_started(case_display_id, title, case_url=None):
    """
    【3番】審査が開始されたこと（全担当委員が応諾し reviewing に遷移）を
    チャンネルへ一括通知する。

    routes.py の api_reviewer_response から、reviewing への遷移が
    実際に起きたとき・commit 後にのみ呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 審査が開始されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '審査開始通知')


def notify_decision(case_display_id, title, decision_label,
                    case_url=None):
    """
    【7番】委員会としての判断（承認／不承認／条件付き承認）が
    宣言されたことをチャンネルへ一括通知する。

    decision_label … STATUS_LABELS による判断結果の表示名
                      （例 '承認' '不承認' '条件付き承認'）

    申請者本人への自動 DM は含まない（申請者 Slack ID 登録の
    仕組みが必要なため次段階）。当面は事務局がこの通知に気づき、
    申請者へ手動で連絡する運用を想定する。

    routes.py の api_decide から、commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 委員会としての判断が示されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
        f'■ 判断：{decision_label or "（不明）"}',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '判断通知')

def notify_alert_message(case_display_id, title, case_url=None):
    """
    【変更C】審査トラックに「アラート発言」が投稿されたことを
    チャンネルへ一括通知する。

    アラート発言とは、委員長・委員が「重要な発言があるので皆すぐ
    確認してほしい」という意図で投稿する発言。Slack には発言の
    中身は載せず、案件番号と「確認してください」の誘導だけを流す
    （アウェアネスのドアベルに徹する。中身は FUJIN-P 側で読む）。

    routes.py の api_post_review_message から、message_kind='alert'
    の投稿・commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 審査案件について重要な発言があります。ご確認ください。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
        '',
        '審査トラックに重要な発言が投稿されました。'
        '担当の方は案件ページでご確認ください。',
    ]
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), 'アラート発言通知')


def notify_applicant_message(case_display_id, title,
                             applicant_name=None, case_url=None):
    """
    申請者が申請者チャンネルにメッセージを発信したことを
    業務会議（事務局）チャンネルへ通知する。

    申請者からの連絡はいつ来るか予測できないため、新規案件提出と
    同様に業務会議へ知らせ、事務局が速やかに気づけるようにする。
    投稿者が申請者本人のときのみ呼ばれる（委員長・事務局・admin の
    投稿では呼ばない）。本文の中身は載せず、連絡があった事実と
    案件番号だけを流す（中身は FUJIN-P 側で読む）。

    routes.py の api_post_applicant_message から、commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 申請者から連絡がありました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('申請者チャンネルに新しいメッセージが投稿されました。'
                 '内容を案件ページでご確認ください。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '申請者連絡通知', purpose='business')


def notify_document_added(case_display_id, title,
                          applicant_name=None, case_url=None):
    """
    申請者が（申請後・審査中に）書類を追加したことを
    委員会チャンネルへ通知する。

    審査が始まっている案件への書類追加は、審査に関わる重要な事案
    なので委員会へ知らせる。新規申請の準備段階での初回アップロード
    では呼ばれない（routes.py 側で状態を判定して制御する）。
    書類の中身は載せず、追加があった事実と案件番号だけを流す。

    routes.py の api_add_document から、commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 申請者が書類を追加しました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('審査中の案件に申請書類が追加されました。'
                 '担当の方は案件ページでご確認ください。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '書類追加通知', purpose='committee')


def notify_review_resumed(case_display_id, title,
                          applicant_name=None, case_url=None):
    """
    申請者が「審査再開申請」を押し、審議が再開されたことを
    委員会チャンネルへ通知する。

    条件付き承認・審査継続を受けた回答書類の提出が完了したときに、
    routes.py の api_submit から commit 後に呼ぶ。

    Returns: {'ok': True/False, ...}
    """
    lines = [
        '<!channel> 申請者から回答書類が提出され、審議が再開されました。',
        '',
        f'■ 案件番号：{case_display_id or "（番号未付与）"}',
        f'■ 研究課題：{title or "（無題）"}',
    ]
    if applicant_name:
        lines.append(f'■ 申請者：{applicant_name}')
    lines.append('')
    lines.append('担当の方は案件ページで回答書類をご確認のうえ、'
                 '審議をお願いします。')
    if case_url:
        lines.append('')
        lines.append(f'案件ページ：{case_url}')
    return _send('\n'.join(lines), '審査再開通知', purpose='committee')

# ════════════════════════════════════════════════════════════════
# 追加分：個人宛 DM（メールアドレス照合方式）
#
#   FUJIN-P 側のメールアドレスから Slack ユーザーを引き当て、
#   その人と Bot の DM を開いてメッセージを送る。
#   名簿テーブルは持たない。FUJIN-P と Slack で同じメールアドレスを
#   使っている人にのみ届く（食い違う人には届かない＝後述の戻り値で判別）。
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

    例外は投げない。失敗は必ず戻り値で表現する。
    照合できない（Slack に同じメールのユーザーがいない）場合も
    {'ok': False} を返すだけで、呼び出し元は止めない。
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
        logging.warning("slack_notifier.lookup_slack_user_id_by_email: %s", err)
        return {'ok': False, 'error': msg}
    except Exception as e:
        logging.error("slack_notifier.lookup_slack_user_id_by_email "
                      "unexpected error: %s", e)
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
        logging.error("slack_notifier.post_dm_to_user error: %s "
                      "(user=%s)", err, slack_user_id)
        return {'ok': False, 'error': msg}
    except Exception as e:
        logging.error("slack_notifier.post_dm_to_user unexpected error: %s", e)
        return {'ok': False, 'error': f'DM送信に失敗しました: {e}'}


def send_dm_by_email(email, text, log_label='DM'):
    """
    メールアドレスから Slack ユーザーを引き当て、その人へ DM を送る。
    lookup_slack_user_id_by_email と post_dm_to_user をつないだ高レベル関数。

    routes.py からはこれ一本を呼べばよい。

    Args:
        email     : 送り先の人の（FUJIN-P 上の）メールアドレス
        text      : 本文
        log_label : 失敗ログに出す通知の種類名（例 '期限設定依頼DM'）

    Returns:
        {'ok': True/False, ...}

    例外は投げない。メール照合に失敗（食い違い）した場合も
    {'ok': False} を返すだけ。呼び出し元の本処理は止めない。
    """
    look = lookup_slack_user_id_by_email(email)
    if not look.get('ok'):
        logging.warning("slack_notifier: %s — メール照合に失敗 — %s",
                         log_label, look.get('error'))
        return {'ok': False, 'error': look.get('error')}

    result = post_dm_to_user(look['user_id'], text)
    if not result.get('ok'):
        logging.warning("slack_notifier: %s の送信に失敗 — %s",
                         log_label, result.get('error'))
    return result
