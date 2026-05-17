import os
import re
import argparse
import logging
from abc import ABC, abstractmethod
from datetime import datetime

from pyspark.sql import SparkSession
from pyspark.sql.functions import udf, col
from pyspark.sql.types import MapType, StringType


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("CY_Extractor")


class BaseExtractor(ABC):
    @abstractmethod
    def extract(self, text: str) -> dict:
        pass


EXTRACTION_FIELDS = [
    "broker_name", "broker_phone", "broker_fax", "broker_address",
    "broker_city", "broker_state", "broker_zipcode", "broker_email",
    "loadConfirmationNumber", "totalCarrierPay",
    "carrier_name", "carrier_mc", "carrier_address", "carrier_city",
    "carrier_state", "carrier_zipcode", "carrier_phone",
    "carrier_fax", "carrier_contact",
    *[f"{p}_{i}" for p in [
        "pickup_customer", "pickup_address", "pickup_city",
        "pickup_state", "pickup_zipcode",
        "pickup_start_datetime", "pickup_end_datetime"
    ] for i in range(1, 4)],
    *[f"{p}_{i}" for p in [
        "delivery_customer", "delivery_address", "delivery_city",
        "delivery_state", "delivery_zipcode",
        "delivery_start_datetime", "delivery_end_datetime"
    ] for i in range(1, 4)],
    "processed_at"
]


class CYExtractor(BaseExtractor):
    def _normalize(self, text: str) -> str:
        if not text:
            return ""

        text = re.sub(r"\r\n?", "\n", text)
        text = re.sub(r"\*\*", "", text)
        text = re.sub(r"[ \t\u00A0]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = "\n".join([line.strip() for line in text.splitlines() if line.strip()])
        return text.strip()

    def _money(self, value: str) -> str:
        if not value:
            return ""
        return value.replace("$", "").replace(",", "").strip()

    def _parse_date(self, value: str) -> str:
        if not value:
            return ""

        value = value.strip()

        for fmt in ["%a %m/%d/%Y", "%m/%d/%Y"]:
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass

        return ""

    def _combine_datetime(self, date_value: str, time_value: str) -> str:
        date_iso = self._parse_date(date_value)
        if not date_iso or not time_value:
            return ""

        time_value = time_value.strip()

        for fmt in ["%H:%M", "%I:%M %p"]:
            try:
                t = datetime.strptime(time_value, fmt).time()
                return f"{date_iso}T{t.strftime('%H:%M:%S')}"
            except ValueError:
                pass

        return ""

    def _extract_address(self, block: str):
        address = city = state = zipcode = ""

        m = re.search(
            r"Address\s+(.*?)(?=\nContact\b|\nPhone\b|\nScheduled For\b|\nAppointment Scheduled For\b|\nDriver Work\b|\nSLIC\b|\nCommodity\b)",
            block,
            re.I | re.S
        )

        if not m:
            return address, city, state, zipcode

        lines = [x.strip() for x in m.group(1).splitlines() if x.strip()]

        # Caso: Pasadena, TX 77501
        # Caso: Lithia Springs, GA / 30122-3626
        for i, line in enumerate(lines):
            city_state_zip = re.search(r"^(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", line, re.I)
            city_state = re.search(r"^(.+?),\s*([A-Z]{2})$", line, re.I)

            if city_state_zip:
                city = city_state_zip.group(1).strip()
                state = city_state_zip.group(2).strip()
                zipcode = city_state_zip.group(3).strip()
                address = " ".join(lines[:i]).strip()
                return address, city, state, zipcode

            if city_state and i + 1 < len(lines):
                zip_m = re.search(r"^(\d{5}(?:-\d{4})?)$", lines[i + 1])
                if zip_m:
                    city = city_state.group(1).strip()
                    state = city_state.group(2).strip()
                    zipcode = zip_m.group(1).strip()
                    address = " ".join(lines[:i]).strip()
                    return address, city, state, zipcode

        address = " ".join(lines).strip()
        return address, city, state, zipcode

    def _stop_blocks(self, text: str):
        pattern = re.compile(r"Stop\s+(\d+):\s*(Pick Up|Delivery)", re.I)
        matches = list(pattern.finditer(text))

        blocks = []
        for i, m in enumerate(matches):
            stop_number = int(m.group(1))
            stop_type = m.group(2).lower()
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            blocks.append((stop_number, stop_type, text[start:end]))

        return blocks

    def extract(self, text: str) -> dict:
        data = {field: "" for field in EXTRACTION_FIELDS}

        if not text:
            return data

        text = self._normalize(text)

        # Broker
        data["broker_name"] = "Coyote Logistics, LLC"

        if m := re.search(r"CarrierInvoices@coyote\.com", text, re.I):
            data["broker_email"] = m.group(0).strip()

        if re.search(r"960 Northpoint Parkway", text, re.I):
            data["broker_address"] = "960 Northpoint Parkway Suite 150"
            data["broker_city"] = "Alpharetta"
            data["broker_state"] = "GA"
            data["broker_zipcode"] = "30005"

        if m := re.search(r"Broker\s+Coyote Logistics,\s*LLC.*?Phone\s+([+\d\s\(\)-]+(?:x\d+)?)", text, re.I | re.S):
            data["broker_phone"] = re.sub(r"\s+", " ", m.group(1)).strip()

        if m := re.search(r"Broker\s+Coyote Logistics,\s*LLC.*?Fax\s+([+\d\s\(\)-]+)", text, re.I | re.S):
            data["broker_fax"] = re.sub(r"\s+", " ", m.group(1)).strip()

        # Load and pay
        if m := re.search(r"\bLoad\s+(\d+)\b", text, re.I):
            data["loadConfirmationNumber"] = m.group(1).strip()

        if m := re.search(r"Total\s+USD\s+\$?([\d,]+\.\d{2})", text, re.I):
            data["totalCarrierPay"] = self._money(m.group(1))

        # Carrier
        if m := re.search(r"\[Carrier Legal Name\s*-\s*([^\]]+)\]", text, re.I):
            data["carrier_name"] = m.group(1).strip()
        elif m := re.search(r"\nCarrier\n+([A-Za-z0-9 &.,-]+)\n", text, re.I):
            data["carrier_name"] = m.group(1).strip()

        if m := re.search(r"\[Carrier USDOT\s*-\s*(\d+)\]", text, re.I):
            data["carrier_mc"] = m.group(1).strip()
        elif m := re.search(r"\bUSDOT\s+(\d+)", text, re.I):
            data["carrier_mc"] = m.group(1).strip()

        if m := re.search(r"Carrier\s+GTT Freight Corp\s+USDOT\s+\d+\s+Phone\s+([^\n]+)\s+Email\s+([^\n]+)\s+Fax\s+([^\n]+)", text, re.I):
            phone = m.group(1).strip()
            email = m.group(2).strip()
            fax = m.group(3).strip()
            data["carrier_phone"] = "" if phone.lower() == "none" else phone
            data["carrier_contact"] = email
            data["carrier_fax"] = "" if fax.lower() == "none" else fax
        else:
            if m := re.search(r"\nPhone\s+([^\n]+)\nEmail\s+([^\n]+)\nFax\s+([^\n]+)", text, re.I):
                phone = m.group(1).strip()
                email = m.group(2).strip()
                fax = m.group(3).strip()
                data["carrier_phone"] = "" if phone.lower() == "none" else phone
                data["carrier_contact"] = email
                data["carrier_fax"] = "" if fax.lower() == "none" else fax

        # Stops
        pickup_idx = 0
        delivery_idx = 0

        for _, stop_type, block in self._stop_blocks(text):
            is_pickup = "pick" in stop_type
            prefix = "pickup" if is_pickup else "delivery"

            if is_pickup:
                pickup_idx += 1
                idx = pickup_idx
            else:
                delivery_idx += 1
                idx = delivery_idx

            if idx > 3:
                continue

            # if m := re.search(r"Facility\s+(.+?)(?=\nAddress\b)", block, re.I | re.S):
            #     data[f"{prefix}_customer_{idx}"] = " ".join(m.group(1).split()).strip()
            facility_matches = list(
                re.finditer(
                    r"(?:^|\n)Facility\s+(?!Notes\b)(.+?)(?=\nAddress\b)",
                    block,
                    re.I | re.S
                )
            )

            if facility_matches:
                facility_value = facility_matches[-1].group(1)
                data[f"{prefix}_customer_{idx}"] = " ".join(facility_value.split()).strip()

            address, city, state, zipcode = self._extract_address(block)
            data[f"{prefix}_address_{idx}"] = address
            data[f"{prefix}_city_{idx}"] = city
            data[f"{prefix}_state_{idx}"] = state
            data[f"{prefix}_zipcode_{idx}"] = zipcode

            date_m = re.search(
                r"(?:Scheduled For|Appointment Scheduled For)\s*\n+([A-Za-z]{3}\s+\d{2}/\d{2}/\d{4})",
                block,
                re.I
            )

            time_range_m = re.search(r"from\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", block, re.I)
            time_at_m = re.search(r"\bat\s+(\d{2}:\d{2})", block, re.I)

            if date_m and time_range_m:
                data[f"{prefix}_start_datetime_{idx}"] = self._combine_datetime(date_m.group(1), time_range_m.group(1))
                data[f"{prefix}_end_datetime_{idx}"] = self._combine_datetime(date_m.group(1), time_range_m.group(2))
            elif date_m and time_at_m:
                dt = self._combine_datetime(date_m.group(1), time_at_m.group(1))
                data[f"{prefix}_start_datetime_{idx}"] = dt
                data[f"{prefix}_end_datetime_{idx}"] = dt

        return data


def extract_fields_udf():
    extractor = CYExtractor()

    def _extract(text):
        result = extractor.extract(text)
        result["processed_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        return result

    return udf(_extract, MapType(StringType(), StringType()))


def main(p):
    spark = SparkSession.builder.appName("CY TruckR Extraction").getOrCreate()
    logger.info("Starting CY extraction process")

    input_path = os.path.join(p["source_path"], "*.txt")

    df = (
        spark.read.format("binaryFile")
        .option("pathGlobFilter", "*.txt")
        .option("recursiveFileLookup", "false")
        .load(input_path)
        .select(col("_metadata.file_path").alias("source_file"), col("content"))
    )

    df = df.withColumn("text", col("content").cast("string")).drop("content")
    logger.info(f"Files detected: {df.count()}")

    extract_udf = extract_fields_udf()
    df = df.withColumn("extracted", extract_udf(col("text")))

    for field in EXTRACTION_FIELDS:
        df = df.withColumn(field, col("extracted").getItem(field))

    df = df.drop("text", "extracted")

    logger.info(f"Writing {df.count()} records to {p['target_table']}")

    (
        df.write.format("delta")
        .mode("overwrite")
        .option("mergeSchema", "true")
        .saveAsTable(p["target_table"])
    )

    logger.info("CY extraction completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CY PDF Extraction Parameters")
    parser.add_argument("--source_path", required=True)
    parser.add_argument("--target_table", required=True)
    args = parser.parse_args()

    main({
        "source_path": args.source_path,
        "target_table": args.target_table
    })