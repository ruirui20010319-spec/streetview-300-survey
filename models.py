from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from database import Base


class SurveyConfig(Base):
    __tablename__ = "survey_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    survey_version = Column(String, nullable=False)
    assignment_version = Column(String, nullable=False)
    dimension_config_version = Column(String, nullable=False)
    dimension_key = Column(String, nullable=False)
    dimension_label = Column(String, nullable=False)
    dimension_description = Column(Text, nullable=True)
    dimension_order = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    expected_pairs_per_attempt = Column(Integer, nullable=False, default=30)
    expected_dimension_count = Column(Integer, nullable=False, default=8)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    activated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "dimension_config_version",
            "dimension_key",
            name="uq_dimension_version_key",
        ),
    )


class ImageMaster(Base):
    __tablename__ = "image_master"

    qid = Column(String, primary_key=True)
    image_id = Column(String, nullable=False, unique=True, index=True)
    city = Column(String, nullable=True)
    longitude = Column(Float, nullable=True)
    latitude = Column(Float, nullable=True)
    capture_date = Column(String, nullable=True)
    capture_year = Column(Integer, nullable=True)
    cluster_id = Column(String, nullable=True)
    image_filename = Column(Text, nullable=True)
    image_relative_path = Column(Text, nullable=True)
    oss_url = Column(Text, nullable=False)
    source_master_version = Column(String, nullable=True)
    image_sha256_or_oss_etag = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)


class PairAssignment(Base):
    __tablename__ = "pair_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    assignment_version = Column(String, nullable=False, index=True)
    source_row_id = Column(Integer, nullable=True)
    pair_id = Column(String, nullable=False, index=True)
    participant_slot = Column(String, nullable=False, index=True)
    order_in_participant = Column(Integer, nullable=False)

    left_qid = Column(String, nullable=False)
    right_qid = Column(String, nullable=False)
    left_image_id = Column(String, nullable=False)
    right_image_id = Column(String, nullable=False)
    left_image_filename = Column(Text, nullable=True)
    right_image_filename = Column(Text, nullable=True)
    left_image_relative_path = Column(Text, nullable=True)
    right_image_relative_path = Column(Text, nullable=True)
    left_oss_url = Column(Text, nullable=False)
    right_oss_url = Column(Text, nullable=False)
    left_city = Column(String, nullable=True)
    right_city = Column(String, nullable=True)
    left_cluster = Column(String, nullable=True)
    right_cluster = Column(String, nullable=True)
    same_city = Column(Boolean, nullable=True)
    same_cluster = Column(Boolean, nullable=True)
    pair_type = Column(String, nullable=True)
    left_right_random_seed = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "assignment_version",
            "participant_slot",
            "order_in_participant",
            name="uq_assignment_slot_order",
        ),
        UniqueConstraint(
            "assignment_version",
            "participant_slot",
            "pair_id",
            name="uq_assignment_slot_pair",
        ),
    )


class SurveySlot(Base):
    __tablename__ = "survey_slots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    assignment_version = Column(String, nullable=False)
    participant_slot = Column(String, nullable=False)
    slot_status = Column(String, nullable=False, default="available")

    active_attempt_id = Column(String, nullable=True)
    completed_attempt_id = Column(String, nullable=True)

    claimed_at = Column(DateTime, nullable=True)
    last_activity_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    expired_at = Column(DateTime, nullable=True)
    release_count = Column(Integer, nullable=False, default=0)
    release_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "assignment_version",
            "participant_slot",
            name="uq_slot_version",
        ),
    )


class SurveyAttempt(Base):
    __tablename__ = "survey_attempts"

    attempt_id = Column(String, primary_key=True)
    participant_id = Column(String, nullable=False, index=True)
    participant_slot = Column(String, nullable=False, index=True)
    session_id = Column(String, nullable=False, index=True)

    survey_version = Column(String, nullable=False)
    assignment_version = Column(String, nullable=False)
    dimension_config_version = Column(String, nullable=False)
    attempt_number_for_slot = Column(Integer, nullable=False, default=1)
    completion_status = Column(String, nullable=False, default="in_progress")

    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_activity_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    expired_at = Column(DateTime, nullable=True)
    current_order = Column(Integer, nullable=False, default=0)
    answered_pair_count = Column(Integer, nullable=False, default=0)

    consent_given = Column(Boolean, nullable=False, default=True)
    consent_version = Column(String, nullable=True)
    consent_at = Column(DateTime, nullable=True)

    is_valid = Column(Boolean, nullable=False, default=False)
    invalid_reason = Column(Text, nullable=True)
    admin_note = Column(Text, nullable=True)

    gender = Column(String, nullable=True)
    age_group = Column(String, nullable=True)
    current_residence = Column(String, nullable=True)
    chengdu_familiarity = Column(String, nullable=True)
    chongqing_familiarity = Column(String, nullable=True)
    professional_background = Column(String, nullable=True)

    travel_walk = Column(Boolean, nullable=False, default=False)
    travel_bike_ebike = Column(Boolean, nullable=False, default=False)
    travel_public_transit = Column(Boolean, nullable=False, default=False)
    travel_private_car = Column(Boolean, nullable=False, default=False)
    travel_taxi_ridehailing = Column(Boolean, nullable=False, default=False)
    travel_other = Column(Boolean, nullable=False, default=False)
    travel_other_text = Column(Text, nullable=True)

    device_type = Column(String, nullable=True)
    browser_name = Column(String, nullable=True)
    operating_system = Column(String, nullable=True)
    screen_width = Column(Integer, nullable=True)
    screen_height = Column(Integer, nullable=True)
    language = Column(String, nullable=True)
    timezone = Column(String, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SurveyResponse(Base):
    __tablename__ = "survey_responses"

    response_id = Column(String, primary_key=True)
    pair_submission_id = Column(String, nullable=False, index=True)

    attempt_id = Column(String, nullable=False, index=True)
    participant_id = Column(String, nullable=False, index=True)
    participant_slot = Column(String, nullable=False, index=True)
    session_id = Column(String, nullable=False, index=True)

    survey_version = Column(String, nullable=False)
    assignment_version = Column(String, nullable=False)
    dimension_config_version = Column(String, nullable=False)

    pair_id = Column(String, nullable=False, index=True)
    order_in_participant = Column(Integer, nullable=False)
    dimension_key = Column(String, nullable=False)
    dimension_order = Column(Integer, nullable=False)

    left_qid = Column(String, nullable=False)
    right_qid = Column(String, nullable=False)
    left_image_id = Column(String, nullable=False)
    right_image_id = Column(String, nullable=False)
    choice = Column(String, nullable=False)

    question_served_at = Column(DateTime, nullable=False)
    left_image_loaded = Column(Boolean, nullable=False)
    right_image_loaded = Column(Boolean, nullable=False)
    both_images_loaded_at = Column(DateTime, nullable=False)
    question_submit_time_client = Column(DateTime, nullable=False)
    server_received_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    server_saved_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    response_time_sec = Column(Float, nullable=False)

    is_duplicate_retry = Column(Boolean, nullable=False, default=False)
    response_status = Column(String, nullable=False, default="saved")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "pair_id",
            "dimension_key",
            name="uq_attempt_pair_dimension",
        ),
    )


class SurveyEventLog(Base):
    __tablename__ = "survey_event_logs"

    event_id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(String, nullable=True, index=True)
    participant_id = Column(String, nullable=True)
    participant_slot = Column(String, nullable=True)
    session_id = Column(String, nullable=True)
    pair_id = Column(String, nullable=True)
    order_in_participant = Column(Integer, nullable=True)
    event_type = Column(String, nullable=False)
    client_time = Column(DateTime, nullable=True)
    server_time = Column(DateTime, nullable=False, default=datetime.utcnow)
    event_data = Column(Text, nullable=True)
