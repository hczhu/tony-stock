#!/usr/bin/env python3
"""Fetch AWS EC2 GPU / Trainium / Inferentia rental prices from AWS's official APIs.

Sources (all called through boto3, which is installed in the tony-stock
container; on the host, `pip install --user boto3` first):

  * Price List Query API (`pricing:GetProducts`, service AmazonEC2):
    On-Demand and Reserved Instance list prices, plus which instance types and
    regions sell EC2 Capacity Blocks for ML.
  * `ec2:DescribeSpotPriceHistory`: the current Spot price in each
    Availability Zone.
  * `ec2:DescribeCapacityBlockOfferings`: the Capacity Block offerings
    currently for sale. Capacity Block SKUs appear in the price list at $0
    because their price is set per offering, so this is the only source of
    their price.

  * `ec2:DescribeInstanceTypes`: the accelerator name and count of each
    instance type, which decides what is watched (see below).

Watches mainstream AI chips: NVIDIA GPUs in the P instance family newer than
A100 (H100, H200, B200, B300, ...) and AWS Trainium / Inferentia (not first-gen
Inferentia). Chips are recognized by the names DescribeInstanceTypes reports,
not a hardcoded list, so new ones such as NVIDIA Vera Rubin or Trainium4 are
picked up automatically once AWS lists them. Only instance sizes with at least
--min-accel (default 8) accelerators; Linux, shared tenancy, no pre-installed
software. Each chip is watched in one region: the first region in
REGION_PREFERENCE (largest first: us-east-1, us-east-2, us-west-2, ...) that
sells it. --regions overrides this for all chips ("all" for every region).
China regions, which are priced in CNY, are skipped.
Spot and Capacity Block prices are only fetched in regions enabled for the
account (opt-in regions such as ap-south-2 are left blank) and not in Local
Zones.

Output: by default the script replaces the "AWS" worksheet of the
"GPU&ASIC pricing" spreadsheet (SPREADSHEET_ID below) using the smart-stocker
service-account key; -o FILE / --dry-run write CSV instead. One row per
(instance type, region):

  on_demand_hr       On-Demand USD per instance-hour
  ri_1yr_hr/3yr_hr   cheapest *effective* Standard Reserved Instance rate for
                     that term, i.e. hourly fee + upfront / hours-in-term,
                     across No/Partial/All Upfront
  spot_hr, spot_az   lowest current Spot price across the region's AZs, and
                     that AZ
  cb_hr              cheapest Capacity Block offering for --cb-hours (default
                     24) hours, as upfront fee / hours / instances
  cb_start, cb_az    start time (UTC) and AZ of that offering
  *_per_accel_hr     the same divided by accel_count (GPUs or Trainium /
                     Inferentia chips per instance)

A row exists for every type/region with an On-Demand price or a Capacity Block
listing, so Capacity-Block-only types (e.g. trn2 in us-east-2) have a blank
on_demand_hr. Not included: Savings Plans (a separate API).

Credentials: needs an AWS identity allowed `pricing:GetProducts`,
`ec2:DescribeRegions`, `ec2:DescribeInstanceTypes`, `ec2:DescribeSpotPriceHistory` and
`ec2:DescribeCapacityBlockOfferings`. On hp-omen the `diag` profile has them,
so it is the default here (override with --profile or AWS_PROFILE).

    python3 scrape_aws_accelerator_prices.py                      # each chip's region → "AWS" sheet
    python3 scrape_aws_accelerator_prices.py --dry-run            # CSV to stdout, no sheet
    python3 scrape_aws_accelerator_prices.py -o prices.csv        # CSV file, no sheet
    python3 scrape_aws_accelerator_prices.py --regions us-east-1,us-east-2,us-west-2
    python3 scrape_aws_accelerator_prices.py --regions all --min-accel 1
    python3 scrape_aws_accelerator_prices.py --cheapest           # cheapest region per type
    python3 scrape_aws_accelerator_prices.py --match 'p5|p6|trn|inf'
    python3 scrape_aws_accelerator_prices.py --no-spot --no-cb    # list prices only
    python3 scrape_aws_accelerator_prices.py --cb-hours 168       # one-week blocks
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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

# Which chips are watched. Chip names come from ec2:DescribeInstanceTypes, so a
# new chip (e.g. NVIDIA Vera Rubin, Trainium4) is picked up with no code
# change as soon as AWS lists it:
#   * NVIDIA GPUs in AWS's P instance family (its ML-training GPU line; the G
#     family is graphics/inference cards like L4, L40S, RTX PRO), except the
#     generations up to A100.
#   * AWS's own Trainium / Inferentia chips, except first-gen Inferentia.
OLD_NVIDIA_GPUS = {"K80", "V100", "A100"}
OLD_AWS_CHIPS = {"Inferentia"}
AWS_CHIP_MAKERS = {"AWS", "Annapurna"}  # Trainium3 reports "Annapurna"

# Each chip is watched in the first of these regions that sells it (largest
# regions first), unless --regions is given. E.g. H100 → us-east-1, Trainium2
# → us-east-2. GB200 is only sold in the Dallas Local Zone as UltraServer
# Capacity Blocks the API won't price for this account, so it never shows.
REGION_PREFERENCE = [
    "us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1",
    "ap-northeast-1", "eu-west-2", "ap-southeast-2", "ap-south-1", "eu-north-1",
    "ap-northeast-2", "ca-central-1", "us-west-1", "sa-east-1",
]

# EC2 errors meaning "no Capacity Blocks for this type here", not a failure.
UNSUPPORTED_CB_ERRORS = {"InvalidParameterValue", "InvalidAction"}

HOURS = {"1yr": 365 * 24, "3yr": 3 * 365 * 24}

PRICE_COLUMNS = ["on_demand", "ri_1yr", "ri_3yr", "spot", "cb"]

COLUMNS = [
    "instance_type", "accelerator", "family", "region", "accel_count",
    "accel_memory", "vcpu", "memory",
    "on_demand_hr", "on_demand_per_accel_hr",
    "ri_1yr_hr", "ri_1yr_per_accel_hr",
    "ri_3yr_hr", "ri_3yr_per_accel_hr",
    "spot_hr", "spot_per_accel_hr", "spot_az",
    "cb_hr", "cb_per_accel_hr", "cb_start", "cb_az",
    "fetched_at",
]

# DescribeCapacityBlockOfferings has a low request-rate limit; adaptive retry
# mode backs off client-side when throttled.
BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


# --------------------------------------------------------------------------- #
# AWS clients
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


# --------------------------------------------------------------------------- #
# Price list (On-Demand, Reserved, Capacity Block listings)
# --------------------------------------------------------------------------- #

def fetch_products(client, regions, market):
    """Accelerated-instance products for marketoption=market (OnDemand / CapacityBlock)."""
    pages = client.pricing.get_paginator("get_products")
    products = []
    for family in FAMILIES:
        for region in regions or [None]:
            filters = dict(BASE_FILTERS, instanceFamily=family, marketoption=market)
            if region:
                filters["regionCode"] = region
            for page in pages.paginate(
                    ServiceCode="AmazonEC2",
                    Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v}
                             for k, v in filters.items()]):
                products.extend(json.loads(item) for item in page["PriceList"])
    return products


def fetch_chip_info(client, types_by_region):
    """Return {instance_type: (accelerator, count)} from ec2:DescribeInstanceTypes.

    types_by_region: {region: set of instance types listed there}. Each type is
    described in the first region (in the given order) that lists it.
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
    """(display name, count) of an instance type's accelerator, or None."""
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


def reserved_rates(terms):
    """Return {"1yr": rate, "3yr": rate}: cheapest effective Standard RI $/hr."""
    best = {}
    for t in terms.get("Reserved", {}).values():
        attrs = t["termAttributes"]
        if attrs.get("OfferingClass") != "standard":
            continue
        term = attrs.get("LeaseContractLength")
        if term not in HOURS:
            continue
        dims = list(t["priceDimensions"].values())
        if any("USD" not in d["pricePerUnit"] for d in dims):
            continue  # China regions price in CNY
        hourly = sum(float(d["pricePerUnit"]["USD"]) for d in dims if d["unit"] == "Hrs")
        upfront = sum(float(d["pricePerUnit"]["USD"]) for d in dims if d["unit"] == "Quantity")
        rate = hourly + upfront / HOURS[term]
        if rate > 0 and (term not in best or rate < best[term]):
            best[term] = rate
    return best


def to_number(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def base_row(attrs, chip):
    accelerator, count = chip
    return {
        "instance_type": attrs["instanceType"],
        "accelerator": accelerator,
        "family": attrs.get("instanceFamily", ""),
        "region": attrs.get("regionCode", ""),
        "accel_count": float(count),
        "accel_memory": "" if attrs.get("gpuMemory") == "NA" else attrs.get("gpuMemory", ""),
        "vcpu": to_number(attrs.get("vcpu")),
        "memory": attrs.get("memory", ""),
        **{f"{c}_hr": None for c in PRICE_COLUMNS},
        "spot_az": "", "cb_start": "", "cb_az": "",
    }


def build_rows(od_products, cb_products, chips, min_accel):
    """Rows keyed by (instance_type, region) for watched chips with at least
    min_accel accelerators per instance."""
    def keep(p):
        itype = p["product"]["attributes"]["instanceType"]
        chip = chips.get(itype)
        return chip and is_watched_chip(itype, chip[0]) and chip[1] >= min_accel

    rows = {}
    for p in filter(keep, od_products):
        od = on_demand_rate(p["terms"])
        if od is None:
            continue
        attrs = p["product"]["attributes"]
        row = base_row(attrs, chips[attrs["instanceType"]])
        ri = reserved_rates(p["terms"])
        row.update(on_demand_hr=od, ri_1yr_hr=ri.get("1yr"), ri_3yr_hr=ri.get("3yr"))
        key = (row["instance_type"], row["region"])
        # Same type/region can appear under more than one SKU; keep the cheapest.
        if key not in rows or od < rows[key]["on_demand_hr"]:
            rows[key] = row
    cb_keys = set()
    for p in filter(keep, cb_products):
        attrs = p["product"]["attributes"]
        key = (attrs["instanceType"], attrs.get("regionCode", ""))
        cb_keys.add(key)
        if key not in rows:
            rows[key] = base_row(attrs, chips[attrs["instanceType"]])
    return rows, cb_keys


def keep_preferred_region(rows, regions):
    """Per chip, keep only the rows in its first region (in regions order)."""
    rank = {r: i for i, r in enumerate(regions)}
    best = {}
    for (_, region), row in rows.items():
        chip = row["accelerator"]
        if chip not in best or rank[region] < rank[best[chip]]:
            best[chip] = region
    return {k: r for k, r in rows.items() if best[r["accelerator"]] == k[1]}


# --------------------------------------------------------------------------- #
# Spot and Capacity Blocks (EC2 API, per region)
# --------------------------------------------------------------------------- #

def enabled_regions(client):
    out = client.ec2(PRICING_ENDPOINT_REGION).describe_regions()
    return {r["RegionName"] for r in out["Regions"]}


def fetch_spot(ec2, instance_types):
    """Return {instance_type: (price, az)}: lowest current Spot price in the region."""
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
    for (itype, az), r in latest.items():
        price = float(r["SpotPrice"])
        if itype not in best or price < best[itype][0]:
            best[itype] = (price, az)
    return best


def fetch_capacity_block(ec2, instance_type, hours):
    """Return (price_per_instance_hr, start, az) of the cheapest offering, or None."""
    out = ec2.describe_capacity_block_offerings(
        InstanceType=instance_type, InstanceCount=1, CapacityDurationHours=hours)
    best = None
    for o in out["CapacityBlockOfferings"]:
        if o.get("CurrencyCode", "USD") != "USD":
            continue
        dur = o["CapacityBlockDurationHours"] + o.get("CapacityBlockDurationMinutes", 0) / 60
        if dur <= 0:
            continue
        rate = float(o["UpfrontFee"]) / dur / o.get("InstanceCount", 1)
        cand = (rate, o["StartDate"], o["AvailabilityZone"])
        if best is None or cand < best:
            best = cand
    return best


def add_market_prices(rows, cb_keys, client, enabled, args):
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
    for _, region, _ in jobs:
        client.ec2(region)  # create clients before the threads start

    unsupported = []

    def run(job):
        kind, region, what = job
        ec2 = client.ec2(region)
        try:
            if kind == "spot":
                return job, fetch_spot(ec2, what)
            return job, fetch_capacity_block(ec2, what, args.cb_hours)
        except ClientError as e:
            # The price list lists some Capacity Block type/regions that the
            # EC2 API rejects as unsupported, and a few regions lack the API.
            if e.response["Error"]["Code"] in UNSUPPORTED_CB_ERRORS and kind == "cb":
                unsupported.append(f"{what}@{region}")
            else:
                warn(f"{kind} {region} {what if kind == 'cb' else ''}: {e}")
            return job, None
        except BotoCoreError as e:
            warn(f"{kind} {region} {what if kind == 'cb' else ''}: {e}")
            return job, None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for (kind, region, what), result in pool.map(run, jobs):
            if not result:
                continue
            if kind == "spot":
                for itype, (price, az) in result.items():
                    row = rows.get((itype, region))
                    if row:
                        row.update(spot_hr=price, spot_az=az)
            else:
                rate, start, az = result
                rows[(what, region)].update(
                    cb_hr=rate, cb_start=start.strftime("%Y-%m-%dT%H:%MZ"), cb_az=az)
    if unsupported:
        print(f"note: {len(unsupported)} listed Capacity Block type/regions not offered "
              f"by the EC2 API: {', '.join(sorted(unsupported))}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Selection and output
# --------------------------------------------------------------------------- #

def add_per_accel(rows):
    for r in rows:
        n = r["accel_count"]
        for c in PRICE_COLUMNS:
            v = r[f"{c}_hr"]
            r[f"{c}_per_accel_hr"] = v / n if v is not None and n else None


def headline_price(r):
    """Cheapest reservable price: On-Demand or Capacity Block (not interruptible Spot)."""
    prices = [p for p in (r["on_demand_hr"], r["cb_hr"]) if p is not None]
    return min(prices) if prices else math.inf


def cheapest_per_type(rows):
    # Many regions tie on price; prefer us-east-1, then alphabetical region.
    def key(r):
        return (headline_price(r), r["region"] != "us-east-1", r["region"])
    best = {}
    for r in rows:
        t = r["instance_type"]
        if t not in best or key(r) < key(best[t]):
            best[t] = r
    return list(best.values())


def type_sort_key(row):
    # Group by family then series, then size by accelerator count.
    series = row["instance_type"].split(".")[0]
    return (row["family"], series, row["accel_count"] or 0,
            headline_price(row), row["region"])


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:g}" if v == int(v) or v < 0.01 else f"{v:.4f}".rstrip("0").rstrip(".")
    return v


def write_csv(rows, out):
    w = csv.writer(out)
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([fmt(r[c]) for c in COLUMNS])


def write_sheet(rows, credential_path):
    """Replace the AWS worksheet with the header and rows (numbers stay numeric)."""
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
        ws = book.add_worksheet(title=SHEET_NAME, rows=len(rows) + 1, cols=len(COLUMNS))
    matrix = [COLUMNS] + [["" if r[c] is None else r[c] for c in COLUMNS] for r in rows]
    ws.clear()
    ws.resize(rows=len(matrix), cols=len(COLUMNS))
    ws.update(matrix, value_input_option="RAW")
    ws.freeze(rows=1)
    print(f"wrote {len(rows)} rows to '{SHEET_NAME}' sheet", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "diag"),
                    help="AWS CLI profile (default: $AWS_PROFILE or diag)")
    ap.add_argument("--regions",
                    help="comma-separated region codes, or 'all' (default: per chip, "
                         "the first region in REGION_PREFERENCE that sells it)")
    ap.add_argument("--min-accel", type=float, default=8,
                    help="minimum accelerators (GPUs / chips) per instance (default: 8)")
    ap.add_argument("--match", help="regex an instance type must match, e.g. 'p5|trn|inf'")
    ap.add_argument("--cheapest", action="store_true",
                    help="keep only the cheapest region per instance type "
                         "(by On-Demand or Capacity Block price)")
    ap.add_argument("--no-spot", action="store_true", help="skip Spot prices")
    ap.add_argument("--no-cb", action="store_true", help="skip Capacity Block offerings")
    ap.add_argument("--cb-hours", type=int, default=24,
                    help="Capacity Block duration to price, in hours (default 24)")
    ap.add_argument("--workers", type=int, default=16,
                    help="parallel EC2 API calls (default 16)")
    ap.add_argument("-o", "--output",
                    help="write CSV to this file instead of the Google Sheet ('-' = stdout)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print CSV to stdout instead of writing the Google Sheet")
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

    # Fetch every region at once (4 queries) and filter locally; cheaper than
    # one query per region when watching several.
    od = fetch_products(client, None, "OnDemand")
    cb = [] if args.no_cb else fetch_products(client, None, "CapacityBlock")
    if regions is not None:
        od, cb = ([p for p in ps if p["product"]["attributes"].get("regionCode") in regions]
                  for ps in (od, cb))

    # Describe each listed type in an enabled region that lists it, preferring
    # the order of `regions` (us-east-1 first).
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
    if not (args.no_spot and args.no_cb):
        add_market_prices(rows, cb_keys, client, enabled, args)

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    # Drop type/regions with no price at all (e.g. Capacity-Block-only types in
    # opt-in regions or Local Zones, where the EC2 API isn't queried).
    rows = [r for r in rows.values()
            if any(r[f"{c}_hr"] is not None for c in PRICE_COLUMNS)]
    for r in rows:
        r["fetched_at"] = fetched_at
    add_per_accel(rows)
    if args.cheapest:
        rows = cheapest_per_type(rows)
    rows.sort(key=type_sort_key)

    if args.dry_run or args.output == "-":
        write_csv(rows, sys.stdout)
    elif args.output:
        with open(args.output, "w", newline="") as f:
            write_csv(rows, f)
        print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    else:
        write_sheet(rows, args.credential)


if __name__ == "__main__":
    main()
