import csv
import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from config import (
    ADMIN_PASSWORD,
    ASSIGNMENT_VERSION,
    DIMENSION_CONFIG_VERSION,
    EXPECTED_PAIRS_PER_ATTEMPT,
    SECRET_KEY,
    SURVEY_DIMENSIONS,
    SURVEY_VERSION,
    VALID_CHOICES,
)
from database import SessionLocal
from models import (
    PairAssignment,
    SurveyAttempt,
    SurveyEventLog,
    SurveyResponse,
    SurveySlot,
)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_client_datetime(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("客户端时间缺失")

    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)

    return parsed


app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=128 * 1024,
)


@app.before_request
def open_database_session():
    g.db = SessionLocal()


@app.teardown_request
def close_database_session(exception=None):
    db = g.pop("db", None)
    if db is not None:
        if exception is not None:
            db.rollback()
        db.close()


def get_db():
    return g.db


def get_session_attempt(db, *, for_update=False):
    attempt_id = session.get("attempt_id")
    participant_id = session.get("participant_id")
    participant_slot = session.get("participant_slot")
    session_id = session.get("session_id")

    if not all(
        [attempt_id, participant_id, participant_slot, session_id]
    ):
        return None

    stmt = select(SurveyAttempt).where(
        SurveyAttempt.attempt_id == attempt_id,
        SurveyAttempt.participant_id == participant_id,
        SurveyAttempt.participant_slot == participant_slot,
        SurveyAttempt.session_id == session_id,
    )
    if for_update:
        stmt = stmt.with_for_update()

    return db.execute(stmt).scalar_one_or_none()


def add_event(
    db,
    *,
    attempt=None,
    event_type,
    pair=None,
    event_data=None,
    client_time=None,
):
    db.add(
        SurveyEventLog(
            attempt_id=attempt.attempt_id if attempt else None,
            participant_id=(
                attempt.participant_id if attempt else None
            ),
            participant_slot=(
                attempt.participant_slot if attempt else None
            ),
            session_id=attempt.session_id if attempt else None,
            pair_id=pair.pair_id if pair else None,
            order_in_participant=(
                pair.order_in_participant if pair else None
            ),
            event_type=event_type,
            client_time=client_time,
            server_time=utcnow(),
            event_data=(
                json.dumps(event_data, ensure_ascii=False)
                if event_data is not None
                else None
            ),
        )
    )


def validate_profile_form(form):
    required_fields = {
        "gender": "性别",
        "age_group": "年龄段",
        "current_residence": "常住地",
        "chengdu_familiarity": "成都熟悉度",
        "chongqing_familiarity": "重庆熟悉度",
        "professional_background": "专业背景",
    }

    missing = [
        label
        for key, label in required_fields.items()
        if not form.get(key)
    ]
    if missing:
        raise ValueError("请完整填写：" + "、".join(missing))


def validate_choices(choices):
    if not isinstance(choices, dict):
        raise ValueError("回答数据格式错误")

    dimension_keys = [item["key"] for item in SURVEY_DIMENSIONS]
    missing = [key for key in dimension_keys if key not in choices]
    if missing:
        raise ValueError(
            "存在未回答指标：" + "、".join(missing)
        )

    extra = [key for key in choices if key not in dimension_keys]
    if extra:
        raise ValueError(
            "存在未配置指标：" + "、".join(extra)
        )

    illegal = {
        key: value
        for key, value in choices.items()
        if value not in VALID_CHOICES
    }
    if illegal:
        raise ValueError(f"存在非法选项：{illegal}")


def require_admin(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        supplied = request.args.get("pwd", "")
        authorization = request.authorization
        basic_password = (
            authorization.password if authorization else ""
        )

        if supplied != ADMIN_PASSWORD and basic_password != ADMIN_PASSWORD:
            return Response(
                "需要管理员密码",
                401,
                {"WWW-Authenticate": 'Basic realm="Survey Admin"'},
            )
        return view_function(*args, **kwargs)

    return wrapped


@app.route("/", methods=["GET", "POST"])
def index():
    db = get_db()
    existing_attempt = get_session_attempt(db)

    if (
        existing_attempt is not None
        and existing_attempt.completion_status == "in_progress"
    ):
        return redirect(url_for("survey"))

    if request.method == "GET":
        return render_template("index.html")

    try:
        validate_profile_form(request.form)

        now = utcnow()
        slot = db.execute(
            select(SurveySlot)
            .where(
                SurveySlot.assignment_version
                == ASSIGNMENT_VERSION,
                SurveySlot.slot_status == "available",
            )
            .order_by(SurveySlot.participant_slot)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()

        if slot is None:
            db.rollback()
            return (
                "<h3 style='font-family:sans-serif;text-align:center;"
                "margin-top:100px'>当前没有可分配的问卷槽位。"
                "感谢您的关注。</h3>",
                503,
            )

        participant_id = (
            f"U{uuid.uuid4().hex[:10].upper()}"
        )
        attempt_id = str(uuid.uuid4())
        browser_session_id = str(uuid.uuid4())

        previous_attempt_count = db.scalar(
            select(func.count())
            .select_from(SurveyAttempt)
            .where(
                SurveyAttempt.assignment_version
                == ASSIGNMENT_VERSION,
                SurveyAttempt.participant_slot
                == slot.participant_slot,
            )
        )

        attempt = SurveyAttempt(
            attempt_id=attempt_id,
            participant_id=participant_id,
            participant_slot=slot.participant_slot,
            session_id=browser_session_id,
            survey_version=SURVEY_VERSION,
            assignment_version=ASSIGNMENT_VERSION,
            dimension_config_version=DIMENSION_CONFIG_VERSION,
            attempt_number_for_slot=(
                int(previous_attempt_count or 0) + 1
            ),
            completion_status="in_progress",
            started_at=now,
            last_activity_at=now,
            current_order=0,
            answered_pair_count=0,
            consent_given=True,
            consent_version="consent_v1",
            consent_at=now,
            is_valid=False,
            gender=request.form.get("gender"),
            age_group=request.form.get("age_group"),
            current_residence=request.form.get(
                "current_residence"
            ),
            chengdu_familiarity=request.form.get(
                "chengdu_familiarity"
            ),
            chongqing_familiarity=request.form.get(
                "chongqing_familiarity"
            ),
            professional_background=request.form.get(
                "professional_background"
            ),
            travel_walk=bool(
                request.form.get("travel_walk")
            ),
            travel_bike_ebike=bool(
                request.form.get("travel_bike_ebike")
            ),
            travel_public_transit=bool(
                request.form.get("travel_public_transit")
            ),
            travel_private_car=bool(
                request.form.get("travel_private_car")
            ),
            travel_taxi_ridehailing=bool(
                request.form.get(
                    "travel_taxi_ridehailing"
                )
            ),
            travel_other=bool(
                request.form.get("travel_other")
            ),
            travel_other_text=request.form.get(
                "travel_other_text", ""
            ).strip(),
            language=request.headers.get(
                "Accept-Language", ""
            )[:100],
            created_at=now,
            updated_at=now,
        )

        slot.slot_status = "in_progress"
        slot.active_attempt_id = attempt_id
        slot.claimed_at = now
        slot.last_activity_at = now
        slot.updated_at = now

        db.add(attempt)
        add_event(
            db,
            attempt=attempt,
            event_type="slot_claimed",
            event_data={
                "user_agent": request.headers.get(
                    "User-Agent", ""
                )
            },
        )
        db.commit()

        session.clear()
        session.permanent = True
        session["attempt_id"] = attempt_id
        session["participant_id"] = participant_id
        session["participant_slot"] = slot.participant_slot
        session["session_id"] = browser_session_id

        return redirect(url_for("survey"))

    except ValueError as error:
        db.rollback()
        return (
            render_template(
                "index.html",
                error_message=str(error),
            ),
            400,
        )
    except Exception as error:
        db.rollback()
        app.logger.exception("领取问卷槽位失败")
        return (
            "<h3 style='font-family:sans-serif;text-align:center;"
            "margin-top:100px'>系统暂时无法分配问卷，请稍后重试。"
            "</h3>",
            500,
        )


@app.route("/survey")
def survey():
    db = get_db()
    attempt = get_session_attempt(db)

    if attempt is None:
        session.clear()
        return redirect(url_for("index"))

    if attempt.completion_status == "completed":
        return redirect(url_for("thank_you"))

    if attempt.completion_status != "in_progress":
        session.clear()
        return redirect(url_for("index"))

    return render_template("survey.html")


@app.route("/get_current_question")
def get_current_question():
    db = get_db()
    attempt = get_session_attempt(db)

    if attempt is None:
        return jsonify(
            status="error",
            message="会话不存在或已过期",
        ), 401

    if attempt.completion_status == "completed":
        return jsonify(status="completed")

    if attempt.completion_status != "in_progress":
        return jsonify(
            status="error",
            message="当前问卷状态不可继续",
        ), 409

    next_order = attempt.answered_pair_count + 1
    pair = db.execute(
        select(PairAssignment).where(
            PairAssignment.assignment_version
            == attempt.assignment_version,
            PairAssignment.participant_slot
            == attempt.participant_slot,
            PairAssignment.order_in_participant
            == next_order,
        )
    ).scalar_one_or_none()

    if pair is None:
        return jsonify(
            status="error",
            message="未找到当前槽位对应的固定题目",
        ), 500

    served_at = utcnow()
    attempt.last_activity_at = served_at
    attempt.updated_at = served_at

    slot = db.execute(
        select(SurveySlot).where(
            SurveySlot.assignment_version
            == attempt.assignment_version,
            SurveySlot.participant_slot
            == attempt.participant_slot,
        )
    ).scalar_one_or_none()
    if slot is not None:
        slot.last_activity_at = served_at
        slot.updated_at = served_at

    add_event(
        db,
        attempt=attempt,
        pair=pair,
        event_type="question_served",
    )
    db.commit()

    return jsonify(
        status="success",
        current_index=next_order - 1,
        total_questions=EXPECTED_PAIRS_PER_ATTEMPT,
        question_served_at=served_at.isoformat() + "Z",
        question={
            "order": pair.order_in_participant,
            "pair_id": pair.pair_id,
            "left_img_url": pair.left_oss_url,
            "right_img_url": pair.right_oss_url,
        },
    )


@app.route("/submit_response", methods=["POST"])
def submit_response():
    db = get_db()
    payload = request.get_json(silent=True) or {}

    try:
        attempt = get_session_attempt(db, for_update=True)
        if attempt is None:
            raise PermissionError("会话不存在或已过期")

        if attempt.completion_status == "completed":
            return jsonify(
                status="completed",
                completed=True,
            )

        if attempt.completion_status != "in_progress":
            raise ValueError("当前问卷状态不可提交")

        submitted_pair_id = str(
            payload.get("pair_id", "")
        ).strip()
        if not submitted_pair_id:
            raise ValueError("缺少 pair_id")

        choices = payload.get("choices")
        validate_choices(choices)

        # 网络重试或双击后，若这一题已经完整保存，
        # 直接返回成功，不再重复写入和增加进度。
        existing_count = db.scalar(
            select(func.count())
            .select_from(SurveyResponse)
            .where(
                SurveyResponse.attempt_id
                == attempt.attempt_id,
                SurveyResponse.pair_id
                == submitted_pair_id,
            )
        )
        expected_dimension_count = len(SURVEY_DIMENSIONS)

        if existing_count == expected_dimension_count:
            db.rollback()
            return jsonify(
                status="success",
                duplicate_retry=True,
                completed=(
                    attempt.completion_status == "completed"
                ),
            )

        if existing_count not in (0, None):
            raise RuntimeError(
                "检测到当前题存在不完整的历史记录，请联系管理员"
            )

        next_order = attempt.answered_pair_count + 1
        expected_pair = db.execute(
            select(PairAssignment).where(
                PairAssignment.assignment_version
                == attempt.assignment_version,
                PairAssignment.participant_slot
                == attempt.participant_slot,
                PairAssignment.order_in_participant
                == next_order,
            )
        ).scalar_one_or_none()

        if expected_pair is None:
            raise RuntimeError("未找到服务器固定题目")

        if submitted_pair_id != expected_pair.pair_id:
            raise ValueError("提交题目与服务器固定配对不一致")

        submitted_order = int(
            payload.get("order_in_participant", 0)
        )
        if (
            submitted_order
            != expected_pair.order_in_participant
        ):
            raise ValueError("提交题序与服务器当前进度不一致")

        if payload.get("left_image_loaded") is not True:
            raise ValueError("左侧图片未加载成功")
        if payload.get("right_image_loaded") is not True:
            raise ValueError("右侧图片未加载成功")

        both_images_loaded_at = parse_client_datetime(
            payload.get("both_images_loaded_at")
        )
        submit_time_client = parse_client_datetime(
            payload.get("question_submit_time_client")
        )

        served_event = db.execute(
            select(SurveyEventLog)
            .where(
                SurveyEventLog.attempt_id
                == attempt.attempt_id,
                SurveyEventLog.pair_id
                == expected_pair.pair_id,
                SurveyEventLog.event_type
                == "question_served",
            )
            .order_by(SurveyEventLog.event_id.desc())
            .limit(1)
        ).scalar_one_or_none()

        if served_event is None:
            raise ValueError("未找到当前题的展示记录，请刷新页面")

        now = utcnow()
        response_time_sec = max(
            round(
                (
                    now - served_event.server_time
                ).total_seconds(),
                3,
            ),
            0.0,
        )

        pair_submission_id = str(uuid.uuid4())

        for dimension in SURVEY_DIMENSIONS:
            db.add(
                SurveyResponse(
                    response_id=str(uuid.uuid4()),
                    pair_submission_id=pair_submission_id,
                    attempt_id=attempt.attempt_id,
                    participant_id=attempt.participant_id,
                    participant_slot=attempt.participant_slot,
                    session_id=attempt.session_id,
                    survey_version=attempt.survey_version,
                    assignment_version=(
                        attempt.assignment_version
                    ),
                    dimension_config_version=(
                        attempt.dimension_config_version
                    ),
                    pair_id=expected_pair.pair_id,
                    order_in_participant=(
                        expected_pair.order_in_participant
                    ),
                    dimension_key=dimension["key"],
                    dimension_order=dimension["order"],
                    left_qid=expected_pair.left_qid,
                    right_qid=expected_pair.right_qid,
                    left_image_id=(
                        expected_pair.left_image_id
                    ),
                    right_image_id=(
                        expected_pair.right_image_id
                    ),
                    choice=choices[dimension["key"]],
                    question_served_at=(
                        served_event.server_time
                    ),
                    left_image_loaded=True,
                    right_image_loaded=True,
                    both_images_loaded_at=(
                        both_images_loaded_at
                    ),
                    question_submit_time_client=(
                        submit_time_client
                    ),
                    server_received_at=now,
                    server_saved_at=now,
                    response_time_sec=response_time_sec,
                    is_duplicate_retry=False,
                    response_status="saved",
                    created_at=now,
                )
            )

        attempt.answered_pair_count += 1
        attempt.current_order = attempt.answered_pair_count
        attempt.last_activity_at = now
        attempt.updated_at = now

        slot = db.execute(
            select(SurveySlot)
            .where(
                SurveySlot.assignment_version
                == attempt.assignment_version,
                SurveySlot.participant_slot
                == attempt.participant_slot,
            )
            .with_for_update()
        ).scalar_one()

        slot.last_activity_at = now
        slot.updated_at = now

        add_event(
            db,
            attempt=attempt,
            pair=expected_pair,
            event_type="response_saved",
            event_data={
                "pair_submission_id": pair_submission_id,
                "response_time_sec": response_time_sec,
            },
            client_time=submit_time_client,
        )

        completed = (
            attempt.answered_pair_count
            == EXPECTED_PAIRS_PER_ATTEMPT
        )
        if completed:
            attempt.completion_status = "completed"
            attempt.completed_at = now
            attempt.is_valid = True

            slot.slot_status = "completed"
            slot.active_attempt_id = None
            slot.completed_attempt_id = attempt.attempt_id
            slot.completed_at = now

            add_event(
                db,
                attempt=attempt,
                event_type="survey_completed",
            )

        db.commit()

        return jsonify(
            status="success",
            completed=completed,
            answered_pair_count=attempt.answered_pair_count,
        )

    except PermissionError as error:
        db.rollback()
        return jsonify(
            status="error",
            message=str(error),
        ), 401
    except (ValueError, RuntimeError) as error:
        db.rollback()
        app.logger.warning(
            "回答校验失败：%s", error
        )
        return jsonify(
            status="error",
            message=str(error),
        ), 400
    except IntegrityError:
        db.rollback()
        app.logger.exception("数据库唯一约束拦截重复提交")
        return jsonify(
            status="error",
            message="检测到重复提交，请刷新页面继续",
        ), 409
    except Exception as error:
        db.rollback()
        app.logger.exception("保存答题记录失败")
        return jsonify(
            status="error",
            message="服务器保存失败，请稍后重试",
        ), 500


@app.route("/thank_you")
def thank_you():
    db = get_db()
    attempt = get_session_attempt(db)

    if attempt is None:
        return redirect(url_for("index"))

    if attempt.completion_status != "completed":
        return redirect(url_for("survey"))

    participant_slot = attempt.participant_slot
    session.clear()

    return f"""
    <div style="text-align:center;margin-top:100px;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
    sans-serif;">
      <h1 style="color:#34c759;font-size:32px;">
        🎉 问卷全部完成！
      </h1>
      <p style="color:#666;font-size:16px;">
        感谢您的参与，您的完整作答已安全保存。
      </p>
      <p style="color:#999;font-size:13px;">
        问卷编号：{participant_slot}
      </p>
    </div>
    """


@app.route("/admin")
@require_admin
def admin_dashboard():
    db = get_db()

    slot_counts = dict(
        db.execute(
            select(
                SurveySlot.slot_status,
                func.count(SurveySlot.id),
            )
            .where(
                SurveySlot.assignment_version
                == ASSIGNMENT_VERSION
            )
            .group_by(SurveySlot.slot_status)
        ).all()
    )

    attempt_count = db.scalar(
        select(func.count()).select_from(SurveyAttempt)
    )
    response_count = db.scalar(
        select(func.count()).select_from(SurveyResponse)
    )

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:900px;
    margin:40px auto;line-height:1.8">
      <h1>街景感知问卷管理后台</h1>
      <p>可用槽位：{slot_counts.get('available', 0)}</p>
      <p>进行中槽位：{slot_counts.get('in_progress', 0)}</p>
      <p>已完成槽位：{slot_counts.get('completed', 0)}</p>
      <p>尝试总数：{attempt_count}</p>
      <p>回答总行数：{response_count}</p>
      <hr>
      <p><a href="/admin/export/slots">
      导出 survey_slots.csv</a></p>
      <p><a href="/admin/export/attempts">
      导出 survey_attempts.csv</a></p>
      <p><a href="/admin/export/responses_raw">
      导出 survey_responses_raw.csv</a></p>
      <p><a href="/admin/export/responses_valid">
      导出 survey_responses_valid.csv</a></p>
    </div>
    """


def csv_response(filename, headers, rows):
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)

    response = app.response_class(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{filename}"'
    )
    return response


@app.route("/admin/export/slots")
@require_admin
def export_slots():
    db = get_db()
    records = db.execute(
        select(SurveySlot).order_by(
            SurveySlot.participant_slot
        )
    ).scalars().all()

    headers = [
        "assignment_version",
        "participant_slot",
        "slot_status",
        "active_attempt_id",
        "completed_attempt_id",
        "claimed_at",
        "last_activity_at",
        "completed_at",
        "expired_at",
        "release_count",
        "release_reason",
    ]
    rows = [
        [
            item.assignment_version,
            item.participant_slot,
            item.slot_status,
            item.active_attempt_id,
            item.completed_attempt_id,
            item.claimed_at,
            item.last_activity_at,
            item.completed_at,
            item.expired_at,
            item.release_count,
            item.release_reason,
        ]
        for item in records
    ]
    return csv_response("survey_slots.csv", headers, rows)


@app.route("/admin/export/attempts")
@require_admin
def export_attempts():
    db = get_db()
    records = db.execute(
        select(SurveyAttempt).order_by(
            SurveyAttempt.started_at
        )
    ).scalars().all()

    headers = [
        "attempt_id",
        "participant_id",
        "participant_slot",
        "session_id",
        "survey_version",
        "assignment_version",
        "dimension_config_version",
        "attempt_number_for_slot",
        "completion_status",
        "started_at",
        "last_activity_at",
        "completed_at",
        "current_order",
        "answered_pair_count",
        "is_valid",
        "invalid_reason",
        "gender",
        "age_group",
        "current_residence",
        "chengdu_familiarity",
        "chongqing_familiarity",
        "professional_background",
        "travel_walk",
        "travel_bike_ebike",
        "travel_public_transit",
        "travel_private_car",
        "travel_taxi_ridehailing",
        "travel_other",
        "travel_other_text",
    ]
    rows = [
        [getattr(item, column) for column in headers]
        for item in records
    ]
    return csv_response("survey_attempts.csv", headers, rows)


def response_export_query(valid_only):
    stmt = (
        select(SurveyResponse)
        .join(
            SurveyAttempt,
            SurveyAttempt.attempt_id
            == SurveyResponse.attempt_id,
        )
        .order_by(
            SurveyResponse.participant_slot,
            SurveyResponse.order_in_participant,
            SurveyResponse.dimension_order,
        )
    )
    if valid_only:
        stmt = stmt.where(
            SurveyAttempt.completion_status == "completed",
            SurveyAttempt.is_valid.is_(True),
            SurveyAttempt.answered_pair_count
            == EXPECTED_PAIRS_PER_ATTEMPT,
        )
    return stmt


@app.route("/admin/export/responses_raw")
@require_admin
def export_responses_raw():
    return export_responses(valid_only=False)


@app.route("/admin/export/responses_valid")
@require_admin
def export_responses_valid():
    return export_responses(valid_only=True)


def export_responses(valid_only):
    db = get_db()
    records = db.execute(
        response_export_query(valid_only)
    ).scalars().all()

    headers = [
        "response_id",
        "pair_submission_id",
        "attempt_id",
        "participant_id",
        "participant_slot",
        "session_id",
        "survey_version",
        "assignment_version",
        "dimension_config_version",
        "pair_id",
        "order_in_participant",
        "dimension_key",
        "dimension_order",
        "left_qid",
        "right_qid",
        "left_image_id",
        "right_image_id",
        "choice",
        "question_served_at",
        "left_image_loaded",
        "right_image_loaded",
        "both_images_loaded_at",
        "question_submit_time_client",
        "server_received_at",
        "server_saved_at",
        "response_time_sec",
        "is_duplicate_retry",
        "response_status",
    ]
    rows = [
        [getattr(item, column) for column in headers]
        for item in records
    ]
    filename = (
        "survey_responses_valid.csv"
        if valid_only
        else "survey_responses_raw.csv"
    )
    return csv_response(filename, headers, rows)


@app.route("/health")
def health():
    db = get_db()
    db.scalar(select(func.count()).select_from(SurveySlot))
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
