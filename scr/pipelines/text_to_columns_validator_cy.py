import argparse
import logging
from pyspark.sql import SparkSession, functions as F

# ------------------------------------------------------------------------------
# Logger Configuration
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("CY_Validator")

# ------------------------------------------------------------------------------
# Parse Arguments
# ------------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="CY Extractor Validator")
parser.add_argument(
    "--source_table",
    required=True,
    help="Spark table name for validation, e.g. logistics.bronze.truckr_loads_cy"
)
parser.add_argument(
    "--min_non_empty_ratio",
    required=False,
    default="0.70",
    help="Minimum accepted non-empty ratio for key fields"
)
args = parser.parse_args()

source_table = args.source_table
min_non_empty_ratio = float(args.min_non_empty_ratio)

# ------------------------------------------------------------------------------
# Spark Session
# ------------------------------------------------------------------------------
spark = SparkSession.builder.appName("CY_Extractor_Validator").getOrCreate()

# ------------------------------------------------------------------------------
# Expected Columns
# ------------------------------------------------------------------------------
expected_columns = [
    "source_file",
    "broker_name", "broker_phone", "broker_fax", "broker_address",
    "broker_city", "broker_state", "broker_zipcode", "broker_email",
    "loadConfirmationNumber", "totalCarrierPay",
    "carrier_name", "carrier_mc", "carrier_address", "carrier_city",
    "carrier_state", "carrier_zipcode", "carrier_phone",
    "carrier_fax", "carrier_contact",

    "pickup_customer_1", "pickup_address_1", "pickup_city_1",
    "pickup_state_1", "pickup_zipcode_1",
    "pickup_start_datetime_1", "pickup_end_datetime_1",

    "pickup_customer_2", "pickup_address_2", "pickup_city_2",
    "pickup_state_2", "pickup_zipcode_2",
    "pickup_start_datetime_2", "pickup_end_datetime_2",

    "pickup_customer_3", "pickup_address_3", "pickup_city_3",
    "pickup_state_3", "pickup_zipcode_3",
    "pickup_start_datetime_3", "pickup_end_datetime_3",

    "delivery_customer_1", "delivery_address_1", "delivery_city_1",
    "delivery_state_1", "delivery_zipcode_1",
    "delivery_start_datetime_1", "delivery_end_datetime_1",

    "delivery_customer_2", "delivery_address_2", "delivery_city_2",
    "delivery_state_2", "delivery_zipcode_2",
    "delivery_start_datetime_2", "delivery_end_datetime_2",

    "delivery_customer_3", "delivery_address_3", "delivery_city_3",
    "delivery_state_3", "delivery_zipcode_3",
    "delivery_start_datetime_3", "delivery_end_datetime_3",

    "processed_at"
]

# Campos que sí esperamos que estén poblados en la mayoría de documentos CY
key_columns = [
    "source_file",
    "broker_name",
    "loadConfirmationNumber",
    "totalCarrierPay",
    "carrier_name",
    "carrier_mc",
    "pickup_customer_1",
    "pickup_address_1",
    "pickup_city_1",
    "pickup_state_1",
    "delivery_customer_1",
    "delivery_address_1",
    "delivery_city_1",
    "delivery_state_1",
    "processed_at"
]

# ------------------------------------------------------------------------------
# Load Target Table
# ------------------------------------------------------------------------------
logger.info(f"Loading table: {source_table}")
target_df = spark.table(source_table)

total_rows = target_df.count()
logger.info(f"Rows found: {total_rows}")

if total_rows == 0:
    raise ValueError(f"Validation failed: table {source_table} has no records.")

# ------------------------------------------------------------------------------
# Validate Columns
# ------------------------------------------------------------------------------
actual_columns = target_df.columns

missing_columns = [c for c in expected_columns if c not in actual_columns]
extra_columns = [c for c in actual_columns if c not in expected_columns]

if missing_columns:
    logger.error(f"Missing expected columns: {missing_columns}")
    raise ValueError(f"Validation failed: missing columns {missing_columns}")

logger.info("✅ All expected columns are present.")

if extra_columns:
    logger.warning(f"Extra columns found: {extra_columns}")
else:
    logger.info("✅ No unexpected extra columns found.")

# ------------------------------------------------------------------------------
# Validate Non-Empty Ratio for Key Fields
# ------------------------------------------------------------------------------
validation_rows = []

for c in key_columns:
    non_empty_count = (
        target_df
        .filter(F.col(c).isNotNull() & (F.trim(F.col(c)) != ""))
        .count()
    )

    non_empty_ratio = non_empty_count / total_rows if total_rows > 0 else 0

    status = "PASS" if non_empty_ratio >= min_non_empty_ratio else "FAIL"

    validation_rows.append({
        "field": c,
        "non_empty_count": non_empty_count,
        "total_rows": total_rows,
        "non_empty_ratio": non_empty_ratio,
        "status": status
    })

    if status == "PASS":
        logger.info(
            f"✅ {c}: {non_empty_count}/{total_rows} "
            f"({non_empty_ratio:.2%}) non-empty"
        )
    else:
        logger.error(
            f"❌ {c}: {non_empty_count}/{total_rows} "
            f"({non_empty_ratio:.2%}) non-empty. "
            f"Minimum required: {min_non_empty_ratio:.2%}"
        )

failed_fields = [r for r in validation_rows if r["status"] == "FAIL"]

# ------------------------------------------------------------------------------
# Optional Exact Validation for Known CY Sample Load
# ------------------------------------------------------------------------------
sample_load = "29587264"

sample_rows = (
    target_df
    .filter(F.col("loadConfirmationNumber") == sample_load)
    .limit(1)
    .collect()
)

if sample_rows:
    logger.info(f"Found sample CY load {sample_load}. Running exact validation.")

    row = sample_rows[0].asDict()

    expected_values = {
        "broker_name": "Coyote Logistics, LLC",
        "loadConfirmationNumber": "29587264",
        "totalCarrierPay": "2400.00",
        "carrier_name": "GTT Freight Corp",
        "carrier_mc": "3723304",
        "pickup_customer_1": "Wrist USA",
        "pickup_city_1": "Pasadena",
        "pickup_state_1": "TX",
        "delivery_customer_1": "USply LLC",
        "delivery_city_1": "Medley",
        "delivery_state_1": "FL",
    }

    exact_errors = []

    for field, expected in expected_values.items():
        actual = row.get(field)

        norm_expected = str(expected).strip().lower()
        norm_actual = str(actual).strip().lower() if actual is not None else ""

        if norm_expected == norm_actual:
            logger.info(f"✅ Exact match {field}: {actual}")
        else:
            logger.error(
                f"❌ Exact mismatch {field}: expected='{expected}', got='{actual}'"
            )
            exact_errors.append(field)

    if exact_errors:
        failed_fields.extend([
            {
                "field": f,
                "status": "FAIL_EXACT_SAMPLE"
            }
            for f in exact_errors
        ])
else:
    logger.warning(
        f"Sample load {sample_load} was not found. "
        "Skipping exact sample validation."
    )

# ------------------------------------------------------------------------------
# Fail Pipeline if Errors Detected
# ------------------------------------------------------------------------------
if failed_fields:
    logger.error(f"Validation failed for {len(failed_fields)} field(s).")
    raise ValueError(f"CY validation failed: {failed_fields}")

logger.info("✅ CY validation completed successfully.")