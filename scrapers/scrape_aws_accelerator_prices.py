#!/usr/bin/env python3
"""Record AWS EC2 GPU / Trainium / Inferentia rental prices, one row per day.

Each run computes, per accelerator, the hourly rate per accelerator for
On-Demand, 1-year Reserved Instances and Spot, and writes one row to the "AWS"
worksheet of the "GPU&ASIC pricing" spreadsheet:

    row 1:  Date | H100                                          | H200 | ...
    row 2:       | On demand | 1y RI | 3y RI | Capacity block | Spot | ...
                 (all hour rates per accelerator)
    row 3+: one row per date, newest first

If a row for today's date exists it is overwritten; otherwise a new row is
inserted at the top (row 3). Only that row is written, so older rows and any
formatting are left alone. A newly watched accelerator gets a new column group
appended on the right; one missing from a run is left blank.

Sources (boto3, installed in the tony-stock container; on the host,
`pip install --user boto3` first):

  * Price List Query API (`pricing:GetProducts`, service AmazonEC2):
    On-Demand and Reserved Instance list prices, plus which types/regions sell
    EC2 Capacity Blocks (so Capacity-Block-only chips like Trainium2 are found).
  * `ec2:DescribeInstanceTypes`: each instance type's accelerator name and
    count, which decides what is watched.
  * `ec2:DescribeSpotPriceHistory`: the current Spot price per AZ.
  * `ec2:DescribeCapacityBlockOfferings`: Capacity Block offerings for sale,
    per instance type, and per UltraServer for rack-scale GB200 (ULTRASERVERS).

What is watched: NVIDIA GPUs in the P instance family newer than A100 (H100,
H200, B200, B300, ...) and AWS Trainium / Inferentia (not first-gen
Inferentia). Chips are recognized by the names DescribeInstanceTypes reports,
not a hardcoded list, so new ones such as NVIDIA Vera Rubin or Trainium4 are
picked up automatically once AWS lists them. Only instance sizes with at least
--min-accel (default 8) accelerators; Linux, shared tenancy, no pre-installed
software. Each chip is watched in the first region of REGION_PREFERENCE
(largest first: us-east-1, us-east-2, ...) that sells it; --regions overrides
this ("all" for every region).

Rates, per accelerator-hour, averaged over all of the accelerator's instance
types in its region(s):
  On demand   On-Demand price / accelerator count
  1/3 year RI cheapest effective Standard RI rate for the term (hourly fee +
              upfront amortized over the term, across No/Partial/All Upfront)
  Capacity    cheapest Capacity Block offering for a --cb-hours (default 24)
  block       block: upfront fee / hours / accelerators
  Spot        lowest current Spot price across the region's AZs

Credentials: an AWS identity allowed `pricing:GetProducts`,
`ec2:DescribeRegions`, `ec2:DescribeInstanceTypes`,
`ec2:DescribeSpotPriceHistory` and `ec2:DescribeCapacityBlockOfferings`. On hp-omen the `diag` profile has them, so it
is the default (override with --profile or AWS_PROFILE).

    python3 scrape_aws_accelerator_prices.py              # upsert today's row
    python3 scrape_aws_accelerator_prices.py --dry-run    # print it as CSV, no sheet
    python3 scrape_aws_accelerator_prices.py -o row.csv   # write it to a CSV file
"""
import argparse
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

FAMILIES = ["GPU instance", "Machine Learning ASIC Instances"]

SPREADSHEET_ID = "19rqW5crirhEEuz-lfcmoeB7aQPJW_T_7d8sOxTPhhM0"  # "GPU&ASIC pricing"
SHEET_NAME = "AWS"
CREDENTIAL_PATH = os.environ.get(
    "SMART_STOCKER_CREDENTIAL",
    os.path.expanduser("~/.smart-stocker-google-api.json"),
)

# The Price List API's only endpoints are us-east-1, eu-central-1 and
# ap-south-1; every endpoint serves prices for all regions.
PRICING_ENDPOINT_REGION = "us-east-1"

BASE_FILTERS = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
    "licenseModel": "No License required",
}

# Which chips are watched (see the module docstring).
OLD_NVIDIA_GPUS = {"K80", "V100", "A100"}
OLD_AWS_CHIPS = {"Inferentia"}
AWS_CHIP_MAKERS = {"AWS", "Annapurna"}  # Trainium3 reports "Annapurna"

# Each chip is watched in the first of these regions that sells it, unless
# --regions is given. GB200 is only sold in the Dallas Local Zone as
# UltraServer Capacity Blocks (4 GPUs per instance), so it's priced
# separately via ULTRASERVERS.
REGION_PREFERENCE = [
    "us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1",
    "ap-northeast-1", "eu-west-2", "ap-southeast-2", "ap-south-1", "eu-north-1",
    "ap-northeast-2", "ca-central-1", "us-west-1", "sa-east-1",
]

# Column-group order for a new sheet; accelerators found later are appended.
DEFAULT_ACCELERATOR_ORDER = [
    "H100", "H200", "B200", "B300", "GB200",
    "Inferentia2", "Trainium", "Trainium2", "Trainium3",
]

# Rack-scale chips sold only as UltraServer Capacity Blocks. They don't fit
# the per-instance pipeline (4 GPUs per instance, in a Local Zone that
# DescribeInstanceTypes doesn't describe), and EC2 has no API that lists
# UltraServer types, so they are named here: chip → (region, UltraServer
# types; the "xN" suffix is the GPU count). AWS only shows these offerings to
# accounts with a Capacity Block limit increase; otherwise the columns stay
# blank and a note is printed.
ULTRASERVERS = {
    "GB200": ("us-east-1", ["u-p6e-gb200x72", "u-p6e-gb200x36"]),
}

# (row key, sub-column header) for each accelerator's column group.
RATES = [
    ("on_demand", "On demand hour rate per accelerator"),
    ("ri_1yr", "1 year RI hour rate per accelerator"),
    ("ri_3yr", "3 year RI hour rate per accelerator"),
    ("cb", "Capacity block hour rate per accelerator"),
    ("spot", "Spot hour rate per accelerator"),
]
DATE_HEADER = "Date"
HEADER_ROWS = 2

RI_HOURS = {"1yr": 365 * 24, "3yr": 3 * 365 * 24}

# EC2 errors meaning "no Capacity Blocks for this type here", not a failure.
UNSUPPORTED_CB_ERRORS = {"InvalidParameterValue", "InvalidAction"}
CB_LIMIT_ERROR = "CapacityBlockDescribeLimitExceeded"

# DescribeCapacityBlockOfferings has a low request-rate limit; adaptive retry
# mode backs off client-side when throttled.
BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


# --------------------------------------------------------------------------- #
# AWS
# --------------------------------------------------------------------------- #

class Aws:
    """boto3 clients for one profile, created up front (sessions aren't thread-safe;
    clients are)."""

    def __init__(self, profile):
        self.session = boto3.Session(profile_name=profile or None)
        self.pricing = self.session.client(
            "pricing", region_name=PRICING_ENDPOINT_REGION, config=BOTO_CONFIG)
        self._ec2 = {}

    def ec2(self, region):
        if region not in self._ec2:
            self._ec2[region] = self.session.client(
                "ec2", region_name=region, config=BOTO_CONFIG)
        return self._ec2[region]


def warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def enabled_regions(client):
    out = client.ec2(PRICING_ENDPOINT_REGION).describe_regions()
    return {r["RegionName"] for r in out["Regions"]}


def fetch_products(client, market):
    """Accelerated-instance products in every region for marketoption=market."""
    pages = client.pricing.get_paginator("get_products")
    products = []
    for family in FAMILIES:
        filters = dict(BASE_FILTERS, instanceFamily=family, marketoption=market)
        for page in pages.paginate(
                ServiceCode="AmazonEC2",
                Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v}
                         for k, v in filters.items()]):
            products.extend(json.loads(item) for item in page["PriceList"])
    return products


def fetch_chip_info(client, types_by_region):
    """Return {instance_type: (accelerator, count)} from ec2:DescribeInstanceTypes.

    Each type is described in the first region (in dict order) that lists it.
    """
    info = {}
    for region, types in types_by_region.items():
        todo = sorted(t for t in types if t not in info)
        for i in range(0, len(todo), 100):
            pages = client.ec2(region).get_paginator("describe_instance_types").paginate(
                Filters=[{"Name": "instance-type", "Values": todo[i:i + 100]}])
            for page in pages:
                for it in page["InstanceTypes"]:
                    chip = chip_of(it)
                    if chip:
                        info[it["InstanceType"]] = chip
    return info


def chip_of(instance_type_info):
    """("NVIDIA H100", 8)-style accelerator name and count, or None."""
    for dev in instance_type_info.get("GpuInfo", {}).get("Gpus", []):
        maker = "AWS" if dev["Manufacturer"] in AWS_CHIP_MAKERS else dev["Manufacturer"]
        return f"{maker} {dev['Name']}", dev["Count"]
    for dev in instance_type_info.get("NeuronInfo", {}).get("NeuronDevices", []):
        return f"AWS {dev['Name']}", dev["Count"]
    return None


def is_watched_chip(instance_type, accelerator):
    maker, _, name = accelerator.partition(" ")
    if maker == "NVIDIA":
        return instance_type.startswith("p") and name not in OLD_NVIDIA_GPUS
    if maker == "AWS":
        return name not in OLD_AWS_CHIPS
    return False


def on_demand_rate(terms):
    rates = [float(d["pricePerUnit"]["USD"])
             for t in terms.get("OnDemand", {}).values()
             for d in t["priceDimensions"].values()
             if d["unit"] == "Hrs" and "USD" in d["pricePerUnit"]]
    rates = [r for r in rates if r > 0]
    return min(rates) if rates else None


def ri_rate(terms, term):
    """Cheapest effective Standard RI $/hr for term ("1yr"/"3yr"): hourly fee +
    upfront amortized over the term, across No/Partial/All Upfront."""
    best = None
    for t in terms.get("Reserved", {}).values():
        attrs = t["termAttributes"]
        if attrs.get("OfferingClass") != "standard" or attrs.get("LeaseContractLength") != term:
            continue
        dims = list(t["priceDimensions"].values())
        if any("USD" not in d["pricePerUnit"] for d in dims):
            continue  # China regions price in CNY
        hourly = sum(float(d["pricePerUnit"]["USD"]) for d in dims if d["unit"] == "Hrs")
        upfront = sum(float(d["pricePerUnit"]["USD"]) for d in dims if d["unit"] == "Quantity")
        rate = hourly + upfront / RI_HOURS[term]
        if rate > 0 and (best is None or rate < best):
            best = rate
    return best


def build_rows(od_products, cb_products, chips, min_accel):
    """Instance rows keyed by (instance_type, region) for watched chips with at
    least min_accel accelerators. Capacity-Block listings add rows (without an
    On-Demand price) so chips sold only that way are still found."""
    def keep(p):
        itype = p["product"]["attributes"]["instanceType"]
        chip = chips.get(itype)
        return chip and is_watched_chip(itype, chip[0]) and chip[1] >= min_accel

    def new_row(attrs):
        accelerator, count = chips[attrs["instanceType"]]
        return {"accelerator": accelerator.partition(" ")[2], "count": count,
                **{k: None for k, _ in RATES}}

    rows = {}
    for p in filter(keep, od_products):
        od = on_demand_rate(p["terms"])
        if od is None:
            continue
        attrs = p["product"]["attributes"]
        key = (attrs["instanceType"], attrs.get("regionCode", ""))
        # Same type/region can appear under more than one SKU; keep the cheapest.
        if key not in rows or od < rows[key]["on_demand"]:
            rows[key] = dict(new_row(attrs), on_demand=od,
                             ri_1yr=ri_rate(p["terms"], "1yr"),
                             ri_3yr=ri_rate(p["terms"], "3yr"))
    cb_keys = set()
    for p in filter(keep, cb_products):
        attrs = p["product"]["attributes"]
        key = (attrs["instanceType"], attrs.get("regionCode", ""))
        cb_keys.add(key)
        rows.setdefault(key, new_row(attrs))
    return rows, cb_keys


def keep_preferred_region(rows, regions):
    """Per accelerator, keep only the rows in its first region (in regions order)."""
    rank = {r: i for i, r in enumerate(regions)}
    best = {}
    for (_, region), row in rows.items():
        chip = row["accelerator"]
        if chip not in best or rank[region] < rank[best[chip]]:
            best[chip] = region
    return {k: r for k, r in rows.items() if best[r["accelerator"]] == k[1]}


def fetch_spot(ec2, instance_types):
    """Return {instance_type: price}: lowest current Spot price in the region."""
    pages = ec2.get_paginator("describe_spot_price_history").paginate(
        InstanceTypes=sorted(instance_types),
        ProductDescriptions=["Linux/UNIX"],
        StartTime=datetime.now(timezone.utc))
    # Keep the latest record per (type, AZ), then the cheapest AZ per type.
    latest = {}
    for page in pages:
        for r in page["SpotPriceHistory"]:
            k = (r["InstanceType"], r["AvailabilityZone"])
            if k not in latest or r["Timestamp"] > latest[k]["Timestamp"]:
                latest[k] = r
    best = {}
    for (itype, _), r in latest.items():
        best[itype] = min(best.get(itype, float("inf")), float(r["SpotPrice"]))
    return best


def cheapest_offering_rate(offerings, units_per_offering):
    """Cheapest offering $/hr per unit; units_per_offering(o) → units (GPUs or
    instances) the offering covers."""
    best = None
    for o in offerings:
        if o.get("CurrencyCode", "USD") != "USD":
            continue
        dur = o["CapacityBlockDurationHours"] + o.get("CapacityBlockDurationMinutes", 0) / 60
        units = units_per_offering(o)
        if dur <= 0 or not units:
            continue
        rate = float(o["UpfrontFee"]) / dur / units
        best = rate if best is None else min(best, rate)
    return best


def fetch_capacity_block(ec2, instance_type, hours):
    """Cheapest Capacity Block $/instance-hour for a hours-long block, or None."""
    out = ec2.describe_capacity_block_offerings(
        InstanceType=instance_type, InstanceCount=1, CapacityDurationHours=hours)
    return cheapest_offering_rate(out["CapacityBlockOfferings"],
                                  lambda o: o.get("InstanceCount", 1))


def fetch_ultraserver_cb(ec2, ultraserver_types, hours):
    """Cheapest UltraServer Capacity Block $/GPU-hour across types, or None."""
    best = None
    for utype in ultraserver_types:
        gpus = int(utype.rsplit("x", 1)[1])
        out = ec2.describe_capacity_block_offerings(
            UltraserverType=utype, UltraserverCount=1, CapacityDurationHours=hours)
        rate = cheapest_offering_rate(out["CapacityBlockOfferings"],
                                      lambda o: gpus * o.get("UltraserverCount", 1))
        if rate is not None:
            best = rate if best is None else min(best, rate)
    return best


def add_market_prices(rows, cb_keys, client, enabled, args):
    """Fill rows' spot and cb (per instance-hour); return {chip: cb $/GPU-hour}
    for ULTRASERVERS chips."""
    jobs = []
    if not args.no_spot:
        by_region = {}
        for itype, region in rows:
            if region in enabled:
                by_region.setdefault(region, set()).add(itype)
        jobs += [("spot", region, types) for region, types in by_region.items()]
    if not args.no_cb:
        jobs += [("cb", region, itype) for itype, region in cb_keys
                 if region in enabled and (itype, region) in rows]
        jobs += [("ultra", region, (chip, tuple(types)))
                 for chip, (region, types) in ULTRASERVERS.items() if region in enabled]
    for _, region, _ in jobs:
        client.ec2(region)  # create clients before the threads start

    notes = []

    def run(job):
        kind, region, what = job
        ec2 = client.ec2(region)
        try:
            if kind == "spot":
                return job, fetch_spot(ec2, what)
            if kind == "cb":
                return job, fetch_capacity_block(ec2, what, args.cb_hours)
            return job, fetch_ultraserver_cb(ec2, what[1], args.cb_hours)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            name = what[0] if kind == "ultra" else what if kind == "cb" else ""
            if kind != "spot" and code in UNSUPPORTED_CB_ERRORS:
                notes.append(f"{name}@{region}: no Capacity Blocks")
            elif kind != "spot" and code == CB_LIMIT_ERROR:
                notes.append(f"{name}@{region}: Capacity Block offerings hidden "
                             f"for this account ({code})")
            else:
                warn(f"{kind} {region} {name}: {e}")
        except BotoCoreError as e:
            warn(f"{kind} {region}: {e}")
        return job, None

    ultra = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for (kind, region, what), result in pool.map(run, jobs):
            if result is None:
                continue
            if kind == "spot":
                for itype, price in result.items():
                    if (itype, region) in rows:
                        rows[(itype, region)]["spot"] = price
            elif kind == "cb":
                rows[(what, region)]["cb"] = result
            else:
                ultra[what[0]] = result
    for note in sorted(notes):
        print(f"note: {note}", file=sys.stderr)
    return ultra


def summarize(rows):
    """{accelerator: {rate: average per-accelerator $/hr or None}}."""
    per_chip = {}
    for row in rows.values():
        chip = per_chip.setdefault(row["accelerator"], {k: [] for k, _ in RATES})
        for k, _ in RATES:
            if row[k] is not None:
                chip[k].append(row[k] / row["count"])
    return {chip: {k: round(sum(v) / len(v), 4) if v else None for k, v in rates.items()}
            for chip, rates in per_chip.items()}


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def column_letter(n):
    """1 → A, 27 → AA."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def ordered_accelerators(existing, found):
    """Existing column groups first, then any missing DEFAULT_ACCELERATOR_ORDER
    ones (always shown, even with no prices), then newly found ones by name."""
    order = list(existing)
    for chip in DEFAULT_ACCELERATOR_ORDER + sorted(found):
        if chip not in order:
            order.append(chip)
    return order


def data_row(day, accelerators, summary):
    row = [day.isoformat()]
    for chip in accelerators:
        rates = summary.get(chip, {})
        row += ["" if rates.get(k) is None else rates[k] for k, _ in RATES]
    return row


def header_rows(accelerators):
    top, sub = [DATE_HEADER], [""]
    for chip in accelerators:
        top += [chip] + [""] * (len(RATES) - 1)
        sub += [h for _, h in RATES]
    return top, sub


def write_csv(day, summary, out):
    accelerators = ordered_accelerators([], summary)
    w = csv.writer(out)
    w.writerow([DATE_HEADER] + [f"{chip} {h}" for chip in accelerators for _, h in RATES])
    w.writerow(data_row(day, accelerators, summary))


def existing_accelerators(ws):
    """Column-group names from header row 1, or None if the sheet isn't in this
    layout (empty, or the old one-row-per-instance table)."""
    top = ws.row_values(1)
    sub = ws.row_values(2)
    if not top or top[0] != DATE_HEADER or len(sub) < 1 + len(RATES) \
            or sub[1:1 + len(RATES)] != [h for _, h in RATES]:
        return None
    return [top[i] for i in range(1, len(top), len(RATES)) if top[i]]


def write_header(ws, accelerators, start_group=0):
    """Write header rows for column groups start_group.., merging each group's
    name cell across its sub-columns."""
    top, sub = header_rows(accelerators)
    width = len(top)
    if ws.col_count < width:
        ws.add_cols(width - ws.col_count)
    # Freeze first: Sheets refuses merges that straddle frozen and unfrozen rows.
    ws.freeze(rows=HEADER_ROWS, cols=1)
    first_col = 1 + start_group * len(RATES) + (1 if start_group else 0)
    rng = f"{column_letter(first_col)}1:{column_letter(width)}2"
    ws.update([top[first_col - 1:], sub[first_col - 1:]], rng, value_input_option="RAW")
    if start_group == 0:
        ws.merge_cells("A1:A2")
    for g in range(start_group, len(accelerators)):
        c0 = 2 + g * len(RATES)
        ws.merge_cells(f"{column_letter(c0)}1:{column_letter(c0 + len(RATES) - 1)}1")
    ws.format(f"A1:{column_letter(width)}2",
              {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER",
               "wrapStrategy": "WRAP"})


def find_date_row(ws, day):
    """(1-based sheet row holding `day` in column A or None, whether any data
    rows exist)."""
    serial = (day - date(1899, 12, 30)).days  # Sheets date serial number
    col = ws.col_values(1, value_render_option="UNFORMATTED_VALUE")
    data = col[HEADER_ROWS:]
    for i, v in enumerate(data, start=HEADER_ROWS + 1):
        if v == serial or str(v) == day.isoformat():
            return i, True
    return None, any(v != "" for v in data)


def write_sheet(day, summary, credential_path, reset=False):
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(credential_path, scopes=[
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ])
    client = gspread.authorize(creds, http_client=gspread.BackOffHTTPClient)
    book = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = book.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = book.add_worksheet(title=SHEET_NAME, rows=HEADER_ROWS + 1, cols=1)

    existing = existing_accelerators(ws)
    if existing is None:
        # Empty, or a different header layout. Rebuilding clears the sheet, so
        # refuse to discard existing data unless asked.
        if ws.row_values(1) and not reset:
            sys.exit(f"'{SHEET_NAME}' has a different header layout; rerun with "
                     "--reset-sheet to clear it and start over (its rows are lost).")
        ws.clear()
        ws.unmerge_cells(f"A1:{column_letter(ws.col_count)}{ws.row_count}")
        # Keep one row below the header: Sheets can't freeze every row.
        ws.resize(rows=HEADER_ROWS + 1, cols=1)
        accelerators = ordered_accelerators([], summary)
        write_header(ws, accelerators)
        print(f"initialized '{SHEET_NAME}' headers", file=sys.stderr)
    else:
        accelerators = ordered_accelerators(existing, summary)
        if len(accelerators) > len(existing):
            write_header(ws, accelerators, start_group=len(existing))
            print(f"added column group(s): {', '.join(accelerators[len(existing):])}",
                  file=sys.stderr)

    values = data_row(day, accelerators, summary)
    row, has_data = find_date_row(ws, day)
    first = HEADER_ROWS + 1
    if row:
        ws.update([values], f"A{row}:{column_letter(len(values))}{row}",
                  value_input_option="USER_ENTERED")
        print(f"overwrote row {row} ({day}) in '{SHEET_NAME}'", file=sys.stderr)
    elif not has_data:
        # Empty sheet: fill the spare row under the header.
        if ws.row_count < first:
            ws.add_rows(first - ws.row_count)
        ws.update([values], f"A{first}:{column_letter(len(values))}{first}",
                  value_input_option="USER_ENTERED")
        print(f"wrote first row ({day}) in '{SHEET_NAME}'", file=sys.stderr)
    else:
        ws.insert_row(values, index=first, value_input_option="USER_ENTERED")
        print(f"inserted {day} at row {first} of '{SHEET_NAME}'", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "diag"),
                    help="AWS CLI profile (default: $AWS_PROFILE or diag)")
    ap.add_argument("--regions",
                    help="comma-separated region codes, or 'all' (default: per chip, "
                         "the first region in REGION_PREFERENCE that sells it)")
    ap.add_argument("--min-accel", type=float, default=8,
                    help="minimum accelerators (GPUs / chips) per instance (default: 8)")
    ap.add_argument("--match", help="regex an instance type must match, e.g. 'p5|trn'")
    ap.add_argument("--no-spot", action="store_true", help="skip Spot prices")
    ap.add_argument("--no-cb", action="store_true", help="skip Capacity Block offerings")
    ap.add_argument("--cb-hours", type=int, default=24,
                    help="Capacity Block length to price, in hours (default 24)")
    ap.add_argument("--reset-sheet", action="store_true",
                    help="clear the sheet and rewrite its headers if its layout differs")
    ap.add_argument("--workers", type=int, default=16,
                    help="parallel Spot API calls (default 16)")
    ap.add_argument("-o", "--output",
                    help="write the row as CSV to this file instead of the sheet ('-' = stdout)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the row as CSV instead of writing the sheet")
    ap.add_argument("--credential", default=CREDENTIAL_PATH,
                    help="Google service-account key (default: %(default)s)")
    args = ap.parse_args()

    client = Aws(args.profile)
    enabled = enabled_regions(client)
    per_chip_region = not args.regions
    if per_chip_region:
        regions = [r for r in REGION_PREFERENCE if r in enabled]
    elif args.regions == "all":
        regions = None
    else:
        regions = [r.strip() for r in args.regions.split(",")]

    od = fetch_products(client, "OnDemand")
    cb = fetch_products(client, "CapacityBlock")
    if regions is not None:
        od, cb = ([p for p in ps if p["product"]["attributes"].get("regionCode") in regions]
                  for ps in (od, cb))

    # Describe each listed type in an enabled region that lists it, largest first.
    order = {r: i for i, r in enumerate(regions or REGION_PREFERENCE)}
    types_by_region = {}
    for p in od + cb:
        attrs = p["product"]["attributes"]
        if attrs.get("regionCode") in enabled:
            types_by_region.setdefault(attrs["regionCode"], set()).add(attrs["instanceType"])
    types_by_region = dict(sorted(types_by_region.items(),
                                  key=lambda kv: order.get(kv[0], len(order))))
    chips = fetch_chip_info(client, types_by_region)

    rows, cb_keys = build_rows(od, cb, chips, args.min_accel)
    if args.match:
        rows = {k: r for k, r in rows.items() if re.search(args.match, k[0])}
    if per_chip_region:
        rows = keep_preferred_region(rows, regions)
    cb_keys = {k for k in cb_keys if k in rows}
    ultra = add_market_prices(rows, cb_keys, client, enabled, args)

    summary = {chip: rates for chip, rates in summarize(rows).items()
               if any(v is not None for v in rates.values())}
    for chip, rate in ultra.items():
        summary.setdefault(chip, {k: None for k, _ in RATES})["cb"] = round(rate, 4)
    day = datetime.now(timezone.utc).date()

    if args.dry_run or args.output == "-":
        write_csv(day, summary, sys.stdout)
    elif args.output:
        with open(args.output, "w", newline="") as f:
            write_csv(day, summary, f)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        write_sheet(day, summary, args.credential, args.reset_sheet)


if __name__ == "__main__":
    main()
