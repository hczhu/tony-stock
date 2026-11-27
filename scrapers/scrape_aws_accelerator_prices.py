#!/usr/bin/env python3
"""Fetch AWS EC2 GPU / Trainium / Inferentia rental prices from the AWS Price List API.

Source: the official AWS Price List Query API (`pricing:GetProducts`, service
code AmazonEC2), called through the `aws` CLI (boto3 is not installed on the
host or in the tony-stock container; the CLI is on both).

Covers every EC2 instance whose instanceFamily is "GPU instance" (NVIDIA / AMD
GPUs) or "Machine Learning ASIC Instances" (Trainium, Inferentia, Gaudi), for
Linux, shared tenancy, no pre-installed software, in every region (or the ones
given with --regions). China regions, which are priced in CNY, are skipped.

Output is CSV, one row per (instance type, region):

    instance_type, accelerator, family, region, accel_count, accel_memory,
    vcpu, memory, on_demand_hr, on_demand_per_accel_hr,
    ri_1yr_hr, ri_1yr_per_accel_hr, ri_3yr_hr, ri_3yr_per_accel_hr

  on_demand_hr       On-Demand USD per instance-hour
  ri_1yr_hr/3yr_hr   cheapest *effective* Standard Reserved Instance rate for
                     that term, i.e. hourly fee + upfront / hours-in-term,
                     across No/Partial/All Upfront
  *_per_accel_hr     the same divided by accel_count (the API's `gpu` field,
                     which is also the Trainium/Inferentia chip count)

Not covered: Spot prices (ec2:DescribeSpotPriceHistory) and EC2 Capacity
Blocks for ML (ec2:DescribeCapacityBlockOfferings). Capacity Block SKUs appear
in the price list at $0 because their price is set per offering, so they are
skipped. Savings Plans rates are a separate API and also not included.

Credentials: needs an AWS identity allowed `pricing:GetProducts`. On hp-omen
the default profile is not; the `diag` profile is, so it is the default here
(override with --profile or AWS_PROFILE).

    python3 scrape_aws_accelerator_prices.py                      # all regions → stdout
    python3 scrape_aws_accelerator_prices.py -o prices.csv
    python3 scrape_aws_accelerator_prices.py --regions us-east-1,us-east-2,us-west-2
    python3 scrape_aws_accelerator_prices.py --cheapest           # cheapest region per type
    python3 scrape_aws_accelerator_prices.py --match 'p5|p6|trn|inf'
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys

FAMILIES = ["GPU instance", "Machine Learning ASIC Instances"]

# The Price List API's only endpoints are us-east-1, eu-central-1 and
# ap-south-1; every endpoint serves prices for all regions.
PRICING_ENDPOINT_REGION = "us-east-1"

BASE_FILTERS = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
    "licenseModel": "No License required",
    "marketoption": "OnDemand",  # drops the $0 Capacity Block SKUs
}

# Accelerator by instance-type prefix (the API gives the count and memory,
# not the chip name). Longest prefix wins; unknown types are left blank.
ACCELERATORS = {
    "p2": "NVIDIA K80",
    "p3": "NVIDIA V100",
    "p3dn": "NVIDIA V100 32GB",
    "p4d": "NVIDIA A100 40GB",
    "p4de": "NVIDIA A100 80GB",
    "p5": "NVIDIA H100",
    "p5e": "NVIDIA H200",
    "p5en": "NVIDIA H200",
    "p6-b200": "NVIDIA B200",
    "p6-b300": "NVIDIA B300",
    "p6e-gb200": "NVIDIA GB200",
    "p6e-gb300": "NVIDIA GB300",
    "g2": "NVIDIA K520",
    "g3": "NVIDIA M60",
    "g3s": "NVIDIA M60",
    "g4dn": "NVIDIA T4",
    "g4ad": "AMD Radeon Pro V520",
    "g5": "NVIDIA A10G",
    "g5g": "NVIDIA T4G",
    "g6": "NVIDIA L4",
    "g6f": "NVIDIA L4 (fractional)",
    "gr6": "NVIDIA L4",
    "gr6f": "NVIDIA L4 (fractional)",
    "g6e": "NVIDIA L40S",
    "g7e": "NVIDIA RTX PRO 6000 Blackwell",
    "dl1": "Intel Gaudi",
    "dl2q": "Qualcomm AI 100",
    "inf1": "AWS Inferentia",
    "inf2": "AWS Inferentia2",
    "trn1": "AWS Trainium",
    "trn1n": "AWS Trainium",
    "trn2": "AWS Trainium2",
    "trn2u": "AWS Trainium2",
    "trn3": "AWS Trainium3",
}

HOURS = {"1yr": 365 * 24, "3yr": 3 * 365 * 24}

COLUMNS = [
    "instance_type", "accelerator", "family", "region", "accel_count",
    "accel_memory", "vcpu", "memory", "on_demand_hr", "on_demand_per_accel_hr",
    "ri_1yr_hr", "ri_1yr_per_accel_hr", "ri_3yr_hr", "ri_3yr_per_accel_hr",
]


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def get_products(profile, filters):
    """Yield every product (parsed JSON) matching filters, following pagination."""
    cmd = ["aws", "pricing", "get-products",
           "--region", PRICING_ENDPOINT_REGION,
           "--service-code", "AmazonEC2",
           "--output", "json",
           "--filters"]
    cmd += [f"Type=TERM_MATCH,Field={k},Value={v}" for k, v in filters.items()]
    if profile:
        cmd[1:1] = ["--profile", profile]
    token = None
    while True:
        page_cmd = cmd + (["--next-token", token] if token else [])
        # --no-paginate: one API page per call, so we see NextToken ourselves.
        res = subprocess.run(page_cmd + ["--no-paginate"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            sys.exit(f"aws pricing get-products failed:\n{res.stderr.strip()}")
        page = json.loads(res.stdout)
        for item in page.get("PriceList", []):
            yield json.loads(item)
        token = page.get("NextToken")
        if not token:
            return


def fetch(profile, regions):
    products = []
    for family in FAMILIES:
        for region in regions or [None]:
            filters = dict(BASE_FILTERS, instanceFamily=family)
            if region:
                filters["regionCode"] = region
            products.extend(get_products(profile, filters))
    return products


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def accelerator_name(instance_type):
    prefix = instance_type.split(".")[0]
    while prefix:
        if prefix in ACCELERATORS:
            return ACCELERATORS[prefix]
        prefix = prefix[:-1]
    return ""


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


def per_accel(rate, count):
    return rate / count if rate is not None and count else None


def parse(products):
    rows = {}
    for p in products:
        a = p["product"]["attributes"]
        od = on_demand_rate(p["terms"])
        if od is None:
            continue
        ri = reserved_rates(p["terms"])
        count = to_number(a.get("gpu"))
        row = {
            "instance_type": a["instanceType"],
            "accelerator": accelerator_name(a["instanceType"]),
            "family": a.get("instanceFamily", ""),
            "region": a.get("regionCode", ""),
            "accel_count": count,
            "accel_memory": "" if a.get("gpuMemory") == "NA" else a.get("gpuMemory", ""),
            "vcpu": a.get("vcpu", ""),
            "memory": a.get("memory", ""),
            "on_demand_hr": od,
            "on_demand_per_accel_hr": per_accel(od, count),
            "ri_1yr_hr": ri.get("1yr"),
            "ri_1yr_per_accel_hr": per_accel(ri.get("1yr"), count),
            "ri_3yr_hr": ri.get("3yr"),
            "ri_3yr_per_accel_hr": per_accel(ri.get("3yr"), count),
        }
        key = (row["instance_type"], row["region"])
        # Same type/region can appear under more than one SKU; keep the cheapest.
        if key not in rows or od < rows[key]["on_demand_hr"]:
            rows[key] = row
    return list(rows.values())


def cheapest_per_type(rows):
    # Many regions tie on price; prefer us-east-1, then alphabetical region.
    def key(r):
        return (r["on_demand_hr"], r["region"] != "us-east-1", r["region"])
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
            row["on_demand_hr"], row["region"])


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "diag"),
                    help="AWS CLI profile with pricing:GetProducts (default: $AWS_PROFILE or diag)")
    ap.add_argument("--regions", help="comma-separated region codes (default: all)")
    ap.add_argument("--match", help="regex an instance type must match, e.g. 'p5|trn|inf'")
    ap.add_argument("--cheapest", action="store_true",
                    help="keep only the cheapest On-Demand region per instance type")
    ap.add_argument("-o", "--output", help="CSV file to write (default: stdout)")
    args = ap.parse_args()

    regions = [r.strip() for r in args.regions.split(",")] if args.regions else None
    rows = parse(fetch(args.profile, regions))
    if args.match:
        rows = [r for r in rows if re.search(args.match, r["instance_type"])]
    if args.cheapest:
        rows = cheapest_per_type(rows)
    rows.sort(key=type_sort_key)

    if args.output:
        with open(args.output, "w", newline="") as f:
            write_csv(rows, f)
        print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    else:
        write_csv(rows, sys.stdout)


if __name__ == "__main__":
    main()
