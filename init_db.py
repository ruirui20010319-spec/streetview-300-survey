"""Idempotent database initialisation for the 500-image formal assignment.

This script never drops tables, deletes attempts, resets slot states, or changes
an assignment after formal responses have started. Existing 300-image data may
remain in the database under its old assignment version.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import (
    ASSIGNMENT_TABLE_PATH,
    ASSIGNMENT_VERSION,
    DIMENSION_CONFIG_VERSION,
    EXPECTED_DIMENSION_COUNT,
    EXPECTED_IMAGE_COUNT,
    EXPECTED_PAIR_COUNT,
    EXPECTED_PAIRS_PER_ATTEMPT,
    EXPECTED_SLOT_COUNT,
    IMAGE_DETAIL_TABLE_PATH,
    IMAGE_METADATA_TABLE_PATH,
    OSS_BASE_URL,
    SURVEY_DIMENSIONS,
    SURVEY_VERSION,
)
from database import Base, SessionLocal, engine
from models import (
    ImageMaster,
    PairAssignment,
    SurveyAttempt,
    SurveyConfig,
    SurveySlot,
)


REQUIRED_ASSIGNMENT_COLUMNS = {
    "pair_id",
    "left_qid",
    "right_qid",
    "left_image_id",
    "right_image_id",
    "left_image_filename",
    "right_image_filename",
    "left_image_relative_path",
    "right_image_relative_path",
    "left_city",
    "right_city",
    "left_cluster",
    "right_cluster",
    "same_city",
    "same_cluster",
    "pair_type",
    "left_right_random_seed",
    "participant_slot",
    "order_in_participant",
}

REQUIRED_IMAGE_COLUMNS = {
    "qid",
    "image_id",
    "image_filename",
    "final_image_filename",
    "image_relative_path",
    "city",
    "lon",
    "lat",
    "streetclip_cluster_k25",
}


class UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            next_item = self.parent[item]
            self.parent[item] = root
            item = next_item
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"找不到输入表：{path}")
    if path.suffix.lower() in {".xlsx", ".xls"}:
        frame = pd.read_excel(path)
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(path, encoding="utf-8-sig")
    else:
        raise ValueError(f"不支持的输入格式：{path.suffix}")
    frame.columns = frame.columns.astype(str).str.strip()
    return frame


def clean_text(value):
    if pd.isna(value):
        return None
    result = str(value).strip()
    return result or None


def clean_bool(value):
    if pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def clean_int(value):
    if pd.isna(value):
        return None
    return int(float(value))


def normalize_city(value):
    text_value = (clean_text(value) or "").lower()
    if text_value in {"cd", "chengdu", "成都"} or "chengdu" in text_value:
        return "chengdu"
    if text_value in {"cq", "chongqing", "重庆"} or "chongqing" in text_value:
        return "chongqing"
    return text_value or None


def build_oss_url(explicit_url, relative_path, filename):
    explicit = clean_text(explicit_url)
    if explicit:
        return explicit

    path_value = clean_text(relative_path) or clean_text(filename)
    if not path_value:
        raise ValueError("图片缺少OSS URL、相对路径和文件名")

    path_value = path_value.replace("\\", "/").lstrip("/")
    if path_value.startswith("images/"):
        path_value = path_value[len("images/"):]
    return f"{OSS_BASE_URL}/{path_value}"


def validate_assignment(df: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(REQUIRED_ASSIGNMENT_COLUMNS - set(df.columns))
    if missing:
        raise ValueError("固定配对表缺少字段：" + ", ".join(missing))

    if len(df) != EXPECTED_PAIR_COUNT:
        raise ValueError(
            f"固定配对表应为{EXPECTED_PAIR_COUNT}行，实际为{len(df)}行"
        )

    for column in [
        "pair_id", "left_qid", "right_qid", "left_image_id",
        "right_image_id", "participant_slot",
    ]:
        if df[column].isna().any():
            raise ValueError(f"固定配对表字段 {column} 存在空值")
        df[column] = df[column].astype(str).str.strip()

    df["order_in_participant"] = pd.to_numeric(
        df["order_in_participant"], errors="raise"
    ).astype(int)

    if df["pair_id"].nunique() != EXPECTED_PAIR_COUNT:
        raise ValueError("pair_id必须在7500行中全局唯一")

    slot_counts = df.groupby("participant_slot").size().sort_index()
    if len(slot_counts) != EXPECTED_SLOT_COUNT:
        raise ValueError(
            f"应有{EXPECTED_SLOT_COUNT}个槽位，实际为{len(slot_counts)}个"
        )
    if not (slot_counts == EXPECTED_PAIRS_PER_ATTEMPT).all():
        bad = slot_counts[slot_counts != EXPECTED_PAIRS_PER_ATTEMPT].to_dict()
        raise ValueError(f"部分槽位不是30组题：{bad}")

    duplicate_orders = df.duplicated(
        subset=["participant_slot", "order_in_participant"]
    ).sum()
    if duplicate_orders:
        raise ValueError(f"发现{duplicate_orders}条槽位题序重复记录")

    expected_orders = set(range(1, EXPECTED_PAIRS_PER_ATTEMPT + 1))
    for slot, group in df.groupby("participant_slot"):
        actual_orders = set(group["order_in_participant"].tolist())
        if actual_orders != expected_orders:
            raise ValueError(f"槽位{slot}的题序不是1—30")
        slot_images = pd.concat(
            [group["left_image_id"], group["right_image_id"]],
            ignore_index=True,
        )
        if slot_images.nunique() != 2 * EXPECTED_PAIRS_PER_ATTEMPT:
            raise ValueError(f"槽位{slot}内存在重复出现的图片")

    expected_pair_types = {
        "same_city_same_cluster": 750,
        "cross_city_same_cluster": 1500,
        "same_city_cross_cluster": 2250,
        "cross_city_cross_cluster": 3000,
    }
    actual_pair_types = df["pair_type"].value_counts().to_dict()
    if actual_pair_types != expected_pair_types:
        raise ValueError(
            f"四类配对数量不符合500张正式设计：actual={actual_pair_types}, "
            f"expected={expected_pair_types}"
        )

    city_consistency = (
        df["left_city"].apply(normalize_city)
        == df["right_city"].apply(normalize_city)
    )
    cluster_consistency = (
        df["left_cluster"].astype(str).str.strip()
        == df["right_cluster"].astype(str).str.strip()
    )
    stored_same_city = df["same_city"].apply(clean_bool)
    stored_same_cluster = df["same_cluster"].apply(clean_bool)
    if not (city_consistency == stored_same_city).all():
        raise ValueError("same_city字段与左右城市不一致")
    if not (cluster_consistency == stored_same_cluster).all():
        raise ValueError("same_cluster字段与左右cluster不一致")

    expected_slot_type_counts = {
        "same_city_same_cluster": 3,
        "cross_city_same_cluster": 6,
        "same_city_cross_cluster": 9,
        "cross_city_cross_cluster": 12,
    }
    for slot, group in df.groupby("participant_slot"):
        actual = group["pair_type"].value_counts().to_dict()
        if actual != expected_slot_type_counts:
            raise ValueError(
                f"槽位{slot}的四类配对不是3/6/9/12：{actual}"
            )

    left_counts = df["left_image_id"].value_counts()
    right_counts = df["right_image_id"].value_counts()
    if not left_counts.sort_index().equals(right_counts.sort_index()):
        diff = (left_counts - right_counts).fillna(0)
        raise ValueError(
            "图片左右出现次数不平衡，最大差值="
            + str(int(diff.abs().max()))
        )

    appearances = pd.concat(
        [
            df["left_image_id"].rename("image_id"),
            df["right_image_id"].rename("image_id"),
        ],
        ignore_index=True,
    ).value_counts()
    if len(appearances) != EXPECTED_IMAGE_COUNT:
        raise ValueError(
            f"配对表应覆盖{EXPECTED_IMAGE_COUNT}张图，实际覆盖{len(appearances)}张"
        )
    if not (appearances == 30).all():
        bad = appearances[appearances != 30].to_dict()
        raise ValueError(f"部分图片不是恰好出现30次：{bad}")

    image_ids = sorted(appearances.index.tolist())
    union_find = UnionFind(image_ids)
    for row in df.itertuples(index=False):
        union_find.union(row.left_image_id, row.right_image_id)
    component_count = len({union_find.find(item) for item in image_ids})
    if component_count != 1:
        raise ValueError(f"配对网络不连通，共有{component_count}个连通分量")

    return df


def validate_image_metadata(df: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(REQUIRED_IMAGE_COLUMNS - set(df.columns))
    if missing:
        raise ValueError("500张图像元数据表缺少字段：" + ", ".join(missing))

    if len(df) != EXPECTED_IMAGE_COUNT:
        raise ValueError(
            f"图像元数据表应为{EXPECTED_IMAGE_COUNT}行，实际为{len(df)}行"
        )

    for column in ["qid", "image_id", "image_filename", "image_relative_path"]:
        if df[column].isna().any():
            raise ValueError(f"图像元数据字段 {column} 存在空值")
        df[column] = df[column].astype(str).str.strip()

    if df["qid"].nunique() != EXPECTED_IMAGE_COUNT:
        raise ValueError("qid不是500个唯一值")
    if df["image_id"].nunique() != EXPECTED_IMAGE_COUNT:
        raise ValueError("image_id不是500个唯一值")

    expected_qids = {f"Q{i:03d}" for i in range(1, EXPECTED_IMAGE_COUNT + 1)}
    if set(df["qid"]) != expected_qids:
        raise ValueError("qid必须完整覆盖Q001—Q500")

    if "image_file_exists_in_package" in df.columns:
        exists_flags = df["image_file_exists_in_package"].apply(clean_bool)
        if not exists_flags.fillna(False).all():
            raise ValueError("500张元数据表中存在未进入正式文件包的图片")

    df["_city_norm"] = df["city"].apply(normalize_city)
    city_counts = df["_city_norm"].value_counts().to_dict()
    if city_counts != {"chengdu": 250, "chongqing": 250}:
        raise ValueError(f"城市数量应为成都250、重庆250，实际为{city_counts}")

    return df


def load_optional_detail() -> pd.DataFrame:
    if not IMAGE_DETAIL_TABLE_PATH.exists():
        return pd.DataFrame()
    detail = read_table(IMAGE_DETAIL_TABLE_PATH)
    if "qid" not in detail.columns or "image_id" not in detail.columns:
        raise ValueError("图像明细表必须包含qid和image_id")
    detail["qid"] = detail["qid"].astype(str).str.strip()
    detail["image_id"] = detail["image_id"].astype(str).str.strip()
    if detail["qid"].duplicated().any() or detail["image_id"].duplicated().any():
        raise ValueError("图像明细表中qid或image_id重复")
    return detail


def cross_validate_assignment_images(pair_df, image_df):
    image_by_qid = image_df.set_index("qid")
    for side in ("left", "right"):
        qid_col = f"{side}_qid"
        id_col = f"{side}_image_id"
        missing_qids = set(pair_df[qid_col]) - set(image_by_qid.index)
        if missing_qids:
            raise ValueError(f"配对表{side}侧存在未入500图主表的QID：{sorted(missing_qids)[:10]}")
        expected_ids = pair_df[qid_col].map(image_by_qid["image_id"])
        mismatch = expected_ids.astype(str) != pair_df[id_col].astype(str)
        if mismatch.any():
            examples = pair_df.loc[mismatch, [qid_col, id_col]].head().to_dict("records")
            raise ValueError(f"配对表{side}侧QID与image_id错配：{examples}")


def build_image_rows(image_df, detail_df):
    detail_lookup = {}
    if not detail_df.empty:
        detail_lookup = detail_df.set_index("qid").to_dict("index")

    rows = []
    for row in image_df.to_dict("records"):
        qid = clean_text(row["qid"])
        detail = detail_lookup.get(qid, {})
        year_month = clean_text(
            detail.get("year_month_x")
            if detail.get("year_month_x") is not None
            else detail.get("year_month")
        )
        capture_year = None
        if year_month:
            digits = "".join(ch for ch in year_month if ch.isdigit())
            if len(digits) >= 4:
                capture_year = int(digits[:4])

        final_filename = clean_text(row.get("final_image_filename")) or clean_text(
            row.get("image_filename")
        )
        relative_path = clean_text(row.get("image_relative_path")) or (
            f"images/{final_filename}" if final_filename else None
        )
        rows.append(
            {
                "qid": qid,
                "image_id": clean_text(row["image_id"]),
                "city": normalize_city(row.get("city")),
                "longitude": float(row["lon"]) if not pd.isna(row["lon"]) else None,
                "latitude": float(row["lat"]) if not pd.isna(row["lat"]) else None,
                "capture_date": year_month,
                "capture_year": capture_year,
                "year_month": year_month,
                "cluster_id": clean_text(row.get("streetclip_cluster_k25")),
                "image_filename": final_filename,
                "image_relative_path": relative_path,
                "oss_url": build_oss_url(row.get("oss_url"), relative_path, final_filename),
                "source_master_version": ASSIGNMENT_VERSION,
                "image_sha256_or_oss_etag": clean_text(
                    detail.get("source_sha256") or detail.get("sha256")
                ),
                "sample_origin": clean_text(detail.get("sample_origin")),
                "sample_role": "human_trueskill_anchor",
                "point_id": clean_text(detail.get("point_id")),
                "road_segment_id": clean_text(detail.get("road_segment_id")),
                "is_active": True,
            }
        )
    return rows


def build_pair_rows(df):
    rows = []
    for source_row_id, row in enumerate(df.to_dict("records"), start=1):
        left_relative = clean_text(row["left_image_relative_path"])
        right_relative = clean_text(row["right_image_relative_path"])
        rows.append(
            {
                "assignment_version": ASSIGNMENT_VERSION,
                "source_row_id": source_row_id,
                "pair_id": clean_text(row["pair_id"]),
                "participant_slot": clean_text(row["participant_slot"]),
                "order_in_participant": int(row["order_in_participant"]),
                "left_qid": clean_text(row["left_qid"]),
                "right_qid": clean_text(row["right_qid"]),
                "left_image_id": clean_text(row["left_image_id"]),
                "right_image_id": clean_text(row["right_image_id"]),
                "left_image_filename": clean_text(row["left_image_filename"]),
                "right_image_filename": clean_text(row["right_image_filename"]),
                "left_image_relative_path": left_relative,
                "right_image_relative_path": right_relative,
                "left_oss_url": build_oss_url(
                    row.get("left_oss_url"), left_relative, row["left_image_filename"]
                ),
                "right_oss_url": build_oss_url(
                    row.get("right_oss_url"), right_relative, row["right_image_filename"]
                ),
                "left_city": normalize_city(row["left_city"]),
                "right_city": normalize_city(row["right_city"]),
                "left_cluster": clean_text(row["left_cluster"]),
                "right_cluster": clean_text(row["right_cluster"]),
                "same_city": clean_bool(row["same_city"]),
                "same_cluster": clean_bool(row["same_cluster"]),
                "pair_type": clean_text(row["pair_type"]),
                "left_right_random_seed": clean_text(row["left_right_random_seed"]),
            }
        )
    return rows


def dataset_fingerprint(pair_df, image_df):
    pair_payload = pair_df[
        [
            "participant_slot", "order_in_participant", "pair_id",
            "left_qid", "right_qid", "left_image_id", "right_image_id",
        ]
    ].sort_values(["participant_slot", "order_in_participant"]).to_dict("records")
    image_payload = image_df[
        ["qid", "image_id", "final_image_filename", "city", "streetclip_cluster_k25"]
    ].sort_values("qid").to_dict("records")
    encoded = json.dumps(
        {"pairs": pair_payload, "images": image_payload},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_schema_migrations(db):
    """Add columns introduced after the original 300-image deployment."""
    statements = [
        "ALTER TABLE survey_config ADD COLUMN IF NOT EXISTS dimension_definition TEXT",
        "ALTER TABLE survey_config ADD COLUMN IF NOT EXISTS score_direction INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE survey_config ADD COLUMN IF NOT EXISTS high_score_meaning TEXT",
        "ALTER TABLE image_master ADD COLUMN IF NOT EXISTS year_month VARCHAR",
        "ALTER TABLE image_master ADD COLUMN IF NOT EXISTS sample_origin VARCHAR",
        "ALTER TABLE image_master ADD COLUMN IF NOT EXISTS sample_role VARCHAR",
        "ALTER TABLE image_master ADD COLUMN IF NOT EXISTS point_id VARCHAR",
        "ALTER TABLE image_master ADD COLUMN IF NOT EXISTS road_segment_id VARCHAR",
        "ALTER TABLE survey_attempts ADD COLUMN IF NOT EXISTS profile_submission_token VARCHAR",
        "ALTER TABLE survey_attempts ADD COLUMN IF NOT EXISTS age INTEGER",
        "ALTER TABLE survey_attempts ADD COLUMN IF NOT EXISTS distributor_no INTEGER",
        "ALTER TABLE survey_attempts ADD COLUMN IF NOT EXISTS user_agent TEXT",
        "ALTER TABLE survey_attempts ADD COLUMN IF NOT EXISTS ip_hash VARCHAR",
        "ALTER TABLE survey_attempts ALTER COLUMN consent_given SET DEFAULT FALSE",
        "ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS display_position INTEGER",
        "UPDATE survey_responses SET display_position = dimension_order WHERE display_position IS NULL",
        "ALTER TABLE survey_responses ALTER COLUMN display_position SET NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_profile_submission_token ON survey_attempts(profile_submission_token) WHERE profile_submission_token IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dimension_version_order ON survey_config(dimension_config_version, dimension_order)",
    ]
    for statement in statements:
        db.execute(text(statement))
    db.commit()


def ensure_frozen_config_is_unchanged(db):
    attempt_count = db.scalar(
        select(func.count()).select_from(SurveyAttempt).where(
            SurveyAttempt.assignment_version == ASSIGNMENT_VERSION
        )
    ) or 0
    if attempt_count == 0:
        return

    existing = db.execute(
        select(SurveyConfig).where(
            SurveyConfig.dimension_config_version == DIMENSION_CONFIG_VERSION
        ).order_by(SurveyConfig.dimension_order)
    ).scalars().all()
    expected = [
        (
            item["key"], item["label"], item["description"], item["definition"],
            item["order"], item["score_direction"], item["high_score_meaning"],
        )
        for item in SURVEY_DIMENSIONS
    ]
    actual = [
        (
            item.dimension_key, item.dimension_label,
            item.dimension_description, item.dimension_definition,
            item.dimension_order, item.score_direction,
            item.high_score_meaning,
        )
        for item in existing
    ]
    if actual and actual != expected:
        raise RuntimeError(
            "当前assignment已有attempt，禁止修改同一dimension_config_version下的指标文字或顺序。请使用新的版本号。"
        )


def initialize_database():
    print("1/8 读取并验证500张图像表与7500组固定配对……")
    pair_df = validate_assignment(read_table(ASSIGNMENT_TABLE_PATH))
    image_df = validate_image_metadata(read_table(IMAGE_METADATA_TABLE_PATH))
    detail_df = load_optional_detail()
    cross_validate_assignment_images(pair_df, image_df)
    fingerprint = dataset_fingerprint(pair_df, image_df)
    print(f"数据指纹：{fingerprint}")

    print("2/8 建立数据库表并执行非破坏性字段迁移……")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    advisory_lock_id = 2026072850
    try:
        db.execute(text(f"SELECT pg_advisory_lock({advisory_lock_id})"))
        run_schema_migrations(db)
        ensure_frozen_config_is_unchanged(db)

        image_rows = build_image_rows(image_df, detail_df)
        pair_rows = build_pair_rows(pair_df)
        slots = sorted(pair_df["participant_slot"].unique().tolist())

        print("3/8 写入并冻结9个评价指标……")
        db.execute(
            update(SurveyConfig).where(
                SurveyConfig.dimension_config_version != DIMENSION_CONFIG_VERSION
            ).values(is_active=False)
        )
        config_rows = [
            {
                "survey_version": SURVEY_VERSION,
                "assignment_version": ASSIGNMENT_VERSION,
                "dimension_config_version": DIMENSION_CONFIG_VERSION,
                "dimension_key": item["key"],
                "dimension_label": item["label"],
                "dimension_description": item["description"],
                "dimension_definition": item["definition"],
                "dimension_order": item["order"],
                "score_direction": item["score_direction"],
                "high_score_meaning": item["high_score_meaning"],
                "is_active": True,
                "expected_pairs_per_attempt": EXPECTED_PAIRS_PER_ATTEMPT,
                "expected_dimension_count": EXPECTED_DIMENSION_COUNT,
                "activated_at": utcnow_naive(),
            }
            for item in SURVEY_DIMENSIONS
        ]
        excluded = pg_insert(SurveyConfig).excluded
        db.execute(
            pg_insert(SurveyConfig).values(config_rows).on_conflict_do_update(
                constraint="uq_dimension_version_key",
                set_={
                    "survey_version": excluded.survey_version,
                    "assignment_version": excluded.assignment_version,
                    "dimension_label": excluded.dimension_label,
                    "dimension_description": excluded.dimension_description,
                    "dimension_definition": excluded.dimension_definition,
                    "dimension_order": excluded.dimension_order,
                    "score_direction": excluded.score_direction,
                    "high_score_meaning": excluded.high_score_meaning,
                    "is_active": True,
                    "expected_pairs_per_attempt": excluded.expected_pairs_per_attempt,
                    "expected_dimension_count": excluded.expected_dimension_count,
                    "activated_at": excluded.activated_at,
                },
            )
        )

        print("4/8 写入或补全500张图像元数据……")
        excluded_image = pg_insert(ImageMaster).excluded
        for start in range(0, len(image_rows), 250):
            db.execute(
                pg_insert(ImageMaster).values(image_rows[start:start + 250]).on_conflict_do_update(
                    index_elements=["qid"],
                    set_={
                        "image_id": excluded_image.image_id,
                        "city": excluded_image.city,
                        "longitude": excluded_image.longitude,
                        "latitude": excluded_image.latitude,
                        "capture_date": excluded_image.capture_date,
                        "capture_year": excluded_image.capture_year,
                        "year_month": excluded_image.year_month,
                        "cluster_id": excluded_image.cluster_id,
                        "image_filename": excluded_image.image_filename,
                        "image_relative_path": excluded_image.image_relative_path,
                        "oss_url": excluded_image.oss_url,
                        "source_master_version": excluded_image.source_master_version,
                        "image_sha256_or_oss_etag": excluded_image.image_sha256_or_oss_etag,
                        "sample_origin": excluded_image.sample_origin,
                        "sample_role": excluded_image.sample_role,
                        "point_id": excluded_image.point_id,
                        "road_segment_id": excluded_image.road_segment_id,
                        "is_active": True,
                    },
                )
            )

        existing_attempts = db.scalar(
            select(func.count()).select_from(SurveyAttempt).where(
                SurveyAttempt.assignment_version == ASSIGNMENT_VERSION
            )
        ) or 0

        print("5/8 写入7500组固定配对……")
        if existing_attempts:
            existing_pairs = db.scalar(
                select(func.count()).select_from(PairAssignment).where(
                    PairAssignment.assignment_version == ASSIGNMENT_VERSION
                )
            ) or 0
            if existing_pairs != EXPECTED_PAIR_COUNT:
                raise RuntimeError(
                    "当前assignment已有attempt，但数据库内配对数量不完整；为保护正式数据，初始化已停止。"
                )
            print("  当前assignment已有attempt，配对表保持冻结，不执行更新。")
        else:
            excluded_pair = pg_insert(PairAssignment).excluded
            for start in range(0, len(pair_rows), 500):
                db.execute(
                    pg_insert(PairAssignment).values(pair_rows[start:start + 500]).on_conflict_do_update(
                        constraint="uq_assignment_slot_order",
                        set_={
                            "source_row_id": excluded_pair.source_row_id,
                            "pair_id": excluded_pair.pair_id,
                            "left_qid": excluded_pair.left_qid,
                            "right_qid": excluded_pair.right_qid,
                            "left_image_id": excluded_pair.left_image_id,
                            "right_image_id": excluded_pair.right_image_id,
                            "left_image_filename": excluded_pair.left_image_filename,
                            "right_image_filename": excluded_pair.right_image_filename,
                            "left_image_relative_path": excluded_pair.left_image_relative_path,
                            "right_image_relative_path": excluded_pair.right_image_relative_path,
                            "left_oss_url": excluded_pair.left_oss_url,
                            "right_oss_url": excluded_pair.right_oss_url,
                            "left_city": excluded_pair.left_city,
                            "right_city": excluded_pair.right_city,
                            "left_cluster": excluded_pair.left_cluster,
                            "right_cluster": excluded_pair.right_cluster,
                            "same_city": excluded_pair.same_city,
                            "same_cluster": excluded_pair.same_cluster,
                            "pair_type": excluded_pair.pair_type,
                            "left_right_random_seed": excluded_pair.left_right_random_seed,
                        },
                    )
                )

        print("6/8 生成250个问卷槽位（不重置已有状态）……")
        slot_rows = [
            {
                "assignment_version": ASSIGNMENT_VERSION,
                "participant_slot": slot,
                "slot_status": "available",
                "release_count": 0,
            }
            for slot in slots
        ]
        db.execute(
            pg_insert(SurveySlot).values(slot_rows).on_conflict_do_nothing(
                constraint="uq_slot_version"
            )
        )

        print("7/8 提交事务……")
        db.commit()

        print("8/8 验收当前500张正式版本……")
        current_qids = image_df["qid"].tolist()
        counts = {
            "survey_config": db.scalar(
                select(func.count()).select_from(SurveyConfig).where(
                    SurveyConfig.dimension_config_version == DIMENSION_CONFIG_VERSION
                )
            ),
            "image_master": db.scalar(
                select(func.count()).select_from(ImageMaster).where(
                    ImageMaster.qid.in_(current_qids), ImageMaster.is_active.is_(True)
                )
            ),
            "pair_assignments": db.scalar(
                select(func.count()).select_from(PairAssignment).where(
                    PairAssignment.assignment_version == ASSIGNMENT_VERSION
                )
            ),
            "survey_slots": db.scalar(
                select(func.count()).select_from(SurveySlot).where(
                    SurveySlot.assignment_version == ASSIGNMENT_VERSION
                )
            ),
        }
        expected = {
            "survey_config": EXPECTED_DIMENSION_COUNT,
            "image_master": EXPECTED_IMAGE_COUNT,
            "pair_assignments": EXPECTED_PAIR_COUNT,
            "survey_slots": EXPECTED_SLOT_COUNT,
        }
        print("\n数据库初始化结果：")
        for key, value in counts.items():
            print(f"  {key}: {value}")
        if counts != expected:
            raise RuntimeError(f"初始化数量不符合预期：actual={counts}, expected={expected}")

        print("\n✅ 500张、7500组配对、250槽位、9指标初始化验收通过。")
        print("✅ 未删除旧表、旧attempt、旧response或旧assignment版本。")

    except Exception:
        db.rollback()
        raise
    finally:
        try:
            db.execute(text(f"SELECT pg_advisory_unlock({advisory_lock_id})"))
            db.commit()
        except Exception:
            db.rollback()
        db.close()


if __name__ == "__main__":
    initialize_database()
