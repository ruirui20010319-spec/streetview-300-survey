import os
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import (
    ASSIGNMENT_VERSION,
    DIMENSION_CONFIG_VERSION,
    EXPECTED_PAIRS_PER_ATTEMPT,
    OSS_BASE_URL,
    SURVEY_DIMENSIONS,
    SURVEY_VERSION,
)
from database import Base, SessionLocal, engine
from models import (
    ImageMaster,
    PairAssignment,
    SurveyConfig,
    SurveySlot,
)

BASE_DIR = Path(__file__).resolve().parent
ASSIGNMENT_PATH = (
    BASE_DIR / "tables" / "questionnaire_pair_assignment_150x30.xlsx"
)

REQUIRED_COLUMNS = {
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


def clean_text(value):
    if pd.isna(value):
        return None
    return str(value).strip()


def clean_bool(value):
    if pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def build_oss_url(explicit_url, relative_path, filename):
    explicit = clean_text(explicit_url)
    if explicit:
        return explicit

    path_value = clean_text(relative_path) or clean_text(filename)
    if not path_value:
        raise ValueError("图片缺少 OSS URL、相对路径和文件名")

    path_value = path_value.replace("\\", "/").lstrip("/")
    if path_value.startswith("images/"):
        path_value = path_value[len("images/"):]
    return f"{OSS_BASE_URL}/{path_value}"


def load_and_validate_assignment():
    if not ASSIGNMENT_PATH.exists():
        raise FileNotFoundError(
            f"未找到固定配对表：{ASSIGNMENT_PATH}"
        )

    df = pd.read_excel(ASSIGNMENT_PATH)
    df.columns = df.columns.astype(str).str.strip()

    missing_columns = sorted(REQUIRED_COLUMNS - set(df.columns))
    if missing_columns:
        raise ValueError(
            "固定配对表缺少字段：" + ", ".join(missing_columns)
        )

    if len(df) != 4500:
        raise ValueError(f"固定配对表应为4500行，实际为{len(df)}行")

    slot_counts = (
        df.groupby("participant_slot")["pair_id"]
        .count()
        .sort_index()
    )
    if len(slot_counts) != 150:
        raise ValueError(
            f"应有150个槽位，实际为{len(slot_counts)}个"
        )
    if not (slot_counts == EXPECTED_PAIRS_PER_ATTEMPT).all():
        bad = slot_counts[
            slot_counts != EXPECTED_PAIRS_PER_ATTEMPT
        ].to_dict()
        raise ValueError(f"部分槽位不是30组题：{bad}")

    duplicated_order = df.duplicated(
        subset=["participant_slot", "order_in_participant"]
    ).sum()
    if duplicated_order:
        raise ValueError(
            f"发现{duplicated_order}条槽位题序重复记录"
        )

    return df


def build_image_rows(df):
    rows = []

    for side in ("left", "right"):
        for _, row in df.iterrows():
            rows.append(
                {
                    "qid": clean_text(row[f"{side}_qid"]),
                    "image_id": clean_text(row[f"{side}_image_id"]),
                    "city": clean_text(row[f"{side}_city"]),
                    "cluster_id": clean_text(row[f"{side}_cluster"]),
                    "image_filename": clean_text(
                        row[f"{side}_image_filename"]
                    ),
                    "image_relative_path": clean_text(
                        row[f"{side}_image_relative_path"]
                    ),
                    "oss_url": build_oss_url(
                        row.get(f"{side}_oss_url"),
                        row[f"{side}_image_relative_path"],
                        row[f"{side}_image_filename"],
                    ),
                    "source_master_version": ASSIGNMENT_VERSION,
                    "is_active": True,
                }
            )

    image_df = pd.DataFrame(rows)

    qid_conflicts = (
        image_df.groupby("qid")["image_id"].nunique()
    )
    qid_conflicts = qid_conflicts[qid_conflicts > 1]
    if not qid_conflicts.empty:
        raise ValueError(
            "同一 qid 对应多个 image_id："
            + str(qid_conflicts.to_dict())
        )

    image_id_conflicts = (
        image_df.groupby("image_id")["qid"].nunique()
    )
    image_id_conflicts = image_id_conflicts[
        image_id_conflicts > 1
    ]
    if not image_id_conflicts.empty:
        raise ValueError(
            "同一 image_id 对应多个 qid："
            + str(image_id_conflicts.to_dict())
        )

    image_df = image_df.drop_duplicates(
        subset=["qid", "image_id"]
    )

    if len(image_df) != 300:
        raise ValueError(
            f"应提取300张唯一图片，实际为{len(image_df)}张"
        )

    return image_df.to_dict("records")


def build_pair_rows(df):
    rows = []
    for source_row_id, (_, row) in enumerate(
        df.iterrows(), start=1
    ):
        rows.append(
            {
                "assignment_version": ASSIGNMENT_VERSION,
                "source_row_id": source_row_id,
                "pair_id": clean_text(row["pair_id"]),
                "participant_slot": clean_text(
                    row["participant_slot"]
                ),
                "order_in_participant": int(
                    row["order_in_participant"]
                ),
                "left_qid": clean_text(row["left_qid"]),
                "right_qid": clean_text(row["right_qid"]),
                "left_image_id": clean_text(
                    row["left_image_id"]
                ),
                "right_image_id": clean_text(
                    row["right_image_id"]
                ),
                "left_image_filename": clean_text(
                    row["left_image_filename"]
                ),
                "right_image_filename": clean_text(
                    row["right_image_filename"]
                ),
                "left_image_relative_path": clean_text(
                    row["left_image_relative_path"]
                ),
                "right_image_relative_path": clean_text(
                    row["right_image_relative_path"]
                ),
                "left_oss_url": build_oss_url(
                    row.get("left_oss_url"),
                    row["left_image_relative_path"],
                    row["left_image_filename"],
                ),
                "right_oss_url": build_oss_url(
                    row.get("right_oss_url"),
                    row["right_image_relative_path"],
                    row["right_image_filename"],
                ),
                "left_city": clean_text(row["left_city"]),
                "right_city": clean_text(row["right_city"]),
                "left_cluster": clean_text(
                    row["left_cluster"]
                ),
                "right_cluster": clean_text(
                    row["right_cluster"]
                ),
                "same_city": clean_bool(row["same_city"]),
                "same_cluster": clean_bool(
                    row["same_cluster"]
                ),
                "pair_type": clean_text(row["pair_type"]),
                "left_right_random_seed": clean_text(
                    row["left_right_random_seed"]
                ),
            }
        )
    return rows


def initialize_database():
    print("1/6 正在读取并验证固定配对表……")
    df = load_and_validate_assignment()

    print("2/6 正在建立数据库表……")
    Base.metadata.create_all(bind=engine)

    image_rows = build_image_rows(df)
    pair_rows = build_pair_rows(df)
    slots = sorted(
        clean_text(value)
        for value in df["participant_slot"].unique()
    )

    db = SessionLocal()
    try:
        # 防止多个 Gunicorn worker 同时初始化。
        db.execute(text("SELECT pg_advisory_lock(2026071701)"))

        print("3/6 正在写入8个评价指标……")
        config_rows = [
            {
                "survey_version": SURVEY_VERSION,
                "assignment_version": ASSIGNMENT_VERSION,
                "dimension_config_version":
                    DIMENSION_CONFIG_VERSION,
                "dimension_key": item["key"],
                "dimension_label": item["label"],
                "dimension_description":
                    item["description"],
                "dimension_order": item["order"],
                "is_active": True,
                "expected_pairs_per_attempt":
                    EXPECTED_PAIRS_PER_ATTEMPT,
                "expected_dimension_count":
                    len(SURVEY_DIMENSIONS),
            }
            for item in SURVEY_DIMENSIONS
        ]
        db.execute(
            pg_insert(SurveyConfig)
            .values(config_rows)
            .on_conflict_do_nothing(
                index_elements=[
                    "dimension_config_version",
                    "dimension_key",
                ]
            )
        )

        print("4/6 正在写入300张图片……")
        db.execute(
            pg_insert(ImageMaster)
            .values(image_rows)
            .on_conflict_do_nothing(
                index_elements=["qid"]
            )
        )

        print("5/6 正在写入4500组固定配对……")
        chunk_size = 500
        for start in range(0, len(pair_rows), chunk_size):
            chunk = pair_rows[start:start + chunk_size]
            db.execute(
                pg_insert(PairAssignment)
                .values(chunk)
                .on_conflict_do_nothing(
                    constraint="uq_assignment_slot_order"
                )
            )

        print("6/6 正在生成150个问卷槽位……")
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
            pg_insert(SurveySlot)
            .values(slot_rows)
            .on_conflict_do_nothing(
                constraint="uq_slot_version"
            )
        )

        db.commit()

        counts = {
            "survey_config": db.scalar(
                select(func.count())
                .select_from(SurveyConfig)
                .where(
                    SurveyConfig.dimension_config_version
                    == DIMENSION_CONFIG_VERSION
                )
            ),
            "image_master": db.scalar(
                select(func.count()).select_from(ImageMaster)
            ),
            "pair_assignments": db.scalar(
                select(func.count())
                .select_from(PairAssignment)
                .where(
                    PairAssignment.assignment_version
                    == ASSIGNMENT_VERSION
                )
            ),
            "survey_slots": db.scalar(
                select(func.count())
                .select_from(SurveySlot)
                .where(
                    SurveySlot.assignment_version
                    == ASSIGNMENT_VERSION
                )
            ),
        }

        print("\n数据库初始化完成：")
        for table_name, count in counts.items():
            print(f"  {table_name}: {count}")

        expected = {
            "survey_config": 8,
            "image_master": 300,
            "pair_assignments": 4500,
            "survey_slots": 150,
        }
        if counts != expected:
            raise RuntimeError(
                f"初始化数量不符合预期：{counts}"
            )

        print("\n✅ 第一阶段数据库初始化验收通过。")

    except Exception:
        db.rollback()
        raise
    finally:
        try:
            db.execute(
                text("SELECT pg_advisory_unlock(2026071701)")
            )
            db.commit()
        except Exception:
            db.rollback()
        db.close()


if __name__ == "__main__":
    initialize_database()
