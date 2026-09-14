# era5-download

A Python package for submitting jobs to the
[Climate Data Store (CDS)](https://cds.climate.copernicus.eu/) and downloading
the requested data files, with a command-line interface. It coordinates
download workers with exclusive `.lock` reservations, resumes interrupted
transfers, and checks byte counts when a size is available.

The `submit`, `jobs`, `download` and `watch` commands use the CDS API. You
need a free Climate Data Store account, accessed through an ECMWF login,
and its personal access token (API key). The API is the service CDS
provides; the token authenticates your access. Before requesting data,
accept the required dataset licences/Terms of Use on the CDS website.
Follow the [CDS API setup guide](https://cds.climate.copernicus.eu/en/how-to-api)
to register, sign in and obtain your token.

The `check` command reads your local credential configuration and shows the
selected account labels, credential sources and API URLs without revealing
the tokens. It does not contact CDS or verify that a token is accepted by
the server. `variables` lists and searches the bundled variable catalogue,
`filenames` generates names locally, and `fetch-url` downloads directly from
a supplied HTTP(S) URL. These three commands require no CDS credentials.

## Installation

Requires Python 3.13 or newer.

```
pip install --upgrade era5-download
```

Or from source:

```
git clone https://github.com/CongGao-CG/era5-download.git
cd era5-download
pip install .
```

## Credentials

Credentials come from the environment or your own configuration, never from
this project:

- `CDSAPI_KEY` / `CDSAPI_URL` environment variables, or
- a `~/.cdsapirc` file (`CDSAPI_RC` to override the path), or
- a JSON file passed via `--config` with an `accounts` object containing
  profiles with `key_env` and/or `rc_file`.

See CDS's own
[how-to-api guide](https://cds.climate.copernicus.eu/how-to-api) for finding
your personal API key and setting up `~/.cdsapirc`.

A `~/.cdsapirc` file holds one `name: value` pair per line; blank lines and
`#` comments are ignored. `key` is the personal API key from your CDS profile
page; `url` is optional and defaults to `https://cds.climate.copernicus.eu/api`:

```
url: https://cds.climate.copernicus.eu/api
key: <PERSONAL-ACCESS-TOKEN>
```

For example, `accounts.json` can reference your own environment variables or
credential files without containing tokens:

```json
{
  "accounts": {
    "default": {"key_env": "CDSAPI_KEY"},
    "secondary": {"rc_file": "credentials/secondary.cdsapirc"}
  }
}
```

### Account labels

An **account label** is a nickname you choose for a credential profile in
the configuration's `accounts` object. In the example above, `default` and
`secondary` are labels. A label is neither a CDS username nor an API key:
the credentials stored or referenced by that profile identify the actual
CDS account.

For example, `--account secondary` selects the profile that reads
`credentials/secondary.cdsapirc`. The same label appears in the `ACCOUNT`
column of `jobs`, in messages such as `[secondary]`, and in the manifest's
`account` column. It tells the package which credentials to use for a job;
it does not create an output directory or rename your CDS account. Two
labels pointing to the same credentials still access the same CDS account.

Labels are case-sensitive. They must start with a letter or digit and may
contain ASCII letters, digits, `_` and `-`. Keep manifest labels consistent
with configuration labels if you rename a profile. Without `--config`, the
package creates just one profile, labelled `default`, from the environment
or credential file.

Profile `rc_file` paths are relative to the configuration file. Without an
account selection, the profile named `default` is used, except for manifest
downloads, which select the accounts named in the CSV rows. Select other
profiles with repeated `--account NAME`, or use `--all-accounts`. Explicit
profiles use only their configured credential sources; if both `key_env` and
`rc_file` are set, the key must come from the named environment variable.

Without `--config`, `CDSAPI_KEY` takes precedence over the default credential
file. An explicit `--rc-file PATH` instead selects that file's credentials.
`--config` and `--rc-file` cannot be combined. `--url` overrides the API URL
from any credential source.

## How data becomes available

Getting requested data onto your computer has two stages:

1. **Submit a job and wait for CDS to prepare the result.** The request
   specifies the dataset, variables, dates, levels and region. CDS queues
   the job and performs the necessary retrieval and processing.
2. **Download the prepared result.** Once the job is successful, transfer
   its result from CDS to local storage using `download` or `watch`.

These stages are separate in the
[ECMWF client API](https://ecmwf.github.io/ecmwf-datastores-client/_api/datastores/Client.html)
used by this package. Both preparation and transfer contribute to elapsed
time. Transfer time depends on result size, available bandwidth and local
storage performance; preparation time depends on CDS workload and the request.

You can submit another job while earlier jobs are queued, being processed
or being downloaded, subject to CDS limits. This allows remote preparation
and local transfers to overlap. Earlier submission does not guarantee earlier
completion: scheduling also depends on resource availability and request
characteristics. Keep a small number of requests outstanding and avoid
submission bursts. Excessive parallel requests can worsen overall waiting
time. See [ECMWF's CDS best-practice guidance](https://confluence.ecmwf.int/pages/viewpage.action?pageId=435825730).

### Job statuses

This version accepts all five of the following values for `jobs --status`.
They describe a job on CDS, separately from local download progress:

| Status | Meaning | What to do |
| --- | --- | --- |
| `accepted` | CDS has received the request and queued it; execution has not started. | Leave it queued and check later. Submitting a duplicate does not advance this job. |
| `running` | CDS is executing the request, such as retrieving fields and preparing the output. This is server processing, not a transfer to your computer. | Wait for the job to finish. |
| `successful` | CDS completed the request and prepared its result. | Download the result while it is available. The status does not establish that a local copy exists. |
| `failed` | CDS attempted execution but it ended without a successful result. | Check the job's error details on the CDS requests page. Correct the problem or retry if appropriate. |
| `rejected` | CDS declined the request; it is no longer a job waiting to complete normally. The status alone does not identify the reason. | Read the rejection details on CDS before changing or resubmitting the request. |

See the [CDS request-state descriptions](https://confluence.ecmwf.int/spaces/CKB/pages/174856258/Climate+Data+Store+CDS+documentation)
and the [API's supported status values](https://ecmwf.github.io/ecmwf-datastores-client/_api/datastores/Client.html#ecmwf.datastores.Client.get_jobs).

A normal successful progression is `accepted` → `running` → `successful`;
polling may miss short-lived intermediate states. `failed` and `rejected`
are unsuccessful outcomes, rather than additional waiting stages. This
package downloads only `successful` jobs and does not automatically resubmit
failed or rejected jobs.

There is no separate `downloaded` value among these status filters. A job
can remain `successful` after its file is downloaded, or while a local
transfer has failed. Check the package's download messages and local files
for transfer completion. Prepared results are temporary, so a previously
successful request may need to be submitted again if its result has expired.

### Choosing request sizes and submission rate

How you divide variables, dates, levels and regions into requests affects
the total time to obtain your data. ECMWF's
[ERA5 efficiency guidance](https://confluence.ecmwf.int/spaces/CKB/pages/174856258/Climate+Data+Store+CDS+documentation#ClimateDataStore(CDS)documentation-Efficiencytips)
uses one month per request for an hourly ERA5 example and recommends avoiding
very large requests. This is a starting point, not a universal optimum.

The following are practical tradeoffs, not measured speed rankings:

| Request grouping | Tradeoff |
| --- | --- |
| One variable over the entire desired period | Fewer requests, but a long period or many levels may make each request too large or slow to prepare. |
| One variable for one month | Manageable batches for many ERA5 workflows; each batch can be downloaded or retried separately. |
| Several variables for one month | Fewer requests than splitting every variable, but larger results and more processing per request. |
| One variable for one timestep | Small results, but potentially thousands of requests, each with submission, scheduling and transfer overhead. |

### One request, one file, or several files?

A request describes the data selection; it does not specify a fixed number
of output files. One data file can contain multiple variables and timesteps.
The dataset's output rules, conversion and archive option determine how
the result is packaged. For ERA5 NetCDF conversion, ECMWF documents these
cases:

| Requested output | Result |
| --- | --- |
| NetCDF, unarchived, with a single `stepType` | One NetCDF file, which may contain several variables and timesteps. |
| NetCDF with mixed `stepType` values | Separate NetCDF files grouped by `stepType`, delivered together in one ZIP even when unarchived output was requested. |
| Archived/ZIP output | One downloaded ZIP containing the output file or files. |

`stepType` describes time treatment: for example, an instantaneous value
versus a quantity accumulated over a time interval. Mixing those types can
split the output; requesting several variables alone does not necessarily
do so. These are ERA5 converter rules, not a guarantee for every CDS dataset.
See [ECMWF's NetCDF output guidance](https://forum.ecmwf.int/t/forthcoming-update-to-the-format-of-netcdf-files-produced-by-the-conversion-of-grib-data-on-the-cds/7772).

This package saves one downloaded result per job and does not unpack ZIP
archives. A ZIP is one file on disk, but it can contain several data files.
The generated extension is inferred from the request; see
[Generated filenames](#generated-filenames) for the implications.

### Applying a request strategy

Request only the region and fields you need. Cropping reduces transferred
bytes but may not reduce archive work proportionally. For tape-backed ERA5,
follow the archive's grouping guidance instead of making many tiny requests.
See [how to download ERA5](https://confluence.ecmwf.int/spaces/CKB/pages/129135000/How+to+download+ERA5+from+the+Climate+Data+Store+CDS).
Compare representative batches by total preparation and transfer time before
scaling up; no grouping is always fastest.

This package's `submit` creates one job per calendar month from a fixed
template, whether that template comes from `--var`/`--pressure-level` or
from `--template`. It keeps the selected variables, levels, region, days and
times together; it does not automatically split by variable or timestep.
`--max-in-flight 2` is the default cap on accepted/running jobs tracked by
that invocation, not a submissions-per-second limit or a count of jobs from
other runs. It is a package setting, not a CDS allowance. Account for other
outstanding jobs and avoid starting many submission processes at once.
CDS resource limits can change with workload; check the current
[limits guidance](https://confluence.ecmwf.int/spaces/CKB/pages/174856258/Climate+Data+Store+CDS+documentation#ClimateDataStore(CDS)documentation-Limits)
and dataset form before scaling up.

## Usage

Global options (`--config`, `--rc-file`, `--account`, `--all-accounts`,
`--url`, `--timeout`, `--retries`) go **before** the subcommand.

In the examples below, **the default account** means the CDS account
identified by the `CDSAPI_KEY` environment variable; if that variable is
unset, it instead means the account identified by the `key:` line of the rc
file at the path in `CDSAPI_RC`, or at `~/.cdsapirc` if `CDSAPI_RC` is also
unset. With `--config accounts.json`, the default instead comes from the
profile named `default` in that file. `--all-accounts` selects every profile
in the configuration; it does not discover additional CDS accounts.

The commands fall into three groups, covered in the sections below: **Check**
(`check` — account and credential settings; `variables` — available variable
names), **Submit**
(`submit` — create new CDS jobs) and **Download** (`jobs`, `download`,
`watch`, `fetch-url` and `filenames` — discover, inspect and retrieve
results).

### Check

`check` resolves the selected account(s) using the same account-selection
rules as every other command, then prints each one's [account
label](#account-labels), credential source and API URL — without contacting
CDS. The credential source names where the key comes from (an environment
variable name, or an rc file path); the key itself is never printed.

Show which credentials the default account would use:

```sh
era5-download check
```

Show every profile configured in `accounts.json`, one row per account:

```sh
era5-download --config accounts.json --all-accounts check
```

Each line has three tab-separated columns, e.g.
`default    environment variable CDSAPI_KEY    https://cds.climate.copernicus.eu/api`.
Run `check` before `submit`/`download` to confirm which account will be used,
especially with several configured profiles or before changing which
`--account` is selected.

#### Browse and search variables

`variables` displays the same bundled catalogue as the [variable reference tables](#variable-reference)
at the end of this README. It runs locally without credentials or CDS API requests. With no
filters, it shows all 290 variable/parameter records covering 289 distinct
CDS variable names; some names represent different parameters in different
datasets.

Show the complete table, including official short names, CDS names, parameter
IDs and dataset availability:

```sh
era5-download variables
```

Show only variables available in the monthly pressure-level dataset:

```sh
era5-download variables --dataset era5_monthly_pressure
```

Search for wind across all datasets. Search is case-insensitive and matches
substrings in current or older official GRIB short names, CDS variable names
and parameter IDs. Underscores in CDS names are treated as spaces:

```sh
era5-download variables --search wind
```

Search terms can also be positional. All words in the supplied text and
repeated `--search` options must match the same record, in any order:

```sh
era5-download variables "sea surface" temperature
```

Combine search with family and frequency filters to find snow variables in
ERA5-Land daily statistics:

```sh
era5-download variables --family era5-land --frequency daily --search snow
```

Repeat `--dataset` to include several datasets. Dataset aliases and their
full CDS collection IDs are accepted. Combine filters with `--json` to
return structured records for scripts:

```sh
era5-download variables --dataset era5_hourly_single --dataset era5land_hourly --search evaporation --json
```

| Option | Selection |
| --- | --- |
| `--dataset NAME` | One of the nine supported catalogue datasets; repeat to include any of the named datasets. |
| `--family era5` or `--family era5-land` | Restrict to ERA5 or ERA5-Land collections. |
| `--frequency hourly`, `monthly` or `daily` | Restrict to the corresponding collections. |
| `--search TEXT` or positional text | Require all search terms to match a record. |
| `--json` | Print a JSON array instead of the table. |

Different filter types are combined: a record must belong to a matching
dataset and match the search. Table columns `ERA5-S`, `ERA5-P` and `LAND`
mean ERA5 single levels, ERA5 pressure levels and ERA5-Land. Their `HMD`
flags show hourly/monthly/daily availability **within the filtered datasets**;
`-` means absent. JSON records include the matching dataset aliases.
No matches produces a message, or `[]` with `--json`, and a successful exit.
An unknown or unsupported dataset produces an error. The table is a bundled
snapshot dated 12 September 2026, not a live availability check.

### Submit

Submit one request per calendar month under the default account for January
1980 through December 2025 (`2026` is excluded), for sea surface temperature
(`sst`). The alias `era5_monthly_single` selects the CDS collection
[`reanalysis-era5-single-levels-monthly-means`](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels-monthly-means?tab=overview).
`--var` accepts supported official ECMWF GRIB short names (see the
[variable reference](#variable-reference)); it can be repeated or comma-separated to include several variables
in the same request. This dataset
needs no `--pressure-level`, since it has none. Record each new job ID,
generated destination filename and account label in `jobs.csv`; filenames
already recorded there are skipped regardless of job status. This submits up
to 552 jobs (46 years × 12 months), with at most two accepted/running jobs
from this invocation at a time. It may wait for earlier jobs to finish before
submitting more, but returns after recording the final submission without
waiting for the remaining jobs to finish. Downloading results is a separate
step, covered under [Download](#download):

```sh
era5-download submit era5_monthly_single --var sst --begin 1980 --end 2026 --manifest jobs.csv
```

The same command works for any other preset dataset and variable, not only
`era5_monthly_single`/`sst`. Pressure-level datasets need a separate
`--pressure-level` selection, or `pressure_level` in a template:

```sh
era5-download submit era5_monthly_pressure --var u --pressure-level 200 --begin 1980 --end 2026 --manifest jobs_u200.csv
```

For request fields this package has no preset for (a specific region, extra
product type, or a raw/unlisted collection), create `request.json` with the
variables, pressure levels, product type, region and output settings you
want, using valid fields for that collection and omitting `year` and `month`;
combine it with `--var`/`--pressure-level` to override just the template's
variable and level, or omit them to submit the template exactly as written:

```sh
era5-download submit era5_monthly_pressure --template request.json --begin 1980 --end 2026 --manifest jobs.csv
```

`--begin`/`--end` each accept `YYYY`, `YYYYMM` or `YYYYMMDD`; a bare `YYYY`
means January of that year, so year-only values behave exactly like before.
A day, if given, is validated as a real calendar date but otherwise ignored,
since `submit` always works one whole calendar month at a time. `--end` is
exclusive at month granularity: `--begin 1980 --end 2026` covers January
1980 through December 2025, and `--begin 199605 --end 199606` submits
exactly one job, for May 1996:

```sh
era5-download submit era5_hourly_pressure --var u --pressure-level 250 --begin 199605 --end 199606 --manifest jobs_u250.csv
```

`submit` builds one fixed request template (the keys of a CDS request, e.g.
`variable`, `pressure_level`, `product_type`, `time`, `data_format` —
everything except `year`/`month`, which are filled in per month from
`--begin`/`--end`), then reuses it for every month. The dataset can
be a raw CDS collection ID or an alias from
`era5_download.mappings.DATASET_MAPPING`.

The template comes from `--var` (with `--pressure-level` where needed),
`--template PATH`, or both together; at least one of `--var`/`--template` is
required. Without `--template`, `submit` also fills in dataset-appropriate
defaults — `data_format`, `download_format`, `product_type`, `time`, and for
daily-statistics datasets `daily_statistic`/`time_zone`/`frequency` — for the
nine dataset aliases in `DATASET_MAPPING` (and their underlying collection
IDs); a raw or unlisted collection ID has no built-in preset and requires
`--template`. `era5_single_timeseries`, `era5land_timeseries` and
`era5_complete_mars` use date-range or MARS request shapes this batcher does
not build at all; submit those with `ERA5Client.submit_job` and an explicit
request instead.

#### Templates and submission tracking

Combining `--template` with `--var`/`--pressure-level` replaces only the
request's `variable`/`pressure_level` fields, leaving every other template
field (region, times, days, product type, output format) unchanged, and adds
none of the automatic defaults described above — useful for keeping a
hand-written template's custom settings while still choosing the variable
through `--var`. For hourly and daily-statistics datasets, `submit` fills in
every calendar day of each month unless the template already sets its own
`day` selection.

Each successful submission is appended to `--manifest` immediately. On
restart, generated filenames already in that manifest are skipped regardless
of job status. Use a separate manifest for each dataset/request template;
filenames are not a complete request identity. A failure or interruption
between remote submission and recording its row can leave an unrecorded job;
inspect `jobs` before restarting in that case.

With multiple accounts selected, submissions are spread round-robin. Each
account is throttled to `--max-in-flight` (default: 2) accepted/running jobs
**submitted by the current invocation**. It polls the oldest tracked job
every `--poll-interval` seconds (default: 5). Earlier jobs, including jobs
loaded from a resume manifest, do not count toward this limit. Submission
returns once all new jobs have been recorded; it does not wait for the last
jobs to finish or download their results. Use one submission process per
manifest; `.lock` reservations coordinate downloads only.

### Download

The **1,000-job listing limit is a package default**, not a CDS server quota.
Change it with `--limit`; it controls the total jobs listed per account.
The package requests API pages of up to 100 jobs until that total is reached
or the server returns no further results. This is separate from CDS limits
on request size or concurrent processing.

Retrieve up to 1,000 jobs available through the CDS API for the default
account, newest-created first, and print their [account label](#account-labels),
job ID, [status](#job-statuses) and [generated filename](#generated-filenames).
This queries the account's server-side job list with
no status or dataset filter, so it includes pending, running and finished
jobs, including jobs submitted with other tools:

```sh
era5-download jobs
```

To list up to 5,000 available jobs for the default account instead:

```sh
era5-download jobs --limit 5000
```

Check only the jobs recorded in a submission manifest, using each row's
account credentials and original filename:

```sh
era5-download --config accounts.json jobs --from-manifest jobs.csv
```

Save those jobs' current statuses and download URLs to `url.csv`:

```sh
era5-download --config accounts.json jobs --from-manifest jobs.csv --urls-csv url.csv
```

The CSV contains `filename,job_id,account,status,url`. Only successful jobs
have a result URL; other statuses have an empty URL field. Result URLs are
temporary. This fetches metadata without downloading data or changing the
input manifest. The output path must not already exist. `--status` also
filters the exported rows. A result lookup failure reports an error without
creating the URL CSV.

Download directly from that CSV without CDS credentials:

```sh
era5-download download --urls-csv url.csv -o data
```

Only `filename` and `url` are used; all other columns are ignored. Blank
URLs are skipped. Filenames must be safe relative paths under the output
directory (the current directory by default). This option cannot be combined
with job IDs or `--manifest`. Transfers support partial-file resuming and
shared-worker locks. Since the CSV supplies no expected size, existing
completed files require `--overwrite`. Use `--quiet` to hide progress bars.
Failed transfers are reported and remaining rows are still attempted; any
failure produces a nonzero exit code. Refresh expired URLs before retrying.

`--from-manifest` reads an existing CSV without modifying it. Accounts are
selected from its rows unless `--account` or `--all-accounts` is specified;
explicit selection restricts the rows processed. Each job is looked up
directly, so `--limit` does not apply. Combine with `--status running` to
filter results or `--json` for structured status output. Empty manifests
produce an empty listing. `--manifest NEW.csv` remains an optional export
of the listed jobs (filename, job ID and account, without status), and
refuses to overwrite an existing file.

To list only jobs currently running for the default account, apply the
`running` status filter. The 1,000-job limit applies to the filtered results:

```sh
era5-download jobs --status running
```

To list accepted jobs that have not started running for that same account,
query the `accepted` status separately:

```sh
era5-download jobs --status accepted
```

Look up the two specified job IDs directly under the default account and
download their results into the current directory using generated filenames.
Both IDs must belong to that account. Each job is downloaded only if its
status is `successful`; other statuses are skipped without waiting:

```sh
era5-download download JOB_ID_1 JOB_ID_2
```

Read job IDs, destination filenames and account labels from `jobs.csv`, then
look up each job under the account specified in its row. Download successful
results to those filenames relative to the current directory; skip other
statuses without waiting. This example uses default credentials, so the CSV
account column must be absent or contain only `default`:

```sh
era5-download download --manifest jobs.csv
```

For a manifest containing named accounts, load their credentials from
`accounts.json`. Account labels in the CSV must match profiles in that file;
the command automatically selects the profiles referenced by the rows:

```sh
era5-download --config accounts.json download --manifest jobs.csv
```

To discover downloadable jobs without supplying IDs or a manifest, query
the default account for up to 1,000 successful jobs, newest-created first,
and download their results into the current directory:

```sh
era5-download download
```

For the default account, immediately query up to 1,000 successful jobs and
download their results into the current directory. Wait 300 seconds after
that cycle finishes, then repeat until Ctrl-C. Each cycle queries the newest
successful jobs again; existing files with matching expected sizes are
skipped. Increase `--limit` to reach jobs beyond the newest 1,000:

```sh
era5-download watch --interval 300
```

Download the supplied HTTP(S) URL directly to `out.nc` in the current
directory. Replace `URL` with the download URL. This command requires no CDS
account configuration and performs no CDS job lookup:

```sh
era5-download fetch-url URL -o out.nc
```

Print 24 filenames: January–December 2020 for `u200`, followed by the same
months for `sst`. This is an offline naming helper; it uses no credentials,
contacts no service and creates no files:

```sh
era5-download filenames u200 sst --begin 2020 --end 2021
```

`filenames` accepts the same `--begin`/`--end` `YYYY`/`YYYYMM`/`YYYYMMDD`
values as `submit`, with `--end` exclusive at month granularity; this
example prints 24 names: 12 months of 2020 for each field. `filenames` is an
offline helper for simple `.nc` names; it does not generate a submission
manifest or include request/job hashes.

Query every account profile in `accounts.json` independently, using each
profile's credentials. For the configuration shown under
[Credentials](#credentials), that means `default` and `secondary`. Print up
to 1,000 jobs per account with no status filter, grouped by account and
newest-created first within each account:

```sh
era5-download --config accounts.json --all-accounts jobs
```

Look up `JOB_ID` using only the `secondary` profile from `accounts.json`.
The job must belong to that CDS account. If successful, download its result
into the current directory using a generated filename; otherwise skip it
without waiting:

```sh
era5-download --config accounts.json --account secondary download JOB_ID
```

`jobs` authenticates separately for each selected profile using
`ecmwf.datastores.Client`. It requests the account's job list from CDS in
newest-created order, follows the returned pages, and retrieves metadata for
each job ID. It stops when the list ends or `--limit` is reached (default:
1,000 per account). Results are limited to jobs still available through the
CDS API; the command does not maintain a local job history.

Without `--status`, no status filter is sent. Choose one of `accepted`,
`running`, `successful`, `failed` or `rejected` to restrict the listing.
Their meanings are explained under [Job statuses](#job-statuses). `download`
skips other statuses without waiting; `watch` queries successful jobs again
on later cycles. To inspect both accepted and running jobs, run the two
status queries shown above. `jobs --manifest jobs.csv` also writes the listed
IDs, generated filenames and account labels to a CSV, refusing to overwrite
an existing file.

`download` with no job IDs or manifest and each `watch` cycle use the same
account-listing process with a `successful` status filter. Downloads by
explicit ID or manifest instead look up just the supplied IDs; `--limit`
does not restrict these downloads. For each successful job, the package
retrieves its result URL and byte count from CDS, then transfers the result
file over HTTP. Explicit job IDs require exactly one selected account.

`watch` waits 60 seconds between completed cycles by default and runs until
Ctrl-C, or for `--cycles N` cycles. Each cycle applies the listing limit
again; it does not automatically advance past previously downloaded jobs.
Both download commands use the current directory unless `-o DIR` is supplied.
`--quiet` disables their progress bars.

Manifests use the following columns; the legacy `fnm,rid,acc` names are also
accepted. The account column is optional and defaults to `default`:

```csv
filename,job_id,account
u200_202001.nc,EXAMPLE_JOB_ID,default
```

Manifest filenames must be safe relative paths within the output directory.
Downloads use the manifest's filenames and, unless accounts are explicitly
selected, load the profiles named in its rows. Explicit `--account` or
`--all-accounts` selection limits processing to rows whose account is among
the selected profiles. A submit manifest without an
account column can only be appended to using the `default` account.

### Generated filenames

A **generated filename** is the local destination name this package builds
from a job's request metadata. It is not a filename supplied by CDS. The
`FILENAME` column printed by `jobs` shows this proposed name; listing a job
does not create a file.

For a simple request for the u component of wind at 200 hPa in January
2020, the base name is `u200_202001.nc`: `u` identifies the variable, `200`
the pressure level, and `202001` the year and month. Additional selections
and job identity can add suffixes:

| Example name | Meaning |
| --- | --- |
| `u200_202001.nc` | Simple request metadata, or the offline `filenames` helper's output. |
| `u200_202001-fd08714ce1ec.nc` | The same metadata with `time=["00:00"]`; the 12-character suffix identifies the request selections. |
| `u200_202001-fd08714ce1ec-73bd7a28f8c41433.nc` | The preceding request with job ID `example-job`; the additional 16-character suffix identifies that job. |

These examples use list-valued `variable`, `pressure_level`, `year` and
`month`, with `data_format="netcdf"`. A **hash** is a deterministic string
computed from metadata or a job ID; it helps distinguish names but is not
a checksum of the downloaded data. Request hashes are added for extra keys
such as `day`, `time`, `area` or `grid`, multiple selections, and unknown
variable names. Multiple variables use `multivar`; multiple levels use
`multilevel`. Names are limited to 180 characters. Dataset IDs and account
labels are not included, so filenames should not serve as a complete record
of the request.

Which name is used depends on the command:

| Command | Filename source |
| --- | --- |
| `submit` | Generates a name before submission and records it in the manifest, without a job-ID suffix. |
| `jobs`, `jobs --manifest`, `download` without a manifest, `watch` | Generates a name from request metadata and includes the job-ID suffix. |
| `download --manifest` | Uses the manifest's `filename` exactly, including any permitted relative subdirectory. |
| `filenames` | Prints simple `FIELD_YYYYMM.nc` names without inspecting requests or adding hashes. |
| `fetch-url` | Uses the path supplied with `-o`. |

The extension comes from request settings: recognized GRIB formats produce
`.grib`, archive formats produce `.zip`, and other or missing formats default
to `.nc`. The package does not inspect the result's contents to choose the
extension. If CDS returns an automatic ZIP for a NetCDF request, a generated
`.nc` name may therefore contain ZIP bytes. Changing a filename does not
convert the data; use the file's actual format when opening or unpacking it.

### Transfer and worker behavior

Transfers write to `FILE.part`, then atomically replace `FILE` on completion.
They resume partial data when the server supports HTTP ranges, otherwise
restart the partial transfer. Failed transfers retain partial data.

An existing `FILE` is skipped only if it matches a supplied expected byte
count (taken from CDS result metadata for job downloads). Otherwise use
`--overwrite`; the old file remains until the replacement succeeds.
`fetch-url` also accepts a text file containing a URL and an explicit
`--expected-size BYTES`. Without an expected size, existing files require
`--overwrite` even if HTTP size headers would be available. A new chunked
response with no known size is accepted after a clean end of stream; it
cannot be checked against an independent byte count.

Workers using the same output filename coordinate through an exclusive
`FILE.lock`; a worker skips an already reserved target. Locks record the
owner's hostname, process ID and creation time. Normal exits and handled
interruptions release the lock. After a hard crash, manually remove it only
after confirming that the owner has stopped. Locks are never stolen based
on age.

Run `era5-download --help` or `era5-download <command> --help` for full
options. The CLI can also be invoked as `python -m era5_download`.

## Variable reference

`--var` accepts the official GRIB short names in these complete tables for
all nine supported dataset presets. The package translates each name to
the CDS variable for the selected dataset. Full CDS names belong in JSON
templates, not `--var`.

The catalogue was checked on **12 September 2026** against each collection's
[public CDS schema](https://cds.climate.copernicus.eu/api/retrieve/v1/processes),
the [ERA5 parameter tables](https://confluence.ecmwf.int/pages/viewpage.action?pageId=239340673),
the [ERA5-Land parameter tables](https://confluence.ecmwf.int/pages/viewpage.action?pageId=505384848)
and [ECMWF's ecCodes definitions](https://github.com/ecmwf/eccodes/tree/783dddf785d3e7208f5724ccdd6dedccdf0a4063/definitions).
It covers **289 distinct CDS variable names**. Where the ERA5 documentation
uses an older official GRIB name, both it and the current ecCodes name are
listed and accepted; these are official names, not package-created aliases.

**H**, **M** and **D** indicate availability in the hourly, monthly averaged
and daily-statistics collection, respectively. A dash means the variable is
absent from that collection's schema. Presence does not guarantee every
product, date or statistic combination is supported. The CLI rejects a
short name absent from the selected preset's variable list.

**ERA5 single levels — 265 variables**

| Official GRIB short name(s) | CDS variable name | H | M | D |
| --- | --- | :---: | :---: | :---: |
| `100u` | `100m_u_component_of_wind` | ✓ | ✓ | ✓ |
| `100v` | `100m_v_component_of_wind` | ✓ | ✓ | ✓ |
| `u10n` | `10m_u_component_of_neutral_wind` | ✓ | ✓ | ✓ |
| `10u` | `10m_u_component_of_wind` | ✓ | ✓ | ✓ |
| `v10n` | `10m_v_component_of_neutral_wind` | ✓ | ✓ | ✓ |
| `10v` | `10m_v_component_of_wind` | ✓ | ✓ | ✓ |
| `10fg` | `10m_wind_gust_since_previous_post_processing` | ✓ | — | ✓ |
| `10si` | `10m_wind_speed` | — | ✓ | — |
| `2d` | `2m_dewpoint_temperature` | ✓ | ✓ | ✓ |
| `2t` | `2m_temperature` | ✓ | ✓ | ✓ |
| `rhoao` | `air_density_over_the_oceans` | ✓ | ✓ | ✓ |
| `anor` | `angle_of_sub_gridscale_orography` | ✓ | ✓ | ✓ |
| `isor` | `anisotropy_of_sub_gridscale_orography` | ✓ | ✓ | ✓ |
| `bfi` | `benjamin_feir_index` | ✓ | ✓ | ✓ |
| `bld` | `boundary_layer_dissipation` | ✓ | ✓ | ✓ |
| `blh` | `boundary_layer_height` | ✓ | ✓ | ✓ |
| `chnk` | `charnock` | ✓ | ✓ | ✓ |
| `cdir` | `clear_sky_direct_solar_radiation_at_surface` | ✓ | ✓ | ✓ |
| `cbh` | `cloud_base_height` | ✓ | ✓ | ✓ |
| `cdww` | `coefficient_of_drag_with_waves` | ✓ | ✓ | ✓ |
| `cape` | `convective_available_potential_energy` | ✓ | ✓ | ✓ |
| `cin` | `convective_inhibition` | ✓ | ✓ | ✓ |
| `cp` | `convective_precipitation` | ✓ | ✓ | ✓ |
| `crr` | `convective_rain_rate` | ✓ | ✓ | ✓ |
| `csf` | `convective_snowfall` | ✓ | ✓ | ✓ |
| `csfr` | `convective_snowfall_rate_water_equivalent` | ✓ | ✓ | ✓ |
| `uvb` | `downward_uv_radiation_at_the_surface` | ✓ | ✓ | ✓ |
| `dctb` | `duct_base_height` | ✓ | ✓ | ✓ |
| `lgws` | `eastward_gravity_wave_surface_stress` | ✓ | ✓ | ✓ |
| `ewss` | `eastward_turbulent_surface_stress` | ✓ | ✓ | ✓ |
| `e` | `evaporation` | ✓ | ✓ | ✓ |
| `fal` | `forecast_albedo` | ✓ | ✓ | ✓ |
| `flsr` | `forecast_logarithm_of_surface_roughness_for_heat` | ✓ | ✓ | ✓ |
| `fsr` | `forecast_surface_roughness` | ✓ | ✓ | ✓ |
| `wstar` | `free_convective_velocity_over_the_oceans` | ✓ | ✓ | ✓ |
| `zust` | `friction_velocity` | ✓ | ✓ | ✓ |
| `z` | `geopotential` | ✓ | ✓ | ✓ |
| `gwd` | `gravity_wave_dissipation` | ✓ | ✓ | ✓ |
| `hcc` | `high_cloud_cover` | ✓ | ✓ | ✓ |
| `cvh` | `high_vegetation_cover` | ✓ | ✓ | ✓ |
| `istl1` | `ice_temperature_layer_1` | ✓ | ✓ | ✓ |
| `istl2` | `ice_temperature_layer_2` | ✓ | ✓ | ✓ |
| `istl3` | `ice_temperature_layer_3` | ✓ | ✓ | ✓ |
| `istl4` | `ice_temperature_layer_4` | ✓ | ✓ | ✓ |
| `i10fg` | `instantaneous_10m_wind_gust` | ✓ | ✓ | ✓ |
| `iews` | `instantaneous_eastward_turbulent_surface_stress` | ✓ | ✓ | ✓ |
| `ilspf` | `instantaneous_large_scale_surface_precipitation_fraction` | ✓ | ✓ | ✓ |
| `ie` | `instantaneous_moisture_flux` | ✓ | ✓ | ✓ |
| `inss` | `instantaneous_northward_turbulent_surface_stress` | ✓ | ✓ | ✓ |
| `ishf` | `instantaneous_surface_sensible_heat_flux` | ✓ | ✓ | ✓ |
| `kx` | `k_index` | ✓ | ✓ | ✓ |
| `lblt` | `lake_bottom_temperature` | ✓ | ✓ | ✓ |
| `cl` | `lake_cover` | ✓ | ✓ | ✓ |
| `dl` | `lake_depth` | ✓ | ✓ | ✓ |
| `licd` | `lake_ice_depth` | ✓ | ✓ | ✓ |
| `lict` | `lake_ice_temperature` | ✓ | ✓ | ✓ |
| `lmld` | `lake_mix_layer_depth` | ✓ | ✓ | ✓ |
| `lmlt` | `lake_mix_layer_temperature` | ✓ | ✓ | ✓ |
| `lshf` | `lake_shape_factor` | ✓ | ✓ | ✓ |
| `ltlt` | `lake_total_layer_temperature` | ✓ | ✓ | ✓ |
| `lsm` | `land_sea_mask` | ✓ | ✓ | ✓ |
| `lsp` | `large_scale_precipitation` | ✓ | ✓ | ✓ |
| `lspf` | `large_scale_precipitation_fraction` | ✓ | ✓ | ✓ |
| `lsrr` | `large_scale_rain_rate` | ✓ | ✓ | ✓ |
| `lsf` | `large_scale_snowfall` | ✓ | ✓ | ✓ |
| `lssfr` | `large_scale_snowfall_rate_water_equivalent` | ✓ | ✓ | ✓ |
| `lai_hv` | `leaf_area_index_high_vegetation` | ✓ | ✓ | ✓ |
| `lai_lv` | `leaf_area_index_low_vegetation` | ✓ | ✓ | ✓ |
| `lcc` | `low_cloud_cover` | ✓ | ✓ | ✓ |
| `cvl` | `low_vegetation_cover` | ✓ | ✓ | ✓ |
| `magss` | `magnitude_of_turbulent_surface_stress` | — | ✓ | — |
| `mx2t` | `maximum_2m_temperature_since_previous_post_processing` | ✓ | — | ✓ |
| `hmax` | `maximum_individual_wave_height` | ✓ | ✓ | ✓ |
| `mxtpr` | `maximum_total_precipitation_rate_since_previous_post_processing` | ✓ | — | ✓ |
| `avg_ibld`, `mbld` | `mean_boundary_layer_dissipation` | ✓ | ✓ | ✓ |
| `avg_cpr`, `mcpr` | `mean_convective_precipitation_rate` | ✓ | ✓ | ✓ |
| `avg_csfr`, `mcsr` | `mean_convective_snowfall_rate` | ✓ | ✓ | ✓ |
| `mdts` | `mean_direction_of_total_swell` | ✓ | ✓ | ✓ |
| `mdww` | `mean_direction_of_wind_waves` | ✓ | ✓ | ✓ |
| `avg_iegwss`, `megwss` | `mean_eastward_gravity_wave_surface_stress` | ✓ | ✓ | ✓ |
| `avg_iews`, `metss` | `mean_eastward_turbulent_surface_stress` | ✓ | ✓ | ✓ |
| `avg_ie`, `mer` | `mean_evaporation_rate` | ✓ | ✓ | ✓ |
| `avg_igwd`, `mgwd` | `mean_gravity_wave_dissipation` | ✓ | ✓ | ✓ |
| `avg_ilspf`, `mlspf` | `mean_large_scale_precipitation_fraction` | ✓ | ✓ | ✓ |
| `avg_lsprate`, `mlspr` | `mean_large_scale_precipitation_rate` | ✓ | ✓ | ✓ |
| `avg_lssfr`, `mlssr` | `mean_large_scale_snowfall_rate` | ✓ | ✓ | ✓ |
| `avg_imagss`, `mmtss` | `mean_magnitude_of_turbulent_surface_stress` | — | ✓ | — |
| `avg_ingwss`, `mngwss` | `mean_northward_gravity_wave_surface_stress` | ✓ | ✓ | ✓ |
| `avg_inss`, `mntss` | `mean_northward_turbulent_surface_stress` | ✓ | ✓ | ✓ |
| `mpts` | `mean_period_of_total_swell` | ✓ | ✓ | ✓ |
| `mpww` | `mean_period_of_wind_waves` | ✓ | ✓ | ✓ |
| `avg_pevr`, `mper` | `mean_potential_evaporation_rate` | ✓ | ✓ | ✓ |
| `avg_rorwe`, `mror` | `mean_runoff_rate` | ✓ | ✓ | ✓ |
| `msl` | `mean_sea_level_pressure` | ✓ | ✓ | ✓ |
| `avg_esrwe`, `mser` | `mean_snow_evaporation_rate` | ✓ | ✓ | ✓ |
| `avg_tsrwe`, `msr` | `mean_snowfall_rate` | ✓ | ✓ | ✓ |
| `avg_smr`, `msmr` | `mean_snowmelt_rate` | ✓ | ✓ | ✓ |
| `msqs` | `mean_square_slope_of_waves` | ✓ | ✓ | ✓ |
| `avg_ssurfror`, `mssror` | `mean_sub_surface_runoff_rate` | ✓ | ✓ | ✓ |
| `avg_sdirswrf`, `msdrswrf` | `mean_surface_direct_short_wave_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_sdirswrfcs`, `msdrswrfcs` | `mean_surface_direct_short_wave_radiation_flux_clear_sky` | ✓ | ✓ | ✓ |
| `avg_sdlwrf`, `msdwlwrf` | `mean_surface_downward_long_wave_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_sdlwrfcs`, `msdwlwrfcs` | `mean_surface_downward_long_wave_radiation_flux_clear_sky` | ✓ | ✓ | ✓ |
| `avg_sdswrf`, `msdwswrf` | `mean_surface_downward_short_wave_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_sdswrfcs`, `msdwswrfcs` | `mean_surface_downward_short_wave_radiation_flux_clear_sky` | ✓ | ✓ | ✓ |
| `avg_sduvrf`, `msdwuvrf` | `mean_surface_downward_uv_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_slhtf`, `mslhf` | `mean_surface_latent_heat_flux` | ✓ | ✓ | ✓ |
| `avg_snlwrf`, `msnlwrf` | `mean_surface_net_long_wave_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_snlwrfcs`, `msnlwrfcs` | `mean_surface_net_long_wave_radiation_flux_clear_sky` | ✓ | ✓ | ✓ |
| `avg_snswrf`, `msnswrf` | `mean_surface_net_short_wave_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_snswrfcs`, `msnswrfcs` | `mean_surface_net_short_wave_radiation_flux_clear_sky` | ✓ | ✓ | ✓ |
| `avg_surfror`, `msror` | `mean_surface_runoff_rate` | ✓ | ✓ | ✓ |
| `avg_ishf`, `msshf` | `mean_surface_sensible_heat_flux` | ✓ | ✓ | ✓ |
| `avg_tdswrf`, `mtdwswrf` | `mean_top_downward_short_wave_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_tnlwrf`, `mtnlwrf` | `mean_top_net_long_wave_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_tnlwrfcs`, `mtnlwrfcs` | `mean_top_net_long_wave_radiation_flux_clear_sky` | ✓ | ✓ | ✓ |
| `avg_tnswrf`, `mtnswrf` | `mean_top_net_short_wave_radiation_flux` | ✓ | ✓ | ✓ |
| `avg_tnswrfcs`, `mtnswrfcs` | `mean_top_net_short_wave_radiation_flux_clear_sky` | ✓ | ✓ | ✓ |
| `avg_tprate`, `mtpr` | `mean_total_precipitation_rate` | ✓ | ✓ | ✓ |
| `dndza` | `mean_vertical_gradient_of_refractivity_inside_trapping_layer` | ✓ | ✓ | ✓ |
| `avg_vimdf`, `mvimd` | `mean_vertically_integrated_moisture_divergence` | ✓ | ✓ | ✓ |
| `mwd` | `mean_wave_direction` | ✓ | ✓ | ✓ |
| `mwd1` | `mean_wave_direction_of_first_swell_partition` | ✓ | ✓ | ✓ |
| `mwd2` | `mean_wave_direction_of_second_swell_partition` | ✓ | ✓ | ✓ |
| `mwd3` | `mean_wave_direction_of_third_swell_partition` | ✓ | ✓ | ✓ |
| `mwp` | `mean_wave_period` | ✓ | ✓ | ✓ |
| `mp1` | `mean_wave_period_based_on_first_moment` | ✓ | ✓ | ✓ |
| `p1ps` | `mean_wave_period_based_on_first_moment_for_swell` | ✓ | ✓ | ✓ |
| `p1ww` | `mean_wave_period_based_on_first_moment_for_wind_waves` | ✓ | ✓ | ✓ |
| `p2ps` | `mean_wave_period_based_on_second_moment_for_swell` | ✓ | ✓ | ✓ |
| `p2ww` | `mean_wave_period_based_on_second_moment_for_wind_waves` | ✓ | ✓ | ✓ |
| `mwp1` | `mean_wave_period_of_first_swell_partition` | ✓ | ✓ | ✓ |
| `mwp2` | `mean_wave_period_of_second_swell_partition` | ✓ | ✓ | ✓ |
| `mwp3` | `mean_wave_period_of_third_swell_partition` | ✓ | ✓ | ✓ |
| `mp2` | `mean_zero_crossing_wave_period` | ✓ | ✓ | ✓ |
| `mcc` | `medium_cloud_cover` | ✓ | ✓ | ✓ |
| `mn2t` | `minimum_2m_temperature_since_previous_post_processing` | ✓ | — | ✓ |
| `mntpr` | `minimum_total_precipitation_rate_since_previous_post_processing` | ✓ | — | ✓ |
| `dndzn` | `minimum_vertical_gradient_of_refractivity_inside_trapping_layer` | ✓ | ✓ | ✓ |
| `wmb` | `model_bathymetry` | ✓ | ✓ | ✓ |
| `alnid` | `near_ir_albedo_for_diffuse_radiation` | ✓ | ✓ | ✓ |
| `alnip` | `near_ir_albedo_for_direct_radiation` | ✓ | ✓ | ✓ |
| `phioc` | `normalized_energy_flux_into_ocean` | ✓ | ✓ | ✓ |
| `phiaw` | `normalized_energy_flux_into_waves` | ✓ | ✓ | ✓ |
| `tauoc` | `normalized_stress_into_ocean` | ✓ | ✓ | ✓ |
| `mgws` | `northward_gravity_wave_surface_stress` | ✓ | ✓ | ✓ |
| `nsss` | `northward_turbulent_surface_stress` | ✓ | ✓ | ✓ |
| `dwi` | `ocean_surface_stress_equivalent_10m_neutral_wind_direction` | ✓ | ✓ | ✓ |
| `wind` | `ocean_surface_stress_equivalent_10m_neutral_wind_speed` | ✓ | ✓ | ✓ |
| `pp1d` | `peak_wave_period` | ✓ | ✓ | ✓ |
| `tmax` | `period_corresponding_to_maximum_individual_wave_height` | ✓ | ✓ | ✓ |
| `pev` | `potential_evaporation` | ✓ | ✓ | ✓ |
| `ptype` | `precipitation_type` | ✓ | ✓ | ✓ |
| `ro` | `runoff` | ✓ | ✓ | ✓ |
| `ci` | `sea_ice_cover` | ✓ | ✓ | ✓ |
| `sst` | `sea_surface_temperature` | ✓ | ✓ | ✓ |
| `swh` | `significant_height_of_combined_wind_waves_and_swell` | ✓ | ✓ | ✓ |
| `shts` | `significant_height_of_total_swell` | ✓ | ✓ | ✓ |
| `shww` | `significant_height_of_wind_waves` | ✓ | ✓ | ✓ |
| `swh1` | `significant_wave_height_of_first_swell_partition` | ✓ | ✓ | ✓ |
| `swh2` | `significant_wave_height_of_second_swell_partition` | ✓ | ✓ | ✓ |
| `swh3` | `significant_wave_height_of_third_swell_partition` | ✓ | ✓ | ✓ |
| `src` | `skin_reservoir_content` | ✓ | ✓ | ✓ |
| `skt` | `skin_temperature` | ✓ | ✓ | ✓ |
| `slor` | `slope_of_sub_gridscale_orography` | ✓ | ✓ | ✓ |
| `asn` | `snow_albedo` | ✓ | ✓ | ✓ |
| `rsn` | `snow_density` | ✓ | ✓ | ✓ |
| `sd` | `snow_depth` | ✓ | ✓ | ✓ |
| `es` | `snow_evaporation` | ✓ | ✓ | ✓ |
| `sf` | `snowfall` | ✓ | ✓ | ✓ |
| `smlt` | `snowmelt` | ✓ | ✓ | ✓ |
| `stl1` | `soil_temperature_level_1` | ✓ | ✓ | ✓ |
| `stl2` | `soil_temperature_level_2` | ✓ | ✓ | ✓ |
| `stl3` | `soil_temperature_level_3` | ✓ | ✓ | ✓ |
| `stl4` | `soil_temperature_level_4` | ✓ | ✓ | ✓ |
| `slt` | `soil_type` | ✓ | ✓ | ✓ |
| `sdfor` | `standard_deviation_of_filtered_subgrid_orography` | ✓ | ✓ | ✓ |
| `sdor` | `standard_deviation_of_orography` | ✓ | ✓ | ✓ |
| `ssro` | `sub_surface_runoff` | ✓ | ✓ | ✓ |
| `slhf` | `surface_latent_heat_flux` | ✓ | ✓ | ✓ |
| `ssr` | `surface_net_solar_radiation` | ✓ | ✓ | ✓ |
| `ssrc` | `surface_net_solar_radiation_clear_sky` | ✓ | ✓ | ✓ |
| `str` | `surface_net_thermal_radiation` | ✓ | ✓ | ✓ |
| `strc` | `surface_net_thermal_radiation_clear_sky` | ✓ | ✓ | ✓ |
| `sp` | `surface_pressure` | ✓ | ✓ | ✓ |
| `sro` | `surface_runoff` | ✓ | ✓ | ✓ |
| `sshf` | `surface_sensible_heat_flux` | ✓ | ✓ | ✓ |
| `ssrdc` | `surface_solar_radiation_downward_clear_sky` | ✓ | ✓ | ✓ |
| `ssrd` | `surface_solar_radiation_downwards` | ✓ | ✓ | ✓ |
| `strdc` | `surface_thermal_radiation_downward_clear_sky` | ✓ | ✓ | ✓ |
| `strd` | `surface_thermal_radiation_downwards` | ✓ | ✓ | ✓ |
| `tsn` | `temperature_of_snow_layer` | ✓ | ✓ | ✓ |
| `tisr` | `toa_incident_solar_radiation` | ✓ | ✓ | ✓ |
| `tsr` | `top_net_solar_radiation` | ✓ | ✓ | ✓ |
| `tsrc` | `top_net_solar_radiation_clear_sky` | ✓ | ✓ | ✓ |
| `ttr` | `top_net_thermal_radiation` | ✓ | ✓ | ✓ |
| `ttrc` | `top_net_thermal_radiation_clear_sky` | ✓ | ✓ | ✓ |
| `tcc` | `total_cloud_cover` | ✓ | ✓ | ✓ |
| `tciw` | `total_column_cloud_ice_water` | ✓ | ✓ | ✓ |
| `tclw` | `total_column_cloud_liquid_water` | ✓ | ✓ | ✓ |
| `tco3` | `total_column_ozone` | ✓ | ✓ | ✓ |
| `tcrw` | `total_column_rain_water` | ✓ | ✓ | ✓ |
| `tcsw` | `total_column_snow_water` | ✓ | ✓ | ✓ |
| `tcslw` | `total_column_supercooled_liquid_water` | ✓ | ✓ | ✓ |
| `tcw` | `total_column_water` | ✓ | ✓ | ✓ |
| `tcwv` | `total_column_water_vapour` | ✓ | ✓ | ✓ |
| `tp` | `total_precipitation` | ✓ | ✓ | ✓ |
| `fdir` | `total_sky_direct_solar_radiation_at_surface` | ✓ | ✓ | ✓ |
| `totalx` | `total_totals_index` | ✓ | ✓ | ✓ |
| `tplb` | `trapping_layer_base_height` | ✓ | ✓ | ✓ |
| `tplt` | `trapping_layer_top_height` | ✓ | ✓ | ✓ |
| `tvh` | `type_of_high_vegetation` | ✓ | ✓ | ✓ |
| `tvl` | `type_of_low_vegetation` | ✓ | ✓ | ✓ |
| `ust` | `u_component_stokes_drift` | ✓ | ✓ | ✓ |
| `aluvd` | `uv_visible_albedo_for_diffuse_radiation` | ✓ | ✓ | ✓ |
| `aluvp` | `uv_visible_albedo_for_direct_radiation` | ✓ | ✓ | ✓ |
| `vst` | `v_component_stokes_drift` | ✓ | ✓ | ✓ |
| `viiwd` | `vertical_integral_of_divergence_of_cloud_frozen_water_flux` | ✓ | ✓ | ✓ |
| `vilwd` | `vertical_integral_of_divergence_of_cloud_liquid_water_flux` | ✓ | ✓ | ✓ |
| `vigd` | `vertical_integral_of_divergence_of_geopotential_flux` | ✓ | ✓ | ✓ |
| `viked` | `vertical_integral_of_divergence_of_kinetic_energy_flux` | ✓ | ✓ | ✓ |
| `vimad` | `vertical_integral_of_divergence_of_mass_flux` | ✓ | ✓ | ✓ |
| `vimdf`, `viwvd` | `vertical_integral_of_divergence_of_moisture_flux` | ✓ | ✓ | ✓ |
| `viozd` | `vertical_integral_of_divergence_of_ozone_flux` | ✓ | ✓ | ✓ |
| `vithed` | `vertical_integral_of_divergence_of_thermal_energy_flux` | ✓ | ✓ | ✓ |
| `vited`, `vitoed` | `vertical_integral_of_divergence_of_total_energy_flux` | ✓ | ✓ | ✓ |
| `viiwe` | `vertical_integral_of_eastward_cloud_frozen_water_flux` | ✓ | ✓ | ✓ |
| `vilwe` | `vertical_integral_of_eastward_cloud_liquid_water_flux` | ✓ | ✓ | ✓ |
| `vige` | `vertical_integral_of_eastward_geopotential_flux` | ✓ | ✓ | ✓ |
| `vithee` | `vertical_integral_of_eastward_heat_flux` | ✓ | ✓ | ✓ |
| `vikee` | `vertical_integral_of_eastward_kinetic_energy_flux` | ✓ | ✓ | ✓ |
| `vimae` | `vertical_integral_of_eastward_mass_flux` | ✓ | ✓ | ✓ |
| `vioze` | `vertical_integral_of_eastward_ozone_flux` | ✓ | ✓ | ✓ |
| `vitee`, `vitoee` | `vertical_integral_of_eastward_total_energy_flux` | ✓ | ✓ | ✓ |
| `viwve` | `vertical_integral_of_eastward_water_vapour_flux` | ✓ | ✓ | ✓ |
| `viec` | `vertical_integral_of_energy_conversion` | ✓ | ✓ | ✓ |
| `vike` | `vertical_integral_of_kinetic_energy` | ✓ | ✓ | ✓ |
| `vima` | `vertical_integral_of_mass_of_atmosphere` | ✓ | ✓ | ✓ |
| `vimat` | `vertical_integral_of_mass_tendency` | ✓ | ✓ | ✓ |
| `viiwn` | `vertical_integral_of_northward_cloud_frozen_water_flux` | ✓ | ✓ | ✓ |
| `vilwn` | `vertical_integral_of_northward_cloud_liquid_water_flux` | ✓ | ✓ | ✓ |
| `vign` | `vertical_integral_of_northward_geopotential_flux` | ✓ | ✓ | ✓ |
| `vithen` | `vertical_integral_of_northward_heat_flux` | ✓ | ✓ | ✓ |
| `viken` | `vertical_integral_of_northward_kinetic_energy_flux` | ✓ | ✓ | ✓ |
| `viman` | `vertical_integral_of_northward_mass_flux` | ✓ | ✓ | ✓ |
| `viozn` | `vertical_integral_of_northward_ozone_flux` | ✓ | ✓ | ✓ |
| `viten`, `vitoen` | `vertical_integral_of_northward_total_energy_flux` | ✓ | ✓ | ✓ |
| `viwvn` | `vertical_integral_of_northward_water_vapour_flux` | ✓ | ✓ | ✓ |
| `vipie` | `vertical_integral_of_potential_and_internal_energy` | ✓ | ✓ | ✓ |
| `vipile` | `vertical_integral_of_potential_internal_and_latent_energy` | ✓ | ✓ | ✓ |
| `vit` | `vertical_integral_of_temperature` | ✓ | ✓ | ✓ |
| `vithe` | `vertical_integral_of_thermal_energy` | ✓ | ✓ | ✓ |
| `vitoe` | `vertical_integral_of_total_energy` | ✓ | ✓ | ✓ |
| `vimd` | `vertically_integrated_moisture_divergence` | ✓ | ✓ | ✓ |
| `swvl1` | `volumetric_soil_water_layer_1` | ✓ | ✓ | ✓ |
| `swvl2` | `volumetric_soil_water_layer_2` | ✓ | ✓ | ✓ |
| `swvl3` | `volumetric_soil_water_layer_3` | ✓ | ✓ | ✓ |
| `swvl4` | `volumetric_soil_water_layer_4` | ✓ | ✓ | ✓ |
| `wdw` | `wave_spectral_directional_width` | ✓ | ✓ | ✓ |
| `dwps` | `wave_spectral_directional_width_for_swell` | ✓ | ✓ | ✓ |
| `dwww` | `wave_spectral_directional_width_for_wind_waves` | ✓ | ✓ | ✓ |
| `wsk` | `wave_spectral_kurtosis` | ✓ | ✓ | ✓ |
| `wsp` | `wave_spectral_peakedness` | ✓ | ✓ | ✓ |
| `wss` | `wave_spectral_skewness` | ✓ | ✓ | ✓ |
| `deg0l` | `zero_degree_level` | ✓ | ✓ | ✓ |

**ERA5 pressure levels — 16 variables**

| Official GRIB short name(s) | CDS variable name | H | M | D |
| --- | --- | :---: | :---: | :---: |
| `d` | `divergence` | ✓ | ✓ | ✓ |
| `cc` | `fraction_of_cloud_cover` | ✓ | ✓ | ✓ |
| `z` | `geopotential` | ✓ | ✓ | ✓ |
| `o3` | `ozone_mass_mixing_ratio` | ✓ | ✓ | ✓ |
| `pv` | `potential_vorticity` | ✓ | ✓ | ✓ |
| `r` | `relative_humidity` | ✓ | ✓ | ✓ |
| `ciwc` | `specific_cloud_ice_water_content` | ✓ | ✓ | ✓ |
| `clwc` | `specific_cloud_liquid_water_content` | ✓ | ✓ | ✓ |
| `q` | `specific_humidity` | ✓ | ✓ | ✓ |
| `crwc` | `specific_rain_water_content` | ✓ | ✓ | ✓ |
| `cswc` | `specific_snow_water_content` | ✓ | ✓ | ✓ |
| `t` | `temperature` | ✓ | ✓ | ✓ |
| `u` | `u_component_of_wind` | ✓ | ✓ | ✓ |
| `v` | `v_component_of_wind` | ✓ | ✓ | ✓ |
| `w` | `vertical_velocity` | ✓ | ✓ | ✓ |
| `vo` | `vorticity` | ✓ | ✓ | ✓ |

**ERA5-Land — 60 variables**

| Official GRIB short name(s) | CDS variable name | H | M | D |
| --- | --- | :---: | :---: | :---: |
| `10u` | `10m_u_component_of_wind` | ✓ | ✓ | ✓ |
| `10v` | `10m_v_component_of_wind` | ✓ | ✓ | ✓ |
| `2d` | `2m_dewpoint_temperature` | ✓ | ✓ | ✓ |
| `2t` | `2m_temperature` | ✓ | ✓ | ✓ |
| `evabs` | `evaporation_from_bare_soil` | ✓ | ✓ | — |
| `evaow` | `evaporation_from_open_water_surfaces_excluding_oceans` | ✓ | ✓ | — |
| `evatc` | `evaporation_from_the_top_of_canopy` | ✓ | ✓ | — |
| `evavt` | `evaporation_from_vegetation_transpiration` | ✓ | ✓ | — |
| `fal` | `forecast_albedo` | ✓ | ✓ | ✓ |
| `z` | `geopotential` | ✓ | ✓ | — |
| `glm` | `glacier_mask` | ✓ | ✓ | — |
| `cvh` | `high_vegetation_cover` | ✓ | ✓ | — |
| `lblt` | `lake_bottom_temperature` | ✓ | ✓ | ✓ |
| `cl` | `lake_cover` | ✓ | ✓ | — |
| `licd` | `lake_ice_depth` | ✓ | ✓ | ✓ |
| `lict` | `lake_ice_temperature` | ✓ | ✓ | ✓ |
| `lmld` | `lake_mix_layer_depth` | ✓ | ✓ | ✓ |
| `lmlt` | `lake_mix_layer_temperature` | ✓ | ✓ | ✓ |
| `lshf` | `lake_shape_factor` | ✓ | ✓ | ✓ |
| `dl` | `lake_total_depth` | ✓ | ✓ | — |
| `ltlt` | `lake_total_layer_temperature` | ✓ | ✓ | ✓ |
| `lsm` | `land_sea_mask` | ✓ | ✓ | — |
| `lai_hv` | `leaf_area_index_high_vegetation` | ✓ | ✓ | ✓ |
| `lai_lv` | `leaf_area_index_low_vegetation` | ✓ | ✓ | ✓ |
| `cvl` | `low_vegetation_cover` | ✓ | ✓ | — |
| `pev` | `potential_evaporation` | ✓ | ✓ | — |
| `ro` | `runoff` | ✓ | ✓ | — |
| `src` | `skin_reservoir_content` | ✓ | ✓ | ✓ |
| `skt` | `skin_temperature` | ✓ | ✓ | ✓ |
| `asn` | `snow_albedo` | ✓ | ✓ | ✓ |
| `snowc` | `snow_cover` | ✓ | ✓ | ✓ |
| `rsn` | `snow_density` | ✓ | ✓ | ✓ |
| `sde` | `snow_depth` | ✓ | ✓ | ✓ |
| `sd` | `snow_depth_water_equivalent` | ✓ | ✓ | ✓ |
| `es` | `snow_evaporation` | ✓ | ✓ | — |
| `sf` | `snowfall` | ✓ | ✓ | — |
| `smlt` | `snowmelt` | ✓ | ✓ | — |
| `stl1` | `soil_temperature_level_1` | ✓ | ✓ | ✓ |
| `stl2` | `soil_temperature_level_2` | ✓ | ✓ | ✓ |
| `stl3` | `soil_temperature_level_3` | ✓ | ✓ | ✓ |
| `stl4` | `soil_temperature_level_4` | ✓ | ✓ | ✓ |
| `slt` | `soil_type` | ✓ | ✓ | — |
| `ssro` | `sub_surface_runoff` | ✓ | ✓ | — |
| `slhf` | `surface_latent_heat_flux` | ✓ | ✓ | — |
| `ssr` | `surface_net_solar_radiation` | ✓ | ✓ | — |
| `str` | `surface_net_thermal_radiation` | ✓ | ✓ | — |
| `sp` | `surface_pressure` | ✓ | ✓ | ✓ |
| `sro` | `surface_runoff` | ✓ | ✓ | — |
| `sshf` | `surface_sensible_heat_flux` | ✓ | ✓ | — |
| `ssrd` | `surface_solar_radiation_downwards` | ✓ | ✓ | — |
| `strd` | `surface_thermal_radiation_downwards` | ✓ | ✓ | — |
| `tsn` | `temperature_of_snow_layer` | ✓ | ✓ | ✓ |
| `e` | `total_evaporation` | ✓ | ✓ | — |
| `tp` | `total_precipitation` | ✓ | ✓ | — |
| `tvh` | `type_of_high_vegetation` | ✓ | ✓ | — |
| `tvl` | `type_of_low_vegetation` | ✓ | ✓ | — |
| `swvl1` | `volumetric_soil_water_layer_1` | ✓ | ✓ | ✓ |
| `swvl2` | `volumetric_soil_water_layer_2` | ✓ | ✓ | ✓ |
| `swvl3` | `volumetric_soil_water_layer_3` | ✓ | ✓ | ✓ |
| `swvl4` | `volumetric_soil_water_layer_4` | ✓ | ✓ | ✓ |

Some GRIB names map differently by dataset: `e` selects ERA5 `evaporation`
or ERA5-Land `total_evaporation`; `sd` selects ERA5 `snow_depth` or ERA5-Land
`snow_depth_water_equivalent`. ERA5-Land `sde` selects geometric `snow_depth`.
The package uses the dataset to make these distinctions.

Pressure-level datasets require `--pressure-level` or a `pressure_level`
selection in the template. Levels are never appended to `--var` names:
use `--var u --pressure-level 200`, not `--var u200`.

All 37 supported pressure levels are listed below. Supply each selection
with `--pressure-level`:

| Pressure levels (hPa) |
| --- |
| 1, 2, 3, 5, 7, 10, 20, 30, 50, 70 |
| 100, 125, 150, 175, 200, 225, 250, 300, 350, 400 |
| 450, 500, 550, 600, 650, 700, 750, 775, 800, 825 |
| 850, 875, 900, 925, 950, 975, 1000 |

`10u` and `10v` mean wind at 10 metres. For wind at 10 hPa, use
`--var u --pressure-level 10` or `--var v --pressure-level 10` with a
pressure-level dataset. Alternative spellings such as `u10`, `v10`, `t2m`,
`d2m`, `sstk` and `rh`, and full CDS names, are rejected by `--var`.

Repeat `--var` or separate names with commas. For example, `--var u,v
--pressure-level 200 --pressure-level 850` requests both components at both
levels. Every selected variable shares the selected pressure levels.

The tables cover the supported ERA5 and ERA5-Land collections, not every
ECMWF product or the specialized MARS/timeseries request schemas. If CDS
adds a variable after this snapshot, use a template with the new CDS name
until the catalogue is updated.
