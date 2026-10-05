"""
まちなか (machinaka) キャンパス – Blueprint routes

【権限モデル】
  admin ユーザ (user_category='admin') は設定にかかわらず全権限を持つ。

  view    : 公開日前プレビュー閲覧。'__public__' 設定で匿名閲覧可。
  apply   : 常駐職員。イベント申請＋日報の作成・編集。
  approve : 地域連携係。承認・管理・指示。
  inspect : 日報の「確認しました」を押せる点検担当。
            approve 権限を持ち、かつ machinaka_inspectors に active 登録
            されているユーザのみ。

【アクセス設定の保存方式】
  machinaka_access_settings にグループ名 (group_name) で保存する。
  グループ ID が変わっても影響を受けない (まいぐる移行対応)。

【日報のルール】
  * 1 日 1 レコード (UNIQUE KEY report_date)
  * 過去遡及可（開館記録を後から書くケース）
  * 開館状況 (open_status)
      open    : 通常開館 (open_time / close_time 必須)
      closed  : 全日休館 (時刻 NULL 可)
      partial : 部分休館 (例: 午後だけ休館; open_time / close_time +
                closure_note で記述)
  * visitor_count は固定の整数フィールド
  * 点検されている日報を編集すると、inspected_by / inspected_at は
    クリアされ再点検が必要になる。
"""

import datetime
from datetime import timedelta
import calendar as cal_module
import os
from flask import (render_template, request, redirect, url_for,
                   session, flash, abort, jsonify, send_from_directory)
from functools import wraps
from pytz import timezone
import mysql.connector
from werkzeug.utils import secure_filename
from decorators import login_required
from auth import redirect_to_dashboard

# 画像アップロード設定
# 保存先は ~/static/machinaka_imgs/。1415行目が返す URL
#   /static/machinaka_imgs/<file>
# と一致させること（片方だけ変えるとアップロードが壊れる）。
# 日報・イベントに貼る画像はアプリ配下に置く（プラットフォーム規約．GitHub には送られない）．
# 配信は image ルート（/machinaka/img/<名前>）が行う．旧版は ~/static/machinaka_imgs/ に置いていた．
MACHINAKA_UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'machinaka_imgs')
MACHINAKA_LEGACY_UPLOAD_FOLDER = os.path.join(os.path.expanduser('~'), 'static', 'machinaka_imgs')
MACHINAKA_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def _machinaka_allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in MACHINAKA_ALLOWED_EXTENSIONS


try:
    from markdown_converter import process_markdown_for_preview
except ImportError:
    def process_markdown_for_preview(text):   # フォールバック
        return text

from db import DatabaseConfig
from . import machinaka_bp

JST = timezone('Asia/Tokyo')

def get_jst_now():
    return datetime.datetime.now(JST).replace(tzinfo=None)

# ─────────────────────────────────────────────────────────────────
# アクセスログ記録
# ─────────────────────────────────────────────────────────────────
def _log_access(endpoint: str, ref_event_id=None, ref_report_id=None):
    """
    machinaka_access_logs に1行記録。例外は握りつぶしてアプリを止めない。
    """
    try:
        ip  = (request.headers.get('X-Forwarded-For', '') or '').split(',')[0].strip() \
              or request.remote_addr
        ua  = (request.user_agent.string or '')[:512]
        uid = current_uid()
        now = get_jst_now()

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO machinaka_access_logs
              (accessed_at, endpoint, user_id, ip_address, user_agent,
               ref_event_id, ref_report_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (now, endpoint, uid, ip, ua, ref_event_id, ref_report_id))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()
        except Exception:
            pass

# 匿名公開マーカー
PUBLIC_SENTINEL = '__public__'


# ─────────────────────────────────────────────────────────────────
# Jinja2 フィルタ
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.app_template_filter('fmtdate_m')
def fmtdate(d, fmt='%Y/%m/%d'):
    if d is None:
        return ''
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    return d.strftime(fmt)


@machinaka_bp.app_template_filter('fmtdatetime_m')
def fmtdatetime(dt, fmt='%Y/%m/%d %H:%M'):
    if dt is None:
        return ''
    return dt.strftime(fmt)


@machinaka_bp.app_template_filter('fmttime_m')
def fmttime(t, fmt='%H:%M'):
    if t is None:
        return ''
    if isinstance(t, datetime.timedelta):
        total = int(t.total_seconds())
        return f"{total//3600:02d}:{(total%3600)//60:02d}"
    if isinstance(t, str):
        return t[:5] if len(t) >= 5 else t
    return t.strftime(fmt)


# ─────────────────────────────────────────────────────────────────
# 月カレンダー構築
# ─────────────────────────────────────────────────────────────────
def build_month_calendar(events, reports_by_date, year, month):
    """
    月カレンダー構築（日曜始まり）。
    各セルに:
        day          : 日
        event_count  : イベント数
        has_report   : 日報あり
        inspected    : 点検済
    を保持する。
    """
    day_events = {}
    for e in events:
        start = e['event_start_date']
        end   = e['event_end_date']
        if isinstance(start, str):
            start = datetime.date.fromisoformat(start)
        if isinstance(end, str):
            end = datetime.date.fromisoformat(end)
        d = start
        while d <= end:
            if d.year == year and d.month == month:
                day_events[d.day] = day_events.get(d.day, 0) + 1
            d += timedelta(days=1)

    first_day    = datetime.date(year, month, 1)
    first_offset = (first_day.weekday() + 1) % 7  # 0=日
    last_day     = cal_module.monthrange(year, month)[1]

    flat = [None] * first_offset + list(range(1, last_day + 1))
    while len(flat) % 7:
        flat.append(None)

    weeks = []
    for i in range(0, len(flat), 7):
        week = []
        for d in flat[i:i+7]:
            if d:
                dt = datetime.date(year, month, d)
                rep = reports_by_date.get(dt)
                week.append({
                    'day':         d,
                    'event_count': day_events.get(d, 0),
                    'has_report':  rep is not None,
                    'inspected':   rep is not None and rep.get('inspected_at') is not None,
                })
            else:
                week.append({'day': None})
        weeks.append(week)
    return weeks


# ─────────────────────────────────────────────────────────────────
# 権限チェック
# ─────────────────────────────────────────────────────────────────
def is_admin_user():
    return session.get('user_category') == 'admin'


def current_uid():
    return session.get('user_id')


def _ug(name):
    """まいぐる（ユーザとグループ）の公開APIを遅延で引く．無ければ None．"""
    try:
        from fujinp.user_groups import utils as _ug_utils
        return getattr(_ug_utils, name, None)
    except Exception:
        return None


def user_group_names(user_id):
    """現在所属するグループ名．まいぐるの公開API（台帳由来のグループも含む）を使い，
       古いまいぐるで API が無いときだけ user_group_memberships を直接読む．"""
    if not user_id:
        return []
    fn = _ug('get_user_group_names')
    if fn:
        try:
            return list(fn(user_id) or [])
        except Exception:
            pass
    try:
        now  = get_jst_now()
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT g.name
              FROM user_group_memberships m
              JOIN user_groups g ON g.id = m.group_id
             WHERE m.user_id = %s
               AND (m.valid_from  IS NULL OR m.valid_from  <= %s)
               AND (m.valid_until IS NULL OR m.valid_until >= %s)
        """, (user_id, now, now))
        return [r['name'] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def access_group_names(access_type):
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT group_name FROM machinaka_access_settings
             WHERE access_type = %s
        """, (access_type,))
        return [r['group_name'] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def is_registered_inspector(user_id):
    """machinaka_inspectors に active 登録されているか"""
    if not user_id:
        return False
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            SELECT 1 FROM machinaka_inspectors
             WHERE user_id=%s AND active=1
             LIMIT 1
        """, (user_id,))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────
# まちなか管理者（ハードコード）
#
# /settings と /inspectors にアクセスできるのは、
#   * admin ユーザ、または
#   * まいぐるで下記 MANAGER_GROUP に所属しているユーザ
# のみ。このグループの所属メンバーの変更は admin またはまいぐる総管理者が行う。
# ─────────────────────────────────────────────────────────────────
MANAGER_GROUP = 'まちなか管理'


def is_machinaka_manager(user_id=None):
    """admin か 'まちなか管理' グループに所属しているかを判定"""
    if is_admin_user():
        return True
    if user_id is None:
        user_id = current_uid()
    if not user_id:
        return False
    return MANAGER_GROUP in user_group_names(user_id)


def require_manager(f):
    """デコレータ: まちなか管理者以外は 403"""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not is_machinaka_manager(current_uid()):
            abort(403)
        return f(*args, **kwargs)
    return wrapped


def has_perm(access_type, user_id=None):
    """
    汎用権限チェック。
    * view    : view_policy (public/domestic/private/group) で判定
    * inspect : machinaka_inspectors に active 登録されていれば True
    * その他  : access_settings の group_name 一致で判定
    """
    if is_admin_user():
        return True

    if access_type == 'view':
        return _has_view_perm(user_id)

    if access_type == 'apply':
        return _has_apply_perm(user_id)

    if access_type == 'approve':
        if not user_id:
            return False
        return is_registered_receptionist(user_id)

    if access_type == 'inspect':
        if not user_id:
            return False
        return is_registered_inspector(user_id)

    allowed = access_group_names(access_type)
    if PUBLIC_SENTINEL in allowed:
        return True
    if not user_id:
        return False
    return bool(set(user_group_names(user_id)) & set(allowed))


def require_perm(access_type):
    def deco(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not has_perm(access_type, current_uid()):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return deco


# ─────────────────────────────────────────────────────────────────
# view ポリシー
#
# view は文書アーカイブと同じく 4 択のポリシーで管理する。まちなか
# 全体に対する一斉設定（個別イベント単位の設定は今後の拡張）。
#
#   private  - admin のみ閲覧可（テスト中にまず管理者だけで点検する段階）
#   group    - 指定グループ所属者 + admin
#   domestic - user_category='regular' のユーザ + admin
#   public   - 誰でも（ログインなしの匿名閲覧含む）
#
# ストレージ方式：machinaka_access_settings テーブルを再利用する。
#   access_type='view', group_name='__policy_<name>__' の行で現在の
#   ポリシーを表現。group ポリシーの場合、追加で通常の group_name 行を
#   複数持つ（それが許可グループのリスト）。
# ─────────────────────────────────────────────────────────────────
VIEW_POLICY_PREFIX  = '__policy_'
VIEW_POLICY_SUFFIX  = '__'
VALID_VIEW_POLICIES = ('private', 'group', 'domestic', 'public')
DEFAULT_VIEW_POLICY = 'private'

VALID_APPLY_POLICIES = ('private', 'group', 'domestic')  # public は当面不可
DEFAULT_APPLY_POLICY = 'private'

def _policy_marker(policy):
    return f'{VIEW_POLICY_PREFIX}{policy}{VIEW_POLICY_SUFFIX}'


def _is_policy_marker(group_name):
    return group_name.startswith(VIEW_POLICY_PREFIX) and group_name.endswith(VIEW_POLICY_SUFFIX)


def get_view_policy():
    """現在の view ポリシー名を返す。未設定の場合は DEFAULT_VIEW_POLICY。"""
    for gname in access_group_names('view'):
        if _is_policy_marker(gname):
            policy = gname[len(VIEW_POLICY_PREFIX):-len(VIEW_POLICY_SUFFIX)]
            if policy in VALID_VIEW_POLICIES:
                return policy
    return DEFAULT_VIEW_POLICY

def get_apply_policy():
    """現在の apply ポリシー名を返す。"""
    for gname in access_group_names('apply'):
        if _is_policy_marker(gname):
            policy = gname[len(VIEW_POLICY_PREFIX):-len(VIEW_POLICY_SUFFIX)]
            if policy in VALID_APPLY_POLICIES:
                return policy
    return DEFAULT_APPLY_POLICY

def get_apply_allowed_groups():
    """group ポリシー時の許可グループ名一覧。"""
    return [g for g in access_group_names('apply') if not _is_policy_marker(g)]

def get_view_allowed_groups():
    """group ポリシー時の許可グループ名一覧（マーカー以外）。"""
    return [g for g in access_group_names('view') if not _is_policy_marker(g)]


def _has_view_perm(user_id):
    """現在の view ポリシーに照らしてユーザが閲覧権限を持つか判定（非 admin 用）。"""
    policy = get_view_policy()

    if policy == 'private':
        return False

    if policy == 'public':
        return True   # 匿名も含め誰でも

    if policy == 'domestic':
        return session.get('user_category') == 'regular'

    if policy == 'group':
        if not user_id:
            return False
        allowed = set(get_view_allowed_groups())
        if not allowed:
            return False
        return bool(set(user_group_names(user_id)) & allowed)

    return False

def _has_apply_perm(user_id):
    """apply ポリシーに照らして申請権限を持つか判定（非 admin 用）。"""
    policy = get_apply_policy()

    if policy == 'private':
        return False

    if policy == 'domestic':
        return session.get('user_category') == 'regular'

    if policy == 'group':
        if not user_id:
            return False
        allowed = set(get_apply_allowed_groups())
        if not allowed:
            return False
        return bool(set(user_group_names(user_id)) & allowed)

    return False

# ─────────────────────────────────────────────────────────────────
# approve ポリシー（receptionist 方式）
# ─────────────────────────────────────────────────────────────────

def is_registered_receptionist(user_id):
    """machinaka_receptionists に active 登録されているか"""
    if not user_id:
        return False
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            SELECT 1 FROM machinaka_receptionists
             WHERE user_id=%s AND active=1
             LIMIT 1
        """, (user_id,))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────
# 可視性ロジック
# ─────────────────────────────────────────────────────────────────
def eff_public_date(event):
    return event.get('approver_public_date') or event.get('applicant_public_date')


VISIBLE_STATUSES = ('approved', 'cancelled', 'postponed')


def is_public_anonymous(event):
    """
    匿名公開対象か判定。
    view ポリシーが public かつ、ステータスが公開対象、かつ実効公開日が今日以前。
    """
    if get_view_policy() != 'public':
        return False
    if event['status'] not in VISIBLE_STATUSES:
        return False
    pd = eff_public_date(event)
    return pd is not None and pd <= datetime.date.today()


def can_view(event, user_id=None):
    if is_admin_user():
        return True
    if user_id:
        if event.get('applicant_id') == user_id:
            return True
        if has_perm('approve', user_id):
            return True
    if is_public_anonymous(event):
        return True
    if event['status'] in VISIBLE_STATUSES and has_perm('view', user_id):
        return True
    return False


# ─────────────────────────────────────────────────────────────────
# ラベル・装飾
# ─────────────────────────────────────────────────────────────────
STATUS_META = {
    'pending':   ('申請中', '#f59e0b'),
    'approved':  ('承認済', '#10b981'),
    'rejected':  ('不承認', '#ef4444'),
    'withdrawn': ('取下済', '#6b7280'),
    'cancelled': ('中止',   '#dc2626'),
    'postponed': ('延期',   '#7c3aed'),
}

LOC_LABEL = {'none': 'なし', 'partial': '一部専有', 'full': '全部専有'}

OPEN_STATUS_META = {
    'open':    ('通常開館', '#10b981'),
    'closed':  ('全日休館', '#6b7280'),
    'partial': ('部分休館', '#f59e0b'),
}


def enrich_event(event):
    st = event.get('status', 'pending')
    event['status_label'], event['status_color'] = STATUS_META.get(st, (st, '#888'))
    event['location_label']      = LOC_LABEL.get(event.get('location_use', 'none'), '')
    event['eff_public_date']     = eff_public_date(event)
    event['is_public_anonymous'] = is_public_anonymous(event)
    return event


def enrich_report(r):
    st = r.get('open_status', 'open')
    r['open_status_label'], r['open_status_color'] = OPEN_STATUS_META.get(st, (st, '#888'))
    r['is_inspected'] = r.get('inspected_at') is not None
    return r


# ─────────────────────────────────────────────────────────────────
# ダッシュボード
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJIN-Pダッシュボードに戻る"""
    return redirect_to_dashboard()


@machinaka_bp.route('/')
@login_required
def index():
    """アプリのトップ（ダッシュボード）．"""
    _log_access('dashboard')
    uid        = current_uid()
    apply_ok   = has_perm('apply', uid)
    approve_ok = has_perm('approve', uid)
    inspect_ok = has_perm('inspect', uid)

    mode       = request.args.get('mode', 'week')
    anchor_str = request.args.get('anchor', datetime.date.today().isoformat())
    try:
        anchor = datetime.date.fromisoformat(anchor_str)
    except ValueError:
        anchor = datetime.date.today()
    today = datetime.date.today()

    if mode == 'day':
        period_start = anchor
        period_end   = anchor
        prev_anchor  = anchor - timedelta(days=1)
        next_anchor  = anchor + timedelta(days=1)
        period_label = anchor.strftime('%Y年%m月%d日')
    elif mode == 'month':
        period_start = anchor.replace(day=1)
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year+1, month=1, day=1) - timedelta(days=1)
        else:
            period_end = period_start.replace(month=period_start.month+1, day=1) - timedelta(days=1)
        prev_anchor  = (period_start - timedelta(days=1)).replace(day=1)
        next_anchor  = period_end + timedelta(days=1)
        period_label = anchor.strftime('%Y年%m月')
    else:  # week
        weekday      = anchor.weekday()
        period_start = anchor - timedelta(days=weekday)
        period_end   = period_start + timedelta(days=6)
        prev_anchor  = period_start - timedelta(days=7)
        next_anchor  = period_start + timedelta(days=7)
        period_label = (f"{period_start.strftime('%Y年%m月%d日')}〜"
                        f"{period_end.strftime('%m月%d日')}")

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT e.*, u.full_name AS applicant_name
              FROM machinaka_events e
              LEFT JOIN users u ON u.id = e.applicant_id
             WHERE e.event_start_date <= %s
               AND e.event_end_date   >= %s
             ORDER BY e.event_start_date, e.event_end_date
        """, (period_end, period_start))
        events_raw = cur.fetchall()

        cur.execute("""
            SELECT e.*, u.full_name AS applicant_name
              FROM machinaka_events e
              LEFT JOIN users u ON u.id = e.applicant_id
             WHERE e.event_end_date < %s
             ORDER BY e.event_end_date DESC
             LIMIT 30
        """, (period_start,))
        past_raw = cur.fetchall()

        # 日報（ダッシュボード期間）
        cur.execute("""
            SELECT r.*, u.full_name AS staff_name,
                   i.full_name AS inspector_name
              FROM machinaka_daily_reports r
              LEFT JOIN users u ON u.id = r.staff_id
              LEFT JOIN users i ON i.id = r.inspected_by
             WHERE r.report_date BETWEEN %s AND %s
             ORDER BY r.report_date DESC
        """, (period_start, period_end))
        reports_raw = cur.fetchall()

        cur.execute("""
            SELECT id, title, body, published_at
              FROM machinaka_messages
             WHERE DATE(published_at) BETWEEN %s AND %s
             ORDER BY published_at
        """, (period_start, period_end))
        messages_raw = cur.fetchall()

    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        events_raw, past_raw, reports_raw = [], [], []
        messages_raw = []   # ★例外時の初期化も追加
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    events = [enrich_event(e) for e in events_raw if can_view(e, uid)]
    past   = [enrich_event(e) for e in past_raw   if can_view(e, uid)]
    reports = [enrich_report(r) for r in reports_raw]

    # ── ★ここに timeline マージを追加 ──
    timeline_items = []
    for e in events:
        timeline_items.append({'type': 'event', 'date': e['event_start_date'], 'obj': e})
    for m in messages_raw:
        d = m['published_at'].date() if hasattr(m['published_at'], 'date') else m['published_at']
        timeline_items.append({'type': 'message', 'date': d, 'obj': m})
    timeline_items.sort(key=lambda x: x['date'])

    reports_by_date = {r['report_date']: r for r in reports}

    # 公演データ
    all_event_ids = [e['id'] for e in events + past]
    perf_map = {}
    if all_event_ids:
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor(dictionary=True)
            fmt  = ','.join(['%s'] * len(all_event_ids))
            cur.execute(f"""
                SELECT event_id, start_datetime, end_datetime, note
                  FROM machinaka_performances
                 WHERE event_id IN ({fmt})
                 ORDER BY start_datetime
            """, all_event_ids)
            for p in cur.fetchall():
                try:
                    p['note_html'] = process_markdown_for_preview(p['note']) if p['note'] else ''
                except Exception:
                    p['note_html'] = p['note'] or ''
                perf_map.setdefault(p['event_id'], []).append(p)
        except Exception:
            pass
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    for e in events + past:
        e['performances'] = perf_map.get(e['id'], [])

    cal_weeks = build_month_calendar(events, reports_by_date,
                                     anchor.year, anchor.month) if mode == 'month' else None

    # 今日の日報状態（簡易表示）
    today_report = reports_by_date.get(today)

    return render_template('machinaka_dashboard.html',
        events=events, past=past,
        reports=reports,
        today_report=today_report,
        cal_weeks=cal_weeks,
        mode=mode, anchor=anchor,
        period_label=period_label,
        prev_anchor=prev_anchor, next_anchor=next_anchor,
        today=today,
        uid=uid,
        apply_ok=apply_ok,
        approve_ok=approve_ok,
        inspect_ok=inspect_ok,
        manager_ok=is_machinaka_manager(uid),
        timeline_items=timeline_items,          # ★追加
        now_str=get_jst_now().strftime('%Y-%m-%dT%H:%M'))  # ★追加


# ─────────────────────────────────────────────────────────────────
# イベント詳細
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/event/<int:eid>')
def event_detail(eid):
    _log_access('event_detail', ref_event_id=eid)   # ← 追加
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT e.*,
                   u.full_name AS applicant_name,
                   a.full_name AS approver_name
              FROM machinaka_events e
              LEFT JOIN users u ON u.id = e.applicant_id
              LEFT JOIN users a ON a.id = e.approver_id
             WHERE e.id = %s
        """, (eid,))
        event = cur.fetchone()
        if not event:
            abort(404)
        cur.execute("""
            SELECT * FROM machinaka_performances
             WHERE event_id = %s ORDER BY start_datetime
        """, (eid,))
        performances = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not can_view(event, uid):
        abort(403)

    enrich_event(event)
    today        = datetime.date.today()
    pub_date     = eff_public_date(event)
    past_public  = pub_date is not None and pub_date <= today
    is_applicant = uid is not None and event['applicant_id'] == uid
    is_approver  = has_perm('approve', uid)

    detail_html = ''
    if event.get('detail'):
        try:
            detail_html = process_markdown_for_preview(event['detail'])
        except Exception:
            detail_html = event['detail']

    return render_template('machinaka_event_detail.html',
        event=event, performances=performances,
        is_applicant=is_applicant, is_approver=is_approver,
        past_public=past_public, today=today, uid=uid,
        apply_ok=has_perm('apply', uid),
        detail_html=detail_html)


# ─────────────────────────────────────────────────────────────────
# 申請（新規）
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/apply', methods=['GET', 'POST'])
@require_perm('apply')
def apply_new():
    uid = current_uid()
    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        summary  = request.form.get('summary', '').strip()
        s_date   = request.form.get('event_start_date', '').strip()
        e_date   = request.form.get('event_end_date', '').strip()
        loc_use  = request.form.get('location_use', 'none')
        other    = request.form.get('other_notes', '').strip()
        detail   = request.form.get('detail', '').strip()
        pub_date = request.form.get('applicant_public_date', '').strip() or None

        if not title or not s_date or not e_date:
            flash('イベント名・開始日・終了日は必須です', 'error')
            return redirect(url_for('machinaka.apply_new'))

        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor()
            cur.execute("""
                INSERT INTO machinaka_events
                  (title, summary, detail, event_start_date, event_end_date,
                   location_use, other_notes, applicant_id, applicant_public_date, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
            """, (title, summary, detail, s_date, e_date, loc_use, other, uid, pub_date))
            conn.commit()
            new_id = cur.lastrowid
            flash('申請を受け付けました', 'success')
            return redirect(url_for('machinaka.event_detail', eid=new_id))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('machinaka_apply.html', event=None, mode='new')


# ─────────────────────────────────────────────────────────────────
# 申請編集（pending のみ）
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/event/<int:eid>/edit', methods=['GET', 'POST'])
@require_perm('apply')
def apply_edit(eid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM machinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)
    if event['status'] != 'pending':
        flash('申請中のイベントのみ編集できます', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))

    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        summary  = request.form.get('summary', '').strip()
        detail   = request.form.get('detail', '').strip()
        s_date   = request.form.get('event_start_date', '').strip()
        e_date   = request.form.get('event_end_date', '').strip()
        loc_use  = request.form.get('location_use', 'none')
        other    = request.form.get('other_notes', '').strip()
        pub_date = request.form.get('applicant_public_date', '').strip() or None
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor()
            cur.execute("""
                UPDATE machinaka_events SET
                  title=%s, summary=%s, detail=%s,
                  event_start_date=%s, event_end_date=%s,
                  location_use=%s, other_notes=%s, applicant_public_date=%s
                 WHERE id=%s
            """, (title, summary, detail, s_date, e_date, loc_use, other, pub_date, eid))
            conn.commit()
            flash('保存しました', 'success')
            return redirect(url_for('machinaka.event_detail', eid=eid))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('machinaka_apply.html', event=event, mode='edit')


# ─────────────────────────────────────────────────────────────────
# マイ申請
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/my')
@require_perm('apply')
def my_events():
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT * FROM machinaka_events
             WHERE applicant_id = %s
             ORDER BY event_start_date DESC
        """, (uid,))
        events = [enrich_event(e) for e in cur.fetchall()]
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        events = []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return render_template('machinaka_my.html',
        events=events, uid=uid,
        approve_ok=has_perm('approve', uid))


# ─────────────────────────────────────────────────────────────────
# 申請者によるステータス操作
# ─────────────────────────────────────────────────────────────────
def _get_event_for_applicant(eid, uid):
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM machinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid):
        abort(403)
    return event


@machinaka_bp.route('/event/<int:eid>/withdraw', methods=['POST'])
@require_perm('apply')
def withdraw(eid):
    uid   = current_uid()
    event = _get_event_for_applicant(eid, uid)
    today = datetime.date.today()
    pd    = eff_public_date(event)

    if event['status'] == 'approved' and pd and pd <= today:
        flash('公開日を過ぎているため取り下げできません。中止または延期を使用してください。', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))
    if event['status'] not in ('pending', 'approved'):
        flash('この操作はできません', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("UPDATE machinaka_events SET status='withdrawn' WHERE id=%s", (eid,))
        conn.commit()
        flash('取り下げました（記録は残ります）', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=eid))


@machinaka_bp.route('/event/<int:eid>/cancel', methods=['POST'])
@require_perm('apply')
def cancel_event(eid):
    uid   = current_uid()
    event = _get_event_for_applicant(eid, uid)
    if event['status'] != 'approved':
        flash('承認済みイベントのみ中止できます', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))
    note = request.form.get('cancel_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute(
            "UPDATE machinaka_events SET status='cancelled', cancel_note=%s WHERE id=%s",
            (note, eid))
        conn.commit()
        flash('中止としました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=eid))


@machinaka_bp.route('/event/<int:eid>/postpone', methods=['POST'])
@require_perm('apply')
def postpone_event(eid):
    uid   = current_uid()
    event = _get_event_for_applicant(eid, uid)
    if event['status'] != 'approved':
        flash('承認済みイベントのみ延期できます', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))
    note = request.form.get('cancel_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute(
            "UPDATE machinaka_events SET status='postponed', cancel_note=%s WHERE id=%s",
            (note, eid))
        conn.commit()
        flash('延期としました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=eid))


# ─────────────────────────────────────────────────────────────────
# 公演管理
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/event/<int:eid>/performance/add', methods=['POST'])
@require_perm('apply')
def perf_add(eid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM machinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)
    if event['status'] not in ('pending', 'approved'):
        flash('申請中または承認済みのイベントのみ公演を追加できます', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))

    s_dt = request.form.get('start_datetime', '').strip()
    e_dt = request.form.get('end_datetime', '').strip()
    note = request.form.get('perf_note', '').strip()
    if not s_dt or not e_dt:
        flash('開始・終了日時は必須です', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO machinaka_performances (event_id, start_datetime, end_datetime, note)
            VALUES (%s, %s, %s, %s)
        """, (eid, s_dt, e_dt, note))
        conn.commit()
        flash('公演を追加しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=eid))


@machinaka_bp.route('/performance/<int:pid>/edit', methods=['POST'])
@require_perm('apply')
def perf_edit(pid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT p.*, e.applicant_id
              FROM machinaka_performances p
              JOIN machinaka_events e ON e.id = p.event_id
             WHERE p.id = %s
        """, (pid,))
        perf = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not perf:
        abort(404)
    if not (is_admin_user() or perf['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)

    s_dt = request.form.get('start_datetime', '').strip()
    e_dt = request.form.get('end_datetime', '').strip()
    note = request.form.get('perf_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE machinaka_performances SET start_datetime=%s, end_datetime=%s, note=%s
             WHERE id=%s
        """, (s_dt, e_dt, note, pid))
        conn.commit()
        flash('公演を更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=perf['event_id']))


@machinaka_bp.route('/performance/<int:pid>/delete', methods=['POST'])
@require_perm('apply')
def perf_delete(pid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT p.*, e.applicant_id
              FROM machinaka_performances p
              JOIN machinaka_events e ON e.id = p.event_id
             WHERE p.id = %s
        """, (pid,))
        perf = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not perf:
        abort(404)
    if not (is_admin_user() or perf['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)

    eid = perf['event_id']
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("DELETE FROM machinaka_performances WHERE id=%s", (pid,))
        conn.commit()
        flash('公演を削除しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=eid))


# ─────────────────────────────────────────────────────────────────
# 承認者 管理画面
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/manage')
@login_required
@require_perm('approve')
def manage():
    filter_st = request.args.get('filter', 'active')
    where_map = {
        'pending': "WHERE e.status = 'pending'",
        'closed':  "WHERE e.status IN ('rejected','withdrawn','cancelled','postponed')",
        'all':     '',
    }
    where = where_map.get(filter_st, "WHERE e.status IN ('pending','approved')")
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute(f"""
            SELECT e.*, u.full_name AS applicant_name
              FROM machinaka_events e
              LEFT JOIN users u ON u.id = e.applicant_id
             {where}
             ORDER BY
               CASE e.status WHEN 'pending' THEN 0 ELSE 1 END,
               e.event_start_date
        """)
        events = [enrich_event(e) for e in cur.fetchall()]
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        events = []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return render_template('machinaka_manage.html', events=events,
                           filter_st=filter_st, today=datetime.date.today(),
                           manager_ok=is_machinaka_manager(current_uid()))


@machinaka_bp.route('/event/<int:eid>/approve', methods=['POST'])
@require_perm('approve')
def do_approve(eid):
    uid      = current_uid()
    notes    = request.form.get('approver_notes', '').strip()
    pub_date = request.form.get('approver_public_date', '').strip() or None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE machinaka_events SET
              status='approved', approver_id=%s, approve_datetime=%s,
              approver_notes=%s, approver_public_date=%s
             WHERE id=%s
        """, (uid, get_jst_now(), notes, pub_date, eid))
        conn.commit()
        flash('承認しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(request.referrer or url_for('machinaka.manage'))


@machinaka_bp.route('/event/<int:eid>/reject', methods=['POST'])
@require_perm('approve')
def do_reject(eid):
    uid   = current_uid()
    notes = request.form.get('approver_notes', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE machinaka_events SET
              status='rejected', approver_id=%s, approve_datetime=%s, approver_notes=%s
             WHERE id=%s
        """, (uid, get_jst_now(), notes, eid))
        conn.commit()
        flash('不承認としました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(request.referrer or url_for('machinaka.manage'))


@machinaka_bp.route('/event/<int:eid>/set_status', methods=['POST'])
@require_perm('approve')
def set_status(eid):
    uid    = current_uid()
    new_st = request.form.get('new_status', '')
    note   = request.form.get('cancel_note', '').strip()
    if new_st not in STATUS_META:
        flash('無効なステータスです', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE machinaka_events SET
              status=%s, cancel_note=%s, approver_id=%s, approve_datetime=%s
             WHERE id=%s
        """, (new_st, note or None, uid, get_jst_now(), eid))
        conn.commit()
        flash(f'ステータスを「{STATUS_META[new_st][0]}」に変更しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=eid))


@machinaka_bp.route('/event/<int:eid>/update_notes', methods=['POST'])
@require_perm('approve')
def update_notes(eid):
    notes    = request.form.get('approver_notes', '').strip()
    pub_date = request.form.get('approver_public_date', '').strip() or None
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE machinaka_events SET approver_notes=%s, approver_public_date=%s
             WHERE id=%s
        """, (notes, pub_date, eid))
        conn.commit()
        flash('備考・公開日を更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=eid))


@machinaka_bp.route('/event/<int:eid>/update_detail', methods=['POST'])
@require_perm('apply')
def update_detail(eid):
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT applicant_id FROM machinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not event:
        abort(404)
    if not (is_admin_user() or event['applicant_id'] == uid or has_perm('approve', uid)):
        abort(403)

    detail = request.form.get('detail', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("UPDATE machinaka_events SET detail=%s WHERE id=%s", (detail, eid))
        conn.commit()
        flash('メッセージを更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.event_detail', eid=eid))


# ═════════════════════════════════════════════════════════════════
# 日報 (Daily Reports)
# ═════════════════════════════════════════════════════════════════

@machinaka_bp.route('/reports')
@login_required
def reports_list():
    """
    日報一覧ページ。
    新テンプレート (machinaka_reports.html) はクライアント側で
    /reports/all.json から全件取得して描画するため、
    サーバ側はテンプレートの権限チェックと変数渡しだけ行う。
    """
    uid = current_uid()
    if not (has_perm('approve', uid) or has_perm('inspect', uid)):
        abort(403)

    today = datetime.date.today()
    return render_template('machinaka_reports.html',
        uid=uid,
        today=today,
        apply_ok=has_perm('apply', uid),
        approve_ok=has_perm('approve', uid),
        inspect_ok=has_perm('inspect', uid))


@machinaka_bp.route('/reports/all.json')
@login_required
def reports_all_json():
    """
    日報一覧テンプレートが fetch する JSON エンドポイント。
    全件を新しい順で返す（クライアント側で並び替え・月別集計を行う）。
    """
    uid = current_uid()
    if not (has_perm('approve', uid) or has_perm('inspect', uid)):
        abort(403)

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT r.id, r.report_date, r.open_status,
                   r.open_time, r.close_time, r.closure_note,
                   r.visitor_count, r.body,
                   r.inspected_at,
                   u.full_name AS staff_name,
                   i.full_name AS inspector_name
              FROM machinaka_daily_reports r
              LEFT JOIN users u ON u.id = r.staff_id
              LEFT JOIN users i ON i.id = r.inspected_by
             ORDER BY r.report_date DESC
        """)
        rows = cur.fetchall()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    def _fmt_time(t):
        if t is None:
            return ''
        if isinstance(t, datetime.timedelta):
            total = int(t.total_seconds())
            return f"{total//3600:02d}:{(total%3600)//60:02d}"
        if isinstance(t, str):
            return t[:5] if len(t) >= 5 else t
        try:
            return t.strftime('%H:%M')
        except Exception:
            return str(t)

    result = []
    for r in rows:
        status = r['open_status'] or 'open'
        label, color = OPEN_STATUS_META.get(status, (status, '#888'))
        result.append({
            'id':          r['id'],
            'date':        r['report_date'].isoformat() if r['report_date'] else '',
            'status':      status,
            'statusLabel': label,
            'statusColor': color,
            'openTime':    _fmt_time(r['open_time']),
            'closeTime':   _fmt_time(r['close_time']),
            'closureNote': r['closure_note'] or '',
            'visitors':    int(r['visitor_count'] or 0),
            'body':        r['body'] or '',
            'staff':       r['staff_name'] or '',
            'inspected':   r['inspected_at'] is not None,
            'inspector':   r['inspector_name'] or '',
        })

    return jsonify(result)


@machinaka_bp.route('/reports/upload_image', methods=['POST'])
@login_required
def report_upload_image():
    """日報本文用 画像アップロード"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'ファイルが選択されていません'}), 400
    if not _machinaka_allowed_file(file.filename):
        return jsonify({'success': False, 'error': '許可されていないファイル形式です（png/jpg/jpeg/gif/webp）'}), 400
    try:
        os.makedirs(MACHINAKA_UPLOAD_FOLDER, exist_ok=True)
        timestamp = get_jst_now().strftime('%Y%m%d_%H%M%S')
        filename = secure_filename(file.filename)
        name, ext = os.path.splitext(filename)
        unique_filename = f"{name}_{timestamp}{ext}"
        filepath = os.path.join(MACHINAKA_UPLOAD_FOLDER, unique_filename)
        file.save(filepath)
        url = url_for('machinaka.image', filename=unique_filename)
        return jsonify({'success': True, 'filename': unique_filename, 'url': url})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@machinaka_bp.route('/reports/new', methods=['GET', 'POST'])
@require_perm('approve')
def report_new():
    """日報新規作成。既に同日のレコードがあればそちらへリダイレクト。"""
    uid = current_uid()
    default_date = request.args.get('date', datetime.date.today().isoformat())

    if request.method == 'POST':
        r_date   = request.form.get('report_date', '').strip()
        op_st    = request.form.get('open_status', 'open')
        op_time  = request.form.get('open_time', '').strip()  or None
        cl_time  = request.form.get('close_time', '').strip() or None
        cl_note  = request.form.get('closure_note', '').strip()
        v_count  = request.form.get('visitor_count', '0').strip() or '0'
        body     = request.form.get('body', '').strip()

        if not r_date:
            flash('対象日は必須です', 'error')
            return redirect(url_for('machinaka.report_new'))
        if op_st not in OPEN_STATUS_META:
            flash('開館状況の値が不正です', 'error')
            return redirect(url_for('machinaka.report_new'))
        try:
            v_count_int = max(0, int(v_count))
        except ValueError:
            v_count_int = 0
        if op_st == 'closed':
            op_time, cl_time = None, None

        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor()
            cur.execute("""
                INSERT INTO machinaka_daily_reports
                  (report_date, staff_id, open_status, open_time, close_time,
                   closure_note, visitor_count, body)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (r_date, uid, op_st, op_time, cl_time, cl_note, v_count_int, body))
            conn.commit()
            new_id = cur.lastrowid
            flash('日報を保存しました', 'success')
            return redirect(url_for('machinaka.report_detail', rid=new_id))
        except mysql.connector.IntegrityError:
            # 同日既存 → 該当レコードへ誘導
            conn.rollback()
            try:
                cur.execute("SELECT id FROM machinaka_daily_reports WHERE report_date=%s",
                            (r_date,))
                row = cur.fetchone()
                if row:
                    flash('既にその日の日報があります。編集画面に遷移します。', 'error')
                    return redirect(url_for('machinaka.report_edit', rid=row[0]))
            except Exception:
                pass
            flash('同日の日報が既に存在します', 'error')
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('machinaka_report_form.html',
        report=None, mode='new',
        default_date=default_date)


@machinaka_bp.route('/reports/<int:rid>')
@login_required
def report_detail(rid):
    _log_access('report_detail', ref_report_id=rid)  # ← 追加
    uid = current_uid()
    if not (has_perm('approve', uid) or has_perm('inspect', uid)):
        abort(403)

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT r.*, u.full_name AS staff_name,
                   i.full_name AS inspector_name
              FROM machinaka_daily_reports r
              LEFT JOIN users u ON u.id = r.staff_id
              LEFT JOIN users i ON i.id = r.inspected_by
             WHERE r.id = %s
        """, (rid,))
        report = cur.fetchone()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not report:
        abort(404)
    enrich_report(report)

    body_html = ''
    if report.get('body'):
        try:
            body_html = process_markdown_for_preview(report['body'])
        except Exception:
            body_html = report['body']

    return render_template('machinaka_report_detail.html',
        report=report,
        body_html=body_html,
        uid=uid,
        is_staff=(uid is not None and report['staff_id'] == uid),
        apply_ok=has_perm('apply', uid),
        approve_ok=has_perm('approve', uid),
        inspect_ok=has_perm('inspect', uid))


@machinaka_bp.route('/reports/<int:rid>/edit', methods=['GET', 'POST'])
@require_perm('approve')
def report_edit(rid):
    """
    日報編集。作成者本人（approve 権限保有者）、他の approver、admin が編集可能。
    編集時に点検情報があればクリア（再点検を要求）。
    """
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM machinaka_daily_reports WHERE id=%s", (rid,))
        report = cur.fetchone()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not report:
        abort(404)
    if not (is_admin_user() or report['staff_id'] == uid or has_perm('approve', uid)):
        abort(403)

    if request.method == 'POST':
        r_date   = request.form.get('report_date', '').strip()
        op_st    = request.form.get('open_status', 'open')
        op_time  = request.form.get('open_time', '').strip()  or None
        cl_time  = request.form.get('close_time', '').strip() or None
        cl_note  = request.form.get('closure_note', '').strip()
        v_count  = request.form.get('visitor_count', '0').strip() or '0'
        body     = request.form.get('body', '').strip()

        try:
            v_count_int = max(0, int(v_count))
        except ValueError:
            v_count_int = 0
        if op_st == 'closed':
            op_time, cl_time = None, None

        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor()
            cur.execute("""
                UPDATE machinaka_daily_reports SET
                  report_date=%s, open_status=%s, open_time=%s, close_time=%s,
                  closure_note=%s, visitor_count=%s, body=%s,
                  inspected_by=NULL, inspected_at=NULL, inspector_note=NULL
                 WHERE id=%s
            """, (r_date, op_st, op_time, cl_time, cl_note, v_count_int, body, rid))
            conn.commit()
            flash('日報を更新しました（点検記録はクリアされ、再点検が必要です）', 'success')
            return redirect(url_for('machinaka.report_detail', rid=rid))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    return render_template('machinaka_report_form.html',
        report=report, mode='edit',
        default_date=report['report_date'].isoformat())


@machinaka_bp.route('/reports/<int:rid>/delete', methods=['POST'])
@require_perm('approve')
def report_delete(rid):
    """日報の削除は approve 権限のみ（誤登録対応）"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("DELETE FROM machinaka_daily_reports WHERE id=%s", (rid,))
        conn.commit()
        flash('日報を削除しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.reports_list'))


@machinaka_bp.route('/reports/<int:rid>/inspect', methods=['POST'])
@require_perm('inspect')
def report_inspect(rid):
    """
    地域連携係員による「確認しました」マーク。
    登録済の点検者のみ押下可能（require_perm('inspect') が二段ゲート）。
    """
    uid  = current_uid()
    note = request.form.get('inspector_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE machinaka_daily_reports SET
              inspected_by=%s, inspected_at=%s, inspector_note=%s
             WHERE id=%s
        """, (uid, get_jst_now(), note, rid))
        conn.commit()
        flash('点検を記録しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.report_detail', rid=rid))


@machinaka_bp.route('/reports/<int:rid>/unmark', methods=['POST'])
@require_perm('approve')
def report_unmark(rid):
    """点検マークの取り消し（承認者のみ）"""
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE machinaka_daily_reports SET
              inspected_by=NULL, inspected_at=NULL, inspector_note=NULL
             WHERE id=%s
        """, (rid,))
        conn.commit()
        flash('点検記録を取り消しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.report_detail', rid=rid))


@machinaka_bp.route('/reports/<int:rid>/instruction', methods=['POST'])
@require_perm('approve')
def report_instruction(rid):
    """
    承認者（地域連携係）が点検の有無に関係なく、
    指示事項 (inspector_note) のみを更新できる。
    """
    note = request.form.get('inspector_note', '').strip()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("""
            UPDATE machinaka_daily_reports SET inspector_note=%s
             WHERE id=%s
        """, (note, rid))
        conn.commit()
        flash('指示事項を更新しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.report_detail', rid=rid))


# ═════════════════════════════════════════════════════════════════
# 点検者管理
# ═════════════════════════════════════════════════════════════════

@machinaka_bp.route('/inspectors', methods=['GET', 'POST'])
@require_manager
def inspectors():
    """
    点検担当の登録・解除（旧画面。settings に統合済みだが互換のため残置）。
    GET : 登録済一覧＋候補ユーザ一覧
    POST: add/toggle/remove アクションを form.action で切替
    """
    if request.method == 'POST':
        action  = request.form.get('action', '')
        user_id = request.form.get('user_id', '').strip()
        note    = request.form.get('note', '').strip()
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor()

            if action == 'add' and user_id:
                cur.execute("""
                    INSERT INTO machinaka_inspectors (user_id, note, registered_by, active)
                    VALUES (%s, %s, %s, 1)
                    ON DUPLICATE KEY UPDATE active=1, note=VALUES(note),
                                            registered_by=VALUES(registered_by)
                """, (user_id, note, current_uid()))
                conn.commit()
                flash('点検担当に登録しました', 'success')

            elif action == 'toggle' and user_id:
                cur.execute("""
                    UPDATE machinaka_inspectors
                       SET active = 1 - active
                     WHERE user_id=%s
                """, (user_id,))
                conn.commit()
                flash('点検担当の状態を切り替えました', 'success')

            elif action == 'remove' and user_id:
                cur.execute("""
                    DELETE FROM machinaka_inspectors WHERE user_id=%s
                """, (user_id,))
                conn.commit()
                flash('点検担当を削除しました', 'success')

            else:
                flash('不明な操作です', 'error')
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()
        return redirect(url_for('machinaka.inspectors'))

    # --- GET ---
    # approve グループのメンバー一覧を取得
    inspectors_rows = []
    candidates      = []
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)

        cur.execute("""
            SELECT ins.id, ins.user_id, ins.active, ins.note,
                   ins.registered_at, ins.registered_by,
                   u.full_name, u.email,
                   r.full_name AS registered_by_name
              FROM machinaka_inspectors ins
              LEFT JOIN users u ON u.id = ins.user_id
              LEFT JOIN users r ON r.id = ins.registered_by
             ORDER BY ins.active DESC, ins.display_order, u.full_name
        """)
        inspectors_rows = cur.fetchall()

        # 未登録のユーザを候補として取得
        cur.execute("""
            SELECT u.id, u.full_name, u.email
              FROM users u
             WHERE u.id NOT IN (SELECT user_id FROM machinaka_inspectors)
             ORDER BY u.full_name
        """)
        candidates = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    return render_template('machinaka_inspectors.html',
        inspectors=inspectors_rows,
        candidates=candidates,
        approve_groups=[],   # グループ方式廃止のため空リスト
        is_admin=is_admin_user())


# ═════════════════════════════════════════════════════════════════
# アクセス設定
# ═════════════════════════════════════════════════════════════════

@machinaka_bp.route('/settings', methods=['GET', 'POST'])
@require_manager
def settings():
    if request.method == 'POST':
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor(dictionary=True)

            # ─ view: ポリシー1つ＋group選択時のみ許可グループ複数 ─
            view_policy = request.form.get('view_policy', DEFAULT_VIEW_POLICY).strip()
            if view_policy not in VALID_VIEW_POLICIES:
                view_policy = DEFAULT_VIEW_POLICY

            cur.execute("DELETE FROM machinaka_access_settings WHERE access_type='view'")
            cur.execute("""
                INSERT IGNORE INTO machinaka_access_settings (access_type, group_name)
                VALUES (%s, %s)
            """, ('view', _policy_marker(view_policy)))
            if view_policy == 'group':
                for gname in request.form.getlist('view_groups'):
                    gname = (gname or '').strip()
                    if gname and not _is_policy_marker(gname):
                        cur.execute("""
                            INSERT IGNORE INTO machinaka_access_settings (access_type, group_name)
                            VALUES (%s, %s)
                        """, ('view', gname))

            # ─ apply: ポリシー方式（public は不可） ─
            apply_policy = request.form.get('apply_policy', DEFAULT_APPLY_POLICY).strip()
            if apply_policy not in VALID_APPLY_POLICIES:
                apply_policy = DEFAULT_APPLY_POLICY

            cur.execute("DELETE FROM machinaka_access_settings WHERE access_type='apply'")
            cur.execute("""
                INSERT IGNORE INTO machinaka_access_settings (access_type, group_name)
                VALUES (%s, %s)
            """, ('apply', _policy_marker(apply_policy)))
            if apply_policy == 'group':
                for gname in request.form.getlist('apply_groups'):
                    gname = (gname or '').strip()
                    if gname and not _is_policy_marker(gname):
                        cur.execute("""
                            INSERT IGNORE INTO machinaka_access_settings (access_type, group_name)
                            VALUES (%s, %s)
                        """, ('apply', gname))

            uid_self = current_uid()

            # ─ approve: ユーザIDで machinaka_receptionists に保存 ─
            selected_r = [s.strip() for s in request.form.getlist('approve_users') if s.strip()]
            try:
                selected_r_ids = set(int(s) for s in selected_r)
            except ValueError:
                selected_r_ids = set()

            cur.execute("SELECT id, user_id, active FROM machinaka_receptionists")
            current_r = {r['user_id']: r for r in cur.fetchall()}

            for uid in selected_r_ids - set(current_r.keys()):
                cur.execute("""
                    INSERT INTO machinaka_receptionists (user_id, registered_by, active)
                    VALUES (%s, %s, 1)
                """, (uid, uid_self))

            for uid, rec in current_r.items():
                want_active = 1 if uid in selected_r_ids else 0
                if rec['active'] != want_active:
                    cur.execute("""
                        UPDATE machinaka_receptionists
                           SET active=%s
                         WHERE id=%s
                    """, (want_active, rec['id']))

            # ─ inspect: ユーザIDで machinaka_inspectors に保存 ─
            selected = [s.strip() for s in request.form.getlist('inspect_users') if s.strip()]
            try:
                selected_ids = set(int(s) for s in selected)
            except ValueError:
                selected_ids = set()

            cur.execute("SELECT id, user_id, active FROM machinaka_inspectors")
            current = {r['user_id']: r for r in cur.fetchall()}

            for uid in selected_ids - set(current.keys()):
                cur.execute("""
                    INSERT INTO machinaka_inspectors (user_id, registered_by, active)
                    VALUES (%s, %s, 1)
                """, (uid, uid_self))

            for uid, rec in current.items():
                want_active = 1 if uid in selected_ids else 0
                if rec['active'] != want_active:
                    cur.execute("""
                        UPDATE machinaka_inspectors
                           SET active=%s
                         WHERE id=%s
                    """, (want_active, rec['id']))

            conn.commit()
            flash('設定を保存しました', 'success')
            return redirect(url_for('machinaka.settings'))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    # --- GET: 現在の設定を取得 ---
    rows = []
    all_groups = []
    all_users = []
    current_inspector_ids = set()
    current_receptionist_ids = set()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)

        cur.execute("""
            SELECT access_type, group_name
              FROM machinaka_access_settings
             WHERE access_type IN ('view','apply')
             ORDER BY access_type, group_name
        """)
        rows = cur.fetchall()

        cur.execute("SELECT id, name FROM user_groups ORDER BY name")
        all_groups = cur.fetchall()

        cur.execute("""
            SELECT id, full_name, email, category
              FROM users
             ORDER BY full_name, id
        """)
        all_users = cur.fetchall()

        cur.execute("""
            SELECT user_id FROM machinaka_inspectors WHERE active=1
        """)
        current_inspector_ids = {r['user_id'] for r in cur.fetchall()}
        cur.execute("""
            SELECT user_id FROM machinaka_receptionists WHERE active=1
        """)
        current_receptionist_ids = {r['user_id'] for r in cur.fetchall()}
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    # view のポリシーと許可グループ
    view_policy = DEFAULT_VIEW_POLICY
    view_groups = set()
    # apply のポリシーと許可グループ
    apply_policy = DEFAULT_APPLY_POLICY
    apply_groups = set()

    for r in rows:
        if r['access_type'] == 'view':
            if _is_policy_marker(r['group_name']):
                p = r['group_name'][len(VIEW_POLICY_PREFIX):-len(VIEW_POLICY_SUFFIX)]
                if p in VALID_VIEW_POLICIES:
                    view_policy = p
            else:
                view_groups.add(r['group_name'])
        elif r['access_type'] == 'apply':
            if _is_policy_marker(r['group_name']):
                p = r['group_name'][len(VIEW_POLICY_PREFIX):-len(VIEW_POLICY_SUFFIX)]
                if p in VALID_APPLY_POLICIES:
                    apply_policy = p
            else:
                apply_groups.add(r['group_name'])
        # approve は machinaka_receptionists で管理するためここでは処理しない

    return render_template('machinaka_settings.html',
        view_policy=view_policy,
        view_groups=view_groups,
        valid_view_policies=VALID_VIEW_POLICIES,
        apply_policy=apply_policy,
        apply_groups=apply_groups,
        valid_apply_policies=VALID_APPLY_POLICIES,
        all_groups=all_groups,
        all_users=all_users,
        current_inspector_ids=current_inspector_ids,
        current_receptionist_ids=current_receptionist_ids,
        public_sentinel=PUBLIC_SENTINEL,
        manager_group=MANAGER_GROUP,
        is_admin=is_admin_user())


# ═════════════════════════════════════════════════════════════════
# 公開閲覧（匿名）
# ═════════════════════════════════════════════════════════════════

@machinaka_bp.route('/public')
def public_view():
    _log_access('public')             # ← 追加
    mode       = request.args.get('mode', 'week')
    anchor_str = request.args.get('anchor', datetime.date.today().isoformat())
    try:
        anchor = datetime.date.fromisoformat(anchor_str)
    except ValueError:
        anchor = datetime.date.today()
    today = datetime.date.today()

    if mode == 'day':
        period_start = anchor
        period_end   = anchor
        prev_anchor  = anchor - timedelta(days=1)
        next_anchor  = anchor + timedelta(days=1)
        period_label = anchor.strftime('%Y年%m月%d日')
    elif mode == 'month':
        period_start = anchor.replace(day=1)
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year+1, month=1, day=1) - timedelta(days=1)
        else:
            period_end = period_start.replace(month=period_start.month+1, day=1) - timedelta(days=1)
        prev_anchor  = (period_start - timedelta(days=1)).replace(day=1)
        next_anchor  = period_end + timedelta(days=1)
        period_label = anchor.strftime('%Y年%m月')
    else:
        weekday      = anchor.weekday()
        period_start = anchor - timedelta(days=weekday)
        period_end   = period_start + timedelta(days=6)
        prev_anchor  = period_start - timedelta(days=7)
        next_anchor  = period_start + timedelta(days=7)
        period_label = (f"{period_start.strftime('%Y年%m月%d日')}〜"
                        f"{period_end.strftime('%m月%d日')}")

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT e.*, u.full_name AS applicant_name
              FROM machinaka_events e
              LEFT JOIN users u ON u.id = e.applicant_id
             WHERE e.event_start_date <= %s
               AND e.event_end_date   >= %s
             ORDER BY e.event_start_date, e.event_end_date
        """, (period_end, period_start))
        events_raw = cur.fetchall()

        cur.execute("""
            SELECT e.*, u.full_name AS applicant_name
              FROM machinaka_events e
              LEFT JOIN users u ON u.id = e.applicant_id
             WHERE e.event_end_date < %s
             ORDER BY e.event_end_date DESC
             LIMIT 30
        """, (period_start,))
        past_raw = cur.fetchall()
        cur.execute("""
            SELECT id, title, body, published_at
              FROM machinaka_messages
             WHERE DATE(published_at) BETWEEN %s AND %s
             ORDER BY published_at
        """, (period_start, period_end))
        messages_raw = cur.fetchall()

    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        events_raw, past_raw = [], []
        messages_raw = []   # ← これも追加
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    events = [enrich_event(e) for e in events_raw if is_public_anonymous(e)]
    past   = [enrich_event(e) for e in past_raw   if is_public_anonymous(e)]

    timeline_items = []
    for e in events:
        timeline_items.append({'type': 'event', 'date': e['event_start_date'], 'obj': e})
    for m in messages_raw:
        d = m['published_at'].date() if hasattr(m['published_at'], 'date') else m['published_at']
        timeline_items.append({'type': 'message', 'date': d, 'obj': m})
    timeline_items.sort(key=lambda x: x['date'])

    all_event_ids = [e['id'] for e in events + past]
    perf_map = {}
    if all_event_ids:
        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor(dictionary=True)
            fmt  = ','.join(['%s'] * len(all_event_ids))
            cur.execute(f"""
                SELECT event_id, start_datetime, end_datetime, note
                  FROM machinaka_performances
                 WHERE event_id IN ({fmt})
                 ORDER BY start_datetime
            """, all_event_ids)
            for p in cur.fetchall():
                try:
                    p['note_html'] = process_markdown_for_preview(p['note']) if p['note'] else ''
                except Exception:
                    p['note_html'] = p['note'] or ''
                perf_map.setdefault(p['event_id'], []).append(p)
        except Exception:
            pass
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    for e in events + past:
        e['performances'] = perf_map.get(e['id'], [])

    cal_weeks = build_month_calendar(events, {},
                                     anchor.year, anchor.month) if mode == 'month' else None

    return render_template('machinaka_public.html',
        events=events, past=past,
        cal_weeks=cal_weeks,
        mode=mode, anchor=anchor,
        period_label=period_label,
        prev_anchor=prev_anchor, next_anchor=next_anchor,
        today=today,
        uid=None,
        apply_ok=False, approve_ok=False,
        timeline_items=timeline_items)


@machinaka_bp.route('/public/event/<int:eid>')
def public_event_detail(eid):
    _log_access('public_event_detail', ref_event_id=eid)  # ← 追加
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT e.* FROM machinaka_events e WHERE e.id = %s", (eid,))
        event = cur.fetchone()
        if not event:
            abort(404)
        cur.execute("""
            SELECT * FROM machinaka_performances
             WHERE event_id = %s ORDER BY start_datetime
        """, (eid,))
        performances = cur.fetchall()
    except Exception:
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not is_public_anonymous(event):
        abort(403)
    enrich_event(event)

    for p in performances:
        try:
            p['note_html'] = process_markdown_for_preview(p['note']) if p['note'] else ''
        except Exception:
            p['note_html'] = p['note'] or ''

    detail_html = ''
    if event.get('detail'):
        try:
            detail_html = process_markdown_for_preview(event['detail'])
        except Exception:
            detail_html = event['detail']

    return render_template('machinaka_public_event_detail.html',
        event=event,
        performances=performances,
        detail_html=detail_html)

# ═════════════════════════════════════════════════════════════════
# 統計ダッシュボード
# ═════════════════════════════════════════════════════════════════

@machinaka_bp.route('/reports/stats')
@login_required
def reports_stats():
    """日報統計ダッシュボード。approve / inspect / admin が閲覧可。"""
    uid = current_uid()
    if not (has_perm('approve', uid) or has_perm('inspect', uid)):
        abort(403)

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)

        # ── 全日報（日付昇順）──
        cur.execute("""
            SELECT report_date, open_status, visitor_count
              FROM machinaka_daily_reports
             ORDER BY report_date ASC
        """)
        all_rows = cur.fetchall()

        # ── 最多来訪日トップ5 ──
        cur.execute("""
            SELECT r.report_date, r.visitor_count, u.full_name AS staff_name
              FROM machinaka_daily_reports r
              LEFT JOIN users u ON u.id = r.staff_id
             ORDER BY r.visitor_count DESC
             LIMIT 5
        """)
        top5 = cur.fetchall()

        # ── 年度別集計 ──
        cur.execute("""
            SELECT
              CASE WHEN MONTH(report_date) >= 4
                   THEN YEAR(report_date)
                   ELSE YEAR(report_date) - 1
              END AS fiscal_year,
              SUM(visitor_count)  AS total_visitors,
              COUNT(*)            AS total_days,
              SUM(CASE WHEN open_status = 'open'    THEN 1 ELSE 0 END) AS open_days,
              SUM(CASE WHEN open_status = 'closed'  THEN 1 ELSE 0 END) AS closed_days,
              SUM(CASE WHEN open_status = 'partial' THEN 1 ELSE 0 END) AS partial_days
            FROM machinaka_daily_reports
            GROUP BY fiscal_year
            ORDER BY fiscal_year
        """)
        fiscal_rows = cur.fetchall()

    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        all_rows, top5, fiscal_rows = [], [], []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    # ── Python 側で集計 ──
    import json
    from collections import defaultdict

    # サマリー
    total_visitors = sum(r['visitor_count'] for r in all_rows)
    total_days     = len(all_rows)
    open_days      = sum(1 for r in all_rows if r['open_status'] == 'open')
    closed_days    = sum(1 for r in all_rows if r['open_status'] == 'closed')
    avg_visitors   = round(total_visitors / total_days, 1) if total_days else 0
    avg_open       = round(total_visitors / open_days,  1) if open_days  else 0

    # 月別集計
    monthly = defaultdict(lambda: {'visitors': 0, 'open': 0, 'closed': 0, 'partial': 0})
    for r in all_rows:
        key = r['report_date'].strftime('%Y-%m')
        monthly[key]['visitors'] += r['visitor_count']
        monthly[key][r['open_status']] += 1

    months_sorted = sorted(monthly.keys())
    monthly_labels    = months_sorted
    monthly_visitors  = [monthly[m]['visitors'] for m in months_sorted]
    monthly_open_days = [monthly[m]['open'] for m in months_sorted]

    # 日別（直近90日）
    recent = all_rows[-90:] if len(all_rows) > 90 else all_rows
    daily_labels   = [r['report_date'].strftime('%m/%d') for r in recent]
    daily_visitors = [r['visitor_count'] for r in recent]

    # 累積訪問者数
    cumulative = []
    acc = 0
    for r in all_rows:
        acc += r['visitor_count']
        cumulative.append(acc)
    cumul_labels = [r['report_date'].strftime('%Y/%m') for r in all_rows]

    # 曜日別平均（0=月〜6=日）
    DOW_LABELS = ['月', '火', '水', '木', '金', '土', '日']
    dow_sum   = defaultdict(int)
    dow_count = defaultdict(int)
    for r in all_rows:
        if r['open_status'] == 'open':
            dow = r['report_date'].weekday()
            dow_sum[dow]   += r['visitor_count']
            dow_count[dow] += 1
    dow_avg = [round(dow_sum[i] / dow_count[i], 1) if dow_count[i] else 0
               for i in range(7)]

    # top5 の日付を文字列に
    for t in top5:
        t['report_date'] = t['report_date'].strftime('%Y/%m/%d')

    # fiscal_rows の fiscal_year を文字列に
    for f in fiscal_rows:
        f['fiscal_year'] = f"{f['fiscal_year']}年度"

    return render_template('machinaka_stats.html',
        uid=uid,
        approve_ok=has_perm('approve', uid),
        inspect_ok=has_perm('inspect', uid),
        # サマリー
        total_visitors=total_visitors,
        total_days=total_days,
        open_days=open_days,
        closed_days=closed_days,
        avg_visitors=avg_visitors,
        avg_open=avg_open,
        # チャートデータ（JSON）
        monthly_labels   =json.dumps(monthly_labels,    ensure_ascii=False),
        monthly_visitors =json.dumps(monthly_visitors),
        monthly_open_days=json.dumps(monthly_open_days),
        daily_labels     =json.dumps(daily_labels,      ensure_ascii=False),
        daily_visitors   =json.dumps(daily_visitors),
        cumul_labels     =json.dumps(cumul_labels,      ensure_ascii=False),
        cumulative       =json.dumps(cumulative),
        dow_labels       =json.dumps(DOW_LABELS,        ensure_ascii=False),
        dow_avg          =json.dumps(dow_avg),
        # テーブル
        top5=top5,
        fiscal_rows=fiscal_rows,
    )

@machinaka_bp.route('/reports/export_md')
@login_required
def reports_export_md():
    """日報をMarkdownテキストで出力（クリップボード貼り付け用）"""
    uid = current_uid()
    if not (has_perm('approve', uid) or has_perm('inspect', uid)):
        abort(403)

    today = datetime.date.today()
    from_str = request.args.get('from', (today - timedelta(days=60)).isoformat())
    to_str   = request.args.get('to',   today.isoformat())
    try:
        d_from = datetime.date.fromisoformat(from_str)
        d_to   = datetime.date.fromisoformat(to_str)
    except ValueError:
        d_from = today - timedelta(days=60)
        d_to   = today

    insp_filter = request.args.get('insp', 'all')
    insp_where = ''
    if insp_filter == 'inspected':
        insp_where = ' AND r.inspected_at IS NOT NULL'
    elif insp_filter == 'pending':
        insp_where = ' AND r.inspected_at IS NULL'

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute(f"""
            SELECT r.report_date, r.open_status, r.open_time, r.close_time,
                   r.closure_note, r.visitor_count, r.body,
                   r.inspected_at, r.inspector_note,
                   u.full_name AS staff_name,
                   i.full_name AS inspector_name
              FROM machinaka_daily_reports r
              LEFT JOIN users u ON u.id = r.staff_id
              LEFT JOIN users i ON i.id = r.inspected_by
             WHERE r.report_date BETWEEN %s AND %s
             {insp_where}
             ORDER BY r.report_date ASC
        """, (d_from, d_to))
        reports = cur.fetchall()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        reports = []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    # MD テキスト生成
    OPEN_STATUS_JA = {'open': '通常開館', 'closed': '全日休館', 'partial': '部分休館'}
    DOW_JA = ['月', '火', '水', '木', '金', '土', '日']

    lines = []
    lines.append(f'# まちなかキャンパス 日報')
    lines.append(f'対象期間: {d_from.strftime("%Y年%m月%d日")} ～ {d_to.strftime("%Y年%m月%d日")}')
    lines.append(f'出力日: {today.strftime("%Y年%m月%d日")}')
    lines.append(f'記録件数: {len(reports)}件')
    lines.append('')

    for r in reports:
        d   = r['report_date']
        dow = DOW_JA[d.weekday()]
        lines.append(f'---')
        lines.append(f'## {d.strftime("%Y年%m月%d日")}（{dow}）の記録')
        lines.append('')

        st = OPEN_STATUS_JA.get(r['open_status'], r['open_status'])
        lines.append(f'**開館状況**: {st}')

        if r['open_time'] and r['close_time']:
            ot = str(r['open_time'])[:5]
            ct = str(r['close_time'])[:5]
            lines.append(f'**開館時間**: {ot} ～ {ct}')

        lines.append(f'**訪問者数**: {r["visitor_count"]}名')

        if r['closure_note']:
            lines.append(f'**備考**: {r["closure_note"]}')

        if r['body']:
            lines.append('')
            lines.append(r['body'])

        if r['inspector_note']:
            lines.append('')
            lines.append(f'**点検コメント**: {r["inspector_note"]}')

        lines.append('')

    md_text = '\n'.join(lines)

    return render_template('machinaka_export_md.html',
        md_text=md_text,
        d_from=d_from, d_to=d_to,
        count=len(reports),
    )

# ─────────────────────────────────────────────────────────────────
# 窓口職員によるイベント編集（ステータス不問）
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/event/<int:eid>/staff_edit', methods=['GET', 'POST'])
@require_perm('approve')
def staff_edit(eid):
    """
    窓口職員（approve権限）による現場編集。
    申請日時・申請者は変更不可。ステータスを問わず編集可能。
    """
    uid = current_uid()
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM machinaka_events WHERE id=%s", (eid,))
        event = cur.fetchone()
    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        abort(500)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    if not event:
        abort(404)

    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        summary  = request.form.get('summary', '').strip()
        detail   = request.form.get('detail', '').strip()
        s_date   = request.form.get('event_start_date', '').strip()
        e_date   = request.form.get('event_end_date', '').strip()
        loc_use  = request.form.get('location_use', 'none')
        other    = request.form.get('other_notes', '').strip()
        pub_date = request.form.get('applicant_public_date', '').strip() or None

        if not title or not s_date or not e_date:
            flash('イベント名・開始日・終了日は必須です', 'error')
            return redirect(url_for('machinaka.event_detail', eid=eid))

        try:
            conn = mysql.connector.connect(**DatabaseConfig.default())
            cur  = conn.cursor()
            cur.execute("""
                UPDATE machinaka_events SET
                  title=%s, summary=%s, detail=%s,
                  event_start_date=%s, event_end_date=%s,
                  location_use=%s, other_notes=%s,
                  applicant_public_date=%s
                 WHERE id=%s
            """, (title, summary, detail, s_date, e_date,
                  loc_use, other, pub_date, eid))
            conn.commit()
            flash('イベント内容を更新しました', 'success')
            return redirect(url_for('machinaka.event_detail', eid=eid))
        except Exception as ex:
            conn.rollback()
            flash(f'エラー: {ex}', 'error')
        finally:
            if 'conn' in locals() and conn.is_connected():
                cur.close(); conn.close()

    # 編集フォームはイベント詳細画面にある（専用テンプレートは持たない）
    return redirect(url_for('machinaka.event_detail', eid=eid))


# ─────────────────────────────────────────────────────────────────
# 窓口職員によるイベント削除（物理削除）
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/event/<int:eid>/delete', methods=['POST'])
@require_perm('approve')
def event_delete(eid):
    """
    窓口職員（approve権限）による物理削除。
    公演データも同時に削除する。
    確認フォームからのPOSTのみ受け付ける（GETでは削除しない）。
    """
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute(
            "DELETE FROM machinaka_performances WHERE event_id=%s", (eid,))
        cur.execute(
            "DELETE FROM machinaka_events WHERE id=%s", (eid,))
        conn.commit()
        flash('イベントを削除しました', 'success')
        return redirect(url_for('machinaka.manage'))
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
        return redirect(url_for('machinaka.event_detail', eid=eid))
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

@machinaka_bp.route('/messages/save', methods=['POST'])
@require_perm('approve')
def message_save():
    """
    ダッシュボードのインラインフォームから新規作成・編集を一本化。
    mid が空なら INSERT、あれば UPDATE。
    """
    uid  = current_uid()
    mid  = request.form.get('mid', '').strip()
    title  = request.form.get('title', '').strip()
    body   = request.form.get('body', '').strip()
    pub_at = request.form.get('published_at', '').strip() \
             or get_jst_now().strftime('%Y-%m-%dT%H:%M')
    anchor = request.form.get('anchor', datetime.date.today().isoformat())
    mode   = request.form.get('mode', 'week')

    if not title:
        flash('タイトルは必須です', 'error')
        return redirect(url_for('machinaka.index',
                                anchor=anchor, mode=mode))
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        if mid:
            cur.execute("""
                UPDATE machinaka_messages
                   SET title=%s, body=%s, published_at=%s, updated_at=%s
                 WHERE id=%s
            """, (title, body, pub_at, get_jst_now(), mid))
        else:
            cur.execute("""
                INSERT INTO machinaka_messages
                  (title, body, published_at, created_by, updated_at)
                VALUES (%s,%s,%s,%s,%s)
            """, (title, body, pub_at, uid, get_jst_now()))
        conn.commit()
        flash('メッセージを保存しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.index', anchor=anchor, mode=mode))


@machinaka_bp.route('/messages/<int:mid>/delete', methods=['POST'])
@require_perm('approve')
def message_delete(mid):
    anchor = request.form.get('anchor', datetime.date.today().isoformat())
    mode   = request.form.get('mode', 'week')
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor()
        cur.execute("DELETE FROM machinaka_messages WHERE id=%s", (mid,))
        conn.commit()
        flash('メッセージを削除しました', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'エラー: {ex}', 'error')
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    return redirect(url_for('machinaka.index', anchor=anchor, mode=mode))


@machinaka_bp.route('/messages/<int:mid>')
def message_detail(mid):
    _log_access('message_detail')     # ← 追加
    uid = current_uid()
    if not uid and get_view_policy() != 'public':
        abort(403)
    if uid and not (has_perm('view', uid) or has_perm('approve', uid)):
        abort(403)
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT m.*, u.full_name AS author_name
              FROM machinaka_messages m
              LEFT JOIN users u ON u.id = m.created_by
             WHERE m.id = %s
        """, (mid,))
        msg = cur.fetchone()
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()
    if not msg:
        abort(404)

    body_html = ''
    if msg.get('body'):
        try:
            body_html = process_markdown_for_preview(msg['body'])
        except Exception:
            body_html = msg['body']

    return render_template('machinaka_message_detail.html',
        msg=msg, body_html=body_html, uid=uid,
        approve_ok=has_perm('approve', uid))

# ─────────────────────────────────────────────────────────────────
# アクセスログ管理（admin または まちなか管理者専用）
# ─────────────────────────────────────────────────────────────────
@machinaka_bp.route('/admin/access_log')
@require_manager
def access_log():
    page      = max(1, int(request.args.get('page', 1)))
    per_page  = 50
    offset    = (page - 1) * per_page
    ep_filter = request.args.get('endpoint', '')

    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cur  = conn.cursor(dictionary=True)

        # サマリー統計
        cur.execute("""
            SELECT
              COUNT(*)                           AS total,
              COUNT(DISTINCT DATE(accessed_at))  AS active_days,
              COUNT(DISTINCT user_id)            AS unique_users,
              SUM(user_id IS NULL)               AS anon_count,
              SUM(user_id IS NOT NULL)           AS login_count
            FROM machinaka_access_logs
        """)
        stats = cur.fetchone()

        # エンドポイント別集計
        cur.execute("""
            SELECT endpoint, COUNT(*) AS cnt
            FROM machinaka_access_logs
            GROUP BY endpoint ORDER BY cnt DESC
        """)
        by_endpoint = cur.fetchall()

        # 直近7日 日別アクセス数
        cur.execute("""
            SELECT DATE(accessed_at) AS day, COUNT(*) AS cnt
            FROM machinaka_access_logs
            WHERE accessed_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
            GROUP BY day ORDER BY day
        """)
        daily = cur.fetchall()

        # ログ一覧（ページング）
        where      = "WHERE endpoint = %s" if ep_filter else ""
        p_count    = (ep_filter,) if ep_filter else ()
        cur.execute(f"SELECT COUNT(*) AS n FROM machinaka_access_logs {where}", p_count)
        total_rows = cur.fetchone()['n']

        p_list = (ep_filter, per_page, offset) if ep_filter else (per_page, offset)
        cur.execute(f"""
            SELECT l.*, u.full_name AS user_name
            FROM machinaka_access_logs l
            LEFT JOIN users u ON u.id = l.user_id
            {where}
            ORDER BY l.accessed_at DESC
            LIMIT %s OFFSET %s
        """, p_list)
        logs = cur.fetchall()

    except Exception as e:
        flash(f'DB エラー: {e}', 'error')
        stats, by_endpoint, daily, logs, total_rows = {}, [], [], [], 0
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()

    total_pages = max(1, (total_rows + per_page - 1) // per_page)

    return render_template('machinaka_access_log.html',
        stats=stats,
        by_endpoint=by_endpoint,
        daily=daily,
        logs=logs,
        page=page,
        total_pages=total_pages,
        ep_filter=ep_filter,
        endpoints=[
            'dashboard',
            'event_detail',
            'report_detail',
            'public',                 # ← 追加
            'public_event_detail',    # ← 追加
            'message_detail',         # ← 追加
        ],
    )

# ─────────────────────────────────────────────────────────────────
# API: T_07_05_吹風舎利用状況 同期
# ─────────────────────────────────────────────────────────────────

@machinaka_bp.route('/api/sync_t0705', methods=['POST'])
@login_required
@require_manager
def api_sync_t0705():
    """
    machinaka_daily_reports を年度集計して
    nishida$fujinp.T_07_05_吹風舎利用状況 に UPSERT する。
    年度 = 4月始まり（report_date の月が4以上なら同年、1-3月なら前年）
    """
    try:
        # ── 集計（nishida$default の machinaka_daily_reports）──
        conn_d = mysql.connector.connect(**DatabaseConfig.default())
        cur_d  = conn_d.cursor(dictionary=True)
        cur_d.execute("""
            SELECT
                CASE
                    WHEN MONTH(report_date) >= 4 THEN YEAR(report_date)
                    ELSE YEAR(report_date) - 1
                END AS 年度,
                COUNT(CASE WHEN open_status != 'closed' THEN 1 END) AS 年間開館日数,
                SUM(visitor_count) AS 年間訪問者数
            FROM machinaka_daily_reports
            GROUP BY 年度
            ORDER BY 年度
        """)
        rows = cur_d.fetchall()
        cur_d.close(); conn_d.close()

        if not rows:
            return jsonify({'success': False, 'error': 'データがありません'}), 404

        # ── UPSERT（nishida$fujinp の T_07_05）──
        _cfg_f = DatabaseConfig.default().copy()
        _cfg_f['database'] = _cfg_f['database'].replace('default', 'fujinp')
        conn_f = mysql.connector.connect(**_cfg_f)
        cur_f  = conn_f.cursor()

        # テーブルがなければ作成
        cur_f.execute("""
            CREATE TABLE IF NOT EXISTS T_07_05_吹風舎利用状況 (
                年度          INT PRIMARY KEY,
                年間開館日数  INT,
                年間訪問者数  INT,
                備考          TEXT
            )
        """)

        upserted = []
        for r in rows:
            cur_f.execute("""
                INSERT INTO T_07_05_吹風舎利用状況 (年度, 年間開館日数, 年間訪問者数)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    年間開館日数 = VALUES(年間開館日数),
                    年間訪問者数 = VALUES(年間訪問者数)
            """, (
                r['年度'],
                int(r['年間開館日数'] or 0),
                int(r['年間訪問者数'] or 0),
            ))
            upserted.append({
                'year':        r['年度'],
                'open_days':   int(r['年間開館日数'] or 0),
                'visitors':    int(r['年間訪問者数'] or 0),
                'avg_per_day': round(r['年間訪問者数'] / r['年間開館日数'], 1)
                               if r['年間開館日数'] else None,
            })

        conn_f.commit()
        cur_f.close(); conn_f.close()

        print(f"T_07_05 sync: {len(upserted)} years upserted by user {current_uid()}")
        return jsonify({'success': True, 'upserted': upserted, 'total': len(upserted)})

    except Exception as e:
        print(f"api_sync_t0705 error: {e}")
        for vname in ('conn_d', 'conn_f'):
            c = locals().get(vname)
            if c and c.is_connected():
                try: c.rollback()
                except: pass
                c.close()
        return jsonify({'success': False, 'error': str(e)}), 500


@machinaka_bp.route('/img/<path:filename>')
def image(filename):
    """日報・イベントの画像．公開ページからも参照されるのでログイン不要．
       名前はアップロード時刻つきで推測しにくい．アプリ配下に無ければ旧置き場を見る．"""
    name = secure_filename(filename)
    if not name or name != filename:
        abort(404)
    for folder in (MACHINAKA_UPLOAD_FOLDER, MACHINAKA_LEGACY_UPLOAD_FOLDER):
        if os.path.isfile(os.path.join(folder, name)):
            return send_from_directory(folder, name)
    abort(404)


# 旧エンドポイント名 machinaka.dashboard でも同じトップを開けるようにしておく（ブックマーク・旧ランチャ用）
machinaka_bp.add_url_rule('/', endpoint='dashboard', view_func=index)
