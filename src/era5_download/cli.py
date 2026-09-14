"""Console entry point; no credentials are embedded or printed."""

from __future__ import annotations

import argparse
import calendar
import csv
import itertools
import json
import math
import re
import sys
import time
from collections import deque
from pathlib import Path

from . import __version__
from .variables import find_variables, print_variable_table
from .client import CDSError, ERA5Client
from .config import load_accounts
from .manifest import (ManifestEntry, append_manifest, read_manifest,
                       validate_filename, validate_manifest_append, write_manifest)
from .mappings import resolve_dataset
from .naming import filename_from_request, generate_flist, month_range
from .requests import build_template, monthly_request
from .transfer import DownloadBusy, DownloadError, download_file

_OPEN_STATUSES = {"accepted", "running"}


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _count(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def _year_month(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})(\d{2})?(\d{2})?", value)
    if not match:
        raise argparse.ArgumentTypeError("must be YYYY, YYYYMM, or YYYYMMDD")
    year = int(match[1])
    month = int(match[2]) if match[2] else 1
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError("month must be between 01 and 12")
    if match[3]:
        try:
            days_in_month = calendar.monthrange(year, month)[1]
        except calendar.IllegalMonthError:
            days_in_month = 0
        if not 1 <= int(match[3]) <= days_in_month:
            raise argparse.ArgumentTypeError("day is not valid for that year and month")
    return year, month


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit and download ERA5 CDS jobs with shared-worker reservations.")
    parser.add_argument("--version", action="version", version=f"era5-download {__version__}")
    parser.add_argument("--config", type=Path, help="JSON account configuration")
    parser.add_argument("--rc-file", type=Path, help="CDS credential file (default: ~/.cdsapirc)")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--account", action="append", help="account profile; may be repeated")
    selection.add_argument("--all-accounts", action="store_true", help="use every configured account")
    parser.add_argument("--url", help="override the CDS API URL")
    parser.add_argument("--timeout", type=_positive, default=60, help="HTTP timeout in seconds (default: 60)")
    parser.add_argument("--retries", type=_nonnegative, default=5, help="additional attempts (default: 5)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="show which account(s)/credentials will be used, without contacting CDS")
    variables = sub.add_parser("variables", help="list, filter and search the bundled variable catalogue offline")
    variables.add_argument("query", nargs="*", help="search short names, CDS names and parameter IDs")
    variables.add_argument("--search", action="append", default=[], help="search text; repeat to require all terms")
    variables.add_argument("--dataset", action="append", default=[], help="dataset alias or collection ID; repeat to include several")
    variables.add_argument("--family", choices=("era5", "era5-land"), help="filter by dataset family")
    variables.add_argument("--frequency", choices=("hourly", "monthly", "daily"), help="filter by dataset frequency")
    variables.add_argument("--json", action="store_true", help="print matching records as JSON")
    jobs = sub.add_parser("jobs", help="list submitted CDS jobs")
    jobs.add_argument("--status", choices=("accepted", "running", "successful", "failed", "rejected"))
    jobs.add_argument("--limit", type=_count, default=1000, help="maximum jobs per account (default: 1000)")
    jobs.add_argument("--json", action="store_true", help="print job summaries as JSON")
    jobs.add_argument("--manifest", type=Path, help="also write a CSV manifest; fails if it already exists")
    jobs.add_argument("--from-manifest", type=Path,
                      help="look up only jobs in this CSV, using its accounts and filenames; ignores --limit")
    jobs.add_argument("--urls-csv", type=Path,
                      help="save job statuses and successful result URLs to a new CSV")
    for name, help_text in (("download", "download job IDs, a manifest, or all successful jobs"),
                            ("watch", "poll and download successful jobs repeatedly")):
        command = sub.add_parser(name, help=help_text)
        if name == "download":
            command.add_argument("job_ids", nargs="*", help="existing CDS request IDs")
            command.add_argument("--manifest", type=Path, help="canonical or legacy CSV manifest")
            command.add_argument("--urls-csv", type=Path,
                                 help="download filename/url CSV rows without CDS credentials; ignores other columns")
        else:
            command.add_argument("--interval", type=_positive, default=60, help="seconds between cycles (default: 60)")
            command.add_argument("--cycles", type=_count, help="stop after this many cycles; default: run until Ctrl-C")
        command.add_argument("-o", "--output-dir", type=Path, default=Path("."))
        command.add_argument("--limit", type=_count, default=1000, help="maximum jobs per account per cycle")
        command.add_argument("--overwrite", action="store_true", help="replace completed outputs")
        command.add_argument("--quiet", action="store_true", help="disable progress bars")
    submit = sub.add_parser("submit", help="submit new CDS jobs for a range of months")
    submit.add_argument("dataset", help="dataset alias (see mappings.DATASET_MAPPING) or raw CDS collection id")
    submit.add_argument("--var", action="append", dest="variables",
                        help="official ECMWF GRIB short name, e.g. u, sst, 2t, 10u; "
                             "may be repeated or comma-separated")
    submit.add_argument("--pressure-level", action="append", dest="pressure_levels",
                        help="ERA5 pressure level in hPa; required for pressure-level datasets unless in the template; may be repeated")
    submit.add_argument("--template", type=Path,
                        help="JSON request object; must omit 'year' and 'month'. Required unless --var is "
                             "given; combine both to override just the template's variable/pressure_level")
    submit.add_argument("--begin", required=True, type=_year_month,
                        help="YYYY, YYYYMM, or YYYYMMDD; inclusive start month (day is validated but ignored)")
    submit.add_argument("--end", required=True, type=_year_month,
                        help="YYYY, YYYYMM, or YYYYMMDD; exclusive end month (day is validated but ignored)")
    submit.add_argument("--manifest", required=True, type=Path,
                        help="CSV manifest to append submitted jobs to; read first to skip repeats")
    submit.add_argument("--max-in-flight", type=_count, default=2,
                        help="max accepted/running jobs submitted per account in this invocation (default: 2)")
    submit.add_argument("--poll-interval", type=_positive, default=5,
                        help="seconds between status polls while throttled (default: 5)")
    submit.add_argument("--quiet", action="store_true", help="suppress per-job output")
    direct = sub.add_parser("fetch-url", help="download an HTTP(S) URL without a CDS account")
    direct.add_argument("url_or_file", help="URL, or plain-text .link/.txt/.url file containing a URL")
    direct.add_argument("-o", "--output", required=True, type=Path, help="destination file")
    direct.add_argument("--expected-size", type=_nonnegative, help="expected byte count")
    direct.add_argument("--overwrite", action="store_true")
    direct.add_argument("--quiet", action="store_true")
    filenames = sub.add_parser("filenames", help="print monthly filenames without contacting CDS")
    filenames.add_argument("fields", nargs="+", help="field codes, e.g. u200 v850 sst")
    filenames.add_argument("--begin", required=True, type=_year_month,
                           help="YYYY, YYYYMM, or YYYYMMDD; inclusive start month (day is validated but ignored)")
    filenames.add_argument("--end", required=True, type=_year_month,
                           help="YYYY, YYYYMM, or YYYYMMDD; exclusive end month (day is validated but ignored)")
    return parser


def _download_cycle(args, clients, entries=None) -> int:
    errors = 0
    for account, client in clients.items():
        try:
            if entries is not None:
                tasks = ((entry.job_id, entry.filename) for entry in entries if entry.account == account)
            elif getattr(args, "job_ids", None):
                tasks = ((job_id, None) for job_id in args.job_ids)
            else:
                tasks = ((job, None) for job in client.iter_jobs(status="successful", limit=args.limit))
            for job, filename in tasks:
                job_id = job if isinstance(job, str) else job.job_id
                try:
                    path = client.download_job(job, args.output_dir, filename=filename,
                                               overwrite=args.overwrite, progress=not args.quiet)
                    if path is None:
                        print(f"[{account}] {job_id}: not successful; skipped")
                    else:
                        print(f"[{account}] complete: {path}")
                except DownloadBusy:
                    print(f"[{account}] {job_id}: reserved by another worker; skipped")
                except (CDSError, DownloadError, OSError, ValueError) as exc:
                    errors += 1
                    print(f"[{account}] {job_id}: {exc}", file=sys.stderr)
        except CDSError as exc:
            errors += 1
            print(f"[{account}] {exc}", file=sys.stderr)
    return 1 if errors else 0


def _submit_cycle(args, clients) -> int:
    months = list(month_range(args.begin, args.end))
    file_template = None
    template_path = getattr(args, "template", None)
    if template_path is not None:
        try:
            file_template = json.loads(template_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"Cannot read request template: {exc}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"Request template must be valid JSON: {exc}") from None
    template = build_template(args.dataset, file_template, variables=getattr(args, "variables", None),
                              pressure_levels=getattr(args, "pressure_levels", None))
    dataset = resolve_dataset(args.dataset)
    accounts = list(clients)
    if not accounts:
        raise ValueError("Select at least one account")
    existing = {entry.filename for entry in read_manifest(args.manifest)} if args.manifest.exists() else set()
    validate_manifest_append(args.manifest, accounts)
    in_flight = {name: deque() for name in accounts}
    account_cycle = itertools.cycle(accounts)
    submitted = skipped = 0
    for year, month in months:
        request = monthly_request(args.dataset, template, year, month, fill_days=True)
        filename = filename_from_request(request)
        if filename in existing:
            skipped += 1
            continue
        account = next(account_cycle)
        client = clients[account]
        queue = in_flight[account]
        while len(queue) >= args.max_in_flight:
            if client.get_job(queue[0]).status not in _OPEN_STATUSES:
                queue.popleft()
            else:
                time.sleep(args.poll_interval)
        job = client.submit_job(dataset, request)
        append_manifest(args.manifest, ManifestEntry(filename, job.job_id, account))
        existing.add(filename)
        queue.append(job.job_id)
        submitted += 1
        if not args.quiet:
            print(f"[{account}] submitted {filename}: {job.job_id}")
    print(f"Submitted {submitted} job(s); skipped {skipped} already in the manifest")
    return 0


def _download_urls(args) -> int:
    if args.job_ids or args.manifest:
        raise ValueError("Use only one of job IDs, --manifest, or --urls-csv")
    root = args.output_dir.expanduser().resolve()
    tasks = {}
    with args.urls_csv.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        headers = reader.fieldnames or []
        if any(headers.count(name) != 1 for name in ("filename", "url")):
            raise ValueError("URL CSV requires unique filename and url columns")
        try:
            for row in reader:
                if None in row or row.get("filename") is None or row.get("url") is None:
                    raise ValueError(f"URL CSV line {reader.line_num}: missing or extra values")
                url = row["url"].strip()
                if not url:
                    continue
                filename = validate_filename(row["filename"])
                if not url.startswith(("http://", "https://")):
                    raise ValueError(f"URL CSV line {reader.line_num}: expected an HTTP(S) URL")
                target = root / filename
                for candidate in (target, Path(str(target) + ".part"), Path(str(target) + ".lock")):
                    if not candidate.resolve().is_relative_to(root):
                        raise ValueError("Download path must stay inside the output directory")
                if filename in tasks and tasks[filename] != url:
                    raise ValueError("URL CSV assigns one filename to different URLs")
                tasks[filename] = url
        except csv.Error as exc:
            raise ValueError(f"Invalid URL CSV: {exc}") from None
    errors = 0
    for filename, url in tasks.items():
        try:
            path = download_file(url, root / filename, overwrite=args.overwrite,
                                 timeout=args.timeout, retries=args.retries, progress=not args.quiet)
            print(f"complete: {path}")
        except DownloadBusy:
            print(f"{filename}: reserved by another worker; skipped")
        except (DownloadError, OSError, ValueError):
            errors += 1
            print(f"{filename}: download failed; check URL availability and destination", file=sys.stderr)
    return 1 if errors else 0


def run(args) -> int:
    if args.command == "download" and args.urls_csv:
        return _download_urls(args)
    if args.command == "variables":
        records = find_variables(datasets=args.dataset, search=[*args.query, *args.search],
                                 family=args.family, frequency=args.frequency)
        if args.json:
            print(json.dumps(records, indent=2))
        else:
            print_variable_table(records)
        return 0
    if args.command == "filenames":
        for name in generate_flist(args.fields, args.begin, args.end):
            print(name)
        return 0
    if args.command == "fetch-url":
        location = args.url_or_file
        if not location.startswith(("https://", "http://")):
            location = Path(location).expanduser().read_text(encoding="utf-8").strip()
        try:
            path = download_file(location, args.output, expected_size=args.expected_size,
                                 overwrite=args.overwrite, timeout=args.timeout,
                                 retries=args.retries, progress=not args.quiet)
        except DownloadBusy:
            print("Reserved by another worker; skipped")
            return 0
        print(path)
        return 0
    if args.command == "check":
        print("ACCOUNT\tCREDENTIALS\tURL")
        for credential in load_accounts(args.config, args.account, args.all_accounts, args.rc_file, args.url):
            print(f"{credential.name}\t{credential.key_source}\t{credential.url}")
        return 0
    entries = None
    names = args.account
    if args.command == "jobs" and args.urls_csv and args.urls_csv.exists():
        raise ValueError("URL CSV already exists; choose a new output path")
    if (args.command == "jobs" and args.urls_csv and args.manifest
            and args.urls_csv.resolve() == args.manifest.resolve()):
        raise ValueError("Use separate paths for --urls-csv and --manifest")
    input_manifest = (args.manifest if args.command == "download" else
                      args.from_manifest if args.command == "jobs" else None)
    if args.command == "jobs" and args.manifest and args.manifest.exists():
        raise ValueError("Manifest already exists; choose a new output path")
    if input_manifest:
        if getattr(args, "job_ids", None):
            raise ValueError("Use either job IDs or --manifest")
        entries = read_manifest(input_manifest)
        if not names and not args.all_accounts:
            names = list(dict.fromkeys(entry.account for entry in entries))
        if not entries and args.command == "download":
            print("Manifest has no downloads")
            return 0
    credentials = ([] if entries == [] else
                   load_accounts(args.config, names, args.all_accounts, args.rc_file, args.url))
    clients = {credential.name: ERA5Client(credential, timeout=args.timeout, retries=args.retries)
               for credential in credentials}
    if args.command == "download":
        if args.job_ids and len(clients) != 1:
            raise ValueError("Select exactly one account when specifying job IDs")
        if entries is not None:
            entries = [entry for entry in entries if entry.account in clients]
            destinations = {}
            for entry in entries:
                previous = destinations.setdefault(entry.filename, (entry.account, entry.job_id))
                if previous != (entry.account, entry.job_id):
                    raise ValueError("Manifest assigns one filename to different jobs; use unique filenames")
        return _download_cycle(args, clients, entries)
    if args.command == "watch":
        cycle = 0
        outcome = 0
        while args.cycles is None or cycle < args.cycles:
            cycle += 1
            print(f"Cycle {cycle}", flush=True)
            outcome = max(outcome, _download_cycle(args, clients))
            if args.cycles is None or cycle < args.cycles:
                time.sleep(args.interval)
        return outcome
    if args.command == "submit":
        return _submit_cycle(args, clients)
    if args.command == "jobs":
        if args.manifest and args.manifest.exists():
            raise ValueError("Manifest already exists; choose a new output path")
        summaries = []
        manifest = []
        for account, client in clients.items():
            if entries is None:
                jobs = ((job, job.filename) for job in
                        client.iter_jobs(status=args.status, limit=args.limit))
            else:
                jobs = ((client.get_job(entry.job_id), entry.filename)
                        for entry in entries if entry.account == account)
            for job, filename in jobs:
                if args.status and job.status != args.status:
                    continue
                summaries.append(dict(account=account, job_id=job.job_id, status=job.status,
                                      dataset=job.dataset, filename=filename))
                if args.urls_csv:
                    summaries[-1]["url"] = client.get_result_url(job) or ""
                manifest.append(ManifestEntry(filename, job.job_id, account))
        if args.json:
            print(json.dumps(summaries, indent=2))
        else:
            print("ACCOUNT\tJOB_ID\tSTATUS\tFILENAME")
            for job in summaries:
                print("\t".join(job[key] for key in ("account", "job_id", "status", "filename")))
        if args.manifest:
            write_manifest(args.manifest, manifest)
        if args.urls_csv:
            with args.urls_csv.open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["filename", "job_id", "account", "status", "url"],
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(summaries)
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return run(args)
    except KeyboardInterrupt:
        print("Interrupted; partial downloads are retained", file=sys.stderr)
        return 130
    except (CDSError, DownloadError, OSError, ValueError) as exc:
        print(f"era5-download: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
