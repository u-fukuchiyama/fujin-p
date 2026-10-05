"""
ethics_review - ルート定義
人を対象とする研究倫理審査ワークフロー

組織構造（用語の整理）:
  委員長プール        : まいぐる '倫理審査_審査委員長' に所属する恒常的なメンバー
                       担当委員長・担当委員の指名・組み換えを主導する。
                       多くの場合、担当委員長として自分自身を指名する。
                       COIなどがあるときは別の委員を担当委員長に指名する。
  委員プール          : まいぐる '倫理審査_審査委員会' に所属する恒常的なメンバー
                       案件ごとに担当委員として指名される候補。
  担当委員長(chair)    : 案件ごとに1名指名され、その案件の審査を主導し、判断（承認等）を宣言する。
                       DB上は ethics_cases.chair_user_id で表現。
  担当委員(reviewer)  : 案件ごとに複数名指名され、審査に参加する（通常4名程度）。
                       DB上は ethics_reviewers テーブルで表現。
  事務局              : 任命・組み換えの代行・プロセス管理。委員長プールと協働。
  admin               : 実験時の参加観察を含む全権。

ステータス遷移:
  channel_open           : 申請者がチャンネルを立ち上げた直後
  documents_uploading    : 書類アップロード中（最大30PDF）
  submitted              : 申請ボタン押下後、担当委員長・担当委員の指名待ち
  reviewer_pending       : 指名された担当委員の応諾待ち
  reviewing              : 審査中（議論・質疑応答）
  conditional_approval   : 条件付き承認（申請者に通知後、再提出待ち）
  review_continuation    : 審査継続（申請者に通知後、再提出待ち。conditional_approval と
                           同じ流れだが、将来のプロセス分岐に備えて独立ステータスとする）
  approved               : 承認
  rejected               : 不承認
  not_applicable         : 非該当（規程第11条第2項(5)。審査を要しない旨の判定。
                           受理段階（submitted / reviewer_pending）では委員長プール、
                           審査段階（reviewing）では担当委員長が宣言する。承認／不承認と
                           同じく、申請者への通知は事務局の「申請者にも通知」で行う）
  withdrawn              : 取り下げ（申請者操作）
  terminated             : 打ち切り（事務局・admin操作、長期放置案件向け）

最終判定（承認／不承認／非該当）の開示:
  最終判定は事務局の「申請者にも通知」を経て初めて申請者に開示する。
  通知前は、申請者向け API と学内公開 API では status を表示専用の
  result_pending（審査結果通知待ち）に置き換え、所見・最終日・承認日も伏せる。
  委員会側（事務局・委員長・担当委員・admin）には常に実際の判定を見せる。

アクセス制御:
  倫理審査_事務局      : 全件閲覧・編集、打ち切り、担当委員長・担当委員の指名代行
  倫理審査_審査委員長  : 委員長プール。担当委員長・担当委員を指名（主導）
  倫理審査_審査委員会  : 委員プール。担当指名された案件のみ閲覧・参加（COI除く）
  申請者              : 自分の案件のみ、申請者チャンネル書き込み、取り下げ
  regular以上         : public閲覧のみ
"""
import datetime
import logging
import csv
import io
import os
import re
import uuid
from pytz import timezone
from auth import redirect_to_dashboard

from flask import (
    render_template, request, jsonify, session, Response, redirect, url_for,
    send_file, abort
)
from werkzeug.utils import secure_filename
import mysql.connector

from config import Config
from db import DatabaseConfig, Tables
from decorators import login_required
from ..user_groups.utils import user_is_in_group, get_group_member_ids

from . import ethics_review_bp

# ────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────

JST = timezone('Asia/Tokyo')

GROUP_SECRETARIAT = '人を対象とする研究倫理審査業務'
GROUP_COMMITTEE   = '人を対象とする研究倫理審査委員会'
GROUP_CHAIR       = '人を対象とする研究倫理審査委員会委員長'

# index 閲覧を許可するグループ（事務局・委員プール・委員長プール）
GROUPS_CAN_VIEW_INDEX = [
    GROUP_SECRETARIAT,
    GROUP_COMMITTEE,
    GROUP_CHAIR,
]

STATUS_LABELS = {
    'channel_open':         'チャンネル開設',
    'documents_uploading':  '回答準備中',
    'submitted':            '申請受付（担当委員指名待ち）',
    'reviewer_pending':     '担当委員応諾待ち',
    'reviewing':            '審査中',
    'conditional_approval': '条件付き承認（再提出待ち）',
    'review_continuation':  '審査継続（再提出待ち）',
    'approved':             '承認',
    'rejected':             '不承認',
    'not_applicable':       '非該当（審査不要）',
    'withdrawn':            '取り下げ',
    'terminated':           '打ち切り',
}

# 終局ステータス（変更不可）
TERMINAL_STATUSES = {'approved', 'rejected', 'not_applicable',
                     'withdrawn', 'terminated'}

# 委員会の最終判定（承認／不承認／非該当）。
# 申請者への通知は決定時ではなく、事務局の「申請者にも通知」で行う。
FINAL_DECISION_STATUSES = {'approved', 'rejected', 'not_applicable'}

# 最終判定が申請者に未通知の間、申請者・学内公開に見せる仮のステータス。
# DB には存在しない表示専用のキー（結果の内容は出さない）。
RESULT_PENDING_STATUS = 'result_pending'
RESULT_PENDING_LABEL  = '審査結果通知待ち'


# 最終判定が申請者へ未通知（＝学内の決裁待ち）の間、委員会側に見せるラベル。
# 委員会内では実際の判定を見せたうえで、決裁がまだであることを明示する。
PENDING_SETTLEMENT_LABELS = {
    'approved':       '承認（決裁待ち）',
    'rejected':       '不承認（決裁待ち）',
    'not_applicable': '非該当（決裁待ち）',
}

# 非該当を宣言できるステータス
#   受理段階: submitted / reviewer_pending（委員長プールが書類を見て判断）
#   審査段階: reviewing（担当委員長の判断のひとつ）
NA_DECLARABLE_STATUSES = ('submitted', 'reviewer_pending', 'reviewing')

# public ビューで「申請受付中」と見せるステータス
ACTIVE_STATUSES = {
    'channel_open', 'documents_uploading',
    'submitted', 'reviewer_pending',
    'reviewing', 'conditional_approval',
    'review_continuation',
}

MAX_DOCUMENTS = 30                 # 1案件あたりの最大書類数　（206 06 17 事務からの要請により10→30）
MAX_FILE_SIZE = 20 * 1024 * 1024   # 1ファイル20MB

# ファイル保管ルート
#   倫理審査の申請書類は非公開データなので、アプリ自身の static/ に置く。
#   ~/static/ は nginx が認証なしで直接配信するため使わない（旧: ~/static/ethics_docs/）。
#   配信は api_download_document（@login_required ＋ 閲覧権限判定）が担う。
#   どのアカウント（nishida / fujinpshowcase / nishida4fujinp）でも
#   アプリの位置から相対に決まるので、環境ごとの分岐は不要。
#     → /home/<owner>/fujinp/ethics_review/static/ethics_docs/
ETHICS_DOCS_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'static', 'ethics_docs'
)


# 申請様式の配布ルート
#   審査で提出された書類（ETHICS_DOCS_ROOT）とは別のディレクトリ。
#   こちらは配布物なので data_for_distribution/ に置く
#   （アプシャのエクスポートパッケージに同梱され、移設先へも一緒に運ばれる）。
#     → /home/<owner>/fujinp/ethics_review/data_for_distribution/forms/
ETHICS_FORMS_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'data_for_distribution', 'forms'
)

# 配布する様式の一覧。並びはそのまま画面の並びになる。
#   code     : URL に出る識別子（英数字とアンダースコアのみ）
#   filename : ETHICS_FORMS_ROOT 配下の実ファイル名
#   download : ダウンロード時にブラウザへ渡すファイル名（日本語可）
#   label    : 様式番号などの見出し
#   title    : 書類の名称
#   desc     : いつ使うかの短い説明
# 様式を足すときは、ファイルを置いてこのリストに1件足すだけでよい。
ETHICS_FORMS = [
    {
        'code':     'checksheet',
        'filename': 'form_00_checksheet.docx',
        'download': '【人を対象とする研究倫理審査申請に関する自己判断チェックシート】.docx',
        'label':    'チェックシート',
        'title':    '人を対象とする研究倫理審査申請に関する自己判断チェックシート',
        'desc':     'まずはこれから。倫理審査の申請が必要な研究かどうかを、'
                    'ご自身で確認するための書類です。'
                    '審査を要しないと判断される場合も、この結果を添えて申請してください。',
    },
    {
        'code':     'application',
        'filename': 'form_01_application.docx',
        'download': '様式第1号（第9条、第14条関係）人を対象とする研究倫理審査申請書.docx',
        'label':    '様式第1号（第9条、第14条関係）',
        'title':    '人を対象とする研究倫理審査申請書',
        'desc':     '審査を申請するときの表紙にあたる書類です。'
                    '研究課題・研究者・研究期間などを記載します。',
    },
    {
        'code':     'research_plan',
        'filename': 'form_02_research_plan.docx',
        'download': '様式第2号（第9条、第14条関係）研究計画書.docx',
        'label':    '様式第2号（第9条、第14条関係）',
        'title':    '研究計画書',
        'desc':     '研究の目的・方法・対象者・個人情報の取り扱いなどを記載します。'
                    '審査の中心となる書類です。',
    },
    {
        'code':     'consent_set',
        'filename': 'form_02b_consent_set.docx',
        'download': '別紙（研究内容説明書・同意書・同意撤回書）.docx',
        'label':    '別紙',
        'title':    '研究内容説明書・同意書・同意撤回書',
        'desc':     '研究対象者に説明し、同意を得るための3点セットです。'
                    '研究の内容に合わせて書き換えて使います。',
    },
    {
        'code':     'objection',
        'filename': 'form_05_objection.docx',
        'download': '様式第5号（第13条関係）人を対象とする研究倫理審査結果異議申立書.docx',
        'label':    '様式第5号（第13条関係）',
        'title':    '人を対象とする研究倫理審査結果異議申立書',
        'desc':     '審査結果に異議を申し立てるときに使います。',
    },
    {
        'code':     'completion_report',
        'filename': 'form_06_completion_report.docx',
        'download': '様式第6号（第16条関係）研究終了報告書.docx',
        'label':    '様式第6号（第16条関係）',
        'title':    '研究終了報告書',
        'desc':     '承認を受けた研究が終了したときに提出します。',
    },
    {
        'code':     'change_application',
        'filename': 'form_07_change_application.docx',
        'download': '様式第7号（第14条関係）人を対象とする研究倫理審査変更申請書.docx',
        'label':    '様式第7号（第14条関係）',
        'title':    '人を対象とする研究倫理審査 変更申請書',
        'desc':     '承認を受けた研究の計画を変更するときに使います。',
    },
]

# 関連規程（学外の例規集へのリンク）
ETHICS_REGULATION_URL = (
    'https://www1.g-reiki.net/UnivFukuchiyama/reiki_honbun/u421RG00000158.html'
)
ETHICS_REGULATION_TITLE = (
    '福知山公立大学における人を対象とする研究倫理審査委員会規程'
)


# ────────────────────────────────────────────
# ユーティリティ
# ────────────────────────────────────────────

def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)


def get_jst_today():
    return get_jst_now().date()


def get_current_fiscal_year():
    now = get_jst_now()
    return now.year if now.month >= 4 else now.year - 1


def fmt_date(d):
    if not d:
        return ''
    if isinstance(d, datetime.datetime):
        d = d.date()
    return d.isoformat()


def fmt_datetime(dt):
    if not dt:
        return ''
    return dt.strftime('%Y-%m-%d %H:%M')


def format_case_id(fiscal_year, seq_no):
    """E-2026-001 形式"""
    return f"E-{fiscal_year}-{seq_no:03d}"


# ────────────────────────────────────────────
# ファイル保管ユーティリティ
# ────────────────────────────────────────────

def _safe_filename(original_name):
    """元のファイル名から安全なファイル名を生成する。
       日本語ファイル名は secure_filename で空になりやすいため、
       拡張子を保持しつつ UUID で衝突回避する命名を採る。
    """
    # 拡張子を抽出（ピリオドを含む。なければ空文字）
    base, ext = os.path.splitext(original_name or '')
    ext = ext.lower().strip()
    # 拡張子側も安全側に絞る（英数とドットのみ、長さ16まで）
    ext = re.sub(r'[^A-Za-z0-9.]', '', ext)[:16]
    # UUID4 ベースの安全な名前
    return f"{uuid.uuid4().hex}{ext}"


def _case_doc_dir(case_id, version):
    """案件・版ごとのファイル保管ディレクトリパスを返す"""
    return os.path.join(ETHICS_DOCS_ROOT,
                        f"case_{case_id}", f"v{int(version)}")


def _ensure_case_doc_dir(case_id, version):
    """案件・版ディレクトリを作成（無ければ）"""
    d = _case_doc_dir(case_id, version)
    os.makedirs(d, exist_ok=True)
    return d


def _doc_absolute_path(relative_path):
    """DBに格納された相対パス（ETHICS_DOCS_ROOTからの相対）を絶対パスに展開。
       パストラバーサルを防ぐため、最終的に ETHICS_DOCS_ROOT 配下であることを確認。
    """
    if not relative_path:
        return None
    # 相対パス内のディレクトリトラバーサル防御
    safe = os.path.normpath(relative_path).lstrip(os.sep).lstrip('/')
    if '..' in safe.split(os.sep):
        return None
    abs_path = os.path.join(ETHICS_DOCS_ROOT, safe)
    # 最終チェック：ETHICS_DOCS_ROOT の中にあること
    root_real = os.path.realpath(ETHICS_DOCS_ROOT)
    target_real = os.path.realpath(abs_path)
    if not target_real.startswith(root_real + os.sep) and target_real != root_real:
        return None
    return abs_path


# ────────────────────────────────────────────
# 添付ファイル保存の共通ヘルパー（変更：メッセージ添付対応）
# ────────────────────────────────────────────

def _store_document_file(conn, cursor, case_id, version, up_file,
                         title, note, user_id, source='application'):
    """アップロードファイルを ETHICS_DOCS_ROOT 配下に保存し、
       ethics_documents に1行 INSERT してその document_id を返す。

       申請書類の追加（source='application'）とメッセージ添付
       （source='attachment'）の両方から呼ばれる共通処理。
       添付＝書類置き場に1本化する方針のため、メッセージ添付も
       必ずこの関数を通して ethics_documents に登録される。

       例外（OSError / DBエラー）は呼び出し側に送出する。
       呼び出し側でトランザクション境界（commit / rollback）を管理すること。

       戻り値: dict(document_id, original_filename, file_size, abs_path)
    """
    original_filename = up_file.filename

    # サイズ確認
    up_file.stream.seek(0, os.SEEK_END)
    file_size = up_file.stream.tell()
    up_file.stream.seek(0)
    if file_size <= 0:
        raise ValueError('ファイルが空です')
    if file_size > MAX_FILE_SIZE:
        raise ValueError(
            f'ファイルサイズは {MAX_FILE_SIZE // (1024*1024)}MB 以下にしてください')

    # 保存先ディレクトリ
    save_dir  = _ensure_case_doc_dir(case_id, version)
    safe_name = _safe_filename(original_filename)
    abs_path  = os.path.join(save_dir, safe_name)
    while os.path.exists(abs_path):
        safe_name = _safe_filename(original_filename)
        abs_path  = os.path.join(save_dir, safe_name)

    up_file.save(abs_path)

    rel_path = os.path.relpath(abs_path, ETHICS_DOCS_ROOT)

    # ── DB 登録。source カラムが無い旧スキーマでもフォールバックで動く。
    try:
        cursor.execute(
            "INSERT INTO ethics_documents "
            "(case_id, version, source, title, drive_url, note, "
            " original_filename, file_size, uploaded_by_user_id, uploaded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (case_id, version, source, title, rel_path, note,
             original_filename, file_size, user_id, get_jst_now()))
    except mysql.connector.Error:
        # source / original_filename / file_size が無い旧スキーマ
        combined_note = note or ''
        if original_filename:
            fn_note = f"[ファイル名: {original_filename}]"
            combined_note = f"{fn_note} {combined_note}".strip()
        cursor.execute(
            "INSERT INTO ethics_documents "
            "(case_id, version, title, drive_url, note, uploaded_by_user_id, uploaded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (case_id, version, title, rel_path, combined_note, user_id, get_jst_now()))

    document_id = cursor.lastrowid
    return {
        'document_id':       document_id,
        'original_filename': original_filename,
        'file_size':         file_size,
        'abs_path':          abs_path,
    }


REVIEW_TRACK_ATTACHMENT_NOTE = '審査トラック添付'


def _is_review_track_document(row):
    """ethics_documents の行が審査トラックのメッセージ添付か。
       審査トラック添付は委員会側だけの資料なので、申請者や
       担当外の委員（申請内容の閲覧）には見せない。
       note カラムで判定する（source='attachment' は申請者チャンネル添付と
       共通で、かつ旧スキーマでは source カラムが無いため）。
    """
    return REVIEW_TRACK_ATTACHMENT_NOTE in (row.get('note') or '')


def _fetch_document_brief(cursor, case_id, document_id):
    """メッセージ表示用に、添付書類の最小情報を取得して dict で返す。
       見つからなければ None。download_url も付与する。
    """
    if not document_id:
        return None
    try:
        cursor.execute(
            "SELECT id, title, drive_url, note, original_filename "
            "FROM ethics_documents WHERE id = %s AND case_id = %s",
            (document_id, case_id))
        r = cursor.fetchone()
    except mysql.connector.Error:
        cursor.execute(
            "SELECT id, title, drive_url, note "
            "FROM ethics_documents WHERE id = %s AND case_id = %s",
            (document_id, case_id))
        r = cursor.fetchone()
    if not r:
        return None
    display_filename = r.get('original_filename')
    if not display_filename and r.get('note'):
        m = re.match(r'^\[ファイル名:\s*([^\]]+)\]', r['note'] or '')
        if m:
            display_filename = m.group(1).strip()
    download_url = None
    if r.get('drive_url'):
        download_url = url_for('ethics_review.api_download_document',
                               case_id=case_id, doc_id=r['id'])
    return {
        'document_id':       r['id'],
        'title':             r['title'],
        'original_filename': display_filename or '',
        'download_url':      download_url,
    }


def get_user_category(user_id):
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT category FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return row['category'] if row else None
    except Exception as e:
        logging.error("get_user_category error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_user_display_name(user_id):
    """ユーザー名（full_name）を取得"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT full_name FROM users WHERE id = %s",
            (user_id,))
        row = cursor.fetchone()
        if not row:
            return f"User#{user_id}"
        return row.get('full_name') or f"User#{user_id}"
    except Exception as e:
        logging.error("get_user_display_name error: %s", e)
        return f"User#{user_id}"
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_user_email(user_id):
    """ユーザーのメールアドレスを取得。
       見つからない／エラー時は None を返す（呼び出し側で None 判定する）。
       Slack 個人宛 DM（メール照合方式）の宛先解決に使う。
    """
    if not user_id:
        return None
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT email FROM users WHERE id = %s",
            (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return row.get('email') or None
    except Exception as e:
        logging.error("get_user_email error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# 権限判定
# ────────────────────────────────────────────

def is_admin(user_id):
    return get_user_category(user_id) == 'admin'


def is_secretariat(user_id):
    if is_admin(user_id):
        return True
    return user_is_in_group(user_id, GROUP_SECRETARIAT)


def is_chair_pool(user_id):
    """委員長プールに属しているか"""
    if is_admin(user_id):
        return True
    return user_is_in_group(user_id, GROUP_CHAIR)


def is_committee_pool(user_id):
    """委員プールに属しているか（委員長プール所属者も含む）"""
    if is_admin(user_id):
        return True
    if user_is_in_group(user_id, GROUP_COMMITTEE):
        return True
    if user_is_in_group(user_id, GROUP_CHAIR):
        return True
    return False


def can_be_case_chair(user_id):
    """担当委員長として指名できる資格があるか。
       通常は委員長プールから担当委員長を指名するが、委員長プールが
       担当委員長になれない場合（多くは委員長プール1名運用でCOI等）に
       備え、委員プールのメンバーも担当委員長に指名できる。
       → 委員長プール ∪ 委員プール のいずれかに属していればよい。
    """
    return is_chair_pool(user_id) or is_committee_pool(user_id)


def can_view_index(user_id):
    """index/Stats を閲覧できるか"""
    if is_admin(user_id):
        return True
    for g in GROUPS_CAN_VIEW_INDEX:
        if user_is_in_group(user_id, g):
            return True
    return False


# ────────────────────────────────────────────
# COI 判定
# ────────────────────────────────────────────

def is_coi(user_id, case_id):
    """この案件において審査参加が不可（COI: committee）か。
       申請者本人は ethics_coi の登録有無にかかわらず常に不可（暗黙のCOI）。
       委員長プール・委員プールの所属者が自分の案件を申請したとき、
       chair_user_id や指名レコードに本人が残っていても審査側に入れない。
    """
    if is_applicant(user_id, case_id):
        return True
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT excluded_roles FROM ethics_coi "
            "WHERE case_id = %s AND user_id = %s",
            (case_id, user_id))
        row = cursor.fetchone()
        if not row:
            return False
        return row['excluded_roles'] == 'committee'
    except Exception as e:
        logging.error("is_coi error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def is_chair_excluded(user_id, case_id):
    """この案件において担当委員長としての関与が不可か。
       申請者本人は常に不可（暗黙のCOI）。
    """
    if is_applicant(user_id, case_id):
        return True
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT excluded_roles FROM ethics_coi "
            "WHERE case_id = %s AND user_id = %s",
            (case_id, user_id))
        row = cursor.fetchone()
        if not row:
            return False
        return row['excluded_roles'] in ('committee', 'chair_only')
    except Exception as e:
        logging.error("is_chair_excluded error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_coi_user_ids(case_id):
    """COI(committee) 設定されたユーザーIDの一覧"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT user_id FROM ethics_coi "
            "WHERE case_id = %s AND excluded_roles = 'committee'",
            (case_id,))
        return [r['user_id'] for r in cursor.fetchall()]
    except Exception as e:
        logging.error("get_coi_user_ids error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_chair_excluded_user_ids(case_id):
    """担当委員長として指名不可なユーザーIDの一覧。
       committee（審査参加不可）と chair_only（委員長就任のみ不可）の両方を含む。
    """
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT user_id FROM ethics_coi "
            "WHERE case_id = %s AND excluded_roles IN ('committee','chair_only')",
            (case_id,))
        return [r['user_id'] for r in cursor.fetchall()]
    except Exception as e:
        logging.error("get_chair_excluded_user_ids error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# 案件レベルの権限判定
# ────────────────────────────────────────────

def get_case(case_id):
    """案件情報を取得"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM ethics_cases WHERE id = %s", (case_id,))
        return cursor.fetchone()
    except Exception as e:
        logging.error("get_case error: %s", e)
        return None
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def get_assigned_reviewer_ids(case_id, accepted_only=False):
    """指名された担当委員のIDリスト"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        if accepted_only:
            cursor.execute(
                "SELECT user_id FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status = 'accepted'",
                (case_id,))
        else:
            cursor.execute(
                "SELECT user_id FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status IN ('pending', 'accepted')",
                (case_id,))
        return [r['user_id'] for r in cursor.fetchall()]
    except Exception as e:
        logging.error("get_assigned_reviewer_ids error: %s", e)
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def is_assigned_reviewer(user_id, case_id):
    """この案件の担当委員として指名されているか（pending含む）"""
    return user_id in get_assigned_reviewer_ids(case_id)


def is_case_chair(user_id, case_id):
    """この案件の現任の担当委員長か"""
    case = get_case(case_id)
    if not case:
        return False
    return case.get('chair_user_id') == user_id


def is_applicant(user_id, case_id):
    """この案件の申請者か"""
    case = get_case(case_id)
    if not case:
        return False
    return case.get('applicant_user_id') == user_id


def can_access_review_track(user_id, case_id):
    """審査トラック（書き込み可）にアクセスできるか"""
    if is_admin(user_id):
        return True
    if is_secretariat(user_id):
        return True
    if is_case_chair(user_id, case_id) and not is_chair_excluded(user_id, case_id):
        return True
    if is_assigned_reviewer(user_id, case_id) and not is_coi(user_id, case_id):
        return True
    return False


def can_view_application_content(user_id, case_id):
    """申請内容（案件基本情報＋提出書類）を閲覧できるか。

    変更A：委員プール・委員長プールに属する委員は、担当でなくても
    全件・全状態の申請内容を閲覧できる（守秘の上）。
    ただしこの判定が許すのは「申請内容の閲覧」のみであり、
    審査トラックの議論・申請者チャンネル・担当委員一覧・COI・
    ステータス履歴は含まない（それらは can_access_review_track 等で別途制御）。

    審査トラックにアクセスできる者（事務局・admin・担当委員長・担当委員）は
    当然これも可。加えて委員プール／委員長プール所属者にも開放する。
    """
    if can_access_review_track(user_id, case_id):
        return True
    if is_committee_pool(user_id):   # 委員プール・委員長プール（admin含む）
        return True
    return False


def can_write_applicant_channel(user_id, case_id):
    """申請者チャンネルに書き込めるか"""
    if is_admin(user_id):
        return True
    if is_secretariat(user_id):
        return True
    if is_case_chair(user_id, case_id) and not is_chair_excluded(user_id, case_id):
        return True
    if is_applicant(user_id, case_id):
        return True
    return False


def can_read_applicant_channel(user_id, case_id):
    """申請者チャンネルを閲覧できるか（read-onlyを含む）"""
    if can_write_applicant_channel(user_id, case_id):
        return True
    # 担当委員は read-only
    if is_assigned_reviewer(user_id, case_id) and not is_coi(user_id, case_id):
        return True
    return False


def can_decide(user_id, case_id):
    """担当委員長としての判断（承認/不承認/条件付き）が可能か"""
    if is_admin(user_id):
        return True
    if is_case_chair(user_id, case_id) and not is_chair_excluded(user_id, case_id):
        return True
    return False


def can_declare_not_applicable(user_id, case_id):
    """非該当（審査不要）の宣言が可能か。
       - 審査段階（reviewing）: 担当委員長（can_decide と同じ）
       - 受理段階（submitted / reviewer_pending）: 担当委員長に加え、
         委員長プール所属者も可（担当委員長未指名のまま書類を見て
         「審査を要しない」と判断する運用のため）。COI除外者は不可。
       事務局（admin以外）は判定者ではないので不可。
    """
    if can_decide(user_id, case_id):
        return True
    case = get_case(case_id)
    if not case:
        return False
    if case['status'] in ('submitted', 'reviewer_pending') \
            and is_chair_pool(user_id) \
            and not is_chair_excluded(user_id, case_id):
        return True
    return False


def is_result_disclosed(case):
    """最終判定（承認／不承認／非該当）が申請者に開示済みか。
       最終判定以外のステータスは常に True（伏せる対象がない）。
    """
    if case.get('status') not in FINAL_DECISION_STATUSES:
        return True
    return bool(case.get('applicant_notified_at'))



def mask_undisclosed_result(item, case):
    """申請者・学内公開向けの辞書 item から、未通知の最終判定を伏せる。
       status は result_pending に置き換え、所見・最終日・承認日・判断日時は空にする。
       item は jsonify 直前の辞書。存在するキーだけ書き換える。
    """
    if is_result_disclosed(case):
        return item
    item['status'] = RESULT_PENDING_STATUS
    if 'status_label' in item:
        item['status_label'] = RESULT_PENDING_LABEL
    for k in ('decision_note',):
        if k in item: item[k] = ''
    for k in ('final_date', 'approved_date', 'decision_at'):
        if k in item: item[k] = None
    return item


def committee_status_label(case):
    """委員会側（事務局・委員長・担当委員・admin）に見せるステータス名。
       最終判定が申請者へ未通知の間は「（決裁待ち）」を付す。
       case は ethics_cases の行、または status と applicant_notified_at を持つ辞書。
    """
    s = case.get('status')
    if s in FINAL_DECISION_STATUSES and not case.get('applicant_notified_at'):
        return PENDING_SETTLEMENT_LABELS.get(s, STATUS_LABELS.get(s, s))
    return STATUS_LABELS.get(s, s)



def is_acting_chair_anywhere(user_id):
    """未終局案件の担当委員長を務めているか（案件を問わない）。
       委員長プール外の委員が担当委員長に指名された場合（代理委員長）を
       体制運営パネルに通すための判定。委員長プール所属者は別途通るので、
       ここでは案件の chair_user_id だけを見る。
    """
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        placeholders = ','.join(['%s'] * len(TERMINAL_STATUSES))
        cursor.execute(
            "SELECT 1 FROM ethics_cases "
            f"WHERE chair_user_id = %s AND applicant_user_id <> %s "
            f"  AND status NOT IN ({placeholders}) "
            "LIMIT 1",
            (user_id, user_id, *sorted(TERMINAL_STATUSES)))
        return cursor.fetchone() is not None
    except Exception as e:
        logging.error("is_acting_chair_anywhere error: %s", e)
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def can_assign_reviewers(user_id, case_id):
    """担当委員長・担当委員の指名・組み換えが可能か。
       委員長主導モデル：
         - 委員長プール所属者：本来の任命権者
         - 当該案件の担当委員長：委員長プール外から指名された代理委員長も
           自分の案件については担当委員を指名できる（COI除外者は不可）
         - 事務局・admin：プロセス管理として代行・補助
       同じ画面（体制運営パネル）を共有して、状況に応じて協働する。
       case_id が None のときは案件を特定しない一般判定（パネル閲覧用）。
    """
    if is_secretariat(user_id):  # admin を含む
        return True
    if case_id is not None and is_applicant(user_id, case_id):
        # 申請者本人は委員長プール所属でも自分の案件の体制には触れない
        return False
    if is_chair_pool(user_id):
        return True
    if case_id is not None \
            and is_case_chair(user_id, case_id) \
            and not is_chair_excluded(user_id, case_id):
        return True
    return False


def can_view_secretariat_panel(user_id):
    """体制運営パネルの閲覧権限。
       任命を担う３者（委員長プール／事務局／admin）に加え、
       未終局案件の担当委員長を務めている代理委員長。
    """
    if can_assign_reviewers(user_id, None):
        return True
    return is_acting_chair_anywhere(user_id)


# ────────────────────────────────────────────
# 採番
# ────────────────────────────────────────────

def next_seq_no(fiscal_year):
    """その年度の次の連番を取得"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT MAX(seq_no) AS mx FROM ethics_cases WHERE fiscal_year = %s",
            (fiscal_year,))
        row = cursor.fetchone()
        return (row['mx'] or 0) + 1
    except Exception as e:
        logging.error("next_seq_no error: %s", e)
        return 1
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# ステータス遷移ヘルパー
# ────────────────────────────────────────────

def record_status_change(case_id, from_status, to_status, user_id, note=''):
    """ステータス遷移を履歴に記録（呼び出し側で connection 管理する場合用）"""
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ethics_status_history "
            "(case_id, from_status, to_status, changed_by_user_id, note, changed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (case_id, from_status, to_status, user_id, note, get_jst_now()))
        conn.commit()
    except Exception as e:
        logging.error("record_status_change error: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


def transition_status(conn, cursor, case_id, new_status, user_id, note='',
                       set_final_date=False, set_received_date=False,
                       set_approved_date=False):
    """ステータス遷移を実行（接続済みのcursorを使用）"""
    cursor.execute(
        "SELECT status FROM ethics_cases WHERE id = %s", (case_id,))
    row = cursor.fetchone()
    if not row:
        return False
    from_status = row['status'] if isinstance(row, dict) else row[0]

    updates = ['status = %s']
    params  = [new_status]

    today = get_jst_today()
    if set_received_date:
        updates.append('received_date = %s')
        params.append(today)
    if set_final_date:
        updates.append('final_date = %s')
        params.append(today)
    if set_approved_date:
        updates.append('approved_date = %s')
        params.append(today)

    # updated_at は MySQL の ON UPDATE CURRENT_TIMESTAMP に頼らず、
    # アプリ側で JST を明示セットする（MySQL が UTC 運用のため）
    updates.append('updated_at = %s')
    params.append(get_jst_now())

    params.append(case_id)
    cursor.execute(
        f"UPDATE ethics_cases SET {', '.join(updates)} WHERE id = %s",
        params)

    cursor.execute(
        "INSERT INTO ethics_status_history "
        "(case_id, from_status, to_status, changed_by_user_id, note, changed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (case_id, from_status, new_status, user_id, note, get_jst_now()))
    return True


# ────────────────────────────────────────────
# 審査体制ヘルパー（節目メモ・組み換え管理）
# ────────────────────────────────────────────

def get_committee_change_count(cursor, case_id):
    """この案件で過去に投稿された審査体制メモの回数を返す
       0 = 初回未投稿、1 = 初回済、2 = 組み換え1回目済、… を判定するために使う
    """
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM ethics_review_messages "
        "WHERE case_id = %s AND body LIKE %s",
        (case_id, '【審査体制】%'))
    row = cursor.fetchone()
    if not row:
        return 0
    return row['cnt'] if isinstance(row, dict) else row[0]


def compose_committee_memo(label, chair_name, reviewer_names):
    """節目メモ本文を生成
       label: '初回' / '組み換え1回目' など
       returns: '【審査体制】(初回)：担当委員長 A、担当委員 B,C,D'
    """
    rv_part = '、'.join(reviewer_names) if reviewer_names else '（未指名）'
    chair_part = chair_name or '（未指名）'
    return f"【審査体制】（{label}）：担当委員長 {chair_part}、担当委員 {rv_part}"


def post_committee_memo(cursor, case_id, user_id, is_initial=False, chair_name=None,
                        reviewer_names=None):
    """審査体制メモを審査トラックに自動投稿
       is_initial=True なら「初回」、それ以外は「組み換えN回目」と命名
       投稿前の体制メモ件数で初回判定する：
         - 既存件数 0 かつ is_initial=True → 「初回」
         - 既存件数 N (>=1) → 「組み換え(N)回目」
    """
    existing_cnt = get_committee_change_count(cursor, case_id)
    if existing_cnt == 0 and is_initial:
        label = '初回'
    else:
        # 初回が既に投稿済み or is_initial=False
        # 既存件数を基準に N回目を計算（初回=1件目、組み換え1回目=2件目、…）
        if existing_cnt == 0:
            # まだ初回も投稿されていないが is_initial=False のケース
            # （想定外だが安全側で「初回」扱い）
            label = '初回'
        else:
            label = f'組み換え{existing_cnt}回目'

    body = compose_committee_memo(label, chair_name, reviewer_names or [])
    cursor.execute(
        "INSERT INTO ethics_review_messages "
        "(case_id, user_id, body, created_at) VALUES (%s, %s, %s, %s)",
        (case_id, user_id, body, get_jst_now()))


def get_current_committee_names(cursor, case_id):
    """現在の審査体制（担当委員長氏名・accepted/pending な担当委員氏名リスト）を返す"""
    # 担当委員長
    cursor.execute(
        "SELECT c.chair_user_id, u.full_name AS chair_name "
        "FROM ethics_cases c "
        "LEFT JOIN users u ON u.id = c.chair_user_id "
        "WHERE c.id = %s", (case_id,))
    row = cursor.fetchone()
    chair_name = None
    if row:
        chair_name = row['chair_name'] if isinstance(row, dict) else row[1]

    # 担当委員（assignment_status が pending または accepted のもの）
    cursor.execute(
        "SELECT u.full_name AS rv_name "
        "FROM ethics_reviewers r "
        "LEFT JOIN users u ON u.id = r.user_id "
        "WHERE r.case_id = %s "
        "  AND r.assignment_status IN ('pending','accepted') "
        "ORDER BY r.assigned_at ASC",
        (case_id,))
    reviewer_names = []
    for r in cursor.fetchall():
        nm = r['rv_name'] if isinstance(r, dict) else r[0]
        if nm:
            reviewer_names.append(nm)
    return chair_name, reviewer_names
# ────────────────────────────────────────────
# ページ表示ルート
# ────────────────────────────────────────────

@ethics_review_bp.route('/')
@login_required
def index():
    """ポータル：全FUJIN-Pユーザに開放、ロールに応じてカードを段階表示"""
    user_id = session.get('user_id')
    return render_template(
        'ethics_review/index.html',
        current_fy=get_current_fiscal_year(),
        status_labels=STATUS_LABELS,
        is_admin=is_admin(user_id),
        is_secretariat=is_secretariat(user_id),
        is_chair_pool=is_chair_pool(user_id),
        is_committee_pool=is_committee_pool(user_id),
        is_acting_chair=is_acting_chair_anywhere(user_id),
    )


@ethics_review_bp.route('/my')
@login_required
def my_cases():
    """申請者の自分の案件一覧"""
    return render_template(
        'ethics_review/my.html',
        current_fy=get_current_fiscal_year(),
        status_labels=STATUS_LABELS,
        result_pending_status=RESULT_PENDING_STATUS,
        result_pending_label=RESULT_PENDING_LABEL,
    )


@ethics_review_bp.route('/new')
@login_required
def new_case():
    """新規申請：チャンネル立ち上げ＋書類登録＋申請"""
    return render_template(
        'ethics_review/new.html',
        max_documents=MAX_DOCUMENTS,
    )


@ethics_review_bp.route('/case/<int:case_id>')
@login_required
def case_detail(case_id):
    """案件詳細：審査トラック中心の画面（担当委員長・担当委員・委員長・事務局向け）"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return '<h2 style="font-family:sans-serif;padding:40px;color:#6b7280">案件が見つかりません</h2><p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査トップへ戻る</a></p>', 404
    if not can_access_review_track(user_id, case_id):
        return '<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">権限がありません</h2><p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査トップへ戻る</a></p>', 403

    return render_template(
        'ethics_review/detail.html',
        case_id=case_id,
        status_labels=STATUS_LABELS,
        terminal_statuses=list(TERMINAL_STATUSES),
        is_admin=is_admin(user_id),
        is_secretariat=is_secretariat(user_id),
        can_decide=can_decide(user_id, case_id),
        can_assign=can_assign_reviewers(user_id, case_id),
    )


@ethics_review_bp.route('/case/<int:case_id>/view')
@login_required
def case_view(case_id):
    """申請内容の閲覧ページ（変更A）。

    担当でない委員（委員プール・委員長プール）も含め、申請内容を
    守秘の上で閲覧できる。表示するのは案件基本情報と提出書類のみ。
    審査トラックの議論・申請者チャンネル・担当委員一覧・COI・
    ステータス履歴は表示しない。

    担当委員長・担当委員・事務局・admin がこのページを開いた場合も
    閲覧できる（その人たちは case_detail も使えるが、view も拒まない）。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return '<h2 style="font-family:sans-serif;padding:40px;color:#6b7280">案件が見つかりません</h2><p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査トップへ戻る</a></p>', 404
    if not can_view_application_content(user_id, case_id):
        return '<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">権限がありません</h2><p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査トップへ戻る</a></p>', 403

    return render_template(
        'ethics_review/case_view.html',
        case_id=case_id,
        status_labels=STATUS_LABELS,
    )


@ethics_review_bp.route('/case/<int:case_id>/applicant')
@login_required
def case_applicant_channel(case_id):
    """申請者チャンネル画面"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return '<h2 style="font-family:sans-serif;padding:40px;color:#6b7280">案件が見つかりません</h2><p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査トップへ戻る</a></p>', 404
    if not can_read_applicant_channel(user_id, case_id):
        return '<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">権限がありません</h2><p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査トップへ戻る</a></p>', 403

    read_only = not can_write_applicant_channel(user_id, case_id)

    return render_template(
        'ethics_review/applicant_channel.html',
        case_id=case_id,
        read_only=read_only,
        is_applicant=is_applicant(user_id, case_id),
        status_labels=STATUS_LABELS,
        result_pending_status=RESULT_PENDING_STATUS,
        result_pending_label=RESULT_PENDING_LABEL,
    )


@ethics_review_bp.route('/public')
@login_required
def public_view():
    """学内一般公開：最小限情報のみ"""
    return render_template(
        'ethics_review/public.html',
        current_fy=get_current_fiscal_year(),
        status_labels=STATUS_LABELS,
        result_pending_status=RESULT_PENDING_STATUS,
        result_pending_label=RESULT_PENDING_LABEL,
    )


@ethics_review_bp.route('/public/approved')
@login_required
def public_approved():
    """承認済み案件の公開（タイトル・申請者・承認日）"""
    return render_template(
        'ethics_review/public_approved.html',
        current_fy=get_current_fiscal_year(),
    )


@ethics_review_bp.route('/report')
@login_required
def report():
    """年度報告（事務局向け）"""
    user_id = session.get('user_id')
    if not is_secretariat(user_id):
        return '<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">権限がありません</h2><p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査トップへ戻る</a></p>', 403
    return render_template(
        'ethics_review/report.html',
        current_fy=get_current_fiscal_year(),
        status_labels=STATUS_LABELS,
    )


@ethics_review_bp.route('/secretariat')
@login_required
def secretariat_panel():
    """事務局拡張パネル：申請一覧、恒常メンバー、担当委員長・担当委員の指名・組み換え
       閲覧者：委員長プール／事務局／admin（同じUIで協働）に加え、
       未終局案件の担当委員長を務めている代理委員長（自分の担当案件のみ操作可）
    """
    user_id = session.get('user_id')
    if not can_view_secretariat_panel(user_id):
        return '<h2 style="font-family:sans-serif;padding:40px;color:#991b1b">権限がありません</h2><p style="font-family:sans-serif;padding:0 40px"><a href="/ethics_review/">研究倫理審査トップへ戻る</a></p>', 403
    return render_template(
        'ethics_review/secretariat.html',
        current_fy=get_current_fiscal_year(),
        status_labels=STATUS_LABELS,
        terminal_statuses=list(TERMINAL_STATUSES),
        is_admin=is_admin(user_id),
        is_secretariat=is_secretariat(user_id),
        is_chair_pool=is_chair_pool(user_id),
        is_acting_chair=is_acting_chair_anywhere(user_id),
    )
# ════════════════════════════════════════════
# API
# ════════════════════════════════════════════

# ────────────────────────────────────────────
# API: 申請者ワークフロー
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case', methods=['POST'])
@login_required
def api_create_case():
    """
    申請者：新規チャンネル立ち上げ。
    必要最小限の情報（タイトル、所属、研究期間）のみ受け取り、
    status='channel_open' で案件を作成。
    """
    user_id = session.get('user_id')
    data = request.json or {}

    title = (data.get('title') or '').strip()
    applicant_name        = (data.get('applicant_name') or '').strip()
    applicant_affiliation = (data.get('applicant_affiliation') or '').strip()
    research_period_start = data.get('research_period_start') or None
    research_period_end   = data.get('research_period_end') or None

    if not title:
        return jsonify({'success': False, 'error': '研究タイトルは必須です'}), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        fiscal_year = get_current_fiscal_year()
        seq_no = next_seq_no(fiscal_year)
        display_id = format_case_id(fiscal_year, seq_no)

        # 申請者名が未指定なら users から補完
        if not applicant_name:
            applicant_name = get_user_display_name(user_id)

        cursor.execute(
            "INSERT INTO ethics_cases "
            "(fiscal_year, seq_no, case_display_id, "
            " applicant_user_id, applicant_name, applicant_affiliation, "
            " title, research_period_start, research_period_end, "
            " status, created_at, updated_at) "
            "VALUES (%s,%s,%s, %s,%s,%s, %s,%s,%s, 'channel_open', %s, %s)",
            (fiscal_year, seq_no, display_id,
             user_id, applicant_name, applicant_affiliation,
             title, research_period_start, research_period_end,
             get_jst_now(), get_jst_now()))
        case_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO ethics_status_history "
            "(case_id, from_status, to_status, changed_by_user_id, note, changed_at) "
            "VALUES (%s, NULL, 'channel_open', %s, 'チャンネル立ち上げ', %s)",
            (case_id, user_id, get_jst_now()))

        conn.commit()
        logging.info("ethics_review case created: %s by user %s", display_id, user_id)
        return jsonify({
            'success':         True,
            'case_id':         case_id,
            'case_display_id': display_id,
        })

    except Exception as e:
        logging.error("api_create_case error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/case/<int:case_id>/document', methods=['POST'])
@login_required
def api_add_document(case_id):
    """
    申請者：書類ファイルをアップロード。multipart/form-data 受信。
    フィールド:
      file   (required) : アップロードするファイル（任意の形式）
      title  (required) : 書類タイトル
      note   (optional) : 備考

    上限: MAX_DOCUMENTS 件、1ファイル MAX_FILE_SIZE バイト。
    保管先: ETHICS_DOCS_ROOT/case_<case_id>/v<version>/<safe_name>
    DBの drive_url カラムには「相対パス」を格納（後方互換のためカラム名は維持）。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    # 書類追加可能なステータス：終局前ならいつでも追加可。
    # （申請後・審査中の差し替え版／追加資料のアップロードに対応）
    if case['status'] in TERMINAL_STATUSES:
        if not is_result_disclosed(case):
            return jsonify({
                'success': False,
                'error':   '審査結果の通知待ちのため書類追加できません'
            }), 400
        return jsonify({
            'success': False,
            'error':   f"このステータス（{STATUS_LABELS.get(case['status'])}）では書類追加できません"
        }), 400

    # multipart/form-data から受け取る
    title = (request.form.get('title') or '').strip()
    note  = (request.form.get('note')  or '').strip()
    up_file = request.files.get('file')

    if not title:
        return jsonify({'success': False, 'error': '書類タイトルは必須です'}), 400
    if not up_file or not (up_file.filename or '').strip():
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400

    original_filename = up_file.filename

    # サイズチェック（ストリーム位置を末尾に移動して取得）
    up_file.stream.seek(0, os.SEEK_END)
    file_size = up_file.stream.tell()
    up_file.stream.seek(0)
    if file_size <= 0:
        return jsonify({'success': False, 'error': 'ファイルが空です'}), 400
    if file_size > MAX_FILE_SIZE:
        return jsonify({
            'success': False,
            'error':   f'ファイルサイズは {MAX_FILE_SIZE // (1024*1024)}MB 以下にしてください'
        }), 400

    # version: conditional_approval から再アップロードの場合は新しいversion
    current_version = case.get('resubmission_count', 0) + 1

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 上限チェック（現バージョンのみ）
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM ethics_documents "
            "WHERE case_id = %s AND version = %s",
            (case_id, current_version))
        cnt = cursor.fetchone()['cnt']
        if cnt >= MAX_DOCUMENTS:
            return jsonify({
                'success': False,
                'error':   f'書類は1案件あたり最大{MAX_DOCUMENTS}件までです'
            }), 400

        # ── ファイル保存
        try:
            save_dir = _ensure_case_doc_dir(case_id, current_version)
        except OSError as oe:
            logging.error("ethics_review: failed to create save dir under %s: %s",
                          ETHICS_DOCS_ROOT, oe)
            return jsonify({
                'success': False,
                'error':   f'保存ディレクトリ作成に失敗しました（{ETHICS_DOCS_ROOT}）。管理者にお問い合わせください。'
            }), 500

        safe_name = _safe_filename(original_filename)
        abs_path  = os.path.join(save_dir, safe_name)

        # 万一の衝突に備え、存在チェック（極めて稀）
        while os.path.exists(abs_path):
            safe_name = _safe_filename(original_filename)
            abs_path  = os.path.join(save_dir, safe_name)

        try:
            up_file.save(abs_path)
        except OSError as oe:
            logging.error("ethics_review: failed to save file to %s: %s", abs_path, oe)
            return jsonify({
                'success': False,
                'error':   f'ファイル保存に失敗しました: {oe}'
            }), 500

        # DB に格納する相対パス（ETHICS_DOCS_ROOTからの）
        rel_path = os.path.relpath(abs_path, ETHICS_DOCS_ROOT)
        # note にオリジナルファイル名と備考を結合して保存
        # （表に出すのは title だが、原本ファイル名も追跡できるようにする）
        note_for_db = note

        # ── DB 登録（drive_url カラムを相対パス用に流用）
        cursor.execute(
            "INSERT INTO ethics_documents "
            "(case_id, version, title, drive_url, note, "
            " original_filename, file_size, uploaded_by_user_id, uploaded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (case_id, current_version, title, rel_path, note_for_db,
             original_filename, file_size, user_id, get_jst_now()))
        document_id = cursor.lastrowid

        # ステータスが channel_open なら documents_uploading に遷移
        if case['status'] == 'channel_open':
            transition_status(conn, cursor, case_id,
                              'documents_uploading', user_id,
                              note='書類アップロード開始')
        elif case['status'] == 'conditional_approval':
            # 再申請時：頭からやり直し → documents_uploading
            transition_status(conn, cursor, case_id,
                              'documents_uploading', user_id,
                              note='条件付き承認後、再提出開始')
        elif case['status'] == 'review_continuation':
            # 審査継続からの再申請時：頭からやり直し → documents_uploading
            transition_status(conn, cursor, case_id,
                              'documents_uploading', user_id,
                              note='審査継続後、再提出開始')

        conn.commit()

        # --- Slack 通知（申請後の書類追加 → 委員会チャンネル）-----------
        # 審査が始まっている案件への書類追加は重要事案なので委員会へ知らせる。
        # 新規申請の準備段階（channel_open / documents_uploading）での
        # 初回アップロードでは通知しない（申請ボタン前なので頻発させない）。
        # case['status'] は遷移前の値。
        if case['status'] not in ('channel_open', 'documents_uploading'):
            try:
                from .slack_notifier import notify_document_added
                notify_document_added(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    applicant_name=case.get('applicant_name'),
                    case_url=None,
                )
            except Exception as slack_err:
                logging.warning("api_add_document: Slack通知でエラー"
                                "（追加は成功）: %s", slack_err)
        # --------------------------------------------------------------

        return jsonify({
            'success': True,
            'document_id':       document_id,
            'original_filename': original_filename,
            'file_size':         file_size,
        })

    except mysql.connector.Error as db_err:
        # original_filename / file_size カラムが無いスキーマでも動くようフォールバック
        logging.warning("api_add_document DB fallback (likely missing columns): %s", db_err)
        try:
            if 'conn' in locals() and conn.is_connected():
                try: conn.rollback()
                except: pass
            conn2   = mysql.connector.connect(**DatabaseConfig.default())
            cursor2 = conn2.cursor(dictionary=True)

            # 再度上限チェック
            cursor2.execute(
                "SELECT COUNT(*) AS cnt FROM ethics_documents "
                "WHERE case_id = %s AND version = %s",
                (case_id, current_version))
            if cursor2.fetchone()['cnt'] >= MAX_DOCUMENTS:
                # ファイルは保存済みなので削除
                try: os.remove(abs_path)
                except: pass
                return jsonify({
                    'success': False,
                    'error':   f'書類は1案件あたり最大{MAX_DOCUMENTS}件までです'
                }), 400

            # original_filename を note に組み込んで保存
            combined_note = note
            if original_filename:
                fn_note = f"[ファイル名: {original_filename}]"
                combined_note = f"{fn_note} {note}" if note else fn_note

            cursor2.execute(
                "INSERT INTO ethics_documents "
                "(case_id, version, title, drive_url, note, uploaded_by_user_id, uploaded_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (case_id, current_version, title, rel_path,
                 combined_note, user_id, get_jst_now()))
            document_id = cursor2.lastrowid

            if case['status'] == 'channel_open':
                transition_status(conn2, cursor2, case_id,
                                  'documents_uploading', user_id,
                                  note='書類アップロード開始')
            elif case['status'] == 'conditional_approval':
                transition_status(conn2, cursor2, case_id,
                                  'documents_uploading', user_id,
                                  note='条件付き承認後、再提出開始')
            elif case['status'] == 'review_continuation':
                transition_status(conn2, cursor2, case_id,
                                  'documents_uploading', user_id,
                                  note='審査継続後、再提出開始')

            conn2.commit()
            cursor2.close(); conn2.close()

            # --- Slack 通知（申請後の書類追加 → 委員会チャンネル）-------
            # 正常系と同じ条件。準備段階の初回アップロードでは通知しない。
            if case['status'] not in ('channel_open', 'documents_uploading'):
                try:
                    from .slack_notifier import notify_document_added
                    notify_document_added(
                        case_display_id=case.get('case_display_id'),
                        title=case.get('title'),
                        applicant_name=case.get('applicant_name'),
                        case_url=None,
                    )
                except Exception as slack_err:
                    logging.warning("api_add_document: Slack通知でエラー"
                                    "（追加は成功）: %s", slack_err)
            # ----------------------------------------------------------

            return jsonify({
                'success': True,
                'document_id':       document_id,
                'original_filename': original_filename,
                'file_size':         file_size,
            })
        except Exception as fallback_err:
            logging.error("api_add_document fallback error: %s", fallback_err)
            try: os.remove(abs_path)
            except: pass
            return jsonify({'success': False, 'error': str(fallback_err)}), 500

    except Exception as e:
        logging.error("api_add_document error: %s", e)
        # 保存済みファイルがあれば消す
        try:
            if 'abs_path' in locals() and os.path.exists(abs_path):
                os.remove(abs_path)
        except: pass
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/case/<int:case_id>/document/<int:doc_id>',
                        methods=['DELETE'])
@login_required
def api_delete_document(case_id, doc_id):
    """書類の削除（申請者のみ、書類提出中のみ）。実ファイルも削除する。"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] not in ('channel_open', 'documents_uploading'):
        return jsonify({
            'success': False,
            'error':   'このステータスでは書類の削除はできません'
        }), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 削除前に保管されているファイルパス（drive_urlカラムを流用）を取得
        cursor.execute(
            "SELECT drive_url FROM ethics_documents "
            "WHERE id = %s AND case_id = %s",
            (doc_id, case_id))
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': False, 'error': '書類が見つかりません'}), 404
        rel_path = row['drive_url']

        # DB から削除
        cursor.execute(
            "DELETE FROM ethics_documents WHERE id = %s AND case_id = %s",
            (doc_id, case_id))
        conn.commit()

        # 実ファイル削除（DB削除コミット成功後）
        if rel_path:
            abs_path = _doc_absolute_path(rel_path)
            if abs_path and os.path.exists(abs_path):
                try:
                    os.remove(abs_path)
                except OSError as oe:
                    logging.warning("Failed to remove document file: %s (%s)",
                                    abs_path, oe)

        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_delete_document error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/case/<int:case_id>/document/<int:doc_id>/download',
                        methods=['GET'])
@login_required
def api_download_document(case_id, doc_id):
    """書類ファイルのダウンロード。
       閲覧可能者：申請者本人、担当委員長、担当委員、事務局、admin、
       および委員プール・委員長プールの委員（変更A：申請内容の閲覧）。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return abort(404)

    # 権限：申請者は自分の案件のみ。それ以外は申請内容の閲覧権が必要。
    # can_view_application_content は審査トラックアクセス権者＋委員プールを含む。
    if is_applicant(user_id, case_id):
        pass
    elif can_view_application_content(user_id, case_id):
        pass
    else:
        return abort(403)

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, title, drive_url, note "
            "FROM ethics_documents WHERE id = %s AND case_id = %s",
            (doc_id, case_id))
        row = cursor.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

    if not row:
        return abort(404)

    # 審査トラック添付は審査トラックにアクセスできる者だけ
    if _is_review_track_document(row) \
            and not can_access_review_track(user_id, case_id):
        return abort(403)

    rel_path = row.get('drive_url')
    abs_path = _doc_absolute_path(rel_path) if rel_path else None
    if not abs_path or not os.path.exists(abs_path):
        return abort(404)

    # ダウンロード時のファイル名：original_filename があればそれ、なければ title + 拡張子
    # まず別途 SELECT で original_filename を取得（無いスキーマでも動くようにフォールバック）
    download_name = None
    try:
        conn2   = mysql.connector.connect(**DatabaseConfig.default())
        cursor2 = conn2.cursor(dictionary=True)
        cursor2.execute(
            "SELECT original_filename FROM ethics_documents WHERE id = %s",
            (doc_id,))
        r2 = cursor2.fetchone()
        if r2 and r2.get('original_filename'):
            download_name = r2['original_filename']
    except Exception:
        # original_filename カラムが無いスキーマでは note から抽出を試みる
        m = re.match(r'^\[ファイル名:\s*([^\]]+)\]', row.get('note') or '')
        if m:
            download_name = m.group(1).strip()
    finally:
        try:
            if 'conn2' in locals() and conn2.is_connected():
                cursor2.close(); conn2.close()
        except: pass

    if not download_name:
        # 最後の手段：title + 保管ファイルの拡張子
        _, ext = os.path.splitext(abs_path)
        download_name = (row['title'] or f"document_{doc_id}") + ext

    # ブラウザでインライン表示できる形式は inline、それ以外は添付（ダウンロード）
    # PDF・画像・テキスト系はブラウザ内で開く。Word/Excel等は自動ダウンロードになる。
    # HTML はインライン表示するとスクリプト実行の余地があるため、安全側でダウンロード扱い。
    _, ext = os.path.splitext(download_name)
    ext = ext.lower()
    INLINE_MIME = {
        '.pdf':  'application/pdf',
        '.png':  'image/png',
        '.jpg':  'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif':  'image/gif',
        '.webp': 'image/webp',
        '.bmp':  'image/bmp',
        '.svg':  'image/svg+xml',
        '.txt':  'text/plain; charset=utf-8',
        '.csv':  'text/plain; charset=utf-8',
        '.json': 'text/plain; charset=utf-8',
        '.xml':  'text/plain; charset=utf-8',
    }
    inline_mime   = INLINE_MIME.get(ext)
    as_attachment = inline_mime is None

    # send_file には download_name / as_attachment を渡さない。
    # （Flask のバージョンによっては download_name を渡すと Content-Disposition が
    #  attachment 優先で設定され、後からの上書きが効かないことがあるため）
    # Content-Disposition は下で完全に自前で制御する。
    if inline_mime:
        resp = send_file(abs_path, mimetype=inline_mime, conditional=True)
    else:
        # 添付（ダウンロード）。MIMEはブラウザに推測させる（octet-streamで安全側）
        resp = send_file(abs_path, mimetype='application/octet-stream',
                         conditional=True)

    # Content-Disposition を自前で設定（inline / attachment を明示）
    from urllib.parse import quote
    disposition = 'inline' if not as_attachment else 'attachment'
    encoded_name = quote(download_name)
    resp.headers['Content-Disposition'] = (
        f"{disposition}; filename*=UTF-8''{encoded_name}"
    )

    if not as_attachment:
        # SVG等のインライン表示時のスクリプト実行を抑止
        resp.headers['Content-Security-Policy'] = "script-src 'none'; object-src 'none'"
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@ethics_review_bp.route('/api/case/<int:case_id>/submit', methods=['POST'])
@login_required
def api_submit(case_id):
    """
    申請者：申請ボタン押下。
    documents_uploading → submitted へ遷移。担当委員長・担当委員の指名待ちに入る。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] != 'documents_uploading':
        return jsonify({
            'success': False,
            'error':   '回答準備中の状態でのみ申請できます'
        }), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 現バージョンの書類が1件以上あるか確認
        current_version = case.get('resubmission_count', 0) + 1
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM ethics_documents "
            "WHERE case_id = %s AND version = %s",
            (case_id, current_version))
        cnt = cursor.fetchone()['cnt']
        if cnt < 1:
            return jsonify({
                'success': False,
                'error':   '書類が1件もありません'
            }), 400

        # 初回申請 : submitted へ。書類を見てから担当委員長・担当委員を指名する。
        # 再提出   : reviewing へ。体制は既にあるので、そのまま審議を再開する。
        #            ただし担当委員長が空だと誰も判断できないので submitted に落とす。
        is_resubmission = case.get('resubmission_count', 0) > 0
        resumed = bool(is_resubmission and case.get('chair_user_id'))

        if resumed:
            transition_status(conn, cursor, case_id, 'reviewing', user_id,
                              note='審査再開申請（回答書類の提出完了）')
        else:
            note = '再申請（書類提出完了）' if is_resubmission else '申請受付'
            transition_status(conn, cursor, case_id, 'submitted', user_id,
                              note=note, set_received_date=not is_resubmission)
        conn.commit()

        # --- Slack 通知（チャンネル一括通知）---------------------------
        # commit 後に実行。通知の成否は申請処理に影響させない。
        # 再提出で審議再開したときは委員会チャンネルへ「審査再開」、
        # それ以外（初回申請）は従来どおり「申請受付」。
        try:
            if resumed:
                from .slack_notifier import notify_review_resumed
                notify_review_resumed(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    applicant_name=case.get('applicant_name'),
                    case_url=None,
                )
            else:
                from .slack_notifier import notify_application_submitted
                notify_application_submitted(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    applicant_name=case.get('applicant_name'),
                    case_url=None,
                )
        except Exception as slack_err:
            logging.warning("api_submit: Slack通知でエラー（申請は成功）: %s",
                            slack_err)
        # --------------------------------------------------------------

        return jsonify({'success': True, 'resumed': resumed})

    except Exception as e:
        logging.error("api_submit error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/case/<int:case_id>/withdraw', methods=['POST'])
@login_required
def api_withdraw(case_id):
    """申請者：取り下げ"""
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not is_applicant(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    if case['status'] in TERMINAL_STATUSES:
        return jsonify({
            'success': False,
            'error':   ('審査結果の通知待ちのため取り下げできません'
                        if not is_result_disclosed(case)
                        else '終局済みのため取り下げできません')
        }), 400

    data = request.json or {}
    reason = (data.get('reason') or '').strip()

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        transition_status(conn, cursor, case_id, 'withdrawn', user_id,
                          note=f'取り下げ: {reason}' if reason else '取り下げ',
                          set_final_date=True)
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_withdraw error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()
# ────────────────────────────────────────────
# API: 担当委員の指名（委員長主導）
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/assign_reviewers', methods=['POST'])
@login_required
def api_assign_reviewers(case_id):
    """
    担当委員を指名する（委員長主導モデル）。
    委員長プール所属者・当該案件の担当委員長（代理委員長を含む）・事務局・admin が実行可能。
    submitted ステータスから reviewer_pending へ遷移。
    request: {
        reviewers: [{ user_id, message }],
        chair_message,
        pre_accepted: bool   # 変更B：True なら最初から accepted で指名
    }
    担当委員長は事前に api_assign_chair で指名済みである必要がある。

    変更B（即承認指名）：
      pre_accepted=True のとき、指名する担当委員を最初から accepted 状態で
      登録する。新規指名分の全員が accepted で、pending が残らなければ
      その場で reviewing に遷移する（＝審査開始）。

    指名後、審査トラックに体制メモを自動投稿する。
    Slack 通知：
      - 通常指名（pending）→ 2番（担当委員依頼通知）
      - 即承認指名で reviewing に遷移 → 3番（審査開始通知）
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

    if not can_assign_reviewers(user_id, case_id):
        return jsonify({
            'success': False,
            'error':   '権限がありません（担当委員長／委員長プール／事務局／admin のみ）'
        }), 403

    if case.get('chair_user_id') is None:
        return jsonify({
            'success': False,
            'error':   '先に担当委員長を指名してください'
        }), 400

    if case['status'] not in ('submitted', 'reviewer_pending', 'reviewing'):
        return jsonify({
            'success': False,
            'error':   'この状態では担当委員を指名できません'
        }), 400

    data = request.json or {}
    reviewers     = data.get('reviewers') or []
    chair_message = (data.get('chair_message') or '').strip()
    pre_accepted  = bool(data.get('pre_accepted'))   # 変更B：即承認指名フラグ

    if not reviewers:
        return jsonify({'success': False, 'error': '担当委員が指定されていません'}), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 各担当委員に指名レコードを作成（重複は無視）
        # 変更B：pre_accepted のときは assignment_status を accepted にし、
        #        responded_at も記録しておく（応諾を経ずに承認済み扱い）。
        for rv in reviewers:
            rv_uid = rv.get('user_id')
            rv_msg = (rv.get('message') or chair_message or '').strip()
            if not rv_uid:
                continue
            # COI または申請者本人は除外
            if rv_uid == case['applicant_user_id']:
                continue
            if is_coi(rv_uid, case_id):
                continue

            if pre_accepted:
                cursor.execute(
                    "INSERT IGNORE INTO ethics_reviewers "
                    "(case_id, user_id, assignment_message, assigned_by_user_id, "
                    " assignment_status, responded_at) "
                    "VALUES (%s, %s, %s, %s, 'accepted', %s)",
                    (case_id, rv_uid, rv_msg, user_id, get_jst_now()))
            else:
                cursor.execute(
                    "INSERT IGNORE INTO ethics_reviewers "
                    "(case_id, user_id, assignment_message, assigned_by_user_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (case_id, rv_uid, rv_msg, user_id))

        # ステータス遷移
        # 通常指名：submitted → reviewer_pending
        # 即承認指名：pending が残らず accepted が1名以上なら reviewing へ
        is_initial_assignment = (case['status'] == 'submitted')
        review_started = False   # 今回この処理で審査開始（reviewing 遷移）になったか

        if is_initial_assignment:
            transition_status(conn, cursor, case_id, 'reviewer_pending',
                              user_id, note='担当委員指名')

        if pre_accepted:
            # 即承認指名後の整合：pending が0で accepted が1名以上なら reviewing
            cursor.execute(
                "SELECT COUNT(*) AS pending_cnt FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status = 'pending'",
                (case_id,))
            pending_cnt = cursor.fetchone()['pending_cnt']
            cursor.execute(
                "SELECT COUNT(*) AS acc_cnt FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status = 'accepted'",
                (case_id,))
            acc_cnt = cursor.fetchone()['acc_cnt']
            if pending_cnt == 0 and acc_cnt >= 1 and case['status'] != 'reviewing':
                transition_status(conn, cursor, case_id, 'reviewing',
                                  user_id, note='即承認指名により審査開始')
                review_started = True

        # 節目メモ：審査体制（初回）を審査トラックに自動投稿
        chair_nm, rv_nms = get_current_committee_names(cursor, case_id)
        post_committee_memo(cursor, case_id, user_id,
                            is_initial=is_initial_assignment,
                            chair_name=chair_nm, reviewer_names=rv_nms)

        conn.commit()

        # --- Slack 通知 -----------------------------------------------
        # commit 後に実行。通知の成否は指名処理に影響させない。
        # 即承認指名で審査開始になった場合は 3番（審査開始通知）、
        # それ以外（通常の pending 指名）は 2番（担当委員依頼通知）。
        try:
            if review_started:
                from .slack_notifier import notify_review_started
                notify_review_started(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    case_url=None,
                )
            else:
                from .slack_notifier import notify_reviewers_assigned
                notify_reviewers_assigned(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    case_url=None,
                )
        except Exception as slack_err:
            logging.warning("api_assign_reviewers: Slack通知でエラー"
                            "（指名は成功）: %s", slack_err)
        # --------------------------------------------------------------

        return jsonify({'success': True})

    except Exception as e:
        logging.error("api_assign_reviewers error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 担当委員長・担当委員の指名と組み換え
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/assign_chair', methods=['POST'])
@login_required
def api_assign_chair(case_id):
    """
    担当委員長を指名する（委員長主導モデル）。
    委員長プール所属者・事務局・admin が実行可能。
    委員長プールに属し、chair_only/committee COIではないユーザーを指定。
    request: { chair_user_id: int, reason?: str }

    現任の担当委員長と異なるユーザーへの変更は「担当委員長交代」として記録される。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

    if not can_assign_reviewers(user_id, case_id):
        return jsonify({
            'success': False,
            'error':   '権限がありません（担当委員長／委員長プール／事務局／admin のみ）'
        }), 403

    # 終局済みは不可
    if case['status'] in TERMINAL_STATUSES:
        return jsonify({
            'success': False,
            'error':   '終局済みのため担当委員長は変更できません'
        }), 400

    data = request.json or {}
    new_chair_id = data.get('chair_user_id')
    reason       = (data.get('reason') or '').strip()

    if not new_chair_id:
        return jsonify({'success': False, 'error': 'chair_user_id が必要です'}), 400
    try:
        new_chair_id = int(new_chair_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '不正なchair_user_idです'}), 400

    # 申請者本人不可
    if new_chair_id == case['applicant_user_id']:
        return jsonify({
            'success': False,
            'error':   '申請者本人を担当委員長に指名することはできません'
        }), 400

    # 担当委員長の資格確認（委員長プール ∪ 委員プール）
    if not can_be_case_chair(new_chair_id):
        return jsonify({
            'success': False,
            'error':   '指定ユーザーは委員長プールにも委員プールにも属していません'
        }), 400

    # COI確認
    if is_chair_excluded(new_chair_id, case_id):
        return jsonify({
            'success': False,
            'error':   '指定ユーザーは本件のCOIのため担当委員長に指名できません'
        }), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 現任担当委員長の有無で挙動を分ける
        current_chair_id = case.get('chair_user_id')
        if current_chair_id == new_chair_id:
            return jsonify({
                'success': False,
                'error':   'すでに同じユーザーが担当委員長です'
            }), 400

        if current_chair_id is not None:
            # 担当委員長交代として記録
            chg_reason = f"担当委員長交代: {reason}" if reason else "担当委員長交代"
            cursor.execute(
                "UPDATE ethics_cases SET "
                "  original_chair_user_id = COALESCE(original_chair_user_id, chair_user_id), "
                "  chair_user_id = %s, "
                "  chair_change_reason = %s, "
                "  updated_at = %s "
                "WHERE id = %s",
                (new_chair_id, chg_reason, get_jst_now(), case_id))
        else:
            cursor.execute(
                "UPDATE ethics_cases SET chair_user_id = %s, updated_at = %s "
                "WHERE id = %s",
                (new_chair_id, get_jst_now(), case_id))

        conn.commit()
        return jsonify({'success': True})

    except Exception as e:
        logging.error("api_assign_chair error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/case/<int:case_id>/reassign_committee', methods=['POST'])
@login_required
def api_reassign_committee(case_id):
    """
    審査委員会の組み換え（担当委員長＋担当委員の同時変更可）。
    委員長プール所属者・事務局・admin が実行可能。
    既存の担当委員のうち keep_reviewer_ids に含まれない者は'replaced'扱いで除外、
    新規 add_reviewers を追加指名する。担当委員長は chair_user_id で同時に更新可。

    request:
      {
        chair_user_id?: int,            # 省略時は現任の担当委員長を維持
        keep_reviewer_ids?: [int],      # この user_id の担当委員を残す
        add_reviewers?: [{user_id, message}],
        reason?: str                    # 組み換え理由（議事録に残る）
      }

    操作後、現在の体制で「組み換えN回目」のメモを審査トラックに自動投稿する。
    審査トラック（議論履歴）は継承される。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

    if not can_assign_reviewers(user_id, case_id):
        return jsonify({
            'success': False,
            'error':   '権限がありません（担当委員長／委員長プール／事務局／admin のみ）'
        }), 403

    if case['status'] in TERMINAL_STATUSES:
        return jsonify({
            'success': False,
            'error':   '終局済みのため組み換えはできません'
        }), 400

    data = request.json or {}
    new_chair_id  = data.get('chair_user_id')
    keep_ids      = set(data.get('keep_reviewer_ids') or [])
    add_reviewers = data.get('add_reviewers') or []
    reason        = (data.get('reason') or '').strip()

    try:
        keep_ids = {int(x) for x in keep_ids}
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '不正な keep_reviewer_ids です'}), 400

    # 担当委員長指定があれば、申請者本人不可・COI・プール確認
    if new_chair_id is not None:
        try:
            new_chair_id = int(new_chair_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '不正な chair_user_id です'}), 400
        if new_chair_id == case['applicant_user_id']:
            return jsonify({
                'success': False,
                'error':   '申請者本人を担当委員長に指名することはできません'
            }), 400
        if not can_be_case_chair(new_chair_id):
            return jsonify({
                'success': False,
                'error':   '指定ユーザーは委員長プールにも委員プールにも属していません'
            }), 400
        if is_chair_excluded(new_chair_id, case_id):
            return jsonify({
                'success': False,
                'error':   '指定ユーザーは本件のCOIのため担当委員長に指名できません'
            }), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # ── 1. 担当委員長の変更
        if new_chair_id is not None and case.get('chair_user_id') != new_chair_id:
            if case.get('chair_user_id') is not None:
                chg_reason = f"組み換え: {reason}" if reason else "組み換え"
                cursor.execute(
                    "UPDATE ethics_cases SET "
                    "  original_chair_user_id = COALESCE(original_chair_user_id, chair_user_id), "
                    "  chair_user_id = %s, "
                    "  chair_change_reason = %s, "
                    "  updated_at = %s "
                    "WHERE id = %s",
                    (new_chair_id, chg_reason, get_jst_now(), case_id))
            else:
                cursor.execute(
                    "UPDATE ethics_cases SET chair_user_id = %s, updated_at = %s "
                    "WHERE id = %s",
                    (new_chair_id, get_jst_now(), case_id))

        # ── 2. 既存の担当委員のうち keep_ids に含まれない者を 'replaced' に
        cursor.execute(
            "SELECT id, user_id, assignment_status FROM ethics_reviewers "
            "WHERE case_id = %s "
            "  AND assignment_status IN ('pending','accepted')",
            (case_id,))
        existing = cursor.fetchall()
        for r in existing:
            if r['user_id'] not in keep_ids:
                cursor.execute(
                    "UPDATE ethics_reviewers SET "
                    "  assignment_status = 'replaced', "
                    "  decline_reason = %s, "
                    "  responded_at = %s "
                    "WHERE id = %s",
                    (f"組み換えにより交代: {reason}" if reason else "組み換えにより交代",
                     get_jst_now(), r['id']))

        # ── 3. 新規追加の担当委員を指名
        newly_added_count = 0   # この組み換えで新しく pending 追加した委員数
        for rv in add_reviewers:
            rv_uid = rv.get('user_id')
            rv_msg = (rv.get('message') or '').strip()
            if not rv_uid:
                continue
            try:
                rv_uid = int(rv_uid)
            except (TypeError, ValueError):
                continue
            if rv_uid == case['applicant_user_id']:
                continue
            if is_coi(rv_uid, case_id):
                continue
            # 既に accepted/pending として残っている場合はスキップ
            cursor.execute(
                "SELECT id FROM ethics_reviewers "
                "WHERE case_id = %s AND user_id = %s "
                "  AND assignment_status IN ('pending','accepted')",
                (case_id, rv_uid))
            if cursor.fetchone():
                continue
            cursor.execute(
                "INSERT INTO ethics_reviewers "
                "(case_id, user_id, assignment_message, assigned_by_user_id, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (case_id, rv_uid, rv_msg, user_id, get_jst_now()))
            newly_added_count += 1

        # ── 4. ステータスの整合
        # 現在 pending な指名がひとつでも残っていれば reviewer_pending、
        # 全員 accepted なら reviewing に。
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM ethics_reviewers "
            "WHERE case_id = %s AND assignment_status = 'pending'",
            (case_id,))
        pending_cnt = cursor.fetchone()['cnt']
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM ethics_reviewers "
            "WHERE case_id = %s AND assignment_status = 'accepted'",
            (case_id,))
        accepted_cnt = cursor.fetchone()['cnt']

        # ステータス遷移は審査開始前/開始後で扱いを分ける
        review_started = False   # この組み換えで reviewing に遷移したか
        if case['status'] in ('submitted', 'reviewer_pending', 'reviewing'):
            if pending_cnt > 0 and case['status'] != 'reviewer_pending':
                transition_status(conn, cursor, case_id, 'reviewer_pending',
                                  user_id, note='組み換えにより応諾待ち')
            elif pending_cnt == 0 and accepted_cnt >= 1 \
                 and case['status'] != 'reviewing':
                transition_status(conn, cursor, case_id, 'reviewing',
                                  user_id, note='組み換え後、審査再開')
                review_started = True

        # ── 5. 節目メモを審査トラックに投稿
        chair_nm, rv_nms = get_current_committee_names(cursor, case_id)
        post_committee_memo(cursor, case_id, user_id,
                            is_initial=False,
                            chair_name=chair_nm, reviewer_names=rv_nms)

        # 組み換え理由があれば補足メモも投稿
        if reason:
            cursor.execute(
                "INSERT INTO ethics_review_messages "
                "(case_id, user_id, body, created_at) VALUES (%s, %s, %s, %s)",
                (case_id, user_id, f"【組み換え理由】{reason}", get_jst_now()))

        conn.commit()

        # --- Slack 通知 -----------------------------------------------
        # commit 後に実行。通知の成否は組み換え処理に影響させない。
        # 組み換えの結果 reviewing に遷移したら 3番（審査開始通知）。
        # そうでなく、この組み換えで新しく pending 委員を追加したなら
        # 2番（担当委員依頼通知）。担当委員長だけ替えた・委員を外しただけ等、
        # 新しい pending 委員がいない組み換えでは通知は出さない。
        try:
            if review_started:
                from .slack_notifier import notify_review_started
                notify_review_started(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    case_url=None,
                )
            elif newly_added_count > 0:
                from .slack_notifier import notify_reviewers_assigned
                notify_reviewers_assigned(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    case_url=None,
                )
        except Exception as slack_err:
            logging.warning("api_reassign_committee: Slack通知でエラー"
                            "（組み換えは成功）: %s", slack_err)
        # --------------------------------------------------------------

        return jsonify({'success': True})

    except Exception as e:
        logging.error("api_reassign_committee error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/case/<int:case_id>/reviewer/response',
                        methods=['POST'])
@login_required
def api_reviewer_response(case_id):
    """
    指名された担当委員：応諾／辞退の返答。
    request: { action: 'accept' | 'decline', decline_reason }
    全員 accepted になったら reviewing に遷移。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

    data = request.json or {}
    action         = data.get('action')
    decline_reason = (data.get('decline_reason') or '').strip()

    if action not in ('accept', 'decline'):
        return jsonify({'success': False, 'error': '不正なアクションです'}), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 指名レコードの確認
        cursor.execute(
            "SELECT * FROM ethics_reviewers "
            "WHERE case_id = %s AND user_id = %s",
            (case_id, user_id))
        rv = cursor.fetchone()
        if not rv:
            return jsonify({'success': False, 'error': '指名されていません'}), 403
        if is_coi(user_id, case_id):
            return jsonify({'success': False,
                            'error': '本件のCOIのため担当委員になれません'}), 403
        if rv['assignment_status'] != 'pending':
            return jsonify({
                'success': False,
                'error':   'すでに応諾／辞退されています'
            }), 400

        new_status = 'accepted' if action == 'accept' else 'declined'
        cursor.execute(
            "UPDATE ethics_reviewers "
            "SET assignment_status = %s, decline_reason = %s, "
            "    responded_at = %s "
            "WHERE id = %s",
            (new_status, decline_reason if action == 'decline' else None,
             get_jst_now(), rv['id']))

        # 辞退時：審査トラックに通知メッセージ
        if action == 'decline':
            rv_name = get_user_display_name(user_id)
            sys_msg = f"【辞退通知】{rv_name} さんが指名を辞退しました。"
            if decline_reason:
                sys_msg += f" 理由: {decline_reason}"
            cursor.execute(
                "INSERT INTO ethics_review_messages "
                "(case_id, user_id, body, created_at) VALUES (%s, %s, %s, %s)",
                (case_id, user_id, sys_msg, get_jst_now()))

        # 全員 accepted なら reviewing へ遷移
        review_started = False  # 今回この処理で審査開始になったか
        if action == 'accept' and case['status'] == 'reviewer_pending':
            cursor.execute(
                "SELECT COUNT(*) AS pending_cnt FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status = 'pending'",
                (case_id,))
            pending_cnt = cursor.fetchone()['pending_cnt']
            cursor.execute(
                "SELECT COUNT(*) AS acc_cnt FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status = 'accepted'",
                (case_id,))
            acc_cnt = cursor.fetchone()['acc_cnt']

            if pending_cnt == 0 and acc_cnt >= 1:
                transition_status(conn, cursor, case_id, 'reviewing',
                                  user_id, note='全担当委員応諾、審査開始')
                review_started = True

        conn.commit()

        # --- Slack 通知（審査開始 → チャンネル一括通知）---------------
        # 今回の応諾で reviewing に遷移したときだけ送る。
        # commit 後に実行。通知の成否は応諾処理に影響させない。
        if review_started:
            try:
                from .slack_notifier import notify_review_started
                notify_review_started(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    case_url=None,
                )
            except Exception as slack_err:
                logging.warning("api_reviewer_response: Slack通知でエラー"
                                "（応諾は成功）: %s", slack_err)
        # --------------------------------------------------------------

        return jsonify({'success': True})

    except Exception as e:
        logging.error("api_reviewer_response error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 担当委員の手動承認（変更B）
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/reviewer/mark_accepted',
                        methods=['POST'])
@login_required
def api_mark_reviewer_accepted(case_id):
    """
    委員長・事務局・admin が、pending の担当委員を代わりに accepted にする
    （変更B：手動承認）。

    委員本人の応諾を待たず、委員長が委員会チャンネルで内諾を得たうえで
    承認済みにする運用を想定。

    request: { user_ids: [int, ...] }   # 承認済みにする担当委員の user_id
              （省略または空なら、その案件の pending な担当委員を全員 accepted に）

    全員 accepted になり pending が残らなければ reviewing に遷移する。
    その瞬間に 3番（審査開始通知）を送る。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

    if not can_assign_reviewers(user_id, case_id):
        return jsonify({
            'success': False,
            'error':   '権限がありません（担当委員長／委員長プール／事務局／admin のみ）'
        }), 403

    if case['status'] not in ('reviewer_pending', 'reviewing'):
        return jsonify({
            'success': False,
            'error':   'この状態では手動承認できません'
        }), 400

    data = request.json or {}
    target_uids = data.get('user_ids') or []
    try:
        target_uids = [int(x) for x in target_uids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '不正な user_ids です'}), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 承認対象の pending 指名レコードを取得
        if target_uids:
            fmt = ','.join(['%s'] * len(target_uids))
            cursor.execute(
                f"SELECT id, user_id FROM ethics_reviewers "
                f"WHERE case_id = %s AND assignment_status = 'pending' "
                f"  AND user_id IN ({fmt})",
                [case_id] + target_uids)
        else:
            # 指定なし → その案件の pending な担当委員を全員
            cursor.execute(
                "SELECT id, user_id FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status = 'pending'",
                (case_id,))
        pending_rows = cursor.fetchall()

        if not pending_rows:
            return jsonify({
                'success': False,
                'error':   '承認対象（応諾待ち）の担当委員がいません'
            }), 400

        # pending → accepted に更新
        for r in pending_rows:
            cursor.execute(
                "UPDATE ethics_reviewers "
                "SET assignment_status = 'accepted', responded_at = %s "
                "WHERE id = %s",
                (get_jst_now(), r['id']))

        # 審査トラックに手動承認メモを投稿
        names = []
        for r in pending_rows:
            names.append(get_user_display_name(r['user_id']))
        memo = "【手動承認】" + '、'.join(names) + " の担当委員指名を承認済みにしました。"
        cursor.execute(
            "INSERT INTO ethics_review_messages "
            "(case_id, user_id, body, created_at) VALUES (%s, %s, %s, %s)",
            (case_id, user_id, memo, get_jst_now()))

        # 全員 accepted なら reviewing へ遷移
        review_started = False
        if case['status'] == 'reviewer_pending':
            cursor.execute(
                "SELECT COUNT(*) AS pending_cnt FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status = 'pending'",
                (case_id,))
            pending_cnt = cursor.fetchone()['pending_cnt']
            cursor.execute(
                "SELECT COUNT(*) AS acc_cnt FROM ethics_reviewers "
                "WHERE case_id = %s AND assignment_status = 'accepted'",
                (case_id,))
            acc_cnt = cursor.fetchone()['acc_cnt']
            if pending_cnt == 0 and acc_cnt >= 1:
                transition_status(conn, cursor, case_id, 'reviewing',
                                  user_id, note='手動承認により審査開始')
                review_started = True

        conn.commit()

        # --- Slack 通知（審査開始 → チャンネル一括通知）---------------
        # 手動承認で reviewing に遷移したときだけ送る。
        if review_started:
            try:
                from .slack_notifier import notify_review_started
                notify_review_started(
                    case_display_id=case.get('case_display_id'),
                    title=case.get('title'),
                    case_url=None,
                )
            except Exception as slack_err:
                logging.warning("api_mark_reviewer_accepted: Slack通知でエラー"
                                "（承認は成功）: %s", slack_err)
        # --------------------------------------------------------------

        return jsonify({'success': True})

    except Exception as e:
        logging.error("api_mark_reviewer_accepted error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 担当委員長による判断宣言
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/decide', methods=['POST'])
@login_required
def api_decide(case_id):
    """
    担当委員長：委員会としての判断を宣言する。
    action: 'approve' | 'reject' | 'conditional' | 'continue' | 'not_applicable'

    not_applicable（非該当・審査不要）は規程第11条第2項(5)の判定。
      - reviewing では担当委員長の判断のひとつ（他の4つと同じ扱い）。
      - submitted / reviewer_pending（受理段階）でも宣言できる。
        委員長プール所属者が書類を見て「審査を要しない」と判断する運用。
        担当委員長が未指名なら、宣言者を担当委員長として記録する。
      所見（note）は委員長のコメント欄として使う。英訳版の回答が必要な
      ときは委員長がここに書き、事務局の「申請者にも通知」で申請者へ届く。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

    data = request.json or {}
    action = data.get('action')
    note   = (data.get('note') or '').strip()

    if action not in ('approve', 'reject', 'conditional', 'continue',
                      'not_applicable'):
        return jsonify({'success': False, 'error': '不正なアクションです'}), 400

    if action == 'not_applicable':
        if case['status'] not in NA_DECLARABLE_STATUSES:
            return jsonify({
                'success': False,
                'error':   '非該当は申請受付後（担当委員指名待ち・応諾待ち・審査中）'
                           'の案件にのみ宣言できます'
            }), 400
        if not can_declare_not_applicable(user_id, case_id):
            return jsonify({
                'success': False,
                'error':   '非該当を宣言する権限がありません'
                           '（担当委員長、または受理段階では委員長プールのみ）'
            }), 403
    else:
        if not can_decide(user_id, case_id):
            return jsonify({'success': False, 'error': '判断権限がありません'}), 403
        if case['status'] != 'reviewing':
            return jsonify({
                'success': False,
                'error':   '審査中の状態でのみ判断できます'
            }), 400

    status_map = {
        'approve':        'approved',
        'reject':         'rejected',
        'conditional':    'conditional_approval',
        'continue':       'review_continuation',
        'not_applicable': 'not_applicable',
    }
    new_status = status_map[action]

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        if action in ('approve', 'reject', 'not_applicable'):
            # 終局
            cursor.execute(
                "UPDATE ethics_cases SET "
                "  decision_note = %s, decision_by_user_id = %s, "
                "  decision_at = %s "
                "WHERE id = %s",
                (note, user_id, get_jst_now(), case_id))
            # 受理段階の非該当で担当委員長が未指名なら、宣言者を担当委員長として残す
            # （誰の判断で終局したかを chair_user_id でも辿れるようにする）
            if action == 'not_applicable' and not case.get('chair_user_id'):
                cursor.execute(
                    "UPDATE ethics_cases SET chair_user_id = %s WHERE id = %s",
                    (user_id, case_id))
            if action == 'not_applicable' and case['status'] != 'reviewing':
                transition_note = f'受理段階の判定: {STATUS_LABELS[new_status]}'
            else:
                transition_note = f'担当委員長判断: {STATUS_LABELS[new_status]}'
            transition_status(conn, cursor, case_id, new_status, user_id,
                              note=transition_note,
                              set_final_date=True,
                              set_approved_date=(action == 'approve'))
        else:
            # conditional_approval / review_continuation：
            # 再申請に備えて resubmission_count をインクリメント
            cursor.execute(
                "UPDATE ethics_cases SET "
                "  decision_note = %s, decision_by_user_id = %s, "
                "  decision_at = %s, "
                "  resubmission_count = resubmission_count + 1 "
                "WHERE id = %s",
                (note, user_id, get_jst_now(), case_id))
            if action == 'conditional':
                transition_note = '条件付き承認、申請者に再提出要請'
            else:
                transition_note = '審査継続、申請者に再提出要請'
            transition_status(conn, cursor, case_id, new_status, user_id,
                              note=transition_note)

        # 申請者チャンネルへの通知メッセージ自動投稿。
        # ただし「承認(approve)/不承認(reject)/非該当(not_applicable)」の最終決定は、ここでは
        # 申請者へ通知しない（事務局が稟議等で確認後、体制運営パネルの
        # 「申請者にも通知」ボタンで改めて通知する）。
        # 条件付き承認/審査継続は、申請者に再提出を促す必要があるため従来どおり即時通知する。
        notify_applicant_now = action in ('conditional', 'continue')
        if notify_applicant_now:
            notification_body = f"【委員会通知】{STATUS_LABELS[new_status]}\n\n{note}"
            cursor.execute(
                "INSERT INTO ethics_applicant_messages "
                "(case_id, user_id, role, message_type, body, created_at) "
                "VALUES (%s, %s, 'chair', 'notification', %s, %s)",
                (case_id, user_id, notification_body, get_jst_now()))

        conn.commit()

        # --- Slack 通知（委員会の判断 → チャンネル一括通知）-----------
        # commit 後に実行。通知の成否は判断処理に影響させない。
        try:
            from .slack_notifier import notify_decision
            notify_decision(
                case_display_id=case.get('case_display_id'),
                title=case.get('title'),
                decision_label=STATUS_LABELS[new_status],
                case_url=None,
            )
        except Exception as slack_err:
            logging.warning("api_decide: Slack通知でエラー"
                            "（判断は成功）: %s", slack_err)
        # --------------------------------------------------------------

        # --- Slack DM（委員会の判断 → 申請者本人へ個人宛 DM）【7番後半】--
        # 申請者の FUJIN-P 上のメールアドレスから Slack ユーザーを照合し、
        # 本人へ直接 DM を送る。メールが Slack と食い違う申請者には
        # 届かないが、その場合も {'ok': False} が返るだけで判断処理は
        # 止めない（申請者チャンネルへの通知投稿は上で済んでいる）。
        #
        # DM が届いたか否かは applicant_dm にまとめ、戻り値で
        # 委員長・事務局（判断を宣言した本人）に伝える。届かなかった
        # 場合は、別ルートで申請者へ結果を伝えてもらうための合図になる。
        #   status: 'sent'        … DM 送信成功
        #           'no_email'    … 申請者のメールアドレスが未登録
        #           'not_delivered' … メールはあるが Slack に届かなかった
        #           'skipped'     … 承認/不承認/非該当のため、この時点では申請者へ通知しない
        applicant_dm = {'status': 'no_email', 'message': ''}
        if not notify_applicant_now:
            # 承認/不承認/非該当：申請者へは直行通知しない（事務局が後で通知する）
            applicant_dm = {'status': 'skipped', 'message': ''}
        if notify_applicant_now:
          try:
            applicant_email = get_user_email(case.get('applicant_user_id'))
            if applicant_email:
                dm_lines = [
                    'FUJIN-P 研究倫理審査システムからのお知らせです。',
                    '',
                    f'■ 案件番号：{case.get("case_display_id") or "（番号未付与）"}',
                    f'■ 研究課題：{case.get("title") or "（無題）"}',
                    f'■ 委員会の判断：{STATUS_LABELS[new_status]}',
                ]
                if note:
                    dm_lines.append('')
                    dm_lines.append(f'【所見】\n{note}')
                dm_lines.append('')
                dm_lines.append('詳細は研究倫理審査システムの申請者チャンネルで'
                                'ご確認ください。')
                from .slack_notifier import send_dm_by_email
                dm_result = send_dm_by_email(
                    applicant_email, '\n'.join(dm_lines),
                    log_label='判断結果の申請者DM')
                if dm_result.get('ok'):
                    applicant_dm = {'status': 'sent', 'message': ''}
                else:
                    applicant_dm = {
                        'status':  'not_delivered',
                        'message': dm_result.get('error') or '',
                    }
            else:
                logging.warning("api_decide: 申請者メール未取得のため "
                                "DM 省略 (case_id=%s)", case_id)
                applicant_dm = {'status': 'no_email', 'message': ''}
          except Exception as dm_err:
            logging.warning("api_decide: 申請者DMでエラー"
                            "（判断は成功）: %s", dm_err)
            applicant_dm = {'status': 'not_delivered',
                            'message': str(dm_err)}
        # --------------------------------------------------------------

        # --- DM 不達のとき、審査トラックへ記録メモを自動投稿 -----------
        # 申請者への Slack DM が届かなかった事実を、案件に永続的に残す。
        # 後から委員長・事務局が detail.html を開けば確認でき、別ルートで
        # 申請者へ連絡したか追える。message_kind='alert' で目立たせる。
        # 申請者チャンネルへの判断通知は既に投稿済みなので、申請者自身は
        # FUJIN-P 上で結果を確認できる（この記録メモは委員側内部の覚え）。
        if applicant_dm.get('status') not in ('sent', 'skipped'):
            try:
                if applicant_dm.get('status') == 'no_email':
                    reason = '申請者のメールアドレスが登録されていません'
                else:
                    reason = '申請者の Slack アカウントが見つかりませんでした'
                memo = (
                    '【申請者DM不達】委員会の判断について、申請者への '
                    'Slack DM は届きませんでした。\n'
                    f'理由：{reason}。\n'
                    '申請者には申請者チャンネルで判断結果が通知されていますが、'
                    'お急ぎの場合はメール等の別の手段で「審査結果が出ました」と'
                    'お伝えください。'
                )
                cursor.execute(
                    "INSERT INTO ethics_review_messages "
                    "(case_id, user_id, body, message_kind, created_at) "
                    "VALUES (%s, %s, %s, 'alert', %s)",
                    (case_id, user_id, memo, get_jst_now()))
                conn.commit()
            except Exception as memo_err:
                logging.warning("api_decide: DM不達メモの記録に失敗"
                                "（判断は成功）: %s", memo_err)
        # --------------------------------------------------------------

        return jsonify({'success': True, 'applicant_dm': applicant_dm})

    except Exception as e:
        logging.error("api_decide error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 事務局による「申請者にも通知」
#   承認/不承認/非該当の最終決定は決定時に申請者へ通知しない。
#   事務局が稟議等で確認後、この操作で申請者チャンネルへ結果を通知する。
# ────────────────────────────────────────────


@ethics_review_bp.route('/api/case/<int:case_id>/notify_applicant',
                        methods=['POST'])
@login_required
def api_notify_applicant(case_id):
    """事務局・admin：学内の決裁完了として、承認/不承認/非該当の結果を
       申請者チャンネルへ通知する。

       decision_date（YYYY-MM-DD、任意）を受け取り、最終日として記録する。
       承認案件では承認日にも同じ日付を入れる。省略時は当日（JST）。
       委員会の判定日と決裁日がずれることがあるため、事務局が上書きできる。
    """
    user_id = session.get('user_id')
    if not is_secretariat(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

    # 対象は承認/不承認/非該当の最終決定が出ている案件のみ
    if case['status'] not in FINAL_DECISION_STATUSES:
        return jsonify({
            'success': False,
            'error':   '承認／不承認／非該当の決定が出ている案件のみ通知できます'
        }), 400

    # 二重通知の防止
    if case.get('applicant_notified_at'):
        return jsonify({
            'success': False,
            'error':   'この案件はすでに申請者へ通知済みです'
        }), 400

    # 決裁日（＝承認日／最終日）。省略時は当日。
    data = request.json or {}
    date_str = (data.get('decision_date') or '').strip()
    if date_str:
        try:
            decision_date = datetime.datetime.strptime(
                date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({
                'success': False,
                'error':   '日付は YYYY-MM-DD の形式で指定してください'
            }), 400
    else:
        decision_date = get_jst_today()

    is_approved = (case['status'] == 'approved')
    date_label  = '承認日' if is_approved else '最終日'

    note = case.get('decision_note') or ''
    notification_body = (
        f"【委員会通知】{STATUS_LABELS[case['status']]}\n\n{note}")
    if is_approved:
        notification_body += f"\n\n承認日：{decision_date.strftime('%Y-%m-%d')}"

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 申請者チャンネルへ通知を投稿（role は secretariat）
        cursor.execute(
            "INSERT INTO ethics_applicant_messages "
            "(case_id, user_id, role, message_type, body, created_at) "
            "VALUES (%s, %s, 'secretariat', 'notification', %s, %s)",
            (case_id, user_id, notification_body, get_jst_now()))

        # 通知済みを記録し、決裁日を最終日（承認案件は承認日にも）反映
        if is_approved:
            cursor.execute(
                "UPDATE ethics_cases SET "
                "  applicant_notified_at = %s, applicant_notified_by = %s, "
                "  final_date = %s, approved_date = %s "
                "WHERE id = %s",
                (get_jst_now(), user_id, decision_date, decision_date, case_id))
        else:
            cursor.execute(
                "UPDATE ethics_cases SET "
                "  applicant_notified_at = %s, applicant_notified_by = %s, "
                "  final_date = %s "
                "WHERE id = %s",
                (get_jst_now(), user_id, decision_date, case_id))

        # 決裁完了を履歴に残す（ステータスは変わらないので from = to）
        cursor.execute(
            "INSERT INTO ethics_status_history "
            "(case_id, from_status, to_status, changed_by_user_id, note, changed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (case_id, case['status'], case['status'], user_id,
             f"決裁完了・申請者へ通知（{date_label}: "
             f"{decision_date.strftime('%Y-%m-%d')}）",
             get_jst_now()))

        conn.commit()
        return jsonify({
            'success':       True,
            'decision_date': decision_date.strftime('%Y-%m-%d'),
        })

    except Exception as e:
        logging.error("api_notify_applicant error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()



# ────────────────────────────────────────────
# API: 事務局による打ち切り
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/terminate', methods=['POST'])
@login_required
def api_terminate(case_id):
    """事務局・admin：長期放置案件を打ち切り"""
    user_id = session.get('user_id')
    if not is_secretariat(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if case['status'] in TERMINAL_STATUSES:
        return jsonify({
            'success': False,
            'error':   'すでに終局しています'
        }), 400

    data = request.json or {}
    reason = (data.get('reason') or '').strip()

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        transition_status(conn, cursor, case_id, 'terminated', user_id,
                          note=f'打ち切り: {reason}' if reason else '打ち切り',
                          set_final_date=True)

        # 申請者チャンネルに通知
        notif = f"【事務局通知】本件は打ち切りとなりました。\n{reason}" if reason \
                else "【事務局通知】本件は打ち切りとなりました。"
        cursor.execute(
            "INSERT INTO ethics_applicant_messages "
            "(case_id, user_id, role, message_type, body, created_at) "
            "VALUES (%s, %s, 'secretariat', 'notification', %s, %s)",
            (case_id, user_id, notif, get_jst_now()))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_terminate error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: COI 管理（事務局・admin）
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/coi', methods=['POST'])
@login_required
def api_set_coi(case_id):
    """COI設定/解除"""
    user_id = session.get('user_id')
    if not is_secretariat(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    data = request.json or {}
    target_uid     = data.get('user_id')
    excluded_roles = data.get('excluded_roles', 'committee')
    reason         = (data.get('reason') or '').strip()
    remove         = data.get('remove', False)

    if not target_uid:
        return jsonify({'success': False, 'error': 'user_id が必要です'}), 400
    if excluded_roles not in ('committee', 'chair_only'):
        return jsonify({'success': False, 'error': '不正なexcluded_rolesです'}), 400

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        if remove:
            cursor.execute(
                "DELETE FROM ethics_coi WHERE case_id = %s AND user_id = %s",
                (case_id, target_uid))
        else:
            cursor.execute(
                "INSERT INTO ethics_coi "
                "(case_id, user_id, excluded_roles, reason, set_by_user_id, set_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  excluded_roles = VALUES(excluded_roles), "
                "  reason = VALUES(reason), "
                "  set_by_user_id = VALUES(set_by_user_id), "
                "  set_at = VALUES(set_at)",
                (case_id, target_uid, excluded_roles, reason, user_id, get_jst_now()))

            # 現任の担当委員長がCOIになった場合、担当委員長を空に
            case = get_case(case_id)
            if case and case.get('chair_user_id') == int(target_uid) and \
               excluded_roles in ('committee', 'chair_only'):
                cursor.execute(
                    "UPDATE ethics_cases SET "
                    "  original_chair_user_id = COALESCE(original_chair_user_id, chair_user_id), "
                    "  chair_user_id = NULL, "
                    "  chair_change_reason = %s, "
                    "  updated_at = %s "
                    "WHERE id = %s",
                    (f"COI: {reason}" if reason else "COI",
                     get_jst_now(), case_id))

        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("api_set_coi error: %s", e)
        if 'conn' in locals() and conn.is_connected():
            try: conn.rollback()
            except: pass
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()
# ────────────────────────────────────────────
# API: 審査トラックメッセージ
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/review_messages',
                        methods=['GET'])
@login_required
def api_review_messages(case_id):
    """審査トラックメッセージ取得"""
    user_id = session.get('user_id')
    if not can_access_review_track(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        # document_id カラムが無い旧スキーマでも動くようフォールバック
        try:
            cursor.execute(
                "SELECT m.id, m.user_id, m.body, m.message_kind, m.document_id, "
                "       m.created_at, u.full_name AS user_name "
                "FROM ethics_review_messages m "
                "LEFT JOIN users u ON u.id = m.user_id "
                "WHERE m.case_id = %s "
                "ORDER BY m.created_at ASC",
                (case_id,))
            rows = cursor.fetchall()
        except mysql.connector.Error:
            cursor.execute(
                "SELECT m.id, m.user_id, m.body, m.message_kind, "
                "       m.created_at, u.full_name AS user_name "
                "FROM ethics_review_messages m "
                "LEFT JOIN users u ON u.id = m.user_id "
                "WHERE m.case_id = %s "
                "ORDER BY m.created_at ASC",
                (case_id,))
            rows = cursor.fetchall()
        msgs = []
        for r in rows:
            attachment = _fetch_document_brief(cursor, case_id, r.get('document_id'))
            msgs.append({
                'id':           r['id'],
                'user_id':      r['user_id'],
                'user_name':    r['user_name'] or f"User#{r['user_id']}",
                'body':         r['body'],
                'message_kind': r.get('message_kind') or 'normal',
                'attachment':   attachment,
                'created_at':   fmt_datetime(r['created_at']),
            })
        return jsonify({'success': True, 'messages': msgs})
    except Exception as e:
        logging.error("api_review_messages error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/case/<int:case_id>/review_messages',
                        methods=['POST'])
@login_required
def api_post_review_message(case_id):
    """審査トラックメッセージ投稿。

    変更C：message_kind を受け取る。
      'normal'（既定）… 通常の発言
      'alert'          … アラート発言。投稿後、委員会チャンネルへ
                          Slack 通知を出す（中身は載せず案件番号のみ）。

    変更（添付対応）：multipart/form-data でファイルを添付できる。
      添付ファイルは ethics_documents に source='attachment' で保存し
      （＝書類置き場に1本化）、メッセージの document_id で参照する。
      本文・添付の少なくとも一方があれば投稿可。
    """
    user_id = session.get('user_id')
    if not can_access_review_track(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    # JSON / multipart の両方を受け付ける
    if request.content_type and 'multipart/form-data' in request.content_type:
        body         = (request.form.get('body') or '').strip()
        message_kind = request.form.get('message_kind')
        up_file      = request.files.get('file')
    else:
        data = request.json or {}
        body         = (data.get('body') or '').strip()
        message_kind = data.get('message_kind')
        up_file      = None

    has_file = bool(up_file and (up_file.filename or '').strip())
    if not body and not has_file:
        return jsonify({'success': False, 'error': '本文または添付ファイルが必要です'}), 400

    # 種別：'alert' 以外はすべて 'normal' に正規化
    if message_kind != 'alert':
        message_kind = 'normal'

    case = get_case(case_id)
    abs_path_for_cleanup = None
    committed = False

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # ── 添付があれば先に書類として保存
        document_id = None
        if has_file:
            current_version = (case.get('resubmission_count', 0) + 1) if case else 1
            # 添付の書類タイトル：本文がなければ元ファイル名を流用
            doc_title = (body[:80] if body else (up_file.filename or '添付ファイル'))
            stored = _store_document_file(
                conn, cursor, case_id, current_version, up_file,
                title=doc_title, note='審査トラック添付',
                user_id=user_id, source='attachment')
            document_id = stored['document_id']
            abs_path_for_cleanup = stored['abs_path']

        # ── メッセージ本体 INSERT（document_id カラムが無い旧スキーマに対応）
        try:
            cursor.execute(
                "INSERT INTO ethics_review_messages "
                "(case_id, user_id, body, message_kind, document_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (case_id, user_id, body, message_kind, document_id, get_jst_now()))
        except mysql.connector.Error:
            cursor.execute(
                "INSERT INTO ethics_review_messages "
                "(case_id, user_id, body, message_kind, created_at) VALUES (%s, %s, %s, %s, %s)",
                (case_id, user_id, body, message_kind, get_jst_now()))

        conn.commit()
        committed = True
        new_message_id = cursor.lastrowid
        abs_path_for_cleanup = None  # commit 成功＝ファイルは正規

        # --- Slack 通知（アラート発言のみ）-----------------------------
        # commit 後に実行。通知の成否は投稿処理に影響させない。
        if message_kind == 'alert':
            try:
                from .slack_notifier import notify_alert_message
                notify_alert_message(
                    case_display_id=case.get('case_display_id') if case else None,
                    title=case.get('title') if case else None,
                    case_url=None,
                )
            except Exception as slack_err:
                logging.warning("api_post_review_message: Slack通知でエラー"
                                "（投稿は成功）: %s", slack_err)
        # --------------------------------------------------------------

        return jsonify({'success': True, 'message_id': new_message_id})
    except ValueError as ve:
        # ファイルサイズ・空ファイル等のバリデーションエラー
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        logging.error("api_post_review_message error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        # 失敗時に保存済みファイルが残っていれば消す
        if abs_path_for_cleanup:
            try:
                if os.path.exists(abs_path_for_cleanup):
                    os.remove(abs_path_for_cleanup)
            except OSError:
                pass
        if 'conn' in locals() and conn.is_connected():
            if not committed:
                try: conn.rollback()
                except: pass
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 申請者チャンネルメッセージ
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/applicant_messages',
                        methods=['GET'])
@login_required
def api_applicant_messages(case_id):
    """申請者チャンネルメッセージ取得"""
    user_id = session.get('user_id')
    if not can_read_applicant_channel(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        # document_id カラムが無い旧スキーマでも動くようフォールバック
        try:
            cursor.execute(
                "SELECT m.id, m.user_id, m.role, m.message_type, m.body, "
                "       m.document_id, m.created_at, u.full_name AS user_name "
                "FROM ethics_applicant_messages m "
                "LEFT JOIN users u ON u.id = m.user_id "
                "WHERE m.case_id = %s "
                "ORDER BY m.created_at ASC",
                (case_id,))
            rows = cursor.fetchall()
        except mysql.connector.Error:
            cursor.execute(
                "SELECT m.id, m.user_id, m.role, m.message_type, m.body, "
                "       m.created_at, u.full_name AS user_name "
                "FROM ethics_applicant_messages m "
                "LEFT JOIN users u ON u.id = m.user_id "
                "WHERE m.case_id = %s "
                "ORDER BY m.created_at ASC",
                (case_id,))
            rows = cursor.fetchall()
        msgs = []
        for r in rows:
            attachment = _fetch_document_brief(cursor, case_id, r.get('document_id'))
            msgs.append({
                'id':           r['id'],
                'user_id':      r['user_id'],
                'user_name':    r['user_name'] or f"User#{r['user_id']}",
                'role':         r['role'],
                'message_type': r['message_type'],
                'body':         r['body'],
                'attachment':   attachment,
                'created_at':   fmt_datetime(r['created_at']),
            })
        return jsonify({
            'success':   True,
            'messages':  msgs,
            'read_only': not can_write_applicant_channel(user_id, case_id),
        })
    except Exception as e:
        logging.error("api_applicant_messages error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/case/<int:case_id>/applicant_messages',
                        methods=['POST'])
@login_required
def api_post_applicant_message(case_id):
    """申請者チャンネルメッセージ投稿。

    変更（添付対応）：multipart/form-data でファイルを添付できる。
      添付ファイルは ethics_documents に source='attachment' で保存し
      （＝書類置き場に1本化）、メッセージの document_id で参照する。
      本文・添付の少なくとも一方があれば投稿可。
    """
    user_id = session.get('user_id')
    if not can_write_applicant_channel(user_id, case_id):
        return jsonify({'success': False, 'error': '書き込み権限がありません'}), 403

    # JSON / multipart の両方を受け付ける
    if request.content_type and 'multipart/form-data' in request.content_type:
        body         = (request.form.get('body') or '').strip()
        message_type = request.form.get('message_type', 'general')
        up_file      = request.files.get('file')
    else:
        data = request.json or {}
        body         = (data.get('body') or '').strip()
        message_type = data.get('message_type', 'general')
        up_file      = None

    has_file = bool(up_file and (up_file.filename or '').strip())
    if not body and not has_file:
        return jsonify({'success': False, 'error': '本文または添付ファイルが必要です'}), 400
    if message_type not in ('question', 'answer', 'notification', 'general'):
        message_type = 'general'

    # ロール判定（書き込み時の表示用ラベル）
    if is_admin(user_id):
        role = 'admin'
    elif is_applicant(user_id, case_id):
        role = 'applicant'
    elif is_case_chair(user_id, case_id):
        role = 'chair'
    elif is_secretariat(user_id):
        role = 'secretariat'
    else:
        role = 'admin'  # フォールバック

    case = get_case(case_id)
    abs_path_for_cleanup = None
    committed = False

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # ── 添付があれば先に書類として保存
        document_id = None
        if has_file:
            current_version = (case.get('resubmission_count', 0) + 1) if case else 1
            doc_title = (body[:80] if body else (up_file.filename or '添付ファイル'))
            # 申請者本人の添付は「申請ファイル」として扱い、それ以外は添付扱い。
            # いずれにせよ書類置き場には現れる。
            note_label = '申請者チャンネル添付'
            stored = _store_document_file(
                conn, cursor, case_id, current_version, up_file,
                title=doc_title, note=note_label,
                user_id=user_id, source='attachment')
            document_id = stored['document_id']
            abs_path_for_cleanup = stored['abs_path']

        # ── メッセージ本体 INSERT（document_id カラムが無い旧スキーマに対応）
        try:
            cursor.execute(
                "INSERT INTO ethics_applicant_messages "
                "(case_id, user_id, role, message_type, body, document_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (case_id, user_id, role, message_type, body, document_id, get_jst_now()))
        except mysql.connector.Error:
            cursor.execute(
                "INSERT INTO ethics_applicant_messages "
                "(case_id, user_id, role, message_type, body, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (case_id, user_id, role, message_type, body, get_jst_now()))

        conn.commit()
        committed = True
        abs_path_for_cleanup = None  # commit 成功＝ファイルは正規

        # --- Slack 通知（申請者からの連絡のみ → 業務会議チャンネル）-------
        # 投稿者が申請者本人（role='applicant'）のときだけ通知する。
        # 委員長・事務局・admin の投稿では通知しない。
        # 申請者からの連絡はいつ来るか予測できないため、業務会議へ知らせる。
        # commit 後に実行。通知の成否は投稿処理に影響させない。
        if role == 'applicant':
            try:
                from .slack_notifier import notify_applicant_message
                notify_applicant_message(
                    case_display_id=case.get('case_display_id') if case else None,
                    title=case.get('title') if case else None,
                    applicant_name=case.get('applicant_name') if case else None,
                    case_url=None,
                )
            except Exception as slack_err:
                logging.warning("api_post_applicant_message: Slack通知でエラー"
                                "（投稿は成功）: %s", slack_err)
        # --------------------------------------------------------------

        return jsonify({'success': True, 'message_id': cursor.lastrowid})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        logging.error("api_post_applicant_message error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if abs_path_for_cleanup:
            try:
                if os.path.exists(abs_path_for_cleanup):
                    os.remove(abs_path_for_cleanup)
            except OSError:
                pass
        if 'conn' in locals() and conn.is_connected():
            if not committed:
                try: conn.rollback()
                except: pass
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 案件詳細
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>', methods=['GET'])
@login_required
def api_case_detail(case_id):
    """
    案件詳細を返す。
    - 申請者：自分の案件のみ閲覧可（書類・申請者チャンネルメタ情報）
    - 担当委員：担当案件の全情報
    - 事務局・admin：全案件の全情報
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404

    is_app          = is_applicant(user_id, case_id)
    can_access_rt   = can_access_review_track(user_id, case_id)
    can_read_ac     = can_read_applicant_channel(user_id, case_id)
    # 委員長プール：任命権者として全件の体制（担当委員長・担当委員）を見られる。
    # 審査トラックの発言・添付は can_access_rt で別途制御されるので、
    # 自分が申請者の案件では体制は見えても審議内容は見えない。
    can_see_committee = can_access_rt or is_chair_pool(user_id)

    if not (is_app or can_access_rt or can_read_ac or can_see_committee):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 書類（現バージョン）
        current_version = case.get('resubmission_count', 0) + 1
        # original_filename カラムが無いスキーマでも動くよう、まず無しでクエリを試す
        try:
            cursor.execute(
                "SELECT id, version, source, title, drive_url, note, "
                "       original_filename, file_size, "
                "       uploaded_at, uploaded_by_user_id "
                "FROM ethics_documents WHERE case_id = %s "
                "ORDER BY uploaded_at DESC, id DESC",
                (case_id,))
            doc_rows = cursor.fetchall()
            has_filename_col = True
        except mysql.connector.Error:
            cursor.execute(
                "SELECT id, version, title, drive_url, note, "
                "       uploaded_at, uploaded_by_user_id "
                "FROM ethics_documents WHERE case_id = %s "
                "ORDER BY uploaded_at DESC, id DESC",
                (case_id,))
            doc_rows = cursor.fetchall()
            has_filename_col = False

        # 審査トラック添付（委員会側の資料）は審査トラックにアクセスできる者だけに見せる
        if not can_access_rt:
            doc_rows = [r for r in doc_rows if not _is_review_track_document(r)]

        documents = []
        for r in doc_rows:
            # 表示用ファイル名：original_filename → note の [ファイル名: …] → 空
            display_filename = None
            if has_filename_col:
                display_filename = r.get('original_filename')
            if not display_filename and r.get('note'):
                m = re.match(r'^\[ファイル名:\s*([^\]]+)\]', r['note'] or '')
                if m:
                    display_filename = m.group(1).strip()
            # ダウンロードURL（ファイル添付がある場合のみ）
            download_url = None
            if r.get('drive_url'):
                download_url = url_for(
                    'ethics_review.api_download_document',
                    case_id=case_id, doc_id=r['id'])
            documents.append({
                'id':                r['id'],
                'version':           r['version'],
                'title':             r['title'],
                'note':              r.get('note') or '',
                'original_filename': display_filename or '',
                'file_size':         r.get('file_size') if has_filename_col else None,
                'download_url':      download_url,
                'source':            r.get('source') or 'application',
                'uploaded_at':       fmt_datetime(r['uploaded_at']),
                'is_current':        r['version'] == current_version,
            })

        # 担当委員（事務局・委員長・admin・担当委員自身のみ閲覧可）
        # 審査トラックにアクセスできる者、および委員長プール（体制の閲覧のみ）
        reviewers = []
        if can_see_committee:
            cursor.execute(
                "SELECT r.id, r.user_id, r.assignment_status, "
                "       r.assignment_message, r.decline_reason, "
                "       r.assigned_at, r.responded_at, "
                "       u.full_name AS user_name "
                "FROM ethics_reviewers r "
                "LEFT JOIN users u ON u.id = r.user_id "
                "WHERE r.case_id = %s "
                "ORDER BY r.assigned_at ASC",
                (case_id,))
            for r in cursor.fetchall():
                reviewers.append({
                    'id':                 r['id'],
                    'user_id':            r['user_id'],
                    'user_name':          r['user_name'] or f"User#{r['user_id']}",
                    'assignment_status':  r['assignment_status'],
                    'assignment_message': r['assignment_message'] or '',
                    'decline_reason':     r['decline_reason'] or '',
                    'assigned_at':        fmt_datetime(r['assigned_at']),
                    'responded_at':       fmt_datetime(r['responded_at']),
                })

        # COI（事務局・admin のみ）
        cois = []
        if is_admin(user_id) or is_secretariat(user_id):
            cursor.execute(
                "SELECT c.user_id, c.excluded_roles, c.reason, c.set_at, "
                "       u.full_name AS user_name "
                "FROM ethics_coi c "
                "LEFT JOIN users u ON u.id = c.user_id "
                "WHERE c.case_id = %s",
                (case_id,))
            for r in cursor.fetchall():
                cois.append({
                    'user_id':        r['user_id'],
                    'user_name':      r['user_name'] or f"User#{r['user_id']}",
                    'excluded_roles': r['excluded_roles'],
                    'reason':         r['reason'] or '',
                    'set_at':         fmt_datetime(r['set_at']),
                })

        # 担当委員長情報
        chair_name = None
        original_chair_name = None
        if case.get('chair_user_id'):
            chair_name = get_user_display_name(case['chair_user_id'])
        if case.get('original_chair_user_id'):
            original_chair_name = get_user_display_name(case['original_chair_user_id'])

        # ステータス履歴（事務局・admin のみ）
        status_history = []
        if is_admin(user_id) or is_secretariat(user_id):
            cursor.execute(
                "SELECT h.from_status, h.to_status, h.changed_at, h.note, "
                "       u.full_name AS user_name "
                "FROM ethics_status_history h "
                "LEFT JOIN users u ON u.id = h.changed_by_user_id "
                "WHERE h.case_id = %s "
                "ORDER BY h.changed_at ASC",
                (case_id,))
            for r in cursor.fetchall():
                status_history.append({
                    'from_status': r['from_status'],
                    'to_status':   r['to_status'],
                    'changed_at':  fmt_datetime(r['changed_at']),
                    'note':        r['note'] or '',
                    'user_name':   r['user_name'] or '',
                })

        result = {
            'success': True,
            'case': {
                'id':                    case['id'],
                'case_display_id':       case['case_display_id'],
                'fiscal_year':           case['fiscal_year'],
                'seq_no':                case['seq_no'],
                'applicant_user_id':     case['applicant_user_id'],
                'applicant_name':        case.get('applicant_name') or '',
                'applicant_affiliation': case.get('applicant_affiliation') or '',
                'title':                 case.get('title') or '',
                'research_period_start': fmt_date(case.get('research_period_start')),
                'research_period_end':   fmt_date(case.get('research_period_end')),
                'status':                case['status'],
                'status_label':          committee_status_label(case),
                'applicant_notified_at': fmt_datetime(case.get('applicant_notified_at')),
                'received_date':         fmt_date(case.get('received_date')),
                'final_date':            fmt_date(case.get('final_date')),
                'approved_date':         fmt_date(case.get('approved_date')),
                'chair_user_id':         case.get('chair_user_id'),
                'chair_name':            chair_name,
                'original_chair_name':   original_chair_name,
                'chair_change_reason':   case.get('chair_change_reason') or '',
                'decision_note':         case.get('decision_note') or '',
                'decision_at':           fmt_datetime(case.get('decision_at')),
                'resubmission_count':    case.get('resubmission_count', 0),
                'current_version':       current_version,
                'created_at':            fmt_datetime(case.get('created_at')),
                'updated_at':            fmt_datetime(case.get('updated_at')),
            },
            'documents':      documents,
            'reviewers':      reviewers,
            'cois':           cois,
            'status_history': status_history,
            'result_disclosed': is_result_disclosed(case),
            'today': get_jst_today().strftime('%Y-%m-%d'),
            'viewer_role': {
                'is_admin':          is_admin(user_id),
                'is_secretariat':    is_secretariat(user_id),
                'is_chair':          is_case_chair(user_id, case_id)
                                     and not is_chair_excluded(user_id, case_id),
                'is_reviewer':       is_assigned_reviewer(user_id, case_id)
                                     and not is_coi(user_id, case_id),
                'is_applicant':      is_app,
                'can_access_review': can_access_rt,
                'can_read_app_ch':   can_read_ac,
                'can_write_app_ch':  can_write_applicant_channel(user_id, case_id),
                'can_decide':        can_decide(user_id, case_id),
                'can_declare_na':    can_declare_not_applicable(user_id, case_id),
                'can_assign':        can_assign_reviewers(user_id, case_id),
            },
        }
        # 申請者としてしか閲覧できない者には、未通知の最終判定を伏せる
        # （事務局・担当委員長・担当委員・admin には実際の判定を見せる）
        if not can_access_rt:
            mask_undisclosed_result(result['case'], case)
        return jsonify(result)

    except Exception as e:
        logging.error("api_case_detail error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 申請内容の閲覧（変更A）
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/view', methods=['GET'])
@login_required
def api_case_view(case_id):
    """
    申請内容の閲覧専用API（変更A）。

    担当でない委員（委員プール・委員長プール）も含め、申請内容を
    守秘の上で閲覧できる。返すのは案件基本情報と提出書類のみ。

    審査トラックの議論・申請者チャンネル・担当委員一覧・COI・
    ステータス履歴は一切返さない（②の「審議過程は秘密」を守る）。
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_view_application_content(user_id, case_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        # 書類（全バージョン）
        current_version = case.get('resubmission_count', 0) + 1
        try:
            cursor.execute(
                "SELECT id, version, title, drive_url, note, "
                "       original_filename, file_size, uploaded_at "
                "FROM ethics_documents WHERE case_id = %s "
                "ORDER BY uploaded_at DESC, id DESC",
                (case_id,))
            doc_rows = cursor.fetchall()
            has_filename_col = True
        except mysql.connector.Error:
            cursor.execute(
                "SELECT id, version, title, drive_url, note, uploaded_at "
                "FROM ethics_documents WHERE case_id = %s "
                "ORDER BY uploaded_at DESC, id DESC",
                (case_id,))
            doc_rows = cursor.fetchall()
            has_filename_col = False

        # 申請内容の閲覧ページ：審査トラック添付（委員会側の資料）は
        # 審査トラックにアクセスできる者以外には見せない
        if not can_access_review_track(user_id, case_id):
            doc_rows = [r for r in doc_rows if not _is_review_track_document(r)]

        documents = []
        for r in doc_rows:
            display_filename = None
            if has_filename_col:
                display_filename = r.get('original_filename')
            if not display_filename and r.get('note'):
                m = re.match(r'^\[ファイル名:\s*([^\]]+)\]', r['note'] or '')
                if m:
                    display_filename = m.group(1).strip()
            download_url = None
            if r.get('drive_url'):
                download_url = url_for(
                    'ethics_review.api_download_document',
                    case_id=case_id, doc_id=r['id'])
            documents.append({
                'id':                r['id'],
                'version':           r['version'],
                'title':             r['title'],
                'note':              r.get('note') or '',
                'original_filename': display_filename or '',
                'file_size':         r.get('file_size') if has_filename_col else None,
                'download_url':      download_url,
                'source':            r.get('source') or 'application',
                'uploaded_at':       fmt_datetime(r['uploaded_at']),
                'is_current':        r['version'] == current_version,
            })

        # 申請内容のみ。担当委員・COI・履歴・議論は含めない。
        result = {
            'success': True,
            'case': {
                'id':                    case['id'],
                'case_display_id':       case['case_display_id'],
                'fiscal_year':           case['fiscal_year'],
                'seq_no':                case['seq_no'],
                'applicant_name':        case.get('applicant_name') or '',
                'applicant_affiliation': case.get('applicant_affiliation') or '',
                'title':                 case.get('title') or '',
                'research_period_start': fmt_date(case.get('research_period_start')),
                'research_period_end':   fmt_date(case.get('research_period_end')),
                'status':                case['status'],
                'status_label':          committee_status_label(case),
                'resubmission_count':    case.get('resubmission_count', 0),
                'current_version':       current_version,
                'created_at':            fmt_datetime(case.get('created_at')),
            },
            'documents': documents,
        }
        return jsonify(result)

    except Exception as e:
        logging.error("api_case_view error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

@ethics_review_bp.route('/api/cases', methods=['GET'])
@login_required
def api_cases_list():
    """
    案件一覧（権限に応じてフィルタ）
    - admin / 事務局：全件
    - 委員長プール：全件（任命権者として、自分が申請者の案件も含む）
    - 委員プール：担当（指名・担当委員長）案件のみ
    """
    user_id = session.get('user_id')
    fiscal_year = request.args.get('fiscal_year', type=int)
    status_filter = request.args.get('status')

    # 権限フィルタ
    where = []
    params = []
    if fiscal_year:
        where.append("c.fiscal_year = %s")
        params.append(fiscal_year)
    if status_filter:
        where.append("c.status = %s")
        params.append(status_filter)

    # admin / 事務局 / 委員長プール：全件
    # 委員プール：担当（指名・担当委員長）案件のみ。自分が申請者の案件は除く（暗黙のCOI）
    if not (is_secretariat(user_id) or is_chair_pool(user_id)):
        access_clause = (
            "(c.applicant_user_id <> %s AND "
            " (c.chair_user_id = %s OR "
            "  c.id IN (SELECT case_id FROM ethics_reviewers "
            "           WHERE user_id = %s AND assignment_status IN ('pending','accepted'))))"
        )
        where.append(access_clause)
        params.extend([user_id, user_id, user_id])

    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT c.*, "
            f"       au.full_name AS applicant_name_resolved, "
            f"       chu.full_name AS chair_name_resolved "
            f"FROM ethics_cases c "
            f"LEFT JOIN users au  ON au.id  = c.applicant_user_id "
            f"LEFT JOIN users chu ON chu.id = c.chair_user_id "
            f"{where_sql} "
            f"ORDER BY c.fiscal_year DESC, c.seq_no DESC",
            params)
        cases = []
        for r in cursor.fetchall():
            cases.append({
                'id':                  r['id'],
                'case_display_id':     r['case_display_id'],
                'fiscal_year':         r['fiscal_year'],
                'seq_no':              r['seq_no'],
                'applicant_name':      r.get('applicant_name') or r.get('applicant_name_resolved') or '',
                'applicant_affiliation': r.get('applicant_affiliation') or '',
                'title':               r.get('title') or '',
                'status':              r['status'],
                'status_label':        committee_status_label(r),
                'applicant_notified_at': fmt_datetime(r.get('applicant_notified_at')),
                'received_date':       fmt_date(r.get('received_date')),
                'final_date':          fmt_date(r.get('final_date')),
                'approved_date':       fmt_date(r.get('approved_date')),
                'chair_name':          r.get('chair_name_resolved') or '',
                'updated_at':          fmt_datetime(r.get('updated_at')),
            })
        return jsonify({'success': True, 'cases': cases})
    except Exception as e:
        logging.error("api_cases_list error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/my_cases', methods=['GET'])
@login_required
def api_my_cases():
    """申請者の自分の案件一覧"""
    user_id = session.get('user_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM ethics_cases "
            "WHERE applicant_user_id = %s "
            "ORDER BY fiscal_year DESC, seq_no DESC",
            (user_id,))
        cases = []
        for r in cursor.fetchall():
            cases.append(mask_undisclosed_result({
                'id':                r['id'],
                'case_display_id':   r['case_display_id'],
                'title':             r.get('title') or '',
                'status':            r['status'],
                'received_date':     fmt_date(r.get('received_date')),
                'final_date':        fmt_date(r.get('final_date')),
                'approved_date':     fmt_date(r.get('approved_date')),
                'updated_at':        fmt_datetime(r.get('updated_at')),
            }, r))
        return jsonify({'success': True, 'cases': cases})
    except Exception as e:
        logging.error("api_my_cases error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()
# ────────────────────────────────────────────
# API: 学内公開（最小限情報）
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/public/cases', methods=['GET'])
@login_required
def api_public_cases():
    """
    学内一般公開：最小限情報
    返す項目：受付番号、受付日、ステータス、最終日、最終状態
    """
    fiscal_year = request.args.get('fiscal_year', type=int)

    where = []
    params = []
    if fiscal_year:
        where.append("fiscal_year = %s")
        params.append(fiscal_year)
    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT case_display_id, status, "
            f"       received_date, final_date, applicant_notified_at "
            f"FROM ethics_cases "
            f"{where_sql} "
            f"ORDER BY fiscal_year DESC, seq_no DESC",
            params)
        items = []
        for r in cursor.fetchall():
            status     = r['status']
            # 申請者未通知の最終判定は学内にも出さない（通知待ちとして表示）
            if not is_result_disclosed(r):
                items.append({
                    'case_display_id': r['case_display_id'],
                    'received_date':   fmt_date(r.get('received_date')),
                    'status':          RESULT_PENDING_STATUS,
                    'status_label':    RESULT_PENDING_LABEL,
                    'is_active':       True,
                    'is_final':        False,
                    'final_date':      None,
                    'final_label':     None,
                })
                continue
            is_active  = status in ACTIVE_STATUSES
            is_final   = status in TERMINAL_STATUSES
            final_label = STATUS_LABELS.get(status) if is_final else None
            items.append({
                'case_display_id': r['case_display_id'],
                'received_date':   fmt_date(r.get('received_date')),
                'status':          status,
                'status_label':    STATUS_LABELS.get(status, status),
                'is_active':       is_active,
                'is_final':        is_final,
                'final_date':      fmt_date(r.get('final_date')),
                'final_label':     final_label,
            })
        return jsonify({'success': True, 'cases': items})
    except Exception as e:
        logging.error("api_public_cases error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/public/approved', methods=['GET'])
@login_required
def api_public_approved():
    """
    承認案件の公開：研究タイトル・申請者・所属・期間・承認日
    （承認確定後のみ。立命館大学方式に近い）
    """
    fiscal_year = request.args.get('fiscal_year', type=int)

    # 申請者へ通知済みの承認案件のみ公開する
    where = ["status = 'approved'", "applicant_notified_at IS NOT NULL"]
    params = []
    if fiscal_year:
        where.append("fiscal_year = %s")
        params.append(fiscal_year)
    where_sql = 'WHERE ' + ' AND '.join(where)

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT c.case_display_id, c.title, "
            f"       c.applicant_name, c.applicant_affiliation, "
            f"       c.research_period_start, c.research_period_end, "
            f"       c.approved_date, c.decision_note, "
            f"       chu.full_name AS chair_name, "
            f"       ochu.full_name AS original_chair_name, "
            f"       c.chair_change_reason "
            f"FROM ethics_cases c "
            f"LEFT JOIN users chu  ON chu.id  = c.chair_user_id "
            f"LEFT JOIN users ochu ON ochu.id = c.original_chair_user_id "
            f"{where_sql} "
            f"ORDER BY c.fiscal_year DESC, c.seq_no DESC",
            params)
        items = []
        for r in cursor.fetchall():
            items.append({
                'case_display_id':       r['case_display_id'],
                'title':                 r.get('title') or '',
                'applicant_name':        r.get('applicant_name') or '',
                'applicant_affiliation': r.get('applicant_affiliation') or '',
                'research_period_start': fmt_date(r.get('research_period_start')),
                'research_period_end':   fmt_date(r.get('research_period_end')),
                'approved_date':         fmt_date(r.get('approved_date')),
                'decision_note':         r.get('decision_note') or '',
                'chair_name':            r.get('chair_name') or '',
                'original_chair_name':   r.get('original_chair_name') or '',
                'chair_change_reason':   r.get('chair_change_reason') or '',
            })
        return jsonify({'success': True, 'cases': items})
    except Exception as e:
        logging.error("api_public_approved error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 年度報告（事務局向け）
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/report', methods=['GET'])
@login_required
def api_report():
    """事務局向け年度報告"""
    user_id = session.get('user_id')
    if not is_secretariat(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    fy = request.args.get('fiscal_year', type=int) or get_current_fiscal_year()

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT c.*, "
            "       chu.full_name AS chair_name "
            "FROM ethics_cases c "
            "LEFT JOIN users chu ON chu.id = c.chair_user_id "
            "WHERE c.fiscal_year = %s "
            "ORDER BY c.seq_no ASC",
            (fy,))
        cases = []
        summary = {
            'total':                0,
            'approved':             0,
            'rejected':             0,
            'not_applicable':       0,
            'withdrawn':            0,
            'terminated':           0,
            'in_review':            0,
            'conditional_approval': 0,
            'review_continuation':  0,
        }
        cases_raw = cursor.fetchall()
        for r in cases_raw:
            summary['total'] += 1
            s = r['status']
            if s == 'approved':              summary['approved']             += 1
            elif s == 'rejected':            summary['rejected']             += 1
            elif s == 'not_applicable':      summary['not_applicable']       += 1
            elif s == 'withdrawn':           summary['withdrawn']            += 1
            elif s == 'terminated':          summary['terminated']           += 1
            elif s == 'conditional_approval':summary['conditional_approval'] += 1
            elif s == 'review_continuation': summary['review_continuation']  += 1
            elif s in ACTIVE_STATUSES:       summary['in_review']            += 1

            cases.append({
                'id':                    r['id'],
                'case_display_id':       r['case_display_id'],
                'title':                 r.get('title') or '',
                'applicant_name':        r.get('applicant_name') or '',
                'applicant_affiliation': r.get('applicant_affiliation') or '',
                'status':                r['status'],
                'status_label':          committee_status_label(r),
                'received_date':         fmt_date(r.get('received_date')),
                'final_date':            fmt_date(r.get('final_date')),
                'approved_date':         fmt_date(r.get('approved_date')),
                'chair_name':            r.get('chair_name') or '',
                'research_period_start': fmt_date(r.get('research_period_start')),
                'research_period_end':   fmt_date(r.get('research_period_end')),
            })

        return jsonify({
            'success':     True,
            'fiscal_year': fy,
            'summary':     summary,
            'cases':       cases,
        })
    except Exception as e:
        logging.error("api_report error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


@ethics_review_bp.route('/api/report/csv', methods=['GET'])
@login_required
def api_report_csv():
    """年度報告CSV出力"""
    user_id = session.get('user_id')
    if not is_secretariat(user_id):
        return Response('権限がありません', status=403)

    fy = request.args.get('fiscal_year', type=int) or get_current_fiscal_year()

    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.*, "
            "       chu.full_name AS chair_name "
            "FROM ethics_cases c "
            "LEFT JOIN users chu ON chu.id = c.chair_user_id "
            "WHERE c.fiscal_year = %s "
            "ORDER BY c.seq_no ASC",
            (fy,))
        rows = cursor.fetchall()

        buf = io.StringIO()
        buf.write('\ufeff')  # BOM for Excel
        w = csv.writer(buf)
        w.writerow([
            '案件ID', '受付日', '研究課題名',
            '申請者', '所属', '研究期間（開始）', '研究期間（終了）',
            'ステータス', '最終日', '承認日', '担当委員長', '所見'
        ])
        for r in rows:
            w.writerow([
                r['case_display_id'],
                fmt_date(r.get('received_date')),
                r.get('title') or '',
                r.get('applicant_name') or '',
                r.get('applicant_affiliation') or '',
                fmt_date(r.get('research_period_start')),
                fmt_date(r.get('research_period_end')),
                STATUS_LABELS.get(r['status'], r['status']),
                fmt_date(r.get('final_date')),
                fmt_date(r.get('approved_date')),
                r.get('chair_name') or '',
                r.get('decision_note') or '',
            ])

        filename = f'ethics_review_{fy}.csv'
        return Response(
            buf.getvalue(),
            mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        logging.error("api_report_csv error: %s", e)
        return Response(str(e), status=500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# API: 担当委員・担当委員長 候補リスト
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/case/<int:case_id>/reviewer_candidates',
                        methods=['GET'])
@login_required
def api_reviewer_candidates(case_id):
    """
    指名できる候補のリスト（担当委員長候補／担当委員候補を分けて返す）
    委員長プール所属者・事務局・admin が実行可能。
    - chair_candidates: 担当委員長として指名可（委員長プール ∪ 委員プール、COIなし、申請者除く、現任除く）
    - committee_candidates: 担当委員として指名可（委員プール、COIなし、申請者除く、既指名除く）

    クエリパラメータ:
      mode=chair       : 担当委員長候補のみ返す
      mode=committee   : 担当委員候補のみ返す
      （省略時：両方返す）
    """
    user_id = session.get('user_id')
    case = get_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': '案件が見つかりません'}), 404
    if not can_assign_reviewers(user_id, case_id):
        return jsonify({
            'success': False,
            'error':   '権限がありません（担当委員長／委員長プール／事務局／admin のみ）'
        }), 403

    mode = request.args.get('mode')  # 'chair' / 'committee' / None

    try:
        coi_ids          = set(get_coi_user_ids(case_id))           # committee のみ
        chair_excl_ids   = set(get_chair_excluded_user_ids(case_id)) # committee + chair_only
        applicant_id     = case['applicant_user_id']
        assigned_ids     = set(get_assigned_reviewer_ids(case_id))
        current_chair    = case.get('chair_user_id')

        chair_pool_ids     = set(get_group_member_ids(GROUP_CHAIR))
        committee_pool_ids = set(get_group_member_ids(GROUP_COMMITTEE)) | chair_pool_ids

        # 担当委員長候補：委員長プール ∪ 委員プール（通常は委員長プールから
        # 指名するが、委員長プール1名運用でCOI等のため、委員プールからも
        # 担当委員長を指名できる）。申請者除く、COI（committee/chair_only両方）除く。
        chair_cand_ids = set(committee_pool_ids)
        chair_cand_ids.discard(applicant_id)
        # 担当委員長就任不可（committee も chair_only も）を除外
        for uid in chair_excl_ids:
            chair_cand_ids.discard(uid)
        # 現任の担当委員長は候補からは外す（同一者の再指名は意味がない）
        if current_chair:
            chair_cand_ids.discard(current_chair)

        # 担当委員候補：委員プール（委員長プール含む）所属、申請者除く、
        # COI(committee)除く、既指名除く
        committee_cand_ids = set(committee_pool_ids)
        committee_cand_ids.discard(applicant_id)
        for uid in coi_ids:
            committee_cand_ids.discard(uid)
        for uid in assigned_ids:
            committee_cand_ids.discard(uid)
        # 現任の担当委員長は担当委員候補から除外（兼務不可）
        if current_chair:
            committee_cand_ids.discard(current_chair)

        if mode == 'chair':
            committee_cand_ids = set()
        elif mode == 'committee':
            chair_cand_ids = set()

        all_ids = chair_cand_ids | committee_cand_ids
        users_by_id = {}
        if all_ids:
            conn   = mysql.connector.connect(**DatabaseConfig.default())
            cursor = conn.cursor(dictionary=True)
            fmt = ','.join(['%s'] * len(all_ids))
            cursor.execute(
                f"SELECT id, full_name AS name, email "
                f"FROM users WHERE id IN ({fmt}) "
                f"ORDER BY name",
                list(all_ids))
            for r in cursor.fetchall():
                users_by_id[r['id']] = {
                    'user_id': r['id'],
                    'name':    r['name'],
                    'email':   r.get('email') or '',
                }
            cursor.close(); conn.close()

        def sort_users(ids):
            return sorted(
                [users_by_id[i] for i in ids if i in users_by_id],
                key=lambda x: x['name'] or ''
            )

        return jsonify({
            'success': True,
            'chair_candidates':     sort_users(chair_cand_ids),
            'committee_candidates': sort_users(committee_cand_ids),
            # 後方互換：従来の candidates キー（担当委員候補と等価）
            'candidates':           sort_users(committee_cand_ids),
        })

    except Exception as e:
        logging.error("api_reviewer_candidates error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ────────────────────────────────────────────
# API: 自分宛の指名一覧（応諾／辞退用）
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/my_assignments', methods=['GET'])
@login_required
def api_my_assignments():
    """自分が指名されている案件一覧（pending含む）"""
    user_id = session.get('user_id')
    try:
        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT r.case_id, r.assignment_status, r.assignment_message, "
            "       r.assigned_at, r.responded_at, "
            "       c.case_display_id, c.title, c.status AS case_status, "
            "       chu.full_name AS chair_name "
            "FROM ethics_reviewers r "
            "JOIN ethics_cases c ON c.id = r.case_id "
            "LEFT JOIN users chu ON chu.id = c.chair_user_id "
            "WHERE r.user_id = %s AND c.applicant_user_id <> %s "
            "ORDER BY r.assigned_at DESC",
            (user_id, user_id))
        items = []
        for r in cursor.fetchall():
            items.append({
                'case_id':            r['case_id'],
                'case_display_id':    r['case_display_id'],
                'title':              r.get('title') or '',
                'case_status':        r['case_status'],
                'assignment_status':  r['assignment_status'],
                'assignment_message': r.get('assignment_message') or '',
                'chair_name':         r.get('chair_name') or '',
                'assigned_at':        fmt_datetime(r.get('assigned_at')),
                'responded_at':       fmt_datetime(r.get('responded_at')),
            })
        return jsonify({'success': True, 'assignments': items})
    except Exception as e:
        logging.error("api_my_assignments error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()

# ────────────────────────────────────────────
# API: 恒常メンバー一覧（事務局パネル用）
# ────────────────────────────────────────────

@ethics_review_bp.route('/api/pool_members', methods=['GET'])
@login_required
def api_pool_members():
    """
    恒常的審査委員会メンバー（委員長プール／委員プール）を分けて返す。
    閲覧者：委員長プール／事務局／admin／代理委員長（体制運営パネルの閲覧者）
    """
    user_id = session.get('user_id')
    if not can_view_secretariat_panel(user_id):
        return jsonify({'success': False, 'error': '権限がありません'}), 403

    try:
        chair_ids     = set(get_group_member_ids(GROUP_CHAIR))
        committee_ids = set(get_group_member_ids(GROUP_COMMITTEE))
        # 委員プール表示には委員長プールも含めない（区別したいため）
        # → 純粋な「委員プールのみ」 = COMMITTEE - CHAIR
        committee_only_ids = committee_ids - chair_ids

        all_ids = chair_ids | committee_only_ids
        if not all_ids:
            return jsonify({
                'success': True,
                'chair_members':     [],
                'committee_members': [],
            })

        conn   = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        fmt = ','.join(['%s'] * len(all_ids))
        cursor.execute(
            f"SELECT id, full_name AS name, email "
            f"FROM users WHERE id IN ({fmt}) "
            f"ORDER BY name",
            list(all_ids))
        users_by_id = {}
        for r in cursor.fetchall():
            users_by_id[r['id']] = {
                'user_id': r['id'],
                'name':    r['name'],
                'email':   r.get('email') or '',
            }

        def to_list(ids):
            return sorted(
                [users_by_id[i] for i in ids if i in users_by_id],
                key=lambda x: x['name'] or ''
            )

        return jsonify({
            'success': True,
            'chair_members':     to_list(chair_ids),
            'committee_members': to_list(committee_only_ids),
        })

    except Exception as e:
        logging.error("api_pool_members error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close(); conn.close()


# ────────────────────────────────────────────
# 様式ダウンロード
# ────────────────────────────────────────────

@ethics_review_bp.route('/forms')
@login_required
def forms():
    """申請様式のダウンロードページ。

    学内ユーザーであれば誰でも開ける（申請は誰でもできるため）。
    様式の実体は ETHICS_FORMS_ROOT（data_for_distribution/forms/）に置く。
    審査で提出された書類（ETHICS_DOCS_ROOT）とは別のディレクトリ。
    """
    return render_template(
        'ethics_review/forms.html',
        forms=ETHICS_FORMS,
        regulation_url=ETHICS_REGULATION_URL,
        regulation_title=ETHICS_REGULATION_TITLE,
    )


@ethics_review_bp.route('/forms/<code>/download')
@login_required
def download_form(code):
    """申請様式のダウンロード。code は ETHICS_FORMS の code と一致するもののみ。"""
    entry = next((f for f in ETHICS_FORMS if f['code'] == code), None)
    if not entry:
        return abort(404)

    abs_path = os.path.join(ETHICS_FORMS_ROOT, entry['filename'])
    # 念のためのパストラバーサル検査（filename は定数だが、将来の編集ミスに備える）
    root_real   = os.path.realpath(ETHICS_FORMS_ROOT)
    target_real = os.path.realpath(abs_path)
    if not target_real.startswith(root_real + os.sep):
        return abort(404)
    if not os.path.exists(abs_path):
        logging.error("ethics_review: form file not found: %s", abs_path)
        return abort(404)

    resp = send_file(abs_path, mimetype='application/octet-stream',
                     conditional=True)
    from urllib.parse import quote
    encoded_name = quote(entry['download'])
    resp.headers['Content-Disposition'] = (
        f"attachment; filename*=UTF-8''{encoded_name}"
    )
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp



@ethics_review_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()
